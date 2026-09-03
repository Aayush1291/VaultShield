"""Monitor: wraps an Agent_Adapter's process in a PTY and watches the
Sandbox working directory for file changes, merging both into an
append-only Event_Log.

THIS FILE implements Task 7.1 (the PTY-wrapper thread), Task 7.2 (the
inotify-watcher thread), Task 7.3 (`Monitor.supervise`'s merge step plus
the JSONL Event_Log writer) AND Task 7.7 (`off_limits_paths` runtime
enforcement, layered onto the inotify watcher of Task 7.2) described in
design.md's "Monitor (`monitor.py`)" section. Nothing from design.md's
Monitor section is deferred past this point.

Covers Requirement 6.1: "WHEN the Monitor supervises an Agent_Adapter
process, THE Monitor SHALL wrap the process in a PTY and SHALL capture the
process's terminal output with a timestamp for each captured segment."
And Requirement 6.2: "WHILE an Agent_Adapter process runs inside the
Monitor, THE Monitor SHALL watch the Sandbox working directory using
inotify and SHALL record a timestamped event for each detected file
change."
And Requirement 6.3: "THE Monitor SHALL merge captured terminal-output
events and captured file-change events into a single append-only
Event_Log ordered by timestamp."
And Requirement 6.4: "THE VaultShield_CLI SHALL capture agent actions
exclusively through the Monitor's PTY wrapper and inotify watcher,
without relying on shell hooks or agent self-reported logs." -- satisfied
structurally rather than by any code of its own: `supervise` below reads
from exactly one buffer, and the only two things that ever write into
that buffer are `PtyOutputCapture` and `FileChangeWatcher`. There is no
third ingest path (no shell hook installation, no reading of an
agent-written log file, no trusting of `AgentEvent`s as records in their
own right -- a yielded event's payload only ever reaches the Event_Log by
being written to the pty and read back out of it).
And Requirement 4.5: "IF the Monitor detects a file-change event, via
inotify, under a path matching an `off_limits_paths` entry from the
Project_Config_File, THEN THE Monitor SHALL mark the Run as failed and
SHALL record the matched path as a violation in the Event_Log."

design.md states `supervise` "maintains two independent event producers
(PTY reader thread, inotify watcher thread) feeding a single thread-safe
buffer; the merge step sorts strictly by timestamp before writing". Both
producers construct the one shared record shape (`EventLogRecord`, below)
into the same `queue.Queue`: `PtyOutputCapture` only ever constructs
`event_type="output"` records, `FileChangeWatcher` only ever constructs
`event_type="file_change"` and `event_type="violation"` ones.
`event_type`'s literal values intentionally match the field name and
value set of design.md's `RunEvent.event_type` Django field (`backend`'s
eventual ingest target), so the same vocabulary is used end to end from
this in-memory record, through the on-disk JSONL Event_Log (Task 7.3), to
the stored backend record (Task 14.2) -- rather than inventing a separate
name (e.g. `kind`) at this layer alone. "violation" is a third value
added by Task 7.7 beyond the two design.md's inline comment on that field
names; see the `off_limits_paths` section below for why that is a
schema-compatible extension rather than a divergence.

### The PTY-wrapper mechanism (a judgment call, like sandbox.py's
### build/reuse choice and config.py's detection precedence)

design.md specifies THAT the Monitor wraps the adapter process in a PTY,
but not exactly how -- and the Antigravity_Adapter's own design note adds
a hard constraint: a REAL subprocess spawned later inside a concrete
Agent_Adapter's `launch()` (via plain `subprocess.Popen(argv)`, with no
stdout/stderr redirection of its own) must inherit a working pseudo-
terminal as its stdio, so that its own `isatty()` check passes, WITHOUT
that adapter allocating its own pty. Since a process's file descriptor
table is process-wide, not per-thread, the mechanism implemented here is:

1. Open a pty pair with `pty.openpty()`.
2. Save the process's real fd 0/1/2 via `os.dup()`.
3. `os.dup2()` the pty slave onto fd 0/1/2, so ANY code running in this
   process for the duration of the swap -- including a real subprocess
   started by `Popen` with no redirection args of its own -- inherits the
   pty as its stdio.
4. Drive the adapter's `launch(prompt, workdir)` iterator to completion.
   `Mock_Adapter` (explicitly documented in `adapters/mock.py` as "the
   Monitor's first integration target") never touches fd 1 itself, so
   each yielded `AgentEvent`'s `payload` is written to fd 1 directly here
   -- this is what makes a pure-Python adapter still produce real bytes
   flowing through the pty. This writes the payload of EVERY yielded
   `AgentEvent`, regardless of its own `type` ("output"/"final_message"/
   "error"): at the Event_Log layer, Requirement 6.1 only asks for
   captured terminal output with a timestamp per segment, not for
   preserving the AgentEvent-level type tag, so all of it collapses into
   `EventLogRecord(event_type="output", ...)` records here. (A concrete
   adapter that instead spawns a real subprocess and independently
   `yield`s `AgentEvent`s describing that subprocess's own output would
   see that output captured twice -- once directly via the subprocess's
   inherited pty stdio, once via this re-write of its payload. That's a
   real limitation of this mechanism, deliberately accepted for now since
   no adapter that spawns a real subprocess exists yet; it is for that
   future adapter's own task to avoid double-emission if it matters.)
5. Restore the saved fd 0/1/2 (closing the saved dups afterward) and close
   the pty slave, in a `finally` block -- so the original stdio is always
   restored even if the adapter's iterator raises.

A dedicated background thread (`_read_pty_output`, run as its own
`threading.Thread`) concurrently reads the pty master fd in a loop via
`os.read()`, so a real subprocess's writes are drained as they arrive
rather than risking a full pty buffer stalling that subprocess while step
4 above is still busy iterating. Each `os.read()` call's return value is
treated as one chunk, timestamped (ISO-8601, matching the `AgentEvent`
timestamp format used in `adapters/base.py`/`adapters/mock.py`) and
pushed onto the shared `queue.Queue` as one `EventLogRecord`. The reader
thread terminates on its own once every fd referencing the pty slave's
open file description is closed (step 5 above): `os.read` then either
raises `OSError` (commonly seen on Linux) or returns `b""` (commonly seen
on macOS/BSD) -- both are treated as ordinary end-of-stream.

`PtyOutputCapture` (the class implementing all of the above) is itself a
`threading.Thread` subclass -- this IS "the PTY-wrapper thread" the task
title refers to; it internally starts the separate pty-reader thread
described above as its own implementation detail, matching design.md's
"two independent event producers" framing one level up (Monitor's future
`supervise`, Task 7.3, will run one `PtyOutputCapture` alongside one
inotify-watcher thread, both feeding the same `queue.Queue`).

An `AdapterError` raised by the adapter's own iterator (either an
invocation-construction failure, raised before any output is produced, or
a post-execution validation failure, raised after -- see
`adapters/base.py`'s `Agent_Adapter`/`AdapterError` docstrings for the
full contract) is never silently swallowed. Since Python does not
propagate an exception raised inside `Thread.run()` to whatever later
calls `Thread.join()`, `PtyOutputCapture` captures it into a public
`self.error` attribute instead (set once, immediately before `run()`
returns, always after fd/thread cleanup has already happened). A caller
-- today, this task's own tests; from Task 7.3 onward, `supervise` -- is
expected to `join()` the thread and then check `.error`, re-raising it if
present. This is the "store it for the caller to check" option design.md
explicitly allows as an alternative to a `join()`-style re-raise, chosen
here to keep this task's surface area minimal (no new join-wrapping
method) while still making the failure impossible to miss.

### The inotify-watcher mechanism (Task 7.2)

design.md names `watchdog`/`inotify_simple` as the filesystem-watching
library option, but a project-level judgment call is needed to pick one:
`inotify_simple` is a literal ctypes wrapper around Linux's raw inotify
syscalls and is Linux-only by construction, while `watchdog` wraps
inotify on Linux, FSEvents on macOS, and ReadDirectoryChangesW on
Windows behind one cross-platform API. This project's dev machine is
macOS, and the test suite (this task's own tests included) must run
there, so `watchdog` is the library actually added as a dependency
(`pyproject.toml`) and used below -- `inotify_simple` would make this
task's own tests impossible to run locally. The task title's "inotify"
wording tracks design.md/requirements.md's own vocabulary for "the
Monitor's filesystem-watching producer" in the abstract, not a hard
requirement that the raw Linux syscall be the implementation mechanism;
Requirement 6.2's actual text only requires watching the directory and
recording a timestamped event per detected change, which `watchdog`
satisfies identically to a raw inotify wrapper from every caller's
perspective, on every platform this project runs on.

`FileChangeWatcher` (the class implementing this task) mirrors
`PtyOutputCapture`'s shape deliberately:

- It is a `threading.Thread` subclass, daemon=True, constructed with the
  same shared `queue.Queue[EventLogRecord]` (accepted as a parameter, not
  owned internally) that `PtyOutputCapture` also pushes onto -- so a
  future `supervise` (Task 7.3) can create one `queue.Queue` and hand it
  to both producers, exactly as design.md's "two independent event
  producers ... feeding a single thread-safe buffer" describes.
- It only ever constructs `EventLogRecord(event_type="file_change", ...)`
  instances, mirroring how `PtyOutputCapture` only ever constructs
  `event_type="output"` instances.
- A failure that prevents watching from starting at all (most plausibly:
  `workdir` does not exist, or is not a directory) is captured into a
  public `self.error` attribute rather than raised out of `run()`, for
  the same reason `PtyOutputCapture.error` exists: Python does not
  propagate a `Thread.run()` exception to a later `.join()` call, so a
  joining caller needs an attribute to check instead. Unlike
  `PtyOutputCapture`, there is no `AdapterError` in play here -- this
  thread never touches an `Agent_Adapter` -- so `self.error` is typed
  `OSError | None`, matching the exception `Path.exists()`-adjacent
  filesystem calls actually raise.

Unlike the PTY reader, a filesystem watch has no natural end-of-stream:
an adapter's `launch()` iterator eventually stops yielding, which is what
lets `PtyOutputCapture` know it is done, but a directory can go on
existing (and being watched) indefinitely. `FileChangeWatcher` therefore
needs an explicit stop signal -- a `threading.Event` checked by the watch
loop, exposed as a public `stop()` method -- so a future `supervise`
(Task 7.3) can stop this thread once `PtyOutputCapture`'s `run()` (i.e.
the adapter's own process) has finished, the same way one would close a
subprocess's stdout pipe to stop reading it.

Concretely, `FileChangeWatcher` wraps a `watchdog.observers.Observer`
(itself already a `threading.Thread` subclass) rather than reimplementing
directory polling: `Observer` already solves "run a background thread
that watches a directory and calls back into handler methods per event",
which is exactly what this task needs, and reimplementing that on top of
raw inotify/FSEvents calls would only reproduce `watchdog`'s own internals
under a different name. `FileChangeWatcher.run()` starts the `Observer`,
waits on the stop `threading.Event`, and stops the `Observer` once that
event is set -- so `FileChangeWatcher` itself is the "one thread" a caller
starts/stops/joins, per this task's own requirement to mirror
`PtyOutputCapture`'s shape, while `Observer` remains an internal
implementation detail the same way `PtyOutputCapture`'s own internal
`_read_pty_output` reader thread is.

A `watchdog.events.FileSystemEventHandler` subclass (`_FileChangeHandler`,
module-private) translates `watchdog`'s three relevant callbacks
(`on_created`/`on_modified`/`on_deleted`) into `EventLogRecord` pushes.
`watchdog` also offers `on_moved` and, on some platforms/backends,
`on_closed`/`on_opened` -- none of those are "create/modify/delete" per
Requirement 6.2's own text, so none are handled here; a rename inside the
watched directory is consequently not captured as its own event by this
task (a real gap, but one Requirement 6.2 does not ask this task to
close, and no property or design note calls out `on_moved` specifically).
Directory-level events (`event.is_directory`) are filtered out: Property
10 and Requirement 6.2 both talk about "file" create/modify/delete, and a
directory being created as a side effect of a nested file's own creation
would otherwise double up the "one event per operation" guarantee
Property 10 asks for with an extra, redundant directory-level event.

### The merge step and JSONL Event_Log writer (Task 7.3)

`Monitor.supervise` is the one public entry point that runs both
producers above and turns their output into the on-disk Event_Log:

1. Create ONE `queue.Queue[EventLogRecord]` -- design.md's "single
   thread-safe buffer" -- and hand the same instance to both producers,
   which is exactly what `_start_pty_capture`/`_start_inotify_watcher`
   already accept it as a parameter for.
2. Start the inotify watcher FIRST and wait (via
   `FileChangeWatcher.wait_until_watching`) until its watch is actually
   established, only then start the PTY capture. Requirement 6.2 asks for
   file changes to be watched *while* the adapter process runs, so the
   watch must already be live before the adapter can touch the
   filesystem; merely starting the watcher thread first is not enough,
   since it schedules its watch asynchronously and the adapter's earliest
   writes would race that setup. If the watch could not be established at
   all, `supervise` raises before the adapter is ever launched -- running
   an agent nobody is watching would violate Requirement 6.4's
   capture-exclusively-through-these-two-producers guarantee, and there is
   nothing captured to write an Event_Log from at that point anyway.
3. `join()` the PTY capture with NO timeout. That thread finishes exactly
   when the adapter's own event stream is exhausted (or it fails), and a
   real agent's runtime is unbounded by design -- minutes of `codex exec`
   is ordinary -- so any timeout picked here would be a wrong answer to a
   question nobody asked. A hung adapter hangs `supervise`, deliberately.
4. Wait `_WATCHER_SETTLE_DELAY` before stopping the watcher. Filesystem
   notifications are delivered asynchronously relative to the operation
   that triggered them, so the adapter's final writes can still be in
   flight at the instant its event stream ends; stopping the watcher
   immediately would drop them and break the "no event from either source
   is dropped" guarantee design.md states for this merge.
5. `stop()` and `join()` the watcher, so that by the time the buffer is
   drained BOTH producers have terminated and nothing can still be
   pushing into it. This is what makes the drain in `_merge_events` a
   complete snapshot rather than a race.
6. Drain the buffer and sort it strictly by timestamp (`_merge_events`),
   then append every record to `<run_dir>/events.jsonl`
   (`_append_event_log`), and return that path.

The sort is Python's ordinary stable `sorted`, keyed on the record's
parsed timestamp rather than on its raw string. Parsing rather than
comparing strings keeps the ordering correct regardless of UTC-offset
spelling, and stability is load-bearing, not incidental: two records can
carry the identical timestamp (the clock's resolution is finite), and
Property 9 requires captured output chunks to stay in the relative order
they were emitted. `queue.Queue` is FIFO, so drain order already equals
per-producer enqueue order, and a stable sort preserves that for equal
keys instead of scrambling it.

The writer opens the Event_Log in APPEND mode ("a"), never truncating and
never rewriting an existing line -- "append-only" per Requirement 6.3.
This is a deliberate departure from `credentials.py`'s atomic
write-temp-then-`os.replace` convention: that pattern exists there to
stop a reader ever seeing a half-written `.env.sandbox`, and it works by
REPLACING the destination, which is precisely what an append-only log
must never do. A partially written trailing JSONL line is also a far
smaller problem than a replaced log: every complete line before it stays
valid and parseable on its own, which is the property JSONL is chosen for
in the first place.

Errors from either producer -- and the `off_limits_paths` violation
described in the next section -- are re-raised only AFTER the Event_Log
has been written. `PtyOutputCapture.error` (an `AdapterError`, e.g. the
Antigravity/Kiro clean-exit-but-empty-output check) means the Run failed,
but everything captured up to that point is exactly the evidence a
reviewer needs to see why -- discarding it by raising first would be a
regression against Requirement 6.3's own purpose. `FileChangeWatcher.error`
(an `OSError`, i.e. the watch never started) is instead raised up front,
per step 2 above, so it never reaches this point in the normal case -- a
Monitor that silently captured no file changes at all would violate
Requirement 6.2 while looking like a clean success on disk. The PTY error
takes precedence when both are set, being the adapter's own failure rather
than an infrastructure one. Note that the watcher-error branch is
effectively Linux-only in practice: `watchdog`'s inotify backend
surfaces a bad watch path as an `OSError` (ENOENT/ENOTDIR), while its
macOS FSEvents backend accepts both a nonexistent path and a plain file
without complaint, so on a dev machine `FileChangeWatcher.error` simply
stays `None`. The branch is kept because production Sandboxes are Linux
containers, where it does fire.

### `off_limits_paths` enforcement (Task 7.7 / Requirement 4.5)

design.md is explicit about where this lives: "On each inotify
file-change event, the same watcher thread checks the changed path
against `config.off_limits_paths` before enqueuing the event ... this
check lives entirely inside the existing inotify watcher, with no
separate component or process." So there is no new thread, class
hierarchy or scanning pass here -- `_FileChangeHandler._record`, already
called once per detected change on the watcher's own emitter thread,
gained a single match step, and `FileChangeWatcher` gained one public
attribute to surface the verdict.

Three things need deciding, none of them spelled out by design.md:

1. **What counts as a match.** Entries are interpreted as paths relative
   to the repository root, matched against the changed path in two ways
   (see `_match_off_limits`): containment (the changed path IS the entry, or
   sits under it as a directory prefix -- Requirement 4.5's own word is
   "under") and `fnmatch` globbing (so `*.pem` or `infra/**` style
   entries a developer would naturally write in `.vaultshield.yml` also
   work). Both checks run against the changed path in relative AND
   absolute form, so an absolute entry matches too. This is deliberately
   generous: a false positive fails one Run and is visible in the
   Event_Log, whereas a false negative silently lets an agent modify a
   path the developer declared off limits, which is the exact failure
   Requirement 4.5 exists to prevent.

2. **How the violation is recorded.** As its OWN record --
   `EventLogRecord(event_type="violation", ...)` -- pushed onto the same
   shared buffer, "in addition to the ordinary file-change event" per
   design.md, and carrying both the changed path (Requirement 4.5's
   "matched path") and the `off_limits_paths` entry it matched. A
   distinct `event_type` is what makes the violation findable by a
   reviewer or by the Security tab without re-deriving the match from a
   payload string. design.md's inline comment on `RunEvent.event_type`
   lists only "output" | "file_change", but that field is a plain
   `CharField(max_length=32)` with no `choices` -- unlike `Run.status`,
   which design.md constrains explicitly and Task 14.2 enforces -- so a
   third short value is a schema-compatible extension the ingest path
   accepts as-is, with no migration and no translation in the Uploader.
   Both records share ONE timestamp and are enqueued ordinary-first, so
   the merge's stable sort keeps them adjacent and in that order.

3. **What "mark the Run as failed" means here.** There is no Run object
   in the CLI yet (the backend's is Task 14.2, and the pipeline that
   would own one is Task 8.2), so the mechanism follows this module's
   existing precedent for a verdict reached on a producer thread:
   `FileChangeWatcher.violation` is a public attribute holding an
   `OffLimitsViolation` (first violation wins), exactly like
   `.error` -- Python does not deliver a `Thread.run()` exception to a
   later `join()`, and the emitter thread has nobody to raise at anyway.
   `supervise` then raises that `OffLimitsViolation` after writing the
   Event_Log, so a Run whose agent touched an off-limits path can only
   ever end in a failure at its call site, never in a silent success.
   `PtyOutputCapture.error` still takes precedence when both are set:
   the violation is already recorded durably in the Event_Log, so
   shadowing it loses nothing, whereas an `AdapterError`'s message
   exists only as that in-memory exception.

Two deliberate non-behaviors: the agent is NOT interrupted mid-run (the
Monitor holds no process handle at this layer, and Requirement 4.5 asks
for a verdict plus a record, not for termination -- reclaiming the
Sandbox is Requirement 4.3's teardown), and every subsequent violation
after the first is still recorded in the Event_Log even though only the
first is kept in `.violation`, which only decides which one the raised
exception names.
"""

from __future__ import annotations

import fnmatch
import json
import os
import pty
import queue
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from vaultshield.adapters.base import AdapterError, Agent_Adapter
from vaultshield.run_directory import EVENT_LOG_FILENAME, event_log_path, runs_root

# Each `os.read()` call against the pty master fd is treated as exactly
# one captured chunk/segment (Requirement 6.1: "a timestamp for each
# captured segment") -- this is simply the OS's own read-buffering
# granularity, never re-split or re-coalesced across calls. The size
# itself only bounds how much of a single already-arrived burst is read
# in one call; it does not affect correctness.
_READ_CHUNK_SIZE = 4096

# Safety-net timeout for joining the pty-reader thread once every fd
# referencing the pty slave has been closed. Under normal operation the
# reader observes EOF and exits almost immediately once that happens;
# this bound only guards against an unexpected platform quirk turning
# that into an indefinite hang.
_READER_JOIN_TIMEOUT = 5.0

# Safety-net timeout for joining the internal watchdog `Observer` once
# `FileChangeWatcher.stop()` has been called. `Observer.stop()` signals
# its own internal thread to exit; under normal operation that happens
# almost immediately, so this bound only guards against an unexpected
# platform-backend quirk turning that into an indefinite hang -- the
# same role `_READER_JOIN_TIMEOUT` plays for the PTY reader thread.
_OBSERVER_JOIN_TIMEOUT = 5.0

# Safety-net timeout for `supervise` joining the `FileChangeWatcher`
# thread itself after calling `stop()`. Strictly larger than
# `_OBSERVER_JOIN_TIMEOUT`, since that thread's own shutdown path already
# spends up to `_OBSERVER_JOIN_TIMEOUT` joining its internal `Observer`;
# a smaller value here would give up on a watcher that is still shutting
# down perfectly normally.
_WATCHER_JOIN_TIMEOUT = 10.0

# How long `supervise` waits for the file-change watcher to establish its
# watch before launching the adapter. Establishing a watch is a local
# syscall away, so this is a safety net against an unresponsive platform
# backend, not an expected wait; on timeout `supervise` proceeds anyway
# rather than failing a Run over a slow watcher startup.
_WATCHER_STARTUP_TIMEOUT = 5.0

# How long `supervise` waits between the adapter's event stream ending
# and stopping the file-change watcher, so filesystem notifications
# triggered by the adapter's final writes -- which are delivered
# asynchronously, after the write itself returns -- still land in the
# shared buffer. Measured delivery latency on this project's platforms is
# well under 100ms, so 0.5s is a wide margin; it is a fixed wait rather
# than a poll-until-quiet loop because there is no way to know how many
# in-flight notifications to expect, only how long they plausibly take.
_WATCHER_SETTLE_DELAY = 0.5

# NOTE: `EVENT_LOG_FILENAME` ("events.jsonl", per design.md's
# "<run_dir>/events.jsonl") and the `.vaultshield/runs/<run_id>/`
# Run_Directory layout are NOT defined here -- they are imported above
# from `run_directory` (Task 8.1), which owns that convention canonically
# for the whole CLI, so there is no second copy in this module to drift
# from it. Both names stay importable as
# `vaultshield.monitor.EVENT_LOG_FILENAME` / `runs_root` by virtue of
# that import, which is how this module's existing callers reference the
# Event_Log's filename.


@dataclass(frozen=True)
class EventLogRecord:
    """One timestamped record destined for the merged, on-disk Event_Log
    (Task 7.3 writes these out as JSONL). `PtyOutputCapture` (Task 7.1)
    only ever constructs `event_type="output"` instances;
    `FileChangeWatcher` (Task 7.2) only ever constructs
    `event_type="file_change"` instances, plus `event_type="violation"`
    ones for an `off_limits_paths` match (Task 7.7) -- see the module
    docstring for why all of them share this one record type rather than
    each getting their own.

    Mirrors `AgentEvent` (`adapters/base.py`) in shape -- `timestamp`/
    `payload` are the same two fields with the same meaning -- but is a
    distinct type: `AgentEvent.type` has three adapter-level variants
    ("output"/"final_message"/"error"), while `event_type` here carries
    the Event_Log-level vocabulary of design.md's `RunEvent` Django model
    ("output"/"file_change", plus "violation" per the module docstring's
    `off_limits_paths` section). Conflating the two would leak an
    adapter-internal distinction into the Event_Log schema that no
    requirement or property asks for.
    """

    event_type: Literal["output", "file_change", "violation"]
    timestamp: str  # ISO-8601
    payload: str


class OffLimitsViolation(Exception):
    """Raised by `Monitor.supervise` when the inotify watcher detected a
    file change under a path matching an `off_limits_paths` entry from the
    Project_Config_File (Task 7.7 / Requirement 4.5).

    This exception IS the "mark the Run as failed" mechanism at the CLI
    layer: there is no Run object here yet, so the Run's failure is
    expressed the only way a `supervise` call can express one -- by not
    returning normally. Task 8.2's `run` pipeline catches it to report the
    failed Run (and, once Task 18.2's Uploader exists, to upload the
    Event_Log that already contains the matching "violation" record).
    Raised only AFTER the Event_Log has been written, per the module
    docstring, so the evidence always outlives the failure.

    `path` is the changed path that matched (Requirement 4.5's "matched
    path"), `pattern` the `off_limits_paths` entry it matched. Both are
    kept as attributes, not just interpolated into the message, so a
    caller can act on them without re-parsing prose.
    """

    def __init__(self, path: str, pattern: str) -> None:
        super().__init__(
            f"off_limits_paths violation: {path} "
            f"(matched off_limits_paths entry {pattern!r})"
        )
        self.path = path
        self.pattern = pattern


class Monitor:
    """Supervises one Run's Agent_Adapter process and its working
    directory (Task 7.1: PTY capture; Task 7.2: inotify/file-change
    capture; Task 7.3: the merge into an on-disk Event_Log; Task 7.7:
    `off_limits_paths` enforcement inside that same inotify watcher).

    `workdir` and `run_id` are stored exactly as design.md's reference
    `__init__(self, workdir: Path, run_id: str)` declares; neither is
    otherwise transformed or validated here, matching how `Sandbox.__init__`
    (`sandbox.py`) stores its own constructor arguments as-is.

    `off_limits_paths` is Task 7.7's addition and is keyword-only with a
    default, so design.md's two-positional-argument reference signature
    keeps working unchanged for every existing caller -- the same
    compatibility approach `supervise`'s own `run_dir` override already
    uses. Callers that have loaded a `ProjectConfig` (Task 8.2's `run`
    pipeline) pass `config.off_limits_paths` straight through; the
    Monitor takes the list rather than the whole `ProjectConfig` because
    that one field is the only part of it any of these threads reads, and
    a narrower dependency keeps `monitor.py` importable without
    `config.py`. Stored as a tuple so a later mutation of the caller's
    list cannot retroactively change what this Monitor enforces; entry
    normalization and matching belong to `FileChangeWatcher`, which is
    where design.md puts the check.
    """

    def __init__(
        self,
        workdir: Path,
        run_id: str,
        *,
        off_limits_paths: Iterable[str] | None = None,
    ) -> None:
        self.workdir = workdir
        self.run_id = run_id
        self.off_limits_paths: tuple[str, ...] = tuple(off_limits_paths or ())

    def supervise(
        self,
        adapter: Agent_Adapter,
        prompt: str,
        *,
        run_dir: Path | None = None,
    ) -> Path:
        """Runs `adapter.launch(prompt, self.workdir)` under the PTY
        wrapper while watching `self.workdir` for file changes, merges
        both producers' events into one timestamp-sorted sequence, appends
        that sequence to `<run_dir>/events.jsonl`, and returns the
        Event_Log path (Requirements 6.3, 6.4).

        See the module docstring's "merge step and JSONL Event_Log writer"
        section for the full sequence and the reasoning behind each step.

        Blocks for as long as the adapter runs, plus
        `_WATCHER_SETTLE_DELAY`. Raises the adapter's own `AdapterError`,
        or an `OffLimitsViolation` if any detected file change matched an
        entry of `self.off_limits_paths` (Requirement 4.5's "mark the Run
        as failed"), only after the Event_Log has already been written -- so
        a failed Run still leaves behind the evidence of how it failed,
        the violation record among it. An `OSError` that prevented the
        file-change watch from being established is instead raised
        earlier, before the adapter is launched at all.

        `run_dir` defaults to `_default_run_dir(self.workdir, self.run_id)`
        and exists as a keyword-only override for Task 8.2's `run`
        pipeline, which resolves the canonical Run_Directory itself via
        Task 8.1's `run_directory.py` and should not have to agree with a
        second copy of that path convention living here. design.md's
        reference signature (`supervise(self, adapter, prompt) -> Path`)
        is unchanged for every positional caller.
        """
        resolved_run_dir = (
            run_dir if run_dir is not None else _default_run_dir(self.workdir, self.run_id)
        )

        # ONE buffer, both producers -- design.md's "two independent event
        # producers ... feeding a single thread-safe buffer".
        events: "queue.Queue[EventLogRecord]" = queue.Queue()

        # Watcher first, and fully established before the adapter can
        # touch the filesystem (Requirement 6.2's "WHILE an Agent_Adapter
        # process runs"). If the watch could not be established at all,
        # fail before launching the adapter rather than running an agent
        # whose file changes nobody is recording -- there is nothing
        # captured to write an Event_Log from at this point anyway.
        watcher = self._start_inotify_watcher(events)
        watcher.wait_until_watching(timeout=_WATCHER_STARTUP_TIMEOUT)
        if watcher.error is not None:
            watcher.stop()
            watcher.join(timeout=_WATCHER_JOIN_TIMEOUT)
            raise watcher.error

        capture = self._start_pty_capture(adapter, prompt, events)

        try:
            capture.join()
            time.sleep(_WATCHER_SETTLE_DELAY)
        finally:
            # Both producers are guaranteed stopped before the drain
            # below, even if joining the capture was interrupted.
            watcher.stop()
            watcher.join(timeout=_WATCHER_JOIN_TIMEOUT)

        log_path = _append_event_log(
            event_log_path(resolved_run_dir), _merge_events(events)
        )

        if capture.error is not None:
            raise capture.error
        # Requirement 4.5's "mark the Run as failed", raised here rather
        # than at detection time so the violation record written above is
        # already durable -- see the module docstring's `off_limits_paths`
        # section for why this sits behind the adapter's own error.
        if watcher.violation is not None:
            raise watcher.violation
        # Normally already ruled out by the fail-fast check above; still
        # checked here for the case where `wait_until_watching` timed out
        # and the startup failure only surfaced afterward.
        if watcher.error is not None:
            raise watcher.error

        return log_path

    def _start_pty_capture(
        self,
        adapter: Agent_Adapter,
        prompt: str,
        events: "queue.Queue[EventLogRecord]",
    ) -> "PtyOutputCapture":
        """Starts (and returns, already running) the PTY-wrapper thread
        for `adapter.launch(prompt, self.workdir)`, pushing one
        `EventLogRecord` per captured output chunk onto `events`.

        `events` is accepted as a parameter rather than owned internally
        so that a future `supervise` (Task 7.3) can create a single
        `queue.Queue` and hand the SAME instance to both this producer and
        the Task 7.2 inotify-watcher thread -- "two independent event
        producers ... feeding a single thread-safe buffer", per
        design.md, where the buffer itself belongs to the caller
        orchestrating both producers, not to either producer alone.

        Does not block: the returned `PtyOutputCapture` is a live
        `threading.Thread` the caller must `join()` (and then check
        `.error` on) to observe completion/failure -- see
        `PtyOutputCapture`'s own docstring.
        """
        capture = PtyOutputCapture(self.workdir, adapter, prompt, events)
        capture.start()
        return capture

    def _start_inotify_watcher(
        self,
        events: "queue.Queue[EventLogRecord]",
    ) -> "FileChangeWatcher":
        """Starts (and returns, already running) the inotify-watcher
        thread for `self.workdir`, pushing one `EventLogRecord` per
        detected file create/modify/delete onto `events`.

        Mirrors `_start_pty_capture` deliberately: `events` is accepted
        as a parameter for the same reason (a future `supervise`, Task
        7.3, hands the SAME `queue.Queue` to both this producer and
        `_start_pty_capture`'s). Does not block: the returned
        `FileChangeWatcher` is a live `threading.Thread` that keeps
        running until explicitly stopped -- unlike `PtyOutputCapture`,
        which finishes on its own once the adapter's event stream ends,
        a filesystem watch has no natural end-of-stream (see the module
        docstring), so the caller must eventually call `.stop()` and then
        `.join()` (and check `.error`) to shut it down cleanly.

        `self.off_limits_paths` is handed to the watcher here, which is
        where Task 7.7's per-event check runs; a caller that inspects the
        returned watcher directly reads its verdict off `.violation`, the
        same way it reads `.error`.
        """
        watcher = FileChangeWatcher(
            self.workdir, events, off_limits_paths=self.off_limits_paths
        )
        watcher.start()
        return watcher


class PtyOutputCapture(threading.Thread):
    """The PTY-wrapper thread (Task 7.1 / Requirement 6.1).

    See the module docstring for the full mechanism and reasoning. In
    short: for the duration of one `adapter.launch(prompt, workdir)` call,
    this thread swaps the process's real fd 0/1/2 for a pty slave, drives
    the adapter's event stream to completion (writing each yielded
    event's payload to fd 1), restores the original fds, and joins an
    internal reader thread that timestamps every chunk read from the pty
    master fd into `events`.

    `self.error` is `None` on success, or the `AdapterError` raised by the
    adapter's iterator otherwise (see the module docstring for why this
    is a checked attribute rather than an exception re-raised from a
    `join()`-style call). It is only ever set after all fd restoration
    and reader-thread cleanup has already completed -- a caller that
    observes `self.error is not None` after `join()` never needs to run
    any cleanup of its own first.
    """

    def __init__(
        self,
        workdir: Path,
        adapter: Agent_Adapter,
        prompt: str,
        events: "queue.Queue[EventLogRecord]",
    ) -> None:
        super().__init__(name="vaultshield-pty-capture", daemon=True)
        self._workdir = workdir
        self._adapter = adapter
        self._prompt = prompt
        self._events = events
        self.error: AdapterError | None = None

    def run(self) -> None:
        """Thread entry point. Never raises: any `AdapterError` from the
        adapter's iterator is caught and stored on `self.error` instead
        (see the class docstring), since an exception escaping `run()`
        would otherwise only be printed by Python's default thread
        excepthook and never reach a joining caller at all.
        """
        master_fd, slave_fd = pty.openpty()
        saved_fds = (os.dup(0), os.dup(1), os.dup(2))

        reader = threading.Thread(
            target=_read_pty_output,
            args=(master_fd, self._events),
            name="vaultshield-pty-reader",
            daemon=True,
        )
        reader.start()

        try:
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            try:
                for event in self._adapter.launch(self._prompt, str(self._workdir)):
                    _write_all(1, (event["payload"] + "\n").encode("utf-8", errors="replace"))
            except Exception as exc:
                # EVERY exception, not just `AdapterError`. This thread's
                # `run()` is the top of its own stack, so anything that escapes
                # here goes to `threading.excepthook` and is *lost*: the joining
                # caller in `supervise` sees a clean join, writes the Event_Log,
                # finds `self.error is None`, and reports a successful Run. An
                # agent that crashed with, say, a `RuntimeError` would therefore
                # be indistinguishable from one that finished — a false success
                # carrying a truncated transcript.
                #
                # Capturing into `self.error` (rather than re-raising) is what
                # lets `supervise` write the partial Event_Log *first* and only
                # then fail, so the evidence of the crash outlives it.
                #
                # `Exception`, not `BaseException`: `KeyboardInterrupt` and
                # `SystemExit` are delivered to the main thread, and swallowing
                # them here would fight the interpreter's own shutdown.
                self.error = exc
        finally:
            # Restore fd 0/1/2 to what they pointed at before this thread
            # ever touched them, then drop every remaining reference to
            # the pty slave's open file description (the saved dups, plus
            # `slave_fd` itself) so the reader thread's `os.read()` on the
            # master side observes EOF and returns on its own.
            for real_fd, saved_fd in zip((0, 1, 2), saved_fds):
                os.dup2(saved_fd, real_fd)
                os.close(saved_fd)
            os.close(slave_fd)
            reader.join(timeout=_READER_JOIN_TIMEOUT)
            os.close(master_fd)


class FileChangeWatcher(threading.Thread):
    """The inotify-watcher thread (Task 7.2 / Requirement 6.2).

    See the module docstring's "inotify-watcher mechanism" section for
    the full mechanism and reasoning. In short: for as long as this
    thread runs, it watches `workdir` (recursively) for file create/
    modify/delete events via a `watchdog.observers.Observer`, pushing one
    timestamped `EventLogRecord(event_type="file_change", ...)` onto
    `events` per detected change.

    Unlike `PtyOutputCapture`, this thread does not finish on its own:
    a filesystem watch has no natural end-of-stream, so a caller MUST
    call `stop()` and then `join()` to shut it down (daemon=True is still
    set, matching `PtyOutputCapture`, purely as a safety net so a caller
    that forgets to do so doesn't block interpreter shutdown).

    `start()` returning only means this thread is running, not that the
    watch is live yet; `wait_until_watching()` is the signal for that, and
    `supervise` uses it to avoid launching an adapter whose first writes
    could outrun the watch's own setup.

    `self.error` is `None` on success, or the `OSError` raised while
    trying to start watching `workdir` (most plausibly: `workdir` does
    not exist, or is not a directory) otherwise -- see the module
    docstring for why this is a checked attribute rather than an
    exception re-raised from a `join()`-style call, mirroring
    `PtyOutputCapture.error`. It is only ever set after the internal
    `Observer` has already been stopped and joined -- a caller that
    observes `self.error is not None` after `join()` never needs to run
    any cleanup of its own first.

    `off_limits_paths` (Task 7.7 / Requirement 4.5) is checked against
    every detected change before that change is enqueued, on this
    watcher's own emitter thread -- design.md puts the check "entirely
    inside the existing inotify watcher", so this class owns it rather
    than any new component. A match pushes a `event_type="violation"`
    record alongside the ordinary `file_change` one and sets
    `self.violation` (an `OffLimitsViolation`, first match wins) for the
    caller to check after `join()`, exactly as it checks `self.error`.
    `self.violation` staying `None` means no detected change matched --
    including the ordinary case of an empty/omitted `off_limits_paths`,
    where the check is a no-op.
    """

    def __init__(
        self,
        workdir: Path,
        events: "queue.Queue[EventLogRecord]",
        *,
        off_limits_paths: Iterable[str] | None = None,
    ) -> None:
        super().__init__(name="vaultshield-inotify-watcher", daemon=True)
        self._workdir = workdir
        self._events = events
        self._off_limits_paths = _normalize_off_limits_entries(off_limits_paths)
        self._stop_event = threading.Event()
        self._startup_done = threading.Event()
        # Guards `violation`'s first-match-wins write: watchdog is free to
        # dispatch from more than one emitter thread (one per watch), so
        # the read-then-set is not safe unsynchronized even though a
        # single-directory watch normally has just one emitter.
        self._violation_lock = threading.Lock()
        self.error: OSError | None = None
        self.violation: OffLimitsViolation | None = None

    def wait_until_watching(self, timeout: float | None = None) -> bool:
        """Blocks until this thread has finished ATTEMPTING to establish
        the watch, returning `True` if that happened within `timeout` and
        `False` if the wait timed out first.

        Added for `supervise` (Task 7.3), which must not launch the
        adapter until the watch is live: `start()` only guarantees the
        thread is running, not that it has scheduled the watch yet, so
        without this the adapter's earliest filesystem writes could race
        the watch's own setup and go unrecorded -- a hole in the "no event
        from either source is dropped" guarantee design.md makes for the
        merge.

        Returning `True` means the attempt COMPLETED, not that it
        succeeded: check `self.error` afterward to tell the two apart.
        `watchdog`'s `Observer.start()` runs its emitter's startup
        synchronously, so once it returns the watch really is established
        and the signal here is a fact rather than an estimate.
        """
        return self._startup_done.wait(timeout)

    def stop(self) -> None:
        """Signals the watch loop to stop. Does not block -- call `join()`
        afterward to wait for the thread to actually finish (see the
        class docstring for why an explicit stop signal is needed at all,
        unlike `PtyOutputCapture`).

        Safe to call more than once, and safe to call before `run()` has
        actually started watching: the loop in `run()` checks the same
        `threading.Event` this sets before (and while) it waits on the
        `Observer`, so a `stop()` that arrives early is not lost.
        """
        self._stop_event.set()

    def run(self) -> None:
        """Thread entry point. Never raises: any `OSError` from starting
        the watch is caught and stored on `self.error` instead (see the
        class docstring), for the same reason `PtyOutputCapture.run`
        stores its own `AdapterError` rather than letting it escape.
        """
        observer = Observer()
        handler = _FileChangeHandler(
            self._events,
            self._workdir,
            self._off_limits_paths,
            self._note_violation,
        )
        try:
            observer.schedule(handler, str(self._workdir), recursive=True)
            observer.start()
        except OSError as exc:
            self.error = exc
            return
        finally:
            # Unblocks any `wait_until_watching()` caller on success and
            # on failure alike: a waiter that then finds `self.error` set
            # learns the watch never started, instead of sitting out its
            # entire timeout waiting for a watch that will never exist.
            self._startup_done.set()

        try:
            # Waits until either stop() is called, or (defensively) the
            # Observer's own thread dies unexpectedly -- polling on a
            # short timeout rather than a single indefinite wait so a
            # dead Observer doesn't leave this thread blocked forever
            # with no way to ever notice.
            while not self._stop_event.wait(timeout=0.1):
                if not observer.is_alive():
                    break
        finally:
            observer.stop()
            observer.join(timeout=_OBSERVER_JOIN_TIMEOUT)

    def _note_violation(self, changed_path: str, matched_entry: str) -> None:
        """Records the Run-failing verdict for an `off_limits_paths` match
        (Task 7.7 / Requirement 4.5), called by `_FileChangeHandler` from
        the watcher's own emitter thread.

        First match wins: a later violation does not overwrite an earlier
        one, so `self.violation` names the FIRST off-limits path the agent
        touched, which is the one a reviewer wants. Nothing is lost by
        that -- every violation, first or not, is separately recorded in
        the Event_Log as its own "violation" record by the caller of this
        method before it gets here.
        """
        with self._violation_lock:
            if self.violation is None:
                self.violation = OffLimitsViolation(changed_path, matched_entry)


class _FileChangeHandler(FileSystemEventHandler):
    """Translates `watchdog` callbacks into `EventLogRecord` pushes for
    `FileChangeWatcher`. Module-private: this is an implementation detail
    of `FileChangeWatcher`, not a shape any other code should depend on.

    Only `on_created`/`on_modified`/`on_deleted` are handled, and only for
    plain files (`event.is_directory` is `False`) -- see the module
    docstring's "inotify-watcher mechanism" section for why moves and
    directory-level events are deliberately excluded.

    `off_limits_paths` is the already-normalized entry tuple (Task 7.7);
    `on_violation` is `FileChangeWatcher._note_violation`, invoked on a
    match so the verdict lands on the public attribute the watcher's
    caller checks. The callback keeps that state on the watcher, where
    `.error` already lives, instead of on this short-lived, internal
    handler.
    """

    def __init__(
        self,
        events: "queue.Queue[EventLogRecord]",
        workdir: Path,
        off_limits_paths: tuple[str, ...],
        on_violation: Callable[[str, str], None],
    ) -> None:
        self._events = events
        self._workdir = workdir
        self._off_limits_paths = off_limits_paths
        self._on_violation = on_violation

    def on_created(self, event: FileSystemEvent) -> None:
        self._record(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._record(event, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._record(event, "deleted")

    def _record(self, event: FileSystemEvent, change_kind: str) -> None:
        """Records one detected change: the ordinary `file_change` record
        always, plus a `violation` record and the Run-failing verdict when
        the changed path matches an `off_limits_paths` entry (Requirement
        4.5).

        The match is evaluated BEFORE anything is enqueued, per design.md
        ("checks the changed path against `config.off_limits_paths` before
        enqueuing the event"), so a matched change is never briefly
        visible as an ordinary, unflagged one. Both records then share the
        single timestamp taken here and are enqueued ordinary-first: the
        merge step's sort is stable over equal timestamps, so they stay
        adjacent and in that order in the Event_Log, which is what makes
        design.md's "in addition to the ordinary file-change event"
        readable on disk rather than just true.
        """
        if event.is_directory:
            return

        changed_path = str(event.src_path)
        matched_entry = _match_off_limits(
            changed_path, self._workdir, self._off_limits_paths
        )
        timestamp = _now_iso8601()

        self._events.put(
            EventLogRecord(
                event_type="file_change",
                timestamp=timestamp,
                payload=f"{change_kind}: {changed_path}",
            )
        )
        if matched_entry is None:
            return

        self._events.put(
            EventLogRecord(
                event_type="violation",
                timestamp=timestamp,
                payload=(
                    f"off_limits_paths violation: {changed_path} "
                    f"(matched off_limits_paths entry {matched_entry!r})"
                ),
            )
        )
        self._on_violation(changed_path, matched_entry)


def _read_pty_output(master_fd: int, events: "queue.Queue[EventLogRecord]") -> None:
    """Reads `master_fd` in a loop, pushing one timestamped
    `EventLogRecord(event_type="output", ...)` onto `events` per chunk
    returned by a single `os.read()` call -- see `_READ_CHUNK_SIZE`'s
    comment for why no re-chunking/re-coalescing happens here.

    Returns (terminating the thread it runs on) as soon as the pty slave
    side is fully closed: `os.read` then either raises `OSError` (typical
    on Linux, errno EIO) or returns `b""` (typical on macOS/BSD). Both are
    the ordinary, expected end-of-stream signal `PtyOutputCapture.run`
    deliberately triggers once the adapter's event stream is exhausted --
    not a real error -- so both simply return rather than propagating.
    """
    while True:
        try:
            chunk = os.read(master_fd, _READ_CHUNK_SIZE)
        except OSError:
            return
        if not chunk:
            return
        events.put(
            EventLogRecord(
                event_type="output",
                timestamp=_now_iso8601(),
                payload=chunk.decode("utf-8", errors="replace"),
            )
        )


def _normalize_off_limits_entries(entries: Iterable[str] | None) -> tuple[str, ...]:
    """Cleans up raw `off_limits_paths` entries once, at watcher
    construction time, so `_match_off_limits` can stay a pure comparison
    on every subsequent event (Task 7.7 / Requirement 4.5).

    `off_limits_paths` is the one `.vaultshield.yml` field written purely
    by hand (`config.write_config` only ever emits it empty), so its
    entries arrive in whatever shape a developer typed: `"secrets/"`,
    `" .env"`, `"./infra"`. Each is stripped of surrounding whitespace, of
    any leading `./`, and of trailing slashes, so those spellings all
    collapse onto the same comparison form. Blank entries are dropped
    entirely rather than kept as an empty pattern nobody could reason
    about. Nothing else is rewritten -- in particular globs are left
    untouched for `fnmatch` to interpret.

    `None` (the "no config threaded through" case) and an empty list both
    normalize to `()`, which makes `_match_off_limits` a no-op returning
    `None` for every event -- the ordinary behavior for a project that
    declared no off-limits paths.
    """
    normalized: list[str] = []
    for raw_entry in entries or ():
        entry = str(raw_entry).strip()
        while entry.startswith("./"):
            entry = entry[2:]
        entry = entry.rstrip("/")
        if entry:
            normalized.append(entry)
    return tuple(normalized)


def _match_off_limits(
    changed_path: str,
    workdir: Path,
    entries: tuple[str, ...],
) -> str | None:
    """Returns the first `entries` element the changed path violates, or
    `None` if it violates none (Task 7.7 / Requirement 4.5).

    Two match modes per entry, both applied to each candidate spelling of
    the changed path (see `_path_candidates`):

    - Containment: the candidate IS the entry, or sits underneath it as a
      directory prefix (`secrets` matches `secrets/prod.key`). This is
      Requirement 4.5's own wording -- a change "under a path matching an
      `off_limits_paths` entry" -- and is what makes a bare directory
      entry behave the way a developer writing one expects.
    - Glob: `fnmatch` against the entry, so `*.pem`, `infra/**` and
      `.env*` style entries work too. `fnmatch`'s `*` deliberately spans
      `/` here, so `*.pem` covers a nested `secrets/a/b/key.pem` as well
      as a top-level one; a developer listing a file extension as
      off-limits means it everywhere in the repository, not only at the
      root. Case sensitivity follows the platform's own convention
      (`fnmatch` normalizes case exactly where the filesystem does).

    Entries are the outer loop so the FIRST configured entry wins when a
    path violates several, making the reported `pattern` deterministic and
    tied to config order rather than to which spelling happened to match.

    Erring generous is deliberate: a false positive fails one Run and is
    fully visible in the Event_Log, while a false negative silently lets
    an agent modify a path the developer declared off-limits -- exactly
    what Requirement 4.5 exists to catch.
    """
    if not entries:
        return None
    candidates = _path_candidates(changed_path, workdir)
    for entry in entries:
        for candidate in candidates:
            if candidate == entry or candidate.startswith(entry + "/"):
                return entry
            if fnmatch.fnmatch(candidate, entry):
                return entry
    return None


def _path_candidates(changed_path: str, workdir: Path) -> tuple[str, ...]:
    """Returns every spelling of `changed_path` an `off_limits_paths`
    entry could reasonably be written against: the absolute path as
    reported by `watchdog`, and the path relative to `workdir`.

    The relative form is the one that matters in practice --
    `off_limits_paths` entries are repository-root-relative, while
    `watchdog` reports absolute paths derived from the watched directory
    -- but the absolute form is kept as a candidate too so an entry
    written as an absolute path still matches instead of silently never
    firing.

    Both `workdir` and the changed path are also tried in resolved form,
    because the two can disagree about symlinked ancestors even when they
    name the same file (macOS's `/var` -> `/private/var` is the everyday
    example, and a Sandbox working directory reached through a symlink is
    the general one); without that, `relative_to` would fail and the
    relative candidate -- the useful one -- would be missing. Duplicates
    are collapsed while preserving order, so the common case where all
    four combinations agree still yields just two candidates.
    """
    changed = Path(changed_path)
    targets = tuple(dict.fromkeys((changed, _safe_resolve(changed))))
    bases = tuple(dict.fromkeys((workdir, _safe_resolve(workdir))))

    candidates = [target.as_posix() for target in targets]
    for base in bases:
        for target in targets:
            try:
                candidates.append(target.relative_to(base).as_posix())
            except ValueError:
                # `target` is not under `base` in this combination; another
                # combination may still yield a relative candidate.
                continue
    return tuple(dict.fromkeys(candidates))


def _safe_resolve(path: Path) -> Path:
    """`Path.resolve()` that never raises, for use inside the watcher's
    per-event path matching.

    A file-change event routinely names a path that no longer exists (a
    delete, or a temp file already replaced), and resolution can fail on a
    broken symlink or a permission-denied ancestor. None of that should be
    able to take down the watcher's emitter thread, and the unresolved
    path is still a perfectly usable match candidate, so a failure just
    returns the input unchanged.
    """
    try:
        return path.resolve()
    except OSError:
        return path


def _merge_events(events: "queue.Queue[EventLogRecord]") -> list[EventLogRecord]:
    """The merge step (Requirement 6.3): drains every record currently in
    the shared buffer and returns them as one list sorted non-decreasing
    by timestamp.

    Lossless by construction -- the drain removes each record exactly
    once and puts nothing back, and sorting neither adds nor drops
    elements -- provided both producers have already terminated, which is
    `supervise`'s job to guarantee before calling this.

    The sort is stable and keyed on the parsed timestamp; see the module
    docstring for why both of those matter (equal timestamps must keep
    their emitted order, per Property 9).
    """
    drained: list[EventLogRecord] = []
    while True:
        try:
            drained.append(events.get_nowait())
        except queue.Empty:
            break
    return sorted(drained, key=_timestamp_sort_key)


def _append_event_log(path: Path, records: Iterable[EventLogRecord]) -> Path:
    """The JSONL Event_Log writer (Requirement 6.3): appends one JSON
    object per record, one record per line, to `path`, creating `path`
    and its parent directories if needed. Returns `path`.

    Append-only: an existing Event_Log is never truncated, rewritten, or
    replaced -- see the module docstring for why this deliberately does
    not follow `credentials.py`'s atomic write-then-`os.replace`
    convention.

    Each line is `json.dumps` of the record's own three fields
    (`event_type`/`timestamp`/`payload`), which is also the field
    vocabulary design.md's `RunEvent` Django model uses, so the Uploader
    (Task 18.2) can post these lines onward without translating them.
    `json.dumps` escapes any newline or control character inside a payload
    (terminal output routinely carries both), so one record always
    occupies exactly one line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as log_file:
        for record in records:
            log_file.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    return path


def _default_run_dir(workdir: Path, run_id: str) -> Path:
    """Returns the Run_Directory `supervise` writes into when its caller
    doesn't name one: `<workdir>/.vaultshield/runs/<run_id>/`, per
    requirements.md's Glossary definition of Run_Directory.

    The `.vaultshield/runs` prefix comes from `run_directory.runs_root`
    (Task 8.1), which owns that convention canonically, so this default
    cannot drift from the path Task 8.2's `run` pipeline resolves. This
    function still exists rather than calling `run_directory.run_dir_path`
    outright for one reason: `run_dir_path` validates `run_id` and raises
    `RunDirectoryError` on anything that is not a single safe path
    component, whereas `Monitor` deliberately stores `run_id` exactly as
    design.md's `Monitor.__init__(workdir, run_id)` declares, without
    validating or transforming it (see the class docstring). Task 8.2
    resolves the canonical, validated Run_Directory itself and passes it
    to `supervise` via the `run_dir` override, so the validating path is
    the one every real Run actually takes.
    """
    return runs_root(workdir) / run_id


def _timestamp_sort_key(record: EventLogRecord) -> datetime:
    """Sort key for the merge step: `record.timestamp` parsed into an
    aware `datetime`.

    Every record reaching this function was timestamped by
    `_now_iso8601()`, i.e. is already UTC-aware; a naive value is
    nonetheless normalized to UTC rather than left as-is, because
    comparing a naive and an aware `datetime` raises `TypeError` and one
    stray naive timestamp would otherwise take down the whole merge.

    An unparseable timestamp raises `ValueError` and is left to propagate:
    only this module's own two producers construct `EventLogRecord`s, so
    that can only mean a real bug in one of them, and silently sorting a
    malformed record into an arbitrary position would hide it.
    """
    parsed = datetime.fromisoformat(record.timestamp)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _write_all(fd: int, data: bytes) -> None:
    """Writes all of `data` to `fd`, looping as needed: a single
    `os.write` call is not guaranteed to write the entire buffer at once.
    """
    while data:
        written = os.write(fd, data)
        data = data[written:]


def _now_iso8601() -> str:
    """Returns the current UTC time as an ISO-8601 string.

    Duplicates `adapters/mock.py`'s identically-named/-implemented
    helper rather than importing it: each concrete `Agent_Adapter` is
    already responsible for generating its own `AgentEvent.timestamp`
    independently (per `adapters/base.py`), and the Monitor's own
    `EventLogRecord.timestamp` values are a distinct concept (when THIS
    process observed a chunk, not when an adapter produced an event) that
    happens to want the same format -- not a value worth sharing a helper
    across module boundaries for.
    """
    return datetime.now(timezone.utc).isoformat()
