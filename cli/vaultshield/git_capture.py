"""Git branch and structured-diff capture for one Run.

Implements Tasks 12.1 and 12.2 — both functions from design.md's "Git
branch + diff capture (`git_capture.py`)" section:

    def commit_run_branch(sandbox: Sandbox, run_id: str) -> str:
        \"\"\"Commits all working-tree changes inside the sandbox to a new
        branch named f"vaultshield/run-{run_id}". Returns the branch
        name.\"\"\"

    def build_structured_diff(sandbox: Sandbox, base_ref: str, branch: str,
                               run_dir: Path) -> Path:
        \"\"\"Computes per-file addition/deletion counts between base_ref
        and branch (via `git diff --numstat`), writes them as JSON to
        <run_dir>/diff.json, and returns that path.\"\"\"

Covers Requirement 10.1 ("WHEN agent execution completes inside a Sandbox,
THE VaultShield_CLI SHALL commit the agent's changes to a branch named
`vaultshield/run-<run_id>` inside the Sandbox") and Requirement 10.2
("WHEN a Run's changes are committed, THE VaultShield_CLI SHALL generate a
structured diff recording per-file addition and deletion counts and SHALL
save the diff into the Run_Directory").

The `run` pipeline wiring that calls both in sequence (Task 12.4) is not
here: this module knows how to capture a Run's git state, not when to.

### Everything runs inside the Sandbox

Every git command goes through `Sandbox.exec`, never `subprocess`: the
repository being committed is the *clone inside the container*, not the
developer's checkout. `Sandbox.exec` already runs with the container's
working directory set to the cloned repo (see its docstring), so no path
juggling is needed here and no command names an in-container path — this
module never has to know where `sandbox.py` put the clone.

Commands are passed as argument lists (`["git", "checkout", "-b", branch]`),
not as a `sh -c` string, so nothing in a branch name or commit message is
ever shell-interpreted. The `run_id` is additionally validated (see below),
but the argv form is what makes quoting a non-issue in the first place.

A `SandboxError` raised by `Sandbox.exec` itself (no container, dead
container, unreachable Docker daemon) is left to propagate unchanged rather
than being rewrapped: it means the Sandbox is broken, which is a different
failure from "git refused to do this", and `sandbox.py` already guarantees
one exception type for that class of problem. `GitCaptureError` below is
reserved for a git command that ran and failed.

### Step order, and why each step looks the way it does

    1. git checkout -b vaultshield/run-<run_id>
    2. git add --all
    3. git -c user.name=... -c user.email=... commit --allow-empty
           --no-verify -m "<message>"

**Branch first, then stage/commit.** `git checkout -b` creates the branch at
the current `HEAD` and carries the working tree and index across untouched,
so the agent's uncommitted changes survive the switch and land on the new
branch — which is the point: the base branch is left exactly as the clone
found it, with no VaultShield commit on it.

`checkout -b` rather than `switch -c` because `git switch` only exists from
git 2.23 onward, and the Sandbox base image is caller-chosen (`create()`
takes any `base_image` tag), so this module cannot assume a modern git.

An already-existing branch of that name makes `checkout -b` fail, and that
failure is reported as-is rather than being smoothed over with `-B`
(reset-if-exists) or a checkout-if-present fallback. A `run_id` carries 64
bits of randomness (`run_directory.generate_run_id`), so a collision does
not mean "two runs happened to clash" — it means this Run's branch was
already captured, and silently resetting it would destroy the commit a
reviewer may already be looking at.

**`git add --all`.** Stages additions, modifications, *and* deletions
across the whole repository — the three change kinds Property 16 (Task
12.3) names — including files the agent created, which are untracked and
would be skipped by a bare `git add .`-with-old-git or `git commit -a`.
Files excluded by the repository's own `.gitignore` stay excluded: a
dependency directory or build artifact the agent's install step produced is
not part of "the agent's changes" a reviewer evaluates, and committing one
would bury the real diff.

**`--allow-empty`.** A Run whose agent changed nothing still gets a branch
with a commit on it. That keeps this function's contract uniform — the
returned branch name always identifies a real commit representing this Run
— so `build_structured_diff` (Task 12.2), `vaultshield submit` (Task 29.3),
and `vaultshield teardown` (Task 30.1) each have exactly one shape of
branch to handle instead of two. Without it, a no-change Run would fail
here on git's "nothing to commit" exit status, turning "the agent did
nothing" into a capture error, which it is not.

**`--no-verify`.** The target repository's own commit hooks are skipped.
This commit is a *recording* of what the agent produced, not a developer's
contribution: a `pre-commit` hook that rejects the change would lose the
Run's evidence entirely, and a formatting hook that rewrites files would
make the captured diff differ from what the agent actually wrote —
defeating Requirement 10's purpose ("evaluate exactly what the agent
changed").

**Identity via `-c`, not `git config`.** A freshly cloned repo in a
container usually has no `user.name`/`user.email`, and git refuses to
commit without them. Passing them as one-shot `-c` overrides applies them
to this single commit without writing anything into the repository's
`.git/config`, so the clone's configuration is left as the agent left it
and a later `submit` push carries no VaultShield-injected config.

### Branch-name safety

`run_id` is validated with `run_directory.validate_run_id` before it is
interpolated into a ref name, so a value from `vaultshield submit
<run-id>`/`teardown <run-id>` (Tasks 29.6, 30.3) cannot produce a
malformed or surprising ref. That validator's character set was chosen
partly for this exact use — see its module docstring's ref-safety
reasoning — and `RunDirectoryError` is allowed to propagate from it rather
than being rewrapped, because it is the same error the same bad argument
would produce anywhere else in the CLI.

### The diff command: `git diff --numstat -z --no-renames <base> <branch>`

`--numstat` is named by design.md and is the only stat format that gives
exact per-file counts (`--stat` abbreviates long paths and scales its
histogram to the terminal width; `--shortstat` drops paths entirely).

**`-z`.** Without it, git applies its own path munging to output: a path
containing a space, a tab, a quote, or a non-ASCII byte comes back
C-quoted (double-quoted, with the offending bytes spelled as backslash
escapes), and whether that happens at all depends on the repository's
`core.quotePath` setting. Unquoting that faithfully is fiddly,
config-dependent, and easy to get subtly wrong on exactly the paths that
come up least often and break worst. `-z` is git's own answer:
paths are emitted raw, NUL-terminated, so parsing is unambiguous for every
path a filesystem can hold — and it removes the arrow-form rename record
(`old => new`, `{a => b}/c`) from the picture entirely, since the `-z`
form spells a rename as two separate NUL-terminated paths instead of one
composed display string.

**`--no-renames`.** With rename detection on, moving a file with no
content change is reported as a single record with 0 additions and 0
deletions, and the content that moved is attributed to neither path.
Requirement 10.2 asks for "per-file addition and deletion counts", and the
backend's `RunDiff.files` (`[{"path", "additions", "deletions"}, ...]`)
has exactly one path per record with nowhere to put a second — so a
rename record would have to be flattened into a half-truth anyway.
Turning detection off instead reports the honest per-path change set a
reviewer is being asked to evaluate: the old path fully deleted, the new
path fully added, each with real line counts. Passing it explicitly also
overrides a `diff.renames` setting inherited from the container image's
global gitconfig, so the output never depends on the base image.

The two refs are passed as plain positional arguments (`git diff A B`,
not `A..B`), matching design.md's "between base_ref and branch". They are
checked for a leading `-` first: git would read `-p` or `--output=/tmp/x`
in a ref position as an option, and `--end-of-options` (which would settle
it) only exists from git 2.24, which this module cannot assume for the
same reason it avoids `git switch`. A ref that simply does not exist is
*not* pre-checked — git's own "unknown revision" message is clearer than
anything a pre-flight `rev-parse` round trip would produce, and
`_exec_or_raise` already surfaces it verbatim.

### Reading `--numstat -z` output, including the awkward cases

Records are NUL-terminated, not newline-terminated, fields inside a record
are tab-separated, and there are two shapes (TAB and NUL written out):

    <additions> TAB <deletions> TAB <path> NUL
    <additions> TAB <deletions> TAB NUL <old path> NUL <new path> NUL

The second shape cannot occur while `--no-renames` is passed, but it is
parsed anyway (attributing the counts to the new path) rather than left to
crash a Run on a record shape git is documented to produce.

**Binary files** report `-` for both counts instead of numbers. Line
counts are undefined for binary content, so they are recorded as 0/0: the
file still appears in `diff.json` — a reviewer must see that a binary
blob changed — but with no invented line numbers. `FileDiffStat` carries
no `binary` flag because design.md fixes its three fields, and the
backend's JSON column is documented with those same three keys.

A record that matches neither shape (missing a count field, or a count
that is neither an integer nor `-`) raises `GitCaptureError` naming the
raw record. `--numstat -z`'s format is fixed by git, so this is a
should-never-happen guard, and it fails loudly on purpose: `diff.json` is
the evidence a human approves or rejects a change set on, and silently
dropping a record we could not read would understate what the agent did.

### `diff.json`'s shape

    {"files": [{"path": "src/app.py", "additions": 12, "deletions": 3},
               ...]}

An object with a `files` key rather than a bare top-level array, because
`files` is exactly the value the backend's `RunDiff.files` JSON column
holds (design.md's data model), so the Uploader (Task 18.2) posts the
parsed contents straight through without reshaping them, and a later
addition to the artifact does not have to break the format to make room.
Entries keep git's own output order (path order for a given ref pair), so
`diff.json` reads in the same order as `git diff --stat` for the same two
refs, and re-running the command produces a byte-identical file.

The write is a plain whole-file write, not `credentials.py`'s
atomic-write-then-`os.replace`: a Run's diff is written once, by one
process, at a point in the pipeline where nothing else is reading the
Run_Directory, and unlike a mounted credential file there is no reader
that could observe a half-written one. Unlike the Event_Log it is also a
snapshot rather than a log, so re-capturing a Run's diff replaces the file
instead of appending to it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from vaultshield.run_directory import diff_path, validate_run_id
from vaultshield.sandbox import ExecResult, Sandbox

# Ref namespace for a Run's branch, fixed by Requirement 10.1
# (`vaultshield/run-<run_id>`). Public: `build_structured_diff` (Task 12.2),
# `submit` (Task 29.3), and `teardown` (Task 30.1) all need to name the same
# branch for a Run, and they get it from `run_branch_name` below rather than
# rebuilding the string.
BRANCH_PREFIX = "vaultshield/run-"

# Author/committer identity for the capture commit, applied per-commit via
# `-c` (see the module docstring). Not the developer's own identity: the
# commit is VaultShield recording an agent's work, and attributing it to a
# person would misrepresent who wrote the change under review. The address
# uses the `.invalid` reserved TLD (RFC 2606) so it can never resolve to or
# collide with a real mailbox.
COMMITTER_NAME = "VaultShield"
COMMITTER_EMAIL = "vaultshield@vaultshield.invalid"

# Top-level key in `diff.json`, matching the backend's `RunDiff.files` JSON
# column so the Uploader (Task 18.2) can post the parsed artifact through
# unchanged. Public for the same reason `EVENT_LOG_FILENAME` is: whoever
# reads the artifact back should not have to spell the key itself.
DIFF_FILES_KEY = "files"

# `git diff --numstat`'s stand-in for a count it cannot compute, i.e. a
# binary file's added/deleted lines. Recorded as 0 rather than invented —
# see the module docstring.
_BINARY_COUNT_MARKER = "-"

# Field separator inside one `--numstat` record, and the record terminator
# `-z` substitutes for a newline. Named because the parser below reads
# better talking about records and fields than about "\t" and "\0".
_FIELD_SEPARATOR = "\t"
_RECORD_TERMINATOR = "\0"

# A `--numstat` line count: ASCII digits only, no sign and no separators.
_COUNT_PATTERN = re.compile(r"\A[0-9]+\Z")


class GitCaptureError(Exception):
    """Raised when git capture cannot produce what it promised.

    Three causes, all of them local to this module:

    - a git command ran and exited non-zero. The message names the step
      that failed, the exit code, and the captured git output, so a
      developer sees git's own explanation ("fatal: a branch named ...
      already exists") rather than a bare non-zero status.
    - a ref argument was rejected before any command ran, because git
      would have read it as an option rather than a revision.
    - `git diff --numstat -z` produced a record this module could not
      parse (see the module docstring for why that is not swallowed).

    A distinct exception type per module, matching `ConfigError`,
    `SandboxError`, `RunDirectoryError`, and `ReadinessTimeout`: `cli.py`
    maps each to a `click.ClickException` at its command boundary, so a
    failed capture reaches the developer as one line instead of a
    traceback. Deliberately not raised for Sandbox-level failures — see
    the module docstring — so catching this type means "the capture
    failed", never "Docker is broken".
    """


@dataclass(frozen=True)
class FileDiffStat:
    """Per-file addition and deletion counts for one changed path
    (Requirement 10.2), exactly as design.md declares it.

    `additions`/`deletions` are line counts, both 0 for a binary file
    (whose line counts git reports as `-`) and both 0 for a file git
    listed as changed with no line-level change. Frozen because a stat is
    a recorded measurement of a commit that already exists — nothing
    downstream has any business editing one — and because that makes it
    hashable, so a test can compare whole change sets as sets.

    Field order matters a little: `asdict` preserves it, so `diff.json`'s
    per-file objects read `path`, `additions`, `deletions`, the same order
    design.md documents the backend's JSON column with.
    """

    path: str
    additions: int
    deletions: int


def run_branch_name(run_id: str) -> str:
    """Returns the branch name for `run_id`: `vaultshield/run-<run_id>`.

    Raises `RunDirectoryError` if `run_id` is not a single safe path/ref
    component (see the module docstring's branch-name safety note).

    Exists as its own function because four components need this exact
    string for the same Run (capture, diff, submit, teardown) and only one
    of them should own the spelling.
    """
    return f"{BRANCH_PREFIX}{validate_run_id(run_id)}"


def commit_run_branch(sandbox: Sandbox, run_id: str) -> str:
    """Commits all working-tree changes inside `sandbox` to a new branch
    named `vaultshield/run-<run_id>` and returns that branch name
    (Requirement 10.1).

    On return, the branch exists, is checked out, and carries exactly one
    new commit containing every addition, modification, and deletion the
    agent made to tracked and untracked files (ignored files excluded).
    A Run with no changes at all still produces the branch and an empty
    commit — see the module docstring for why.

    Raises:
        RunDirectoryError: `run_id` is not usable as a ref component.
        GitCaptureError: a git command ran and failed — the branch already
            exists, the working directory is not a git repository, or git
            refused the commit. The message carries git's own output.
        SandboxError: the Sandbox could not run a command at all (no
            container, or a Docker-level failure). Propagated unchanged
            from `Sandbox.exec`.
    """
    branch = run_branch_name(run_id)

    _exec_or_raise(
        sandbox,
        ["git", "checkout", "-b", branch],
        step=f"creating branch {branch}",
    )
    _exec_or_raise(
        sandbox,
        ["git", "add", "--all"],
        step="staging working-tree changes",
    )
    _exec_or_raise(
        sandbox,
        [
            "git",
            "-c",
            f"user.name={COMMITTER_NAME}",
            "-c",
            f"user.email={COMMITTER_EMAIL}",
            "commit",
            "--allow-empty",
            "--no-verify",
            "-m",
            _commit_message(run_id),
        ],
        step=f"committing changes to {branch}",
    )

    return branch


def build_structured_diff(
    sandbox: Sandbox, base_ref: str, branch: str, run_dir: Path
) -> Path:
    """Computes per-file addition/deletion counts between `base_ref` and
    `branch` inside `sandbox`, writes them as JSON to `<run_dir>/diff.json`,
    and returns that path (Requirement 10.2).

    The written file is `{"files": [{"path", "additions", "deletions"},
    ...]}` — one entry per changed path, in git's own output order, with
    both counts 0 for a binary file. A change set of nothing at all still
    produces the file, with an empty `files` list: "the agent changed
    nothing" is a reviewable result, not a missing artifact, and the
    Uploader (Task 18.2) always has a diff to post. `run_dir` (and any
    missing parent) is created if it does not exist, matching the
    Monitor's Event_Log writer; an existing `diff.json` is replaced.

    Rename detection is deliberately off, so a moved file appears as one
    deleted path plus one added path rather than a single 0/0 record — see
    the module docstring for that and for the rest of the command's shape.

    Raises:
        GitCaptureError: `base_ref` or `branch` is empty or would be read
            by git as an option; `git diff` itself failed (most often an
            unknown revision, reported with git's own message); or its
            output contained an unparseable record.
        SandboxError: the Sandbox could not run a command at all (no
            container, or a Docker-level failure). Propagated unchanged
            from `Sandbox.exec`.
        OSError: `<run_dir>/diff.json` could not be written.
    """
    result = _exec_or_raise(
        sandbox,
        [
            "git",
            "diff",
            "--numstat",
            "-z",
            "--no-renames",
            _checked_ref(base_ref, "base_ref"),
            _checked_ref(branch, "branch"),
        ],
        step=f"diffing {base_ref}..{branch}",
    )

    return _write_diff_json(diff_path(run_dir), _parse_numstat(result.stdout))


def _checked_ref(ref: str, label: str) -> str:
    """Returns `ref` unchanged if git will read it as a revision, else
    raises `GitCaptureError` naming `label` and the rejected value.

    Rejects the empty string and anything starting with `-`; see the module
    docstring for why this is a pre-flight check rather than a trailing
    `--end-of-options`. Existence is not checked — that is git's job, and
    its message is better than ours would be.
    """
    if not ref or ref.startswith("-"):
        raise GitCaptureError(
            f"Git capture failed while validating {label}: {ref!r} is not usable "
            "as a git revision (it must be non-empty and must not start with '-')"
        )
    return ref


def _parse_numstat(output: str) -> list[FileDiffStat]:
    """Parses `git diff --numstat -z` output into one `FileDiffStat` per
    changed path, preserving git's order.

    Handles both record shapes the module docstring lists — ordinary
    changes, and the rename/copy form whose path field is empty and is
    followed by the old and new paths as two further NUL-terminated fields
    (attributed to the new path, since that is the path that exists after
    the commit). Binary files, whose counts are `-`, become 0/0.

    Raises `GitCaptureError` on a record that matches neither shape, rather
    than dropping it: an under-reported diff would understate what a
    reviewer is approving.
    """
    # `-z` terminates every record, so a well-formed non-empty output ends
    # with the terminator and `split` leaves one trailing empty field. An
    # empty output (no changes at all) splits to a single empty field. Both
    # are dropped the same way, and the resulting list is consumed by index
    # because a rename record has to pull the two fields that follow it.
    fields = output.split(_RECORD_TERMINATOR)
    if fields and fields[-1] == "":
        fields.pop()

    stats: list[FileDiffStat] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1

        parts = record.split(_FIELD_SEPARATOR, 2)
        if len(parts) != 3:
            raise GitCaptureError(_unparseable_message(record))
        raw_additions, raw_deletions, path = parts

        if path == "":
            # Rename/copy: `<counts>TAB<counts>TAB` then old NUL new NUL.
            if index + 1 >= len(fields):
                raise GitCaptureError(_unparseable_message(record))
            path = fields[index + 1]
            index += 2

        stats.append(
            FileDiffStat(
                path=path,
                additions=_parse_count(raw_additions, record),
                deletions=_parse_count(raw_deletions, record),
            )
        )

    return stats


def _parse_count(raw: str, record: str) -> int:
    """Returns one `--numstat` count as an int: `-` (binary) becomes 0, a
    run of decimal digits is converted, anything else raises
    `GitCaptureError` naming the whole `record` it came from.

    The digits-only check is stricter than `int()`, which also accepts a
    sign, surrounding whitespace, underscores, and non-ASCII digits — none
    of which `--numstat` emits, and all of which would turn a
    misunderstood record into a plausible-looking count.
    """
    if raw == _BINARY_COUNT_MARKER:
        return 0
    if not _COUNT_PATTERN.match(raw):
        raise GitCaptureError(_unparseable_message(record))
    return int(raw)


def _unparseable_message(record: str) -> str:
    """Builds the `GitCaptureError` message for an unreadable numstat
    record, quoting the record itself so a bug report carries the evidence.
    """
    return (
        "Git capture failed while parsing 'git diff --numstat -z' output: "
        f"unrecognized record {record!r}"
    )


def _write_diff_json(path: Path, stats: list[FileDiffStat]) -> Path:
    """Writes `stats` to `path` as `{"files": [...]}` and returns `path`,
    creating `path`'s parent directories if needed.

    A whole-file write, not an append and not an atomic replace — see the
    module docstring. `indent=2` because a developer reads this file
    directly while reviewing a Run, `ensure_ascii=False` (with an explicit
    UTF-8 encoding) so a non-ASCII path stays readable rather than becoming
    escape sequences, and a trailing newline so the file is a well-formed
    text file for `cat`/`diff`/git.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {DIFF_FILES_KEY: [asdict(stat) for stat in stats]}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _commit_message(run_id: str) -> str:
    """Returns the capture commit's message.

    Single line, leading with the product name and the `run_id`, so the
    commit is identifiable in `git log` on the developer's machine after a
    `vaultshield submit` push and traceable back to its Run_Directory and
    its dashboard record (which key off the same `run_id`).
    """
    return f"VaultShield run {run_id}"


def _exec_or_raise(sandbox: Sandbox, cmd: list[str], *, step: str) -> ExecResult:
    """Runs `cmd` inside `sandbox` and returns its `ExecResult`, raising
    `GitCaptureError` naming `step`, the exit code, and git's output if the
    command exited non-zero.

    Mirrors `sandbox.Sandbox._exec_or_raise`'s reporting shape (step, exit
    code, stderr-or-stdout detail) so a capture failure reads the same way
    as a Sandbox creation failure. `stderr` is preferred because git writes
    its diagnostics there; `stdout` is the fallback for the commands that
    explain themselves there instead (`git commit` reports "nothing to
    commit" on stdout).
    """
    result = sandbox.exec(cmd)
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "no output captured").strip()
        raise GitCaptureError(
            f"Git capture failed while {step} (exit code {result.exit_code}): "
            f"{detail}"
        )
    return result
