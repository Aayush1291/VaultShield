"""Unit tests for `git_capture.commit_run_branch` (Task 12.1, Requirement
10.1) and `git_capture.build_structured_diff` (Task 12.2, Requirement 10.2).

Property 16 ("Committed branch name and diff stats match the actual change
set") is Task 12.3's own property test and is deliberately NOT written here.
These are the fixed-scenario checks: the branch name, the three change kinds
landing in one commit, the base branch being left alone, the no-change case,
ignored files staying out, `diff.json`'s location and shape, the counts for
each change kind, the awkward inputs a real repository produces (binary
files, renames, paths containing spaces/quotes/tabs), and each failure mode
reporting the right exception.

The per-file `patch` (Requirements 10.2 and 17.1) is covered too: present
for text files, absent for binary ones, cut at a line boundary with
`patch_truncated` when over `MAX_PATCH_BYTES`, kept within
`MAX_DIFF_REQUEST_BYTES` overall, and scoped to its own path even when the
path looks like a glob or pathspec magic. `resolve_diff_base` is checked for
the parent case, the root-commit (empty-tree) case end to end, and its
failure modes. One Hypothesis property checks that the counts match the
`+`/`-` lines of the patch for text files.

Nothing is dropped silently (Requirements 10.2 and 17.1): a file whose
path is not valid UTF-8 gets `patch_truncated` and no `patch`, without git
being run for it, while a valid name that really contains U+FFFD is still
patched (the check is on bytes, through `ExecResult.stdout_bytes`). A diff
whose metadata alone is over `MAX_DIFF_REQUEST_BYTES` raises
`GitCaptureError` instead of writing an oversized `diff.json`.

Realism: the git side is completely real. `LocalGitSandbox` below stands in
only for the Docker container — it runs the exact argument lists
`commit_run_branch` builds against a real git repository on a real temporary
directory, and returns the same `ExecResult` shape `Sandbox.exec` does. So
every assertion here is about what git actually did (branches, commits,
diffs, repo config), not about which calls were recorded. A real container
is not used because Docker is not a prerequisite for this package's tests,
and nothing in this module's behavior depends on the commands running in one.

`HOME` is pointed at an empty temporary directory for every git invocation so
the developer's own global gitconfig (a default branch name, `commit.gpgsign`,
a hooks path) cannot change the outcome.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vaultshield import git_capture
from vaultshield.git_capture import (
    BRANCH_PREFIX,
    COMMITTER_EMAIL,
    COMMITTER_NAME,
    DIFF_FILES_KEY,
    MAX_PATCH_BYTES,
    PATCH_KEY,
    PATCH_TRUNCATED_KEY,
    FileDiffStat,
    GitCaptureError,
    build_structured_diff,
    commit_run_branch,
    resolve_diff_base,
    run_branch_name,
)
from vaultshield.run_directory import DIFF_FILENAME, RunDirectoryError, diff_path
from vaultshield.sandbox import ExecResult, Sandbox, SandboxError

RUN_ID = "20250614-091530-3f9a2b7c8d1e4f60"

# Identity for the fixture's own base commit — the repository's pre-existing
# history, as if a human wrote it. Distinct from `COMMITTER_NAME`/
# `COMMITTER_EMAIL` so a test can tell VaultShield's capture commit from it.
_DEV_NAME = "Dev Eloper"
_DEV_EMAIL = "dev@example.invalid"


@dataclass
class GitRepo:
    """A real git repository on local disk, plus the sanitized environment
    every git command in this file runs under.
    """

    path: Path
    env: dict[str, str]

    def git(self, *args: str) -> ExecResult:
        """Runs one git command in the repo, returning its result without
        judging the exit code (for the assertions that expect a failure).
        """
        completed = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
        )
        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def git_ok(self, *args: str) -> str:
        """Runs one git command that must succeed, returning stripped
        stdout.
        """
        result = self.git(*args)
        assert result.exit_code == 0, (
            f"git {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
        return result.stdout.strip()

    def commit_as_developer(self, message: str) -> None:
        """Makes a commit under the fixture's human identity, with signing
        explicitly off so a signing-enabled machine can still run these
        tests.
        """
        self.git_ok(
            "-c",
            f"user.name={_DEV_NAME}",
            "-c",
            f"user.email={_DEV_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            message,
        )

    @property
    def current_branch(self) -> str:
        return self.git_ok("rev-parse", "--abbrev-ref", "HEAD")

    def write(self, relative: str, text: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def write_bytes(self, relative: str, data: bytes) -> None:
        """Writes a file byte-for-byte, for the binary-file case: a NUL in
        the content is what makes git classify a file as binary and report
        `-` instead of line counts.
        """
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def name_status(self, from_ref: str, to_ref: str) -> set[tuple[str, str]]:
        """Returns `{(status, path)}` for the changes between two refs, e.g.
        `{("A", "new.txt"), ("M", "kept.txt"), ("D", "doomed.txt")}`.
        """
        raw = self.git_ok("diff", "--name-status", from_ref, to_ref)
        if not raw:
            return set()
        pairs = set()
        for line in raw.splitlines():
            status, _, path = line.partition("\t")
            pairs.add((status.strip(), path.strip()))
        return pairs


@dataclass
class LocalGitSandbox:
    """A `Sandbox` stand-in that runs `exec`'d commands against a real local
    git repository instead of inside a container.

    Matches `Sandbox.exec`'s contract in the two ways `commit_run_branch`
    depends on: commands run with the repository as their working directory,
    and a non-zero exit code is returned in the `ExecResult` rather than
    raised.

    Output is decoded the way `Sandbox.exec` decodes it (UTF-8 with
    `errors="replace"`, no newline translation), and the raw stdout is kept
    as `stdout_bytes`, so a path that is not valid UTF-8 reaches
    `git_capture` exactly as it would from a container.
    """

    repo: GitRepo
    commands: list[list[str]] = field(default_factory=list)

    def exec(self, cmd: list[str], *, env: dict[str, str] | None = None) -> ExecResult:
        # `env` mirrors `Sandbox.exec(cmd, env=...)` (Batch 3 FR5): git
        # capture passes its fixed `_GIT_ENV`, merged over the fixture's env.
        self.commands.append(list(cmd))
        completed = subprocess.run(
            cmd,
            cwd=self.repo.path,
            env={**self.repo.env, **(env or {})},
            capture_output=True,
        )
        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
            stdout_bytes=completed.stdout,
        )


def _init_repo(root: Path, *, with_base_commit: bool = True) -> GitRepo:
    """Creates `<root>/repo` (and a throwaway `<root>/home`) and returns it.

    With `with_base_commit`, the repository gets one base commit containing
    three files and a `.gitignore`. Without it, the repository has no
    commits at all — the empty-clone case whose capture commit is a root
    commit. A plain function rather than only a fixture, because the
    Hypothesis property below cannot use function-scoped fixtures.
    """
    work = root / "repo"
    work.mkdir()
    home = root / "home"
    home.mkdir()

    git_repo = GitRepo(
        path=work,
        env={
            **os.environ,
            "HOME": str(home),
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    git_repo.git_ok("init")
    if with_base_commit:
        git_repo.write("kept.txt", "original\n")
        git_repo.write("doomed.txt", "delete me\n")
        git_repo.write(".gitignore", "ignored/\n")
        git_repo.git_ok("add", "--all")
        git_repo.commit_as_developer("base commit")
    return git_repo


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    """A repository with one base commit containing three files and a
    `.gitignore`, standing in for the clone `Sandbox.create` makes.
    """
    return _init_repo(tmp_path)


def _make_agent_changes(repo: GitRepo) -> None:
    """Simulates what an agent leaves behind in the working tree: one added
    file, one modified file, one deleted file, and one ignored artifact.
    """
    repo.write("added.py", "print('new')\n")
    repo.write("kept.txt", "original\nagent edit\n")
    (repo.path / "doomed.txt").unlink()
    repo.write("ignored/artifact.bin", "build output\n")


def test_run_branch_name_uses_required_prefix() -> None:
    assert run_branch_name(RUN_ID) == f"{BRANCH_PREFIX}{RUN_ID}"
    assert run_branch_name(RUN_ID) == f"vaultshield/run-{RUN_ID}"


def test_run_branch_name_rejects_unsafe_run_id() -> None:
    with pytest.raises(RunDirectoryError):
        run_branch_name("../../etc/passwd")


def test_commit_run_branch_creates_and_checks_out_the_run_branch(
    repo: GitRepo,
) -> None:
    _make_agent_changes(repo)

    branch = commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    assert branch == f"vaultshield/run-{RUN_ID}"
    assert repo.current_branch == branch
    assert repo.git("rev-parse", "--verify", branch).exit_code == 0


def test_commit_run_branch_commits_every_working_tree_change(
    repo: GitRepo,
) -> None:
    base_branch = repo.current_branch
    _make_agent_changes(repo)

    branch = commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    assert repo.name_status(base_branch, branch) == {
        ("A", "added.py"),
        ("M", "kept.txt"),
        ("D", "doomed.txt"),
    }
    # Nothing left uncommitted: "all working-tree changes" means all of them.
    assert repo.git_ok("status", "--porcelain") == ""
    # Exactly one capture commit, authored as VaultShield rather than as the
    # repository's human author.
    assert repo.git_ok("rev-list", "--count", f"{base_branch}..{branch}") == "1"
    assert repo.git_ok("log", "-1", "--format=%an <%ae>") == (
        f"{COMMITTER_NAME} <{COMMITTER_EMAIL}>"
    )
    assert RUN_ID in repo.git_ok("log", "-1", "--format=%s")


def test_commit_run_branch_leaves_the_base_branch_untouched(
    repo: GitRepo,
) -> None:
    base_branch = repo.current_branch
    base_tip = repo.git_ok("rev-parse", "HEAD")
    _make_agent_changes(repo)

    commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    assert repo.git_ok("rev-parse", base_branch) == base_tip


def test_commit_run_branch_excludes_gitignored_files(repo: GitRepo) -> None:
    base_branch = repo.current_branch
    _make_agent_changes(repo)

    branch = commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    committed = {path for _, path in repo.name_status(base_branch, branch)}
    assert not any(path.startswith("ignored/") for path in committed)


def test_commit_run_branch_writes_no_identity_into_repo_config(
    repo: GitRepo,
) -> None:
    _make_agent_changes(repo)

    commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    assert repo.git("config", "--local", "--get", "user.name").exit_code != 0
    assert repo.git("config", "--local", "--get", "user.email").exit_code != 0


def test_commit_run_branch_with_no_changes_still_produces_a_commit(
    repo: GitRepo,
) -> None:
    base_branch = repo.current_branch

    branch = commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    assert repo.git_ok("rev-list", "--count", f"{base_branch}..{branch}") == "1"
    assert repo.name_status(base_branch, branch) == set()


def test_commit_run_branch_rejects_an_already_captured_run(
    repo: GitRepo,
) -> None:
    sandbox = LocalGitSandbox(repo)
    _make_agent_changes(repo)
    branch = commit_run_branch(sandbox, RUN_ID)
    captured_tip = repo.git_ok("rev-parse", branch)

    repo.write("second.py", "print('again')\n")
    with pytest.raises(GitCaptureError) as excinfo:
        commit_run_branch(sandbox, RUN_ID)

    assert branch in str(excinfo.value)
    # The already-captured commit is never reset or overwritten.
    assert repo.git_ok("rev-parse", branch) == captured_tip


def test_commit_run_branch_reports_a_non_repository(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    sandbox = LocalGitSandbox(GitRepo(path=plain, env={**os.environ, "HOME": str(home)}))

    with pytest.raises(GitCaptureError) as excinfo:
        commit_run_branch(sandbox, RUN_ID)

    assert "creating branch" in str(excinfo.value)


def test_commit_run_branch_rejects_an_unsafe_run_id_before_running_git(
    repo: GitRepo,
) -> None:
    sandbox = LocalGitSandbox(repo)

    with pytest.raises(RunDirectoryError):
        commit_run_branch(sandbox, "../escape")

    assert sandbox.commands == []


def test_commit_run_branch_propagates_sandbox_failure() -> None:
    # A Sandbox whose container does not exist raises `SandboxError` from
    # `exec`; that is a broken Sandbox, not a git failure, so it must not be
    # rewrapped as `GitCaptureError`.
    sandbox = Sandbox(RUN_ID, client=None)

    with pytest.raises(SandboxError):
        commit_run_branch(sandbox, RUN_ID)


# --- build_structured_diff (Task 12.2, Requirement 10.2) -------------------


@dataclass
class CannedSandbox:
    """A `Sandbox` stand-in that returns fixed output for the `--numstat`
    command and an empty, successful result for anything else (the per-file
    patch commands).

    Only used for the two things a real git repository cannot produce on
    demand: a rename record (which `--no-renames` prevents by design, but
    which the parser still has to handle) and a malformed record.

    Its results carry text only (no `stdout_bytes`), like any hand-built
    `ExecResult`, so it also covers `git_capture`'s fallback of re-encoding
    `stdout` when the raw bytes are missing.
    """

    stdout: str

    def exec(self, cmd: list[str], *, env: dict[str, str] | None = None) -> ExecResult:
        if "--numstat" in cmd:
            return ExecResult(exit_code=0, stdout=self.stdout, stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")


def _captured_stats(run_dir: Path) -> set[tuple[str, int, int]]:
    """Reads `diff.json` back and returns `{(path, additions, deletions)}`."""
    payload = json.loads(diff_path(run_dir).read_text(encoding="utf-8"))
    return {
        (entry["path"], entry["additions"], entry["deletions"])
        for entry in payload[DIFF_FILES_KEY]
    }


def _payload(run_dir: Path) -> list[dict]:
    """Reads `diff.json` back and returns its `files` entries as written."""
    payload = json.loads(diff_path(run_dir).read_text(encoding="utf-8"))
    return payload[DIFF_FILES_KEY]


def _entry(run_dir: Path, path: str) -> dict:
    """Returns the one `diff.json` entry for `path`."""
    (match,) = [entry for entry in _payload(run_dir) if entry["path"] == path]
    return match


def _hunk_line_counts(patch: str) -> tuple[int, int]:
    """Returns `(added, removed)` line counts of a single-file patch.

    Only lines after the first `@@` hunk header are counted, mirroring the
    security scanner's preamble rule, so the `+++ b/...`/`--- a/...` file
    headers are never mistaken for content. Lines are split on `\\n` only,
    which is git's line terminator.
    """
    added = removed = 0
    in_hunks = False
    for line in patch.split("\n"):
        if line.startswith("@@"):
            in_hunks = True
        elif in_hunks and line.startswith("+"):
            added += 1
        elif in_hunks and line.startswith("-"):
            removed += 1
    return added, removed


def _capture(repo: GitRepo, run_dir: Path) -> tuple[Path, str]:
    """Commits the working tree to the Run branch and builds its structured
    diff against the branch the repository started on, returning the written
    `diff.json` path and that base branch name.
    """
    base_branch = repo.current_branch
    sandbox = LocalGitSandbox(repo)
    branch = commit_run_branch(sandbox, RUN_ID)
    return build_structured_diff(sandbox, base_branch, branch, run_dir), base_branch


def test_build_structured_diff_writes_diff_json_into_the_run_directory(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "runs" / RUN_ID
    _make_agent_changes(repo)

    written, _ = _capture(repo, run_dir)

    assert written == run_dir / DIFF_FILENAME
    assert written.is_file()
    # The Run_Directory did not exist beforehand: the writer creates it.
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert list(payload) == [DIFF_FILES_KEY]


def test_build_structured_diff_records_counts_for_every_change_kind(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    _make_agent_changes(repo)

    _capture(repo, run_dir)

    assert _captured_stats(run_dir) == {
        ("added.py", 1, 0),  # one added line, nothing removed
        ("kept.txt", 1, 0),  # one line appended to the original
        ("doomed.txt", 0, 1),  # its single line removed with the file
    }


def test_build_structured_diff_excludes_gitignored_files(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    _make_agent_changes(repo)

    _capture(repo, run_dir)

    paths = {path for path, _, _ in _captured_stats(run_dir)}
    assert not any(path.startswith("ignored/") for path in paths)


def test_build_structured_diff_with_no_changes_writes_an_empty_file_list(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"

    written, _ = _capture(repo, run_dir)

    assert json.loads(written.read_text(encoding="utf-8")) == {DIFF_FILES_KEY: []}


def test_build_structured_diff_reports_binary_files_with_zero_counts(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    repo.write_bytes("logo.bin", b"\x00\x01\x02binary\x00payload")

    _capture(repo, run_dir)

    # Present (a reviewer must see the blob changed), with no invented lines.
    assert _captured_stats(run_dir) == {("logo.bin", 0, 0)}


def test_build_structured_diff_reports_a_rename_as_a_delete_and_an_add(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    # A pure move: identical content, new path. With rename detection on this
    # would collapse to a single 0/0 record naming two paths.
    (repo.path / "kept.txt").rename(repo.path / "moved.txt")

    _capture(repo, run_dir)

    assert _captured_stats(run_dir) == {
        ("kept.txt", 0, 1),
        ("moved.txt", 1, 0),
    }


def test_build_structured_diff_handles_paths_with_spaces_quotes_and_tabs(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    quoted = 'a dir/weird "name".txt'
    tabbed = "has\ttab.txt"
    repo.write(quoted, "one\n")
    repo.write(tabbed, "two\nthree\n")

    _capture(repo, run_dir)

    assert _captured_stats(run_dir) == {(quoted, 1, 0), (tabbed, 2, 0)}
    # The raw `-z` path reaches the per-file `git diff` intact, so each
    # entry's patch is its own file's.
    patches = {entry["path"]: entry[PATCH_KEY] for entry in _payload(run_dir)}
    assert "+one\n" in patches[quoted]
    assert "+two\n+three\n" in patches[tabbed]


def test_build_structured_diff_preserves_git_output_order(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    _make_agent_changes(repo)
    sandbox = LocalGitSandbox(repo)
    base_branch = repo.current_branch
    branch = commit_run_branch(sandbox, RUN_ID)

    written = build_structured_diff(sandbox, base_branch, branch, run_dir)

    expected = [
        line.split("\t")[2]
        for line in repo.git_ok(
            "diff", "--numstat", "--no-renames", base_branch, branch
        ).splitlines()
    ]
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in payload[DIFF_FILES_KEY]] == expected


def test_build_structured_diff_replaces_an_existing_diff_json(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    diff_path(run_dir).write_text("stale content that is not even JSON")
    _make_agent_changes(repo)

    _capture(repo, run_dir)

    assert _captured_stats(run_dir) == {
        ("added.py", 1, 0),
        ("kept.txt", 1, 0),
        ("doomed.txt", 0, 1),
    }


def test_build_structured_diff_attributes_a_rename_record_to_the_new_path(
    tmp_path: Path,
) -> None:
    # `--numstat -z`'s rename shape: counts, an empty path field, then the old
    # and new paths as two further NUL-terminated fields.
    sandbox = CannedSandbox("3\t1\t\0old/name.py\0new/name.py\0" "2\t0\tplain.py\0")

    build_structured_diff(sandbox, "main", "vaultshield/run-x", tmp_path)

    assert _captured_stats(tmp_path) == {
        ("new/name.py", 3, 1),
        ("plain.py", 2, 0),
    }


@pytest.mark.parametrize(
    "output",
    [
        "not-a-record\0",  # no field separators at all
        "1\tonly-two-fields\0",  # missing the path field
        "x\t2\tsrc/app.py\0",  # non-numeric addition count
        "1\t-2\tsrc/app.py\0",  # signed count, which numstat never emits
        "3\t1\t\0old/name.py\0",  # rename record missing its new path
    ],
)
def test_build_structured_diff_rejects_unparseable_output(
    output: str, tmp_path: Path
) -> None:
    with pytest.raises(GitCaptureError) as excinfo:
        build_structured_diff(CannedSandbox(output), "main", "branch", tmp_path)

    assert "parsing" in str(excinfo.value)
    # Nothing half-written: a diff we could not read is not a diff.
    assert not diff_path(tmp_path).exists()


@pytest.mark.parametrize("bad_ref", ["", "-p", "--output=/tmp/pwned"])
def test_build_structured_diff_rejects_a_ref_git_would_read_as_an_option(
    bad_ref: str, repo: GitRepo, tmp_path: Path
) -> None:
    sandbox = LocalGitSandbox(repo)

    with pytest.raises(GitCaptureError):
        build_structured_diff(sandbox, bad_ref, "some-branch", tmp_path)
    with pytest.raises(GitCaptureError):
        build_structured_diff(sandbox, "some-branch", bad_ref, tmp_path)

    # Rejected before git ran at all, so nothing was executed or written.
    assert sandbox.commands == []
    assert not diff_path(tmp_path).exists()


def test_build_structured_diff_reports_an_unknown_revision(
    repo: GitRepo, tmp_path: Path
) -> None:
    sandbox = LocalGitSandbox(repo)

    with pytest.raises(GitCaptureError) as excinfo:
        build_structured_diff(sandbox, "no-such-ref", repo.current_branch, tmp_path)

    message = str(excinfo.value)
    assert "diffing no-such-ref" in message
    assert "no-such-ref" in message
    assert not diff_path(tmp_path).exists()


def test_build_structured_diff_propagates_sandbox_failure(tmp_path: Path) -> None:
    # A broken Sandbox is not a git failure — same contract as
    # `commit_run_branch`, so it must not be rewrapped as `GitCaptureError`.
    sandbox = Sandbox(RUN_ID, client=None)

    with pytest.raises(SandboxError):
        build_structured_diff(sandbox, "main", f"{BRANCH_PREFIX}{RUN_ID}", tmp_path)


def test_file_diff_stat_is_a_frozen_value() -> None:
    stat = FileDiffStat(path="src/app.py", additions=2, deletions=1)

    with pytest.raises(Exception):
        stat.additions = 5  # type: ignore[misc]
    assert {stat} == {FileDiffStat(path="src/app.py", additions=2, deletions=1)}


# --- per-file patches (Requirements 10.2, 17.1) ----------------------------


def _patch_argv(base: str, branch: str, path: str) -> list[str]:
    """The per-file patch command `build_structured_diff` runs, so a test
    can compare against git's own untruncated output.
    """
    return [
        "--literal-pathspecs",
        "diff",
        "--no-renames",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "-U0",
        base,
        branch,
        "--",
        path,
    ]


def test_build_structured_diff_records_a_patch_for_each_text_file(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    _make_agent_changes(repo)

    _capture(repo, run_dir)

    added = _entry(run_dir, "added.py")
    assert "@@ -0,0 +1 @@" in added[PATCH_KEY]
    assert "+print('new')" in added[PATCH_KEY]
    assert "+agent edit" in _entry(run_dir, "kept.txt")[PATCH_KEY]
    assert "-delete me" in _entry(run_dir, "doomed.txt")[PATCH_KEY]
    # A patch that fits is not flagged: the key is omitted, not false.
    assert not any(PATCH_TRUNCATED_KEY in entry for entry in _payload(run_dir))


def test_build_structured_diff_omits_the_patch_for_binary_files(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    repo.write_bytes("logo.bin", b"\x00\x01\x02binary\x00payload")
    repo.write("notes.txt", "a text change\n")

    _capture(repo, run_dir)

    binary = _entry(run_dir, "logo.bin")
    assert binary == {"path": "logo.bin", "additions": 0, "deletions": 0}
    assert "+a text change" in _entry(run_dir, "notes.txt")[PATCH_KEY]


def test_build_structured_diff_replaces_a_late_nul_in_a_text_patch(
    repo: GitRepo, tmp_path: Path
) -> None:
    # Git's binary check reads only the first 8000 bytes, so a NUL after
    # them leaves the file "text" and puts a raw NUL into its patch. The
    # backend's CharField rejects NUL, so it must not reach `diff.json`.
    run_dir = tmp_path / "run"
    repo.write_bytes("late.txt", b"a\n" * 5000 + b"x\x00y\n")
    base_branch = repo.current_branch
    sandbox = LocalGitSandbox(repo)
    branch = commit_run_branch(sandbox, RUN_ID)

    build_structured_diff(sandbox, base_branch, branch, run_dir)

    entry = _entry(run_dir, "late.txt")
    full = repo.git(*_patch_argv(base_branch, branch, "late.txt")).stdout
    assert "\x00" in full  # git really did classify it as text
    assert entry["additions"] == 5001
    patch = entry[PATCH_KEY]
    assert "\x00" not in patch
    assert "+x\ufffdy\n" in patch
    assert patch == full.replace("\x00", "\ufffd")
    assert PATCH_TRUNCATED_KEY not in entry


def test_build_structured_diff_truncates_an_oversized_patch_at_a_line_boundary(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    # ~300 KiB of 100-character lines mixing ASCII with 2- and 3-byte
    # UTF-8 characters, so a byte-offset cut would land mid-character.
    line = ("abcé€" * 20)[:99]
    line_count = 2000
    repo.write("big.txt", "".join(f"{line}\n" for _ in range(line_count)))
    base_branch = repo.current_branch
    sandbox = LocalGitSandbox(repo)
    branch = commit_run_branch(sandbox, RUN_ID)

    build_structured_diff(sandbox, base_branch, branch, run_dir)

    entry = _entry(run_dir, "big.txt")
    patch = entry[PATCH_KEY]
    full = repo.git(*_patch_argv(base_branch, branch, "big.txt")).stdout
    assert len(full.encode("utf-8")) > MAX_PATCH_BYTES
    assert entry[PATCH_TRUNCATED_KEY] is True
    assert 0 < len(patch.encode("utf-8")) <= MAX_PATCH_BYTES
    assert patch.endswith("\n")
    assert full.startswith(patch)
    # The counts still describe the whole change, not the kept prefix.
    assert entry["additions"] == line_count


def test_build_structured_diff_keeps_the_whole_diff_within_the_request_budget(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = 4096
    monkeypatch.setattr(git_capture, "MAX_DIFF_REQUEST_BYTES", budget)
    run_dir = tmp_path / "run"
    for number in range(10):
        # 16 lines of 64 bytes: about 1 KiB of patch per file.
        content = "".join(f"{number:02d}{'x' * 61}\n" for _ in range(16))
        repo.write(f"f{number}.txt", content)
    base_branch = repo.current_branch
    sandbox = LocalGitSandbox(repo)
    branch = commit_run_branch(sandbox, RUN_ID)
    sandbox.commands.clear()

    build_structured_diff(sandbox, base_branch, branch, run_dir)

    entries = _payload(run_dir)
    # Measured the way the budget is: default `json.dumps` of the body.
    assert len(json.dumps({DIFF_FILES_KEY: entries}).encode()) <= budget
    flagged = [
        index for index, entry in enumerate(entries) if entry.get(PATCH_TRUNCATED_KEY)
    ]
    assert flagged, "a 4 KiB budget cannot hold ten 1 KiB patches"
    assert flagged == list(range(flagged[0], len(entries)))
    assert any(entries[index][PATCH_KEY] == "" for index in flagged)
    # Once the budget is spent no further per-file `git diff` runs: one per
    # entry up to and including the first one that was cut.
    patch_runs = [cmd for cmd in sandbox.commands if "-U0" in cmd]
    assert len(patch_runs) == flagged[0] + 1
    # Nothing is dropped silently: every file is present, an unflagged patch
    # is the whole patch, and a flagged one is a prefix of it.
    assert {entry["path"] for entry in entries} == {f"f{n}.txt" for n in range(10)}
    for entry in entries:
        full = repo.git(*_patch_argv(base_branch, branch, entry["path"])).stdout
        if entry.get(PATCH_TRUNCATED_KEY):
            assert full.startswith(entry[PATCH_KEY])
        else:
            assert entry[PATCH_KEY] == full


def test_build_structured_diff_refuses_a_diff_whose_metadata_alone_is_over_the_budget(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ten entries of about 95 bytes each cannot fit in 256 bytes even with
    # every patch left out, so no uploadable `diff.json` exists.
    monkeypatch.setattr(git_capture, "MAX_DIFF_REQUEST_BYTES", 256)
    run_dir = tmp_path / "run"
    for number in range(10):
        repo.write(f"file-{number:02d}.txt", "x\n")
    base_branch = repo.current_branch
    sandbox = LocalGitSandbox(repo)
    branch = commit_run_branch(sandbox, RUN_ID)
    sandbox.commands.clear()

    with pytest.raises(GitCaptureError) as excinfo:
        build_structured_diff(sandbox, base_branch, branch, run_dir)

    message = str(excinfo.value)
    assert "256" in message
    assert "10 changed files" in message
    assert "MAX_DIFF_REQUEST_BYTES" in message
    # Fails before writing anything or running any per-file `git diff`.
    assert not diff_path(run_dir).exists()
    assert not any("-U0" in cmd for cmd in sandbox.commands)


# --- paths that are not valid UTF-8 (Requirement 17.1) ---------------------


def _commit_non_utf8_file(repo: GitRepo, name: bytes, content: bytes) -> None:
    """Commits a file whose name is the raw bytes `name` (not valid UTF-8).

    Tries a real file on disk first, which works on Linux filesystems. APFS
    (macOS) refuses a name that is not valid UTF-8 with `EILSEQ`, so then
    the blob is written with `git hash-object -w` and the path is put
    straight into the index with `git update-index --cacheinfo`. Either way
    git's commit holds exactly that byte path. The commit is made with
    `commit_as_developer`, not `commit_run_branch`: the latter's
    `git add --all` would stage the deletion of an index-only entry that
    has no file on disk. `build_structured_diff` only reads the two
    commits, so how the commit was made does not matter to it.
    """
    target = os.path.join(os.fsencode(repo.path), name)
    try:
        with open(target, "wb") as handle:
            handle.write(content)
    except OSError as exc:
        if exc.errno != errno.EILSEQ:
            raise
        blob = subprocess.run(
            [b"git", b"hash-object", b"-w", b"--stdin"],
            cwd=repo.path,
            env=repo.env,
            input=content,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                b"git",
                b"update-index",
                b"--add",
                b"--cacheinfo",
                b"100644," + blob + b"," + name,
            ],
            cwd=repo.path,
            env=repo.env,
            capture_output=True,
            check=True,
        )
    else:
        subprocess.run(
            [b"git", b"add", b"--", name],
            cwd=repo.path,
            env=repo.env,
            capture_output=True,
            check=True,
        )
    repo.commit_as_developer("non-UTF-8 file name")


@pytest.mark.parametrize(
    "content, additions",
    [
        (b'API_KEY = "sk-abcdefghij123456"\n', 1),  # text
        (b"\x00binary\x00", 0),  # binary: the flag still wins
    ],
)
def test_build_structured_diff_flags_a_non_utf8_path_without_running_git_for_it(
    content: bytes, additions: int, repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    base = repo.git_ok("rev-parse", "HEAD")
    repo.write("ok.txt", "fine\n")
    repo.git_ok("add", "ok.txt")
    _commit_non_utf8_file(repo, b"\xff.txt", content)
    sandbox = LocalGitSandbox(repo)

    build_structured_diff(sandbox, base, "HEAD", run_dir)

    # No `patch` key at all, and the flag makes the scan's blind spot visible.
    assert _entry(run_dir, "\ufffd.txt") == {
        "path": "\ufffd.txt",
        "additions": additions,
        "deletions": 0,
        PATCH_TRUNCATED_KEY: True,
    }
    ok = _entry(run_dir, "ok.txt")
    assert "+fine" in ok[PATCH_KEY]
    assert PATCH_TRUNCATED_KEY not in ok
    # Git ran a per-file patch only for the valid path.
    patch_runs = [cmd for cmd in sandbox.commands if "-U0" in cmd]
    assert len(patch_runs) == 1
    assert patch_runs[0][-1] == "ok.txt"


def test_build_structured_diff_still_patches_a_valid_name_containing_u_fffd(
    repo: GitRepo, tmp_path: Path
) -> None:
    # U+FFFD encoded as valid UTF-8 (EF BF BD) is a legitimate name, so the
    # bytes-level check must not mistake it for a lossy one.
    run_dir = tmp_path / "run"
    repo.write("\ufffd-real.txt", "x\n")

    _capture(repo, run_dir)

    entry = _entry(run_dir, "\ufffd-real.txt")
    assert "+x" in entry[PATCH_KEY]
    assert PATCH_TRUNCATED_KEY not in entry


class _RawContainer:
    """A docker-py container stand-in for `Sandbox.exec`: `exec_run` returns
    raw demuxed bytes, with a non-UTF-8 path in the `--numstat` output.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def exec_run(self, cmd: list[str], **kwargs: object) -> tuple[int, tuple]:
        self.commands.append(list(cmd))
        if "--numstat" in cmd:
            return 0, (b"1\t0\t\xff.txt\x00", None)
        return 0, (None, None)


def test_sandbox_exec_keeps_stdout_bytes_and_flags_a_non_utf8_numstat_path(
    tmp_path: Path,
) -> None:
    sandbox = Sandbox(RUN_ID, client=None)
    container = _RawContainer()
    sandbox.container = container

    result = sandbox.exec(["git", "diff", "--numstat"])
    assert result.stdout_bytes == b"1\t0\t\xff.txt\x00"
    assert result.stdout == "1\t0\t\ufffd.txt\x00"
    assert result.stderr == ""

    container.commands.clear()
    build_structured_diff(sandbox, "main", "branch", tmp_path)

    assert _payload(tmp_path) == [
        {
            "path": "\ufffd.txt",
            "additions": 1,
            "deletions": 0,
            PATCH_TRUNCATED_KEY: True,
        }
    ]
    assert not any("-U0" in cmd for cmd in container.commands)


def test_build_structured_diff_scopes_each_patch_to_its_own_path(
    repo: GitRepo, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    # Without `--literal-pathspecs`, `a*.txt` would also match `ab.txt` and
    # `:(glob)x.txt` would be read as pathspec magic for `x.txt`.
    repo.write("a*.txt", "star line\n")
    repo.write("ab.txt", "ab line\n")
    repo.write(":(glob)x.txt", "magic line\n")

    _capture(repo, run_dir)

    star = _entry(run_dir, "a*.txt")[PATCH_KEY]
    assert "+star line" in star
    assert "ab line" not in star
    assert "+ab line" in _entry(run_dir, "ab.txt")[PATCH_KEY]
    assert "+magic line" in _entry(run_dir, ":(glob)x.txt")[PATCH_KEY]


# --- resolve_diff_base and the root-commit case (Requirement 10.2) ---------

_SHA1_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def test_resolve_diff_base_returns_the_parent_when_there_is_one(
    repo: GitRepo,
) -> None:
    sandbox = LocalGitSandbox(repo)
    branch = commit_run_branch(sandbox, RUN_ID)

    assert resolve_diff_base(sandbox, branch) == f"{branch}~1"


def test_resolve_diff_base_falls_back_to_the_empty_tree_for_a_root_commit(
    repo: GitRepo,
) -> None:
    # The fixture's base branch tip is its only commit, i.e. a root commit.
    base_branch = repo.current_branch

    resolved = resolve_diff_base(LocalGitSandbox(repo), base_branch)

    assert resolved == repo.git_ok("hash-object", "-t", "tree", "/dev/null")
    assert resolved == _SHA1_EMPTY_TREE


def test_build_structured_diff_against_a_root_commit_reports_every_file_as_added(
    tmp_path: Path,
) -> None:
    # An empty clone: the capture commit becomes the repository's root.
    empty = _init_repo(tmp_path, with_base_commit=False)
    empty.write("settings.py", 'API_KEY = "sk-abcdefghij123456"\n')
    empty.write("README.md", "hello\nworld\n")
    sandbox = LocalGitSandbox(empty)
    branch = commit_run_branch(sandbox, RUN_ID)
    run_dir = tmp_path / "run"

    base = resolve_diff_base(sandbox, branch)
    build_structured_diff(sandbox, base, branch, run_dir)

    assert _captured_stats(run_dir) == {("settings.py", 1, 0), ("README.md", 2, 0)}
    settings_patch = _entry(run_dir, "settings.py")[PATCH_KEY]
    assert "@@ -0,0 +1 @@" in settings_patch
    assert '+API_KEY = "sk-abcdefghij123456"' in settings_patch
    # The bug being fixed: the old `<branch>~1` base does not resolve here.
    with pytest.raises(GitCaptureError):
        build_structured_diff(sandbox, f"{branch}~1", branch, tmp_path / "old")


@pytest.mark.parametrize("bad_ref", ["", "-p", "--output=/tmp/pwned"])
def test_resolve_diff_base_rejects_a_ref_git_would_read_as_an_option(
    bad_ref: str, repo: GitRepo
) -> None:
    sandbox = LocalGitSandbox(repo)

    with pytest.raises(GitCaptureError):
        resolve_diff_base(sandbox, bad_ref)

    assert sandbox.commands == []


def test_resolve_diff_base_reports_a_non_repository(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    sandbox = LocalGitSandbox(
        GitRepo(path=plain, env={**os.environ, "HOME": str(home)})
    )

    with pytest.raises(GitCaptureError) as excinfo:
        resolve_diff_base(sandbox, "main")

    assert "resolving the parent of main" in str(excinfo.value)


def test_resolve_diff_base_propagates_sandbox_failure() -> None:
    sandbox = Sandbox(RUN_ID, client=None)

    with pytest.raises(SandboxError):
        resolve_diff_base(sandbox, f"{BRANCH_PREFIX}{RUN_ID}")


# --- property: counts match the patch (Requirements 10.2, 17.1) -----------

_line = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=30
)
_lines = st.lists(_line, max_size=15)


def _file_bytes(lines: list[str], trailing_newline: bool) -> bytes:
    text = "\n".join(lines) + ("\n" if trailing_newline and lines else "")
    return text.encode("utf-8")


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
@given(before=_lines, before_nl=st.booleans(), after=_lines, after_nl=st.booleans())
def test_additions_and_deletions_match_the_patch_lines(
    before: list[str], before_nl: bool, after: list[str], after_nl: bool
) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        prop_repo = _init_repo(root)
        prop_repo.write_bytes("prop.txt", _file_bytes(before, before_nl))
        prop_repo.git_ok("add", "--all")
        prop_repo.commit_as_developer("property base")
        prop_repo.write_bytes("prop.txt", _file_bytes(after, after_nl))
        sandbox = LocalGitSandbox(prop_repo)
        branch = commit_run_branch(sandbox, RUN_ID)
        run_dir = root / "run"

        base = resolve_diff_base(sandbox, branch)
        build_structured_diff(sandbox, base, branch, run_dir)

        matches = [entry for entry in _payload(run_dir) if entry["path"] == "prop.txt"]
        if not matches:
            return  # identical content: no change to check
        (entry,) = matches
        assert PATCH_KEY in entry
        assert not entry.get(PATCH_TRUNCATED_KEY)
        assert _hunk_line_counts(entry[PATCH_KEY]) == (
            entry["additions"],
            entry["deletions"],
        )


# --- Batch 3 FR5: agent-controlled git configuration (§5.4) ----------------


def test_an_agent_set_core_worktree_stages_nothing_outside_the_repo(tmp_path):
    repo = _init_repo(tmp_path)
    outside = tmp_path / "outside" / "secret"
    outside.mkdir(parents=True)
    (outside / "tok").write_text("token\n")
    repo.git_ok("config", "core.worktree", str(tmp_path))
    repo.write("added.py", "x\n")

    branch = commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    listed = repo.git_ok("ls-tree", "-r", "--full-tree", "--name-only", branch).splitlines()
    assert "added.py" in listed
    assert not any("outside" in p or "secret" in p for p in listed)


def test_a_filter_driver_in_repo_config_fails_before_staging(tmp_path):
    repo = _init_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    repo.git_ok("config", "filter.x.clean", f"sh -c 'touch {marker}; cat'")
    repo.write(".gitattributes", "* filter=x\n")
    repo.write("added.py", "x\n")
    sandbox = LocalGitSandbox(repo)

    with pytest.raises(git_capture.UnsafeGitConfig):
        commit_run_branch(sandbox, RUN_ID)

    assert not marker.exists()
    assert not any("add" in cmd for cmd in sandbox.commands)


def test_repo_hooks_never_run(tmp_path):
    repo = _init_repo(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = repo.path / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    repo.write("added.py", "x\n")

    commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    assert not marker.exists()


def test_a_global_excludes_file_cannot_hide_a_file(tmp_path):
    repo = _init_repo(tmp_path)
    home = Path(repo.env["HOME"])
    (home / "hide").write_text("hidden.txt\n")
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home / 'hide'}\n")
    repo.write("hidden.txt", "still here\n")
    base = repo.current_branch

    branch = commit_run_branch(LocalGitSandbox(repo), RUN_ID)

    assert ("A", "hidden.txt") in repo.name_status(base, branch)


def test_a_gitfile_dot_git_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    real = tmp_path / "real.git"
    (repo.path / ".git").rename(real)
    (repo.path / ".git").write_text(f"gitdir: {real}\n")

    with pytest.raises(git_capture.UnsafeGitConfig, match="gitfile or a symlink"):
        commit_run_branch(LocalGitSandbox(repo), RUN_ID)


def test_every_capture_command_uses_the_hardened_prefix(repo):
    sandbox = LocalGitSandbox(repo)
    _make_agent_changes(repo)
    branch = commit_run_branch(sandbox, RUN_ID)
    base_ref = resolve_diff_base(sandbox, branch)
    build_structured_diff(sandbox, base_ref, branch, repo.path.parent / "run")
    prefix = git_capture._git()
    assert sandbox.commands
    assert all(cmd[: len(prefix)] == prefix for cmd in sandbox.commands)


# --- bundle round trip for awkward names (Day-4 submit/bundle gap) ----------


def test_branch_bundle_round_trip_preserves_non_utf8_and_case_variant_names(
    tmp_path: Path,
) -> None:
    """The commands `trusted_capture.export_run_bundle` and `submit` run — a
    thin `git bundle` of `base..branch` and a ref-only `git fetch` of it into
    a repository that already has the base — carry non-UTF-8 and case-only
    distinct file names through unchanged.

    The names are put straight into the git index with `hash-object` +
    `update-index` and read back from the object store with `ls-tree -z`, so
    APFS (which stores neither a non-UTF-8 name nor two names differing only
    in case) is never involved. The import target is a bare repo, which has no
    working tree to check out into.
    """
    repo = _init_repo(tmp_path)
    default = repo.current_branch
    branch = run_branch_name(RUN_ID)
    repo.git_ok("checkout", "-b", branch)

    names = [b"File.txt", b"file.txt", b"weird-\xff-name.txt"]
    for index, name in enumerate(names):
        blob = subprocess.run(
            [b"git", b"hash-object", b"-w", b"--stdin"],
            cwd=repo.path,
            env=repo.env,
            input=b"content-%d\n" % index,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                b"git",
                b"update-index",
                b"--add",
                b"--cacheinfo",
                b"100644," + blob + b"," + name,
            ],
            cwd=repo.path,
            env=repo.env,
            capture_output=True,
            check=True,
        )
    repo.commit_as_developer("odd names")
    base_oid = repo.git_ok("rev-parse", f"{branch}~1")

    bundle = tmp_path / "branch.bundle"
    repo.git_ok("bundle", "create", str(bundle), f"{base_oid}..{branch}")

    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(bare)],
        env=repo.env,
        capture_output=True,
        check=True,
    )
    # Bring the base the thin bundle builds on, then the ref-only import.
    subprocess.run(
        ["git", "-C", str(bare), "fetch", "--quiet", str(repo.path),
         f"{default}:refs/heads/{default}"],
        env=repo.env,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(bare), "fetch", "--quiet", str(bundle), f"{branch}:{branch}"],
        env=repo.env,
        capture_output=True,
        check=True,
    )

    listing = subprocess.run(
        [b"git", b"-C", os.fsencode(bare), b"ls-tree", b"-r", b"-z",
         b"--name-only", branch.encode()],
        env=repo.env,
        capture_output=True,
        check=True,
    ).stdout
    entries = {chunk for chunk in listing.split(b"\x00") if chunk}
    for name in names:
        assert name in entries
