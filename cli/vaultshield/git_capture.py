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

Each changed text file's entry also carries its unified diff as `patch`,
because the backend's security scan reads added lines out of it
(Requirement 17.1: "scan the Run's diff"), and the reviewer reads the same
text (Requirement 13.2). A third public function, `resolve_diff_base`,
names the ref to diff against, so a Run whose capture commit is a root
commit still produces a diff (see "The diff base" below).

The `run` pipeline wiring that calls these in sequence (Task 12.4) is not
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

### Agent-controlled git configuration (Batch 3 FR5)

The repository's `.git` is writable by the agent, so nothing in it is trusted.
Every command is built with `_git(...)` (`--git-dir=.git --work-tree=.` plus
`-c` overrides for hooks, fsmonitor, excludes/attributes files and transports)
and runs with `_GIT_ENV` (no system or global config). `commit_run_branch`
first runs `_assert_capture_repo_is_safe`: a gitfile/symlinked `.git`, or a
filter/textconv/diff-command driver in the repo config, raises
`UnsafeGitConfig` before `git add` could execute anything.

In `vaultshield run` these functions do not run on the Sandbox at all: they run
on the trusted capture helper (`trusted_capture.TrustedCapture`), a fresh
container whose repository is rebuilt from the pre-agent base commit and a
filtered copy of the work tree. The functions stay duck-typed on `.exec`, so
"Sandbox" below means "the exec target".

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

The output is read from `ExecResult.stdout_bytes`, git's raw bytes, not
from the decoded `stdout`, and each path is decoded strictly as UTF-8. A
path that fails is marked lossy (see "Paths that are not valid UTF-8"
below).

**Binary files** report `-` for both counts instead of numbers. Line
counts are undefined for binary content, so they are recorded as 0/0: the
file still appears in `diff.json` — a reviewer must see that a binary
blob changed — but with no invented line numbers. `FileDiffStat` carries
no `binary` flag; what marks a binary entry in `diff.json` is that it has
no `patch` key (see below). The parser reports binary-ness to the caller
through the private `_NumstatRecord` instead.

A record that matches neither shape (missing a count field, or a count
that is neither an integer nor `-`) raises `GitCaptureError` naming the
raw record. `--numstat -z`'s format is fixed by git, so this is a
should-never-happen guard, and it fails loudly on purpose: `diff.json` is
the evidence a human approves or rejects a change set on, and silently
dropping a record we could not read would understate what the agent did.

### The diff base: `resolve_diff_base`

The base is the capture commit's first parent, `<branch>~1`, which is the
commit the Sandbox's clone was on before the agent touched it. That ref does
not resolve when the capture commit is itself a root commit, which is what
an *empty* clone produces (`checkout -b` works on an unborn branch, and the
capture commit then has no parent). `git rev-parse --verify --quiet
<branch>~1` detects that case without an error message (exit 1, no output),
and the base becomes git's empty tree, printed by `git hash-object -t tree
/dev/null`. Diffing against it reports every file in the commit as added,
which is exactly what the agent did to an empty repository. `hash-object`
is used rather than a hard-coded SHA so the answer is also right in a
SHA-256 repository, and git resolves the empty tree without it being
written to the object database, so nothing is written (no `-w`).

### Per-file patches (`git diff -U0`)

For every text record, the patch comes from one further command per path:

    git --literal-pathspecs diff --no-renames --no-color --no-ext-diff
        --no-textconv -U0 <base> <branch> -- <path>

- `-U0` keeps only added and removed lines with their hunk headers, so the
  size cap below is spent on the text the scanner reads, not on context.
- `<path>` is the path from the `-z` numstat record, passed as one argv
  element after `--`. It is git's own name only when that name is valid
  UTF-8; any other path never reaches this command (see below). `--literal-pathspecs` stops a path that starts with
  `:` or contains `*`, `?` or `[` from being read as pathspec magic or a
  glob, so each patch covers exactly its own path.
- `--no-renames` matches the numstat call, so a patch and its counts
  describe the same change. `--no-color`, `--no-ext-diff` and
  `--no-textconv` stop the clone's `.git/config` (which the agent can
  write) or the image's gitconfig from changing what the patch contains,
  for the same reason `--no-renames` is passed explicitly.

**Binary files get no `patch` key at all.** Their counts are `-`, and git's
patch text for them is only "Binary files differ", which carries nothing to
scan or review.

**Known gap: `.gitattributes` decides what is binary.** Unlike the config
that `--no-textconv` and `--no-ext-diff` neutralize, the binary decision
still reads the repository's attributes, and the agent can write
`.gitattributes`. A committed `settings.py -diff` makes numstat print `-`
for a text file, so it gets no `patch` and the security scan never reads
its added lines. The `.gitattributes` change itself is in the diff, so a
reviewer can see it, but the automated scan of that file is bypassed.
Closing the gap (for example `--attr-source=<empty tree>`, git >= 2.42, or
a NUL check of the blob for `-` records) would change which files carry a
patch, so it is left as a follow-up rather than done here.

**Paths that are not valid UTF-8.** A Linux Sandbox allows any byte but `/`
and NUL in a file name, and `Sandbox.exec`'s text output is decoded with
`errors="replace"`. Such a name comes back with U+FFFD in place of the bad
bytes and cannot be handed back to git: the pathspec matches nothing, and
git exits 0 with an empty patch, which would look like a complete empty
patch. So no `git diff` runs for that path, and its entry carries
`"patch_truncated": true` with no `patch`. The scan's blind spot is then
visible downstream instead of silent (Requirement 17.1). This holds for a
binary record too. Detection works on the raw bytes, so a valid name that
really contains U+FFFD is still scanned. The stored `path` is the U+FFFD
spelling, which is valid text for the backend.

**NUL characters.** Git calls a file text when its first 8000 bytes hold no
NUL, so a text patch can still contain a raw NUL further down. The
backend's `CharField` rejects any string containing NUL, which would turn
one such file into a 400 for the whole diff (and let an agent suppress
every scan finding for a Run by appending one NUL past 8 KB). Each NUL in a
captured patch is therefore replaced with U+FFFD before truncation and
costing. That is the same replace-don't-fail policy `Sandbox._decode`
applies to undecodable bytes, and it is one character for one character,
so no line splits or merges and every hunk line number stays correct.

**Known costs.** `Sandbox.exec` returns a command's whole stdout, so a
patch is fully buffered in CLI memory before it is cut to
`MAX_PATCH_BYTES`: a several-hundred-MB generated file costs that much RAM
for a moment. And each changed text file costs one more `docker exec`, so
a diff touching hundreds of files adds noticeable latency. Both follow
from the per-path command the task prescribes and are accepted here.

**Per-file cap: `MAX_PATCH_BYTES` (200 KiB of UTF-8).** One huge generated
file must not crowd every other file out of the upload. The truncation
strategy is to cut at a line boundary: the patch is split on `\\n` only, and
whole lines (terminator included) are kept while the running UTF-8 size
stays within the cap. The kept text is always a whole `str`, so it can
never be invalid UTF-8, and no line is cut in half. That matters because
the scanner reads whole lines: a clipped line could produce a false
`secret-literal` match on a partial literal, or hide a real one. The hunk
headers of the kept lines stay intact, so their line numbers stay correct.
A cut patch keeps the prefix and adds `"patch_truncated": true`. If even
the first line is over the cap, the kept prefix is `""`.

**Total budget: `MAX_DIFF_REQUEST_BYTES` (2 MiB).** The Uploader posts
`diff.json` as one request body, and the backend leaves Django's
`DATA_UPLOAD_MAX_MEMORY_SIZE` at its 2.5 MiB default. That setting is
process-wide (it also bounds the unauthenticated login endpoint), and no
finite value would be enough anyway with no limit on the number of files,
so the CLI keeps its own body under it instead. The budget is spent in two
passes:

1. Before running any per-file git command, the metadata of every entry is
   costed as if it carried `"patch": ""` and `"patch_truncated": true`.
2. Then, in git order, each text file's patch gets whatever remains, up to
   `MAX_PATCH_BYTES`. Once the budget is spent, later text files get
   `"patch": ""` with `"patch_truncated": true`, and their `git diff` is
   never run.

Sizes are measured as `json.dumps` with its defaults (`ensure_ascii=True`,
spaced separators). That is the larger of the two encodings the supported
httpx versions use, so it is an upper bound for either. `patch_truncated`
has one meaning throughout: "the security scan did not see this file's
whole patch". Either `patch` is a strict prefix of the real patch, or it is
absent because the path is not valid UTF-8. No entry is ever dropped.

If metadata alone is over the budget (thousands of changed files), capture
raises `GitCaptureError`, which `cli.py` shows as a `click.ClickException`.
It does so before any per-file `git diff` runs and before `diff.json` is
written: no uploadable diff exists, and sending one anyway would only fail
later, at upload, with a 400 (Requirement 10.2).

### `diff.json`'s shape

    {"files": [{"path": "src/app.py", "additions": 12, "deletions": 3,
                "patch": "diff --git a/src/app.py b/src/app.py\\n..."},
               {"path": "big.sql", "additions": 90000, "deletions": 0,
                "patch": "diff --git ...", "patch_truncated": true},
               {"path": "logo.png", "additions": 0, "deletions": 0},
               {"path": "\\ufffd.txt", "additions": 1, "deletions": 0,
                "patch_truncated": true},
               ...]}

`patch` is omitted (not null) for a binary file and for a path that is not
valid UTF-8 (the last example), and `patch_truncated` is omitted (not
false) unless the scan did not see the whole patch, so an entry only
carries the keys that say something.

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
import os
import re
from dataclasses import dataclass, replace
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

# Per-entry keys for a file's unified diff and its truncation flag, matching
# the optional fields the backend's `FileDiffStatSerializer` accepts and the
# key the security scan reads (`patch`). Public for the same reason as
# `DIFF_FILES_KEY`.
PATCH_KEY = "patch"
PATCH_TRUNCATED_KEY = "patch_truncated"

# Largest `patch` kept for one file, in UTF-8 bytes. Big enough for any
# hand-written change, small enough that one generated file (a lockfile, a
# SQL dump) cannot take the whole request budget below. Truncation cuts at a
# line boundary — see the module docstring.
MAX_PATCH_BYTES = 200 * 1024

# Upper bound on the whole `diff.json` request body, measured as default
# `json.dumps` output. Sized under Django's default
# `DATA_UPLOAD_MAX_MEMORY_SIZE` (2.5 MiB), which the backend deliberately
# does not raise, leaving about 0.5 MiB of slack for encoder differences.
MAX_DIFF_REQUEST_BYTES = 2 * 1024 * 1024

# `git diff --numstat`'s stand-in for a count it cannot compute, i.e. a
# binary file's added/deleted lines. Recorded as 0 rather than invented —
# see the module docstring.
_BINARY_COUNT_MARKER = "-"

# Field separator inside one `--numstat` record, and the record terminator
# `-z` substitutes for a newline. Named because the parser below reads
# better talking about records and fields than about "\t" and "\0". Bytes,
# because the parser works on git's raw stdout so it can tell which paths
# are not valid UTF-8 (see the module docstring).
_FIELD_SEPARATOR = b"\t"
_RECORD_TERMINATOR = b"\0"

# A `--numstat` line count: ASCII digits only, no sign and no separators.
_COUNT_PATTERN = re.compile(r"\A[0-9]+\Z")

# What `git hash-object -t tree /dev/null` must print: a lowercase hex object
# name, 40 digits for SHA-1 or 64 for SHA-256. Anything else is not used as a
# ref, because it would end up in a `git diff` argv position.
_OBJECT_NAME_PATTERN = re.compile(r"\A[0-9a-f]{40}([0-9a-f]{24})?\Z")

# The line terminator a patch is truncated on. Only `\n`, not everything
# `str.splitlines` treats as a break, because git ends patch lines with `\n`
# and a `\r` inside a line is content.
_PATCH_LINE_END = "\n"

# A NUL inside a text file's patch, and what it is sent as. The backend's
# `CharField` rejects NUL outright, so one raw NUL would fail the whole diff
# POST; U+FFFD matches `Sandbox._decode`'s `errors="replace"` policy and
# keeps every line and line number intact. See the module docstring.
_NUL = "\x00"
_NUL_REPLACEMENT = "\ufffd"


# Fixed git environment for every capture command (Batch 3 FR5, §5.4): no
# system or global config, no system attributes, never prompt, no optional
# locks. Passed as `sandbox.exec(cmd, env=_GIT_ENV)`.
_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}

# Config keys whose presence means git would run a program of the repo's
# choosing while staging or diffing (§5.4 check 2).
_UNSAFE_CONFIG_REGEX = r"^filter\..*\.(clean|smudge|process)$|^diff\..*\.(textconv|command)$"


def _git(*args: str) -> list[str]:
    """The capture argv prefix (Batch 3 FR5, §5.4).

    `--git-dir=.git --work-tree=.` beats an agent-set `core.worktree` (the
    measured vector that staged `../outside/secret`), and each `-c` beats
    `.git/config` and its includes: no hooks, no fsmonitor, no global excludes
    or attributes files, no transport. Relative paths, because the exec target
    runs in the repository directory.
    """
    return [
        "git",
        "--git-dir=.git",
        "--work-tree=.",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "protocol.allow=never",
        *args,
    ]


class GitCaptureError(Exception):
    """Raised when git capture cannot produce what it promised.

    Four causes, all of them local to this module:

    - a git command ran and exited non-zero. The message names the step
      that failed, the exit code, and the captured git output, so a
      developer sees git's own explanation ("fatal: a branch named ...
      already exists") rather than a bare non-zero status.
    - a ref argument was rejected before any command ran, because git
      would have read it as an option rather than a revision.
    - `git diff --numstat -z` produced a record this module could not
      parse (see the module docstring for why that is not swallowed).
    - the diff's per-file metadata alone is over `MAX_DIFF_REQUEST_BYTES`,
      so no uploadable `diff.json` exists. The message names the file
      count, the size, and the cap.

    A distinct exception type per module, matching `ConfigError`,
    `SandboxError`, `RunDirectoryError`, and `ReadinessTimeout`: `cli.py`
    maps each to a `click.ClickException` at its command boundary, so a
    failed capture reaches the developer as one line instead of a
    traceback. Deliberately not raised for Sandbox-level failures — see
    the module docstring — so catching this type means "the capture
    failed", never "Docker is broken".
    """


class UnsafeGitConfig(GitCaptureError):
    """The repository's git setup could widen or subvert capture (Batch 3 FR5,
    §5.4): `.git` is a gitfile or symlink, the repo config defines a
    filter/diff driver, or (trusted capture) the Sandbox repo has no `.git`
    directory or a `.git/config` git cannot parse. `cli.py` handles it like a
    token-hygiene violation. Raised before `git add` can run a filter."""


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

    `patch` is the file's `git diff -U0` text, carried because the
    security scan reads added lines out of it (Requirement 17.1) and the
    reviewer reads the same change (Requirement 10.2). It is `None` for a
    binary file, which has no line-level patch, and for a file whose path
    is not valid UTF-8, which cannot be handed back to git. It is `""` with
    `patch_truncated` set when the request budget ran out before this
    file. `patch_truncated` means "the security scan did not see this
    file's whole patch": either `patch` is a strict prefix of the real
    patch (see the module docstring's size caps), or `patch` is absent
    because the path is not valid UTF-8 (Requirement 17.1). A patch-less
    entry without the flag still means "binary". Both default so
    a stat built from counts alone still reads as design.md declares it.

    Field order matters a little: `diff.json`'s per-file objects are
    written as `path`, `additions`, `deletions`, the same order design.md
    documents the backend's JSON column with, followed by `patch` and
    `patch_truncated` only when they carry something.
    """

    path: str
    additions: int
    deletions: int
    patch: str | None = None
    patch_truncated: bool = False


@dataclass(frozen=True)
class _NumstatRecord:
    """One parsed `--numstat -z` record: its stat, plus whether git reported
    `-` counts (a binary file). Private because binary-ness only decides
    whether a patch is fetched; it is not part of `diff.json`.

    `lossy_path` is True when git's path bytes are not valid UTF-8, so
    `stat.path` is a lossy (U+FFFD) spelling that cannot be passed back to
    git as a pathspec. Such a record never gets a `git diff` of its own.
    """

    stat: FileDiffStat
    binary: bool
    lossy_path: bool = False


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

    # Batch 3 FR5 (§5.4): refuse a gitfile/symlinked `.git` and any
    # filter/diff driver BEFORE `git add`, which is what would run a filter.
    _assert_capture_repo_is_safe(sandbox)

    _exec_or_raise(
        sandbox,
        _git("checkout", "-b", branch),
        step=f"creating branch {branch}",
    )
    _exec_or_raise(
        sandbox,
        _git("add", "--all"),
        step="staging working-tree changes",
    )
    _exec_or_raise(
        sandbox,
        _git(
            "-c",
            f"user.name={COMMITTER_NAME}",
            "-c",
            f"user.email={COMMITTER_EMAIL}",
            "commit",
            "--allow-empty",
            "--no-verify",
            "-m",
            _commit_message(run_id),
        ),
        step=f"committing changes to {branch}",
    )

    return branch


def _assert_capture_repo_is_safe(sandbox: Sandbox) -> None:
    """The first step of `commit_run_branch` (Batch 3 FR5, §5.4).

    1. `rev-parse --absolute-git-dir` must equal `--show-toplevel` + `/.git`,
       which refuses a `.git` that is a gitfile or a symlink. When `rev-parse`
       itself fails (not a git repository at all), nothing is decided here:
       the next step fails with git's own message.
    2. `git config --get-regexp` for filter/textconv/diff-command keys must
       print nothing (exit 1 is "no match").

    Under trusted capture this runs, harmlessly, against the helper's own
    repository; the same two checks also run on the untrusted Sandbox archive
    (`trusted_capture`).

    Raises:
        UnsafeGitConfig: either check failed.
    """
    git_dir = sandbox.exec(_git("rev-parse", "--absolute-git-dir"), env=_GIT_ENV)
    top = sandbox.exec(_git("rev-parse", "--show-toplevel"), env=_GIT_ENV)
    if git_dir.exit_code == 0 and top.exit_code == 0:
        expected = top.stdout.strip() + "/.git"
        actual = git_dir.stdout.strip()
        if os.path.realpath(actual) != os.path.realpath(expected) or os.path.basename(
            actual
        ) != ".git":
            raise UnsafeGitConfig(
                "Git capture refused the repository: its .git is not a plain "
                "directory at the repository root (a gitfile or a symlink)"
            )
    elif git_dir.exit_code == 0 or top.exit_code == 0:
        raise UnsafeGitConfig(
            "Git capture refused the repository: git could not agree on its "
            "git directory and work tree"
        )
    else:
        return

    drivers = sandbox.exec(
        _git("config", "--get-regexp", _UNSAFE_CONFIG_REGEX), env=_GIT_ENV
    )
    if drivers.exit_code == 0 and drivers.stdout.strip():
        raise UnsafeGitConfig(
            "Git capture refused the repository: its git config defines a filter "
            "or diff driver, which staging or diffing would execute"
        )
    if drivers.exit_code not in (0, 1):
        raise UnsafeGitConfig(
            "Git capture refused the repository: its git config could not be "
            f"read (git config exit {drivers.exit_code})"
        )


def build_structured_diff(
    sandbox: Sandbox, base_ref: str, branch: str, run_dir: Path
) -> Path:
    """Computes per-file addition/deletion counts between `base_ref` and
    `branch` inside `sandbox`, writes them as JSON to `<run_dir>/diff.json`,
    and returns that path (Requirement 10.2).

    The written file is `{"files": [{"path", "additions", "deletions",
    "patch"?, "patch_truncated"?}, ...]}` — one entry per changed path, in
    git's own output order. A text file's entry carries its `git diff -U0`
    text as `patch` (Requirement 17.1), cut at a line boundary to
    `MAX_PATCH_BYTES` and to what is left of `MAX_DIFF_REQUEST_BYTES`, with
    `"patch_truncated": true` when cut. A binary file has both counts 0 and
    no `patch`. A file whose path is not valid UTF-8 gets no `git diff` and
    no `patch`, but does get `"patch_truncated": true`, so the scan's blind
    spot shows downstream (Requirement 17.1). A change set of nothing at all still
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
            by git as an option (checked before any command runs); a
            `git diff` itself failed (most often an unknown revision,
            reported with git's own message); the numstat output
            contained an unparseable record; or the per-file metadata
            alone (every patch left out) is over `MAX_DIFF_REQUEST_BYTES`.
            The last is raised before any per-file `git diff` runs and
            before `diff.json` is written: dropping entries would silently
            understate the change set (Requirement 10.2), and failing now
            reports it while the Run's artifacts are still local
            (Requirement 14.4) instead of at upload.
        SandboxError: the Sandbox could not run a command at all (no
            container, or a Docker-level failure). Propagated unchanged
            from `Sandbox.exec`.
        OSError: `<run_dir>/diff.json` could not be written.
    """
    base_ref = _checked_ref(base_ref, "base_ref")
    branch = _checked_ref(branch, "branch")

    result = _exec_or_raise(
        sandbox,
        _git(
            "diff",
            "--numstat",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            base_ref,
            branch,
        ),
        step=f"diffing {base_ref}..{branch}",
    )
    records = _parse_numstat(_raw_stdout(result))

    # Pass 1: what the request body costs with every patch empty. If even
    # that is over the cap, no uploadable diff exists: fail now, before any
    # per-file `git diff` and before `diff.json` is written. Pass 2 spends
    # whatever is left on patches, in git order.
    metadata_cost = _metadata_cost(records)
    if metadata_cost > MAX_DIFF_REQUEST_BYTES:
        raise GitCaptureError(
            "Git capture failed while sizing diff.json: the entries for "
            f"{len(records)} changed files take {metadata_cost} bytes even "
            f"with every patch left out, over the {MAX_DIFF_REQUEST_BYTES}-byte "
            "upload cap (MAX_DIFF_REQUEST_BYTES), so the backend would reject "
            "the diff"
        )
    remaining = MAX_DIFF_REQUEST_BYTES - metadata_cost
    stats: list[FileDiffStat] = []
    for record in records:
        stat = record.stat
        if record.lossy_path:
            # The decoded path is not the name git knows, so a `git diff`
            # for it would match nothing and look like an empty patch. Flag
            # the blind spot instead of running git (Requirement 17.1).
            stats.append(replace(stat, patch_truncated=True))
            continue
        if record.binary:
            stats.append(stat)
            continue
        if remaining <= 0:
            stats.append(replace(stat, patch="", patch_truncated=True))
            continue
        patch_result = _exec_or_raise(
            sandbox,
            _git(
                "--literal-pathspecs",
                "diff",
                "--no-renames",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "-U0",
                base_ref,
                branch,
                "--",
                stat.path,
            ),
            step=f"diffing {stat.path!r} between {base_ref}..{branch}",
        )
        # NUL is replaced before truncation and costing, so the budget is
        # spent on the text that is actually sent (see "NUL characters").
        patch_text = patch_result.stdout.replace(_NUL, _NUL_REPLACEMENT)
        patch, truncated, spent, budget_hit = _truncate_patch(
            patch_text, MAX_PATCH_BYTES, remaining
        )
        # A cut forced by the request budget (not the per-file cap) means
        # the budget is spent: later files get no `git diff` at all.
        remaining = 0 if budget_hit else remaining - spent
        stats.append(replace(stat, patch=patch, patch_truncated=truncated))

    return _write_diff_json(diff_path(run_dir), stats)


def resolve_diff_base(sandbox: Sandbox, branch: str) -> str:
    """Returns the ref `build_structured_diff` should diff `branch` against:
    `<branch>~1` when the capture commit has a parent, otherwise the hex
    name of git's empty tree (Requirement 10.2).

    The parent is the pre-agent state of the clone. A capture commit with
    no parent happens when the Sandbox's clone was an empty repository, and
    diffing it against the empty tree reports every file the agent wrote as
    added instead of failing the Run on an unknown revision. Detection uses
    `git rev-parse --verify --quiet`, whose exit 1 means "does not resolve"
    and is therefore not an error here; see the module docstring's "The
    diff base" section.

    Raises:
        GitCaptureError: `branch` is empty or starts with `-` (checked
            before any command runs); `rev-parse` failed for another reason
            (most often: not a git repository); or `hash-object` failed or
            printed something that is not an object name.
        SandboxError: the Sandbox could not run a command at all (no
            container, or a Docker-level failure). Propagated unchanged
            from `Sandbox.exec`.
    """
    branch = _checked_ref(branch, "branch")
    parent = f"{branch}~1"

    probe = sandbox.exec(
        _git("rev-parse", "--verify", "--quiet", parent), env=_GIT_ENV
    )
    if probe.exit_code == 0:
        return parent
    if probe.exit_code != 1:
        detail = (probe.stderr or probe.stdout or "no output captured").strip()
        raise GitCaptureError(
            f"Git capture failed while resolving the parent of {branch} "
            f"(exit code {probe.exit_code}): {detail}"
        )

    empty_tree = _exec_or_raise(
        sandbox,
        _git("hash-object", "-t", "tree", "/dev/null"),
        step="computing the empty tree",
    ).stdout.strip()
    if not _OBJECT_NAME_PATTERN.match(empty_tree):
        raise GitCaptureError(
            "Git capture failed while computing the empty tree: "
            f"unexpected output {empty_tree!r}"
        )
    return empty_tree


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


def _raw_stdout(result: ExecResult) -> bytes:
    """Returns `result`'s stdout as raw bytes for the numstat parser.

    `Sandbox.exec` always fills `stdout_bytes` and `stdout` from the same
    chunk, so non-empty text always comes with non-empty bytes. A result
    built by hand (a test stand-in) may carry only `stdout`; its text is
    then re-encoded as UTF-8 rather than read as "no changes", which would
    silently drop every file (Requirement 10.2).
    """
    if result.stdout_bytes or not result.stdout:
        return result.stdout_bytes
    return result.stdout.encode("utf-8")


def _decode_path(raw: bytes) -> tuple[str, bool]:
    """Returns `(path, lossy)` for one raw numstat path.

    A strict UTF-8 decode gives `(text, False)`. Bytes that are not valid
    UTF-8 give the `errors="replace"` spelling (what `Sandbox._decode`
    would have produced) and `True`. Deciding on the bytes, not on a U+FFFD
    in the text, keeps a valid name that really contains U+FFFD scannable.
    """
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _parse_numstat(output: bytes) -> list[_NumstatRecord]:
    """Parses raw `git diff --numstat -z` output into one `_NumstatRecord`
    per changed path, preserving git's order. A record is `binary` when
    either count is `-`; its stat then reads 0/0. A record is `lossy_path`
    when its path bytes are not valid UTF-8 (see `_decode_path`).

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
    if fields and fields[-1] == b"":
        fields.pop()

    records: list[_NumstatRecord] = []
    index = 0
    while index < len(fields):
        raw_record = fields[index]
        index += 1
        # Text form of the record, only for error messages.
        record = raw_record.decode("utf-8", errors="replace")

        parts = raw_record.split(_FIELD_SEPARATOR, 2)
        if len(parts) != 3:
            raise GitCaptureError(_unparseable_message(record))
        # A non-ASCII byte in a count becomes U+FFFD and fails the
        # digits-only check in `_parse_count`.
        raw_additions = parts[0].decode("ascii", errors="replace")
        raw_deletions = parts[1].decode("ascii", errors="replace")
        raw_path = parts[2]

        if raw_path == b"":
            # Rename/copy: `<counts>TAB<counts>TAB` then old NUL new NUL.
            if index + 1 >= len(fields):
                raise GitCaptureError(_unparseable_message(record))
            raw_path = fields[index + 1]
            index += 2

        path, lossy = _decode_path(raw_path)
        records.append(
            _NumstatRecord(
                stat=FileDiffStat(
                    path=path,
                    additions=_parse_count(raw_additions, record),
                    deletions=_parse_count(raw_deletions, record),
                ),
                binary=_BINARY_COUNT_MARKER in (raw_additions, raw_deletions),
                lossy_path=lossy,
            )
        )

    return records


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


def _truncate_patch(
    text: str, max_raw_bytes: int, max_json_bytes: int
) -> tuple[str, bool, int, bool]:
    """Returns `(kept, truncated, json_cost, budget_hit)` for one file's
    patch.

    `kept` is the longest run of whole `\\n`-terminated lines from the start
    of `text` (the last line may be unterminated) whose UTF-8 size stays
    within `max_raw_bytes` and whose JSON-escaped size stays within
    `max_json_bytes`. `truncated` is True when `kept` is shorter than
    `text`. `json_cost` is what `kept` adds to the request body inside its
    already-costed `"patch": ""` slot. `budget_hit` is True when the cut was
    forced by `max_json_bytes` (the request budget) rather than by
    `max_raw_bytes` (the per-file cap).

    Cutting only between lines keeps `kept` a whole `str`, so it cannot be
    invalid UTF-8, and never hands the scanner a half line (see the module
    docstring's per-file cap for why that matters, Requirement 17.1).
    """
    pieces = text.split(_PATCH_LINE_END)
    lines = [piece + _PATCH_LINE_END for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])

    kept: list[str] = []
    raw_used = 0
    json_used = 0
    for line in lines:
        raw_cost = len(line.encode("utf-8"))
        # `json.dumps` of a string adds two quotes around the escaped text.
        json_cost = len(json.dumps(line)) - 2
        over_cap = raw_used + raw_cost > max_raw_bytes
        over_budget = json_used + json_cost > max_json_bytes
        if over_cap or over_budget:
            return "".join(kept), True, json_used, over_budget
        kept.append(line)
        raw_used += raw_cost
        json_used += json_cost
    return text, False, json_used, False


def _metadata_cost(records: list[_NumstatRecord]) -> int:
    """Returns the request-body size of `{"files": [...]}` with every text
    entry carrying `"patch": ""` and `"patch_truncated": true`, measured as
    default `json.dumps` output (the larger of the encodings the Uploader's
    httpx versions use). Pass 1 of the module docstring's total budget.

    Reuses `_entry_json`, so what is costed and what is written cannot
    drift. Binary entries are costed without patch keys, as they are
    written. An entry whose path is not valid UTF-8 is costed as written
    too: no `patch`, with `"patch_truncated": true`. Each entry adds 2 for
    the `", "` separator, an upper bound.
    """
    total = len(json.dumps({DIFF_FILES_KEY: []}))
    for record in records:
        stat = record.stat
        if record.lossy_path:
            stat = replace(stat, patch_truncated=True)
        elif not record.binary:
            stat = replace(stat, patch="", patch_truncated=True)
        total += len(json.dumps(_entry_json(stat))) + 2
    return total


def _entry_json(stat: FileDiffStat) -> dict[str, object]:
    """Returns one `diff.json` entry for `stat`: `path`, `additions`,
    `deletions`, then `patch` only when it is not None and
    `patch_truncated` only when it is True. Keys are left out rather than
    written as null or false, matching what the backend stores.
    """
    entry: dict[str, object] = {
        "path": stat.path,
        "additions": stat.additions,
        "deletions": stat.deletions,
    }
    if stat.patch is not None:
        entry[PATCH_KEY] = stat.patch
    if stat.patch_truncated:
        entry[PATCH_TRUNCATED_KEY] = True
    return entry


def _write_diff_json(path: Path, stats: list[FileDiffStat]) -> Path:
    """Writes `stats` to `path` as `{"files": [...]}` and returns `path`,
    creating `path`'s parent directories if needed.

    A whole-file write, not an append and not an atomic replace — see the
    module docstring. `indent=2` because a developer reads this file
    directly while reviewing a Run, `ensure_ascii=False` (with an explicit
    UTF-8 encoding) so a non-ASCII path stays readable rather than becoming
    escape sequences, and a trailing newline so the file is a well-formed
    text file for `cat`/`diff`/git. The on-disk form is not what the
    request budget measures; the Uploader re-serializes the parsed JSON.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {DIFF_FILES_KEY: [_entry_json(stat) for stat in stats]}
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
    result = sandbox.exec(cmd, env=_GIT_ENV)
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "no output captured").strip()
        raise GitCaptureError(
            f"Git capture failed while {step} (exit code {result.exit_code}): "
            f"{detail}"
        )
    return result
