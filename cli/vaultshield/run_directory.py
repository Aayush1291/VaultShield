r"""Run_Directory helpers and `run_id` generation.

Implements Task 8.1 — design.md's CLI package layout lists this module as
`run_directory.py  # Run_Directory helpers, run_id generation`, and gives
it no reference signatures, so every name below is this module's own
judgment call, documented inline the way `config.py`'s detection
precedence and `sandbox.py`'s build/reuse choice are.

Covers Requirement 7.1: "WHEN a developer runs `vaultshield run
--agent=mock` against a configured repository, THE VaultShield_CLI SHALL
create a Sandbox, inject the Sandbox_Credential_File, launch the
Mock_Adapter under the Monitor, and write the resulting Event_Log to the
Run_Directory." This module owns the *Run_Directory* half of that
sentence — where a Run's artifacts live and what they are called — while
Task 8.2 owns the pipeline that walks through it. Nothing here starts a
Sandbox, launches an adapter, or writes an artifact; every function is
either a pure path computation or a single `mkdir`.

### The one canonical copy of the path convention

requirements.md's Glossary defines Run_Directory as the local directory
`.vaultshield/runs/<run_id>/` "where the VaultShield_CLI stores the
Event_Log, diff, and screenshots for a Run before upload", and design.md
names the three artifacts inside it:

    <run_dir>/events.jsonl                  # Monitor          (Task 7.3)
    <run_dir>/diff.json                     # git_capture      (Task 12.2)
    <run_dir>/screenshots/<route-slug>.png  # Browser_Capture  (Task 11.2)

Five components end up naming those paths (Monitor, Browser_Capture,
git_capture, Uploader, Teardown_Command), so the convention lives here
once rather than as a string literal in each of them. `monitor.py` — the
only one of the five that exists today — already anticipated this in its
own comments ("Task 8.1 (`run_directory.py`) will own this convention
canonically") and now imports `EVENT_LOG_FILENAME` and `run_dir_path`
from here instead of keeping its own copies.

Path helpers are deliberately split by what they touch: `run_dir_path`,
`runs_root`, `event_log_path`, `diff_path`, and `screenshots_dir` are
pure and never touch the filesystem, and `create_run_dir` is the single
function that creates anything. A caller that needs the `screenshots`
subdirectory to exist creates it explicitly
(`screenshots_dir(run_dir).mkdir(parents=True, exist_ok=True)`), which is
the same one-liner `monitor.py`'s `_append_event_log` already uses for
the Event_Log's own parent — one obvious way to do it, rather than a
`create=` flag on a path helper.

The helpers take the *containing* directory as an argument (`repo_root`
for `run_dir_path`, `run_dir` for the artifact helpers) rather than
resolving `Path.cwd()` themselves: `Path.cwd()` is the CLI entry point's
business (`cli.py` already resolves it once per command), and a helper
that reads process-global state is untestable against `tmp_path`.

### `run_id` generation

Format: `<YYYYmmdd>-<HHMMSS>-<16 hex chars>`, e.g.
`20250614-091530-3f9a2b7c8d1e4f60`. Three properties drive it:

1. **Collision resistance.** The random suffix is
   `secrets.token_hex(_RANDOM_TOKEN_BYTES)` — 8 CSPRNG bytes, 64 bits of
   entropy — so two runs started in the same second on the same machine
   still get distinct ids, and the id stays unique across machines
   uploading into one backend (`Run.run_id` is `unique=True` there, per
   design.md's data model). `secrets` rather than `random` because
   `random`'s Mersenne Twister is seeded per process and is not intended
   for values that must not repeat; `uuid.uuid4().hex` would do equally
   well, but `secrets.token_hex` states the "I need unguessable, unique
   bytes" intent directly and lets the length be chosen.
2. **Filesystem AND git-ref safety.** The whole id is drawn from
   `[0-9a-f-]`: lowercase hex digits plus the two `-` separators. That is
   safe as a single path component on every platform this project runs
   on (no separator, no drive-letter colon, no Windows-reserved
   character, no leading dot, no shell metacharacter needing quoting) and
   it is a legal git ref component, which matters because Task 12.1
   embeds it in the branch name `vaultshield/run-<run_id>`:
   `git check-ref-format` rejects a component containing a space, `~`,
   `^`, `:`, `?`, `*`, `[`, `\`, an ASCII control character or `..`,
   beginning with `.`, or ending with `.lock` — none of which this
   character set can produce. Lowercase throughout (rather than, say, a
   base64 or uppercase-hex token) so that two distinct ids can never
   collide into one directory on a case-insensitive filesystem, which
   macOS uses by default.
3. **Chronological sort order.** The UTC timestamp comes first, in the
   fixed-width `strftime` basic format, so lexicographic order equals
   creation order: `ls .vaultshield/runs/` lists a project's runs oldest
   first, with no metadata read. UTC rather than local time so the
   ordering survives a timezone change or a DST transition.

Length is 32 characters, comfortably inside the backend's
`run_id = models.CharField(max_length=64, unique=True)`, which is where
`RUN_ID_MAX_LENGTH` below comes from.

### Why `run_id` is validated at all

`generate_run_id()` is the only producer of a real Run's id, so on the
`run` path validation can never fail. It exists for the *other*
direction: `vaultshield submit <run-id>` and `vaultshield teardown
<run-id>` (Tasks 29.6, 30.3) take a `run_id` straight from the command
line and this module turns it into a filesystem path. Interpolating an
unvalidated argument into a path is a directory-traversal bug waiting to
happen — `run_dir_path(repo_root, "../../../etc")` would otherwise
cheerfully hand back a path outside the repository, and a later
`teardown` deleting under it would be doing so far from where the caller
believes. So `run_dir_path` validates first and raises
`RunDirectoryError` on anything that is not a single, safe path
component.

The accepted shape (`_RUN_ID_PATTERN`) is broader than what
`generate_run_id` emits — alphanumerics plus `-`/`_`, starting with an
alphanumeric, at most `RUN_ID_MAX_LENGTH` characters — so that ids
predating or postdating the current format, and the short local ids
`cli.py`'s `_sandbox-smoke` debug command makes up, all remain usable.
The dot is excluded outright rather than being allowed and then
special-cased: no dot means no `..` traversal, no `.`-prefixed hidden
directory, no trailing dot, and no `.lock` suffix to trip git's ref
rules — one restriction in place of four exceptions.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

# Directory names making up the Run_Directory root, relative to the
# repository root: `.vaultshield/runs/`, per requirements.md's Glossary
# definition of Run_Directory (".vaultshield/runs/<run_id>/"). Kept as
# separate components rather than one "/"-joined string so `joinpath`
# builds the path with the platform's own separator.
VAULTSHIELD_DIRNAME = ".vaultshield"
RUNS_DIRNAME = "runs"

# The three artifact names inside a Run_Directory, per design.md:
# `<run_dir>/events.jsonl` (Monitor, Task 7.3), `<run_dir>/diff.json`
# (git_capture, Task 12.2), and `<run_dir>/screenshots/<route-slug>.png`
# (Browser_Capture, Task 11.2). Public, because the Uploader (Task 18.2)
# reads all three back out of a Run_Directory that some other component
# wrote. The `<route-slug>` half of the screenshot filename is NOT
# defined here: turning a configured route into a filename stem is
# Browser_Capture's own concern (it owns `config.routes`), and this
# module only fixes the directory those files go in.
EVENT_LOG_FILENAME = "events.jsonl"
DIFF_FILENAME = "diff.json"
SCREENSHOTS_DIRNAME = "screenshots"

# Two more artifacts, written by step 8's git capture alongside `diff.json`
# so that `vaultshield submit` can push the Run's branch from a checkout that
# never saw the (now destroyed) capture helper:
#
#   <run_dir>/branch.bundle   a `git bundle` of `<base>..<branch>`, carrying
#                             exactly the Run's commit and its objects.
#   <run_dir>/branch.json     the metadata `submit` needs to import that
#                             bundle safely: the run id, the branch name, the
#                             base and tip commit ids, the repository URL the
#                             Run was taken from, and the bundle's SHA-256 and
#                             byte length. It holds identifiers only — never a
#                             credential value or any file content.
#
# Public for the same reason the three names above are: step 8 (git_capture /
# trusted_capture) writes them and `submit`/`teardown` read them back out of a
# Run_Directory one side did not create. Both are optional — a Run whose
# branch adds no commit beyond its base writes neither (see
# `trusted_capture.export_run_bundle`), so a reader must treat their absence
# as "this Run has no importable branch", not as an error.
BRANCH_BUNDLE_FILENAME = "branch.bundle"
BRANCH_JSON_FILENAME = "branch.json"

# Maximum accepted `run_id` length, matching design.md's backend data
# model (`run_id = models.CharField(max_length=64, unique=True)`). A
# longer id would be accepted locally and then silently truncated or
# rejected at upload time (Task 18.2), so the CLI refuses it up front.
RUN_ID_MAX_LENGTH = 64

# A `run_id` must be exactly one safe path component: an alphanumeric
# first character followed by alphanumerics, `-`, or `_`. See the module
# docstring for why this is broader than `generate_run_id`'s own output
# and why `.` is excluded entirely.
_RUN_ID_PATTERN = re.compile(rf"\A[A-Za-z0-9][A-Za-z0-9_-]{{0,{RUN_ID_MAX_LENGTH - 1}}}\Z")

# Bytes of CSPRNG randomness in a generated `run_id`'s suffix. 8 bytes =
# 64 bits = 16 hex characters; see the module docstring for the
# collision-resistance reasoning.
_RANDOM_TOKEN_BYTES = 8

# Timestamp prefix format for a generated `run_id`: fixed-width UTC date
# and time in the basic (separator-free) form, joined by `-`, so
# lexicographic order equals chronological order.
_RUN_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


class RunDirectoryError(Exception):
    """Raised when a `run_id` cannot be turned into a Run_Directory path.

    Today that means exactly one thing: the `run_id` is not a single safe
    path component (see `_RUN_ID_PATTERN` and the module docstring's
    validation section). Every message quotes the offending value so a
    `vaultshield submit`/`teardown` caller can see what was rejected.

    A distinct exception type per module, matching `config.ConfigError`
    and `sandbox.SandboxError`: `cli.py` maps each one to a
    `click.ClickException` at its own command boundary, so a bad run id
    reaches the developer as a one-line message rather than a traceback.
    """


def generate_run_id() -> str:
    """Returns a fresh `run_id` for one Run.

    Format is `<YYYYmmdd>-<HHMMSS>-<16 hex chars>` in UTC, e.g.
    `20250614-091530-3f9a2b7c8d1e4f60` — see the module docstring for why
    that shape (collision resistance, filesystem/git-ref safety,
    chronological sort order) and how long it is relative to the
    backend's 64-character column.

    The returned value always satisfies `is_valid_run_id`, so a caller
    that generates an id never needs to validate it. Calling this twice
    within the same second still yields two different ids: the random
    suffix, not the timestamp, is what makes an id unique.
    """
    timestamp = datetime.now(timezone.utc).strftime(_RUN_ID_TIMESTAMP_FORMAT)
    return f"{timestamp}-{secrets.token_hex(_RANDOM_TOKEN_BYTES)}"


def is_valid_run_id(run_id: str) -> bool:
    """Returns whether `run_id` is usable as a Run_Directory name.

    True only for a single safe path component: an alphanumeric first
    character followed by up to `RUN_ID_MAX_LENGTH - 1` alphanumerics,
    hyphens, or underscores. That rejects the empty string, anything
    containing a path separator or a dot (so `..` and `../..` traversal
    are impossible), leading punctuation, whitespace and control
    characters, and anything longer than the backend's column.

    Non-`str` input returns False rather than raising: a `run_id` read
    from a command line, a config file, or a backend response is untyped
    at runtime, and "is this usable?" has a sensible answer for a value
    of the wrong type too.
    """
    if not isinstance(run_id, str):
        return False
    return _RUN_ID_PATTERN.match(run_id) is not None


def validate_run_id(run_id: str) -> str:
    """Returns `run_id` unchanged if `is_valid_run_id(run_id)`, else
    raises `RunDirectoryError` naming the rejected value.

    Returns rather than just asserting so it can wrap a value inline
    (`Sandbox(validate_run_id(raw), client)`); this is also the guard
    `run_dir_path` applies before building any path, so most callers get
    the check for free rather than calling it themselves.
    """
    if not is_valid_run_id(run_id):
        raise RunDirectoryError(
            f"Invalid run_id {run_id!r}: expected 1-{RUN_ID_MAX_LENGTH} characters "
            "starting with a letter or digit and containing only letters, digits, "
            "'-', or '_'"
        )
    return run_id


def runs_root(repo_root: Path) -> Path:
    """Returns `<repo_root>/.vaultshield/runs`, the directory holding every
    Run_Directory for this repository. Pure: touches no filesystem.
    """
    return repo_root / VAULTSHIELD_DIRNAME / RUNS_DIRNAME


def run_dir_path(repo_root: Path, run_id: str) -> Path:
    """Returns the Run_Directory path for `run_id`:
    `<repo_root>/.vaultshield/runs/<run_id>`, per requirements.md's
    Glossary. Pure: computes a path, creates nothing (use
    `create_run_dir` for that).

    Raises `RunDirectoryError` if `run_id` is not a single safe path
    component — this is the traversal guard described in the module
    docstring, and it is why `submit`/`teardown` can pass a command-line
    argument straight in.
    """
    return runs_root(repo_root) / validate_run_id(run_id)


def create_run_dir(repo_root: Path, run_id: str) -> Path:
    """Creates the Run_Directory for `run_id` (and any missing parent,
    including `.vaultshield/runs` itself) and returns its path.

    Idempotent — an existing directory is left exactly as it is, and its
    contents are never removed. Two reasons: a Run_Directory outlives the
    `run` that created it (`submit`, `teardown`, and a retried upload all
    revisit it), and Requirement 14.4 requires a failed upload to
    *preserve* the Run_Directory's contents, so nothing in this module
    may ever be a way to lose them. Only the *directory* is created; the
    `screenshots` subdirectory is created by whoever writes into it, per
    the module docstring.

    Collision is prevented by `generate_run_id`'s 64 bits of randomness,
    not by this function: `exist_ok=True` means a caller reusing an id
    adds to that Run's directory rather than being told about it, which
    is the right behavior for every revisiting caller above.
    """
    path = run_dir_path(repo_root, run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def event_log_path(run_dir: Path) -> Path:
    """Returns `<run_dir>/events.jsonl`, the Monitor's Event_Log for this
    Run (design.md's Monitor section; written by `Monitor.supervise`,
    Task 7.3, and read back by the Uploader, Task 18.2). Pure.
    """
    return run_dir / EVENT_LOG_FILENAME


def diff_path(run_dir: Path) -> Path:
    """Returns `<run_dir>/diff.json`, the structured diff for this Run
    (design.md's `build_structured_diff`, Task 12.2). Pure.
    """
    return run_dir / DIFF_FILENAME


def screenshots_dir(run_dir: Path) -> Path:
    """Returns `<run_dir>/screenshots`, the directory Browser_Capture
    saves one PNG per configured route into (design.md's
    `start_and_capture`, Task 11.2). Pure — the caller creates it when it
    has something to write, per the module docstring.
    """
    return run_dir / SCREENSHOTS_DIRNAME


def branch_bundle_path(run_dir: Path) -> Path:
    """Returns `<run_dir>/branch.bundle`, the `git bundle` of the Run's
    branch that `submit` imports before pushing (written by step 8's
    `trusted_capture.export_run_bundle`). Pure; may not exist (a Run with no
    commit beyond its base writes no bundle).
    """
    return run_dir / BRANCH_BUNDLE_FILENAME


def branch_json_path(run_dir: Path) -> Path:
    """Returns `<run_dir>/branch.json`, the identifiers `submit` and
    `teardown` read to import and later clean up the Run's branch (written
    beside `branch.bundle`). Pure; may not exist (see `branch_bundle_path`).
    """
    return run_dir / BRANCH_JSON_FILENAME
