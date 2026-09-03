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

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vaultshield.git_capture import (
    BRANCH_PREFIX,
    COMMITTER_EMAIL,
    COMMITTER_NAME,
    DIFF_FILES_KEY,
    FileDiffStat,
    GitCaptureError,
    build_structured_diff,
    commit_run_branch,
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
    """

    repo: GitRepo
    commands: list[list[str]] = field(default_factory=list)

    def exec(self, cmd: list[str]) -> ExecResult:
        self.commands.append(list(cmd))
        completed = subprocess.run(
            cmd,
            cwd=self.repo.path,
            env=self.repo.env,
            capture_output=True,
            text=True,
        )
        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    """A repository with one base commit containing three files and a
    `.gitignore`, standing in for the clone `Sandbox.create` makes.
    """
    work = tmp_path / "repo"
    work.mkdir()
    home = tmp_path / "home"
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
    git_repo.write("kept.txt", "original\n")
    git_repo.write("doomed.txt", "delete me\n")
    git_repo.write(".gitignore", "ignored/\n")
    git_repo.git_ok("add", "--all")
    git_repo.commit_as_developer("base commit")
    return git_repo


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
    """A `Sandbox` stand-in that returns fixed output for any command.

    Only used for the two things a real git repository cannot produce on
    demand: a rename record (which `--no-renames` prevents by design, but
    which the parser still has to handle) and a malformed record.
    """

    stdout: str

    def exec(self, cmd: list[str]) -> ExecResult:
        return ExecResult(exit_code=0, stdout=self.stdout, stderr="")


def _captured_stats(run_dir: Path) -> set[tuple[str, int, int]]:
    """Reads `diff.json` back and returns `{(path, additions, deletions)}`."""
    payload = json.loads(diff_path(run_dir).read_text(encoding="utf-8"))
    return {
        (entry["path"], entry["additions"], entry["deletions"])
        for entry in payload[DIFF_FILES_KEY]
    }


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
