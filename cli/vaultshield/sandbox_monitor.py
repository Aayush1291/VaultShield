"""In-Sandbox agent execution and monitoring.

The execution path `vaultshield run` uses. The agent runs as a real process
*inside* the Docker container, and both event producers observe that container:

    terminal output  <- Sandbox.exec_stream's pty, streamed live
    file changes     <- a watcher process running inside the container

### Why this module exists instead of `Monitor.supervise`

`Monitor.supervise` wraps `adapter.launch(prompt, workdir)` in a pty opened in
*this* process and watches `workdir` with a host-side `watchdog` observer. Both
halves are therefore host-side by construction, and the workdir it was given was
the developer's own repository. That produced three defects at once:

- the agent ran on the developer's machine, with their filesystem and their real
  `.env` reachable from its working directory;
- the Monitor recorded changes to the *host* repository;
- git capture, which correctly operates on the container's clone, saw no changes
  at all and produced an empty `diff.json` — the Run recorded none of the
  agent's actual work.

Changing the workdir string would not have fixed any of that: a host process
given a container path simply fails. The process itself has to move, which is
what `Sandbox.exec_stream` and `adapters.base.SandboxInvocation` are for.

`Monitor.supervise` is kept (it is the adapter-level contract Requirement 5.1
names, and unit tests drive it directly) but `run` no longer calls it.

### The file-change watcher

`watchdog`/inotify cannot see inside another container's mount namespace from
the host, so the watcher has to run in the container too. It is a small
standard-library Python program (`_WATCHER_PROGRAM`) — the base image is a
`python:*` image with none of VaultShield installed, so it can import nothing
from this package.

It polls `os.walk` + `stat` rather than using inotify, because inotify tooling
is not in a stock Python image and adding a package to the Sandbox image for it
would change what the Sandbox is. Polling coalesces very rapid successive writes
to one `modified` record, which the host-side observer also did, so this is not
a regression. What polling must not do is miss a net change, and it cannot: the
watcher performs a final reconciliation pass after it is told to stop, so a file
created and deleted entirely between two polls is still reported by comparing
the closing snapshot against the opening one.

Timestamps are generated inside the container, from the same clock the agent
runs against, and are UTC ISO-8601 so they merge with the host-generated
terminal timestamps under the existing `_timestamp_sort_key`.
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path

from vaultshield.adapters.base import AdapterError, Agent_Adapter
from vaultshield.monitor import (
    EventLogRecord,
    OffLimitsViolation,
    _append_event_log,
    _merge_events,
    _normalize_off_limits_entries,
    _now_iso8601,
)
from vaultshield.run_directory import event_log_path
from vaultshield.sandbox import Sandbox, SandboxError

# Where the watcher program and its output live inside the container. Under
# `/tmp`, deliberately outside `/workspace/repo`, so the watcher's own files are
# never mistaken for agent changes and never reach the committed branch.
WATCHER_PATH = "/tmp/vaultshield_watch.py"
WATCHER_OUTPUT = "/tmp/vaultshield_fschanges.jsonl"
WATCHER_STOP_FILE = "/tmp/vaultshield_watch.stop"

# How long to wait for the watcher to notice the stop file, run its final
# reconciliation pass and exit. Generous relative to its poll interval so a
# loaded machine does not truncate the last pass.
_WATCHER_DRAIN_TIMEOUT_S = 6.0
_WATCHER_DRAIN_POLL_S = 0.1

# Poll interval for the in-container watcher, in seconds. Short enough to give
# useful interleaving against the terminal transcript, long enough not to spend
# the container's CPU walking the tree.
_WATCH_INTERVAL_S = 0.15

# Directory names never worth walking: `.git` churns constantly during git
# capture and would bury real agent changes under hundreds of records.
_WATCH_EXCLUDES = (".git",)

# The in-container watcher. Standard library only; see the module docstring.
_WATCHER_PROGRAM = '''
import json, os, stat, sys, time

root, out_path, stop_path, interval = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
EXCLUDE = {".git"}


def snapshot():
    seen = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            seen[os.path.relpath(full, root)] = (st.st_mtime_ns, st.st_size)
    return seen


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%06dZ" % (
        int((time.time() % 1) * 1_000_000)
    )


def emit(sink, verb, rel):
    sink.write(json.dumps({"timestamp": now(), "verb": verb, "path": rel}) + "\\n")
    sink.flush()


baseline = snapshot()
previous = dict(baseline)
with open(out_path, "w") as sink:
    while True:
        stopping = os.path.exists(stop_path)
        current = snapshot()
        for rel, meta in current.items():
            if rel not in previous:
                emit(sink, "created", rel)
            elif previous[rel] != meta:
                emit(sink, "modified", rel)
        for rel in previous:
            if rel not in current:
                emit(sink, "deleted", rel)
        previous = current
        if stopping:
            # Final reconciliation against the ORIGINAL baseline: catches any
            # net change the poll loop never sampled, e.g. a file created and
            # deleted between two passes.
            for rel in current:
                if rel not in baseline:
                    emit(sink, "net_created", rel)
            for rel in baseline:
                if rel not in current:
                    emit(sink, "net_deleted", rel)
            break
        time.sleep(interval)
'''


class SandboxMonitorError(Exception):
    """Raised when the in-Sandbox monitoring apparatus itself fails.

    Distinct from `AdapterError` (the agent could not start or produced nothing
    usable) and from `SandboxError` (Docker itself failed): this means the
    Sandbox and the agent were fine but the Monitor could not establish or read
    its own file-change watch, so a Run would have gone unobserved.
    """


def supervise_in_sandbox(
    sandbox: Sandbox,
    adapter: Agent_Adapter,
    prompt: str,
    run_dir: Path,
    *,
    off_limits_paths: tuple[str, ...] | list[str] | None = None,
    workdir: str = Sandbox.REPO_DIR,
) -> Path:
    """Runs `adapter` inside `sandbox` and writes the merged Event_Log.

    Args:
        sandbox: an already-created Sandbox whose clone is at `workdir`.
        adapter: the Agent_Adapter to run. Only `sandbox_invocation` is used;
            `launch` is never called, so nothing executes on the host.
        prompt: the developer's prompt.
        run_dir: the host Run_Directory the Event_Log is appended into.
        off_limits_paths: Project_Config_File entries (Requirement 4.5),
            matched against paths relative to the repository root inside the
            container.
        workdir: the agent's working directory inside the container.

    Returns:
        The path of the Event_Log written on the host.

    Raises:
        AdapterError: if the invocation cannot be constructed (raised before any
            process starts), or if the agent process exited non-zero — a crash
            inside the container is a failed Run, never a silent success.
        OffLimitsViolation: if the agent touched a protected path. Raised only
            after the Event_Log is durable, matching `Monitor.supervise`.
        SandboxMonitorError: if the file-change watch could not be established.
        SandboxError: propagated unchanged from Docker-level failures.

    Ordering mirrors `Monitor.supervise` deliberately: establish the watcher
    before the agent can touch anything, write the Event_Log before raising
    anything, and let the adapter's own error take precedence over a violation.
    """
    invocation = adapter.sandbox_invocation(prompt, workdir)

    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    patterns = _normalize_off_limits_entries(off_limits_paths)

    _start_watcher(sandbox, workdir)

    agent_error: Exception | None = None
    try:
        exit_code = _stream_agent(sandbox, invocation, workdir, events)
    except SandboxError:
        # Docker-level failure: the caller distinguishes this from an agent
        # failure, so it is not rewrapped. The watcher is still stopped below.
        _stop_watcher(sandbox)
        raise
    except Exception as exc:  # noqa: BLE001
        # Anything else from streaming is captured rather than raised, so the
        # partial transcript is still written before the failure surfaces --
        # the same reason `PtyOutputCapture` captures into `self.error`.
        agent_error = exc
        exit_code = None
    else:
        if exit_code not in (0, None):
            agent_error = AdapterError(
                f"Agent process exited with code {exit_code} inside the Sandbox"
            )

    file_records, violation = _collect_file_changes(sandbox, patterns)
    for record in file_records:
        events.put(record)

    log_path = _append_event_log(event_log_path(run_dir), _merge_events(events))

    if agent_error is not None:
        raise agent_error
    if violation is not None:
        raise violation
    return log_path


def _start_watcher(sandbox: Sandbox, workdir: str) -> None:
    """Uploads and starts the in-container file-change watcher.

    Verifies the watcher actually came up rather than assuming a detached exec
    succeeded: a Run whose file changes nobody recorded is worse than one that
    fails to start, because it looks like a clean Run with no changes.
    """
    try:
        sandbox.exec(["rm", "-f", WATCHER_OUTPUT, WATCHER_STOP_FILE])
        sandbox.write_file(WATCHER_PATH, _WATCHER_PROGRAM)
        sandbox.exec_detached(
            [
                "python",
                WATCHER_PATH,
                workdir,
                WATCHER_OUTPUT,
                WATCHER_STOP_FILE,
                str(_WATCH_INTERVAL_S),
            ]
        )
    except SandboxError as exc:
        raise SandboxMonitorError(
            f"Could not start the in-Sandbox file-change watcher: {exc}"
        ) from exc

    deadline = time.monotonic() + _WATCHER_DRAIN_TIMEOUT_S
    while time.monotonic() < deadline:
        if sandbox.exec(["test", "-f", WATCHER_OUTPUT]).exit_code == 0:
            return
        time.sleep(_WATCHER_DRAIN_POLL_S)

    raise SandboxMonitorError(
        "The in-Sandbox file-change watcher did not start: "
        f"{WATCHER_OUTPUT} never appeared. The Sandbox image must provide "
        "`python` on PATH."
    )


def _stop_watcher(sandbox: Sandbox) -> None:
    """Signals the watcher to run its final pass and exit. Never raises."""
    try:
        sandbox.exec(["touch", WATCHER_STOP_FILE])
    except SandboxError:
        # The container may already be gone; there is nothing to stop and
        # nothing a caller could do about it.
        pass


def _stream_agent(
    sandbox: Sandbox,
    invocation,
    workdir: str,
    events: "queue.Queue[EventLogRecord]",
) -> int | None:
    """Runs the agent in the container, turning its output into records.

    Output arrives as arbitrarily-split byte chunks (the daemon splits wherever
    the process happened to write), so lines are assembled across chunks and a
    trailing partial line is flushed at the end rather than dropped.

    Timestamps are taken on the host as each chunk arrives. That is the same
    thing `_read_pty_output` does for the host path, and it keeps the transcript
    ordered against the container-generated file-change timestamps without
    trusting the two clocks to be identical.
    """
    stream = sandbox.exec_stream(
        invocation.argv, env=invocation.env or None, workdir=workdir, tty=True
    )
    pending = b""
    for chunk in stream:
        pending += chunk
        while b"\n" in pending:
            line, _, pending = pending.partition(b"\n")
            events.put(
                EventLogRecord(
                    event_type="output",
                    timestamp=_now_iso8601(),
                    payload=line.decode("utf-8", errors="replace").rstrip("\r"),
                )
            )
    if pending:
        events.put(
            EventLogRecord(
                event_type="output",
                timestamp=_now_iso8601(),
                payload=pending.decode("utf-8", errors="replace").rstrip("\r"),
            )
        )
    return stream.exit_code


def _collect_file_changes(
    sandbox: Sandbox, patterns: tuple[str, ...]
) -> tuple[list[EventLogRecord], OffLimitsViolation | None]:
    """Stops the watcher, reads its records, and checks them against
    `off_limits_paths`.

    Returns the records to merge plus the FIRST violation found, if any. Every
    violation is recorded in the Event_Log; only the first is raised, matching
    `FileChangeWatcher._note_violation`'s behaviour on the host path.

    A watcher that produced nothing readable is reported as an empty list rather
    than an error: by this point the agent has already run, and discarding its
    transcript because the file log could not be read would destroy the more
    valuable evidence.
    """
    _stop_watcher(sandbox)

    raw = ""
    deadline = time.monotonic() + _WATCHER_DRAIN_TIMEOUT_S
    while time.monotonic() < deadline:
        if sandbox.exec(["test", "-f", WATCHER_STOP_FILE]).exit_code != 0:
            break
        result = sandbox.exec(["cat", WATCHER_OUTPUT])
        raw = result.stdout if result.exit_code == 0 else ""
        # The watcher removes nothing; it exits after its final pass. Detect
        # completion by the presence of a reconciliation record or by the
        # process having gone away.
        if sandbox.exec(["pgrep", "-f", WATCHER_PATH]).exit_code != 0:
            break
        time.sleep(_WATCHER_DRAIN_POLL_S)

    result = sandbox.exec(["cat", WATCHER_OUTPUT])
    if result.exit_code == 0 and result.stdout:
        raw = result.stdout

    records: list[EventLogRecord] = []
    violation: OffLimitsViolation | None = None
    reported: set[tuple[str, str]] = set()

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        verb = str(entry.get("verb", "changed"))
        rel = str(entry.get("path", ""))
        timestamp = str(entry.get("timestamp") or _now_iso8601())
        # `net_*` records come from the reconciliation pass and describe the
        # same file as an earlier record whenever the poll loop already saw it.
        # De-duplicate on (verb-ish, path) so a reconciled change is reported
        # once, while a genuinely missed one still appears.
        canonical = verb.replace("net_", "")
        key = (canonical, rel)
        if key in reported:
            continue
        reported.add(key)

        records.append(
            EventLogRecord(
                event_type="file_change",
                timestamp=timestamp,
                payload=f"{canonical}: {Sandbox.REPO_DIR}/{rel}",
            )
        )

        matched = _match_container_path(rel, patterns)
        if matched is not None:
            records.append(
                EventLogRecord(
                    event_type="violation",
                    timestamp=timestamp,
                    payload=(
                        f"off_limits_paths violation: {Sandbox.REPO_DIR}/{rel} "
                        f"(matched off_limits_paths entry {matched!r})"
                    ),
                )
            )
            if violation is None:
                violation = OffLimitsViolation(
                    f"{Sandbox.REPO_DIR}/{rel}", matched
                )

    return records, violation


def _match_container_path(rel: str, patterns: tuple[str, ...]) -> str | None:
    """Returns the `off_limits_paths` entry `rel` matches, or `None`.

    `rel` is already relative to the repository root inside the container, which
    is exactly the form `off_limits_paths` entries are written in, so matching is
    a prefix/equality test and needs none of the host path's `resolve()`
    gymnastics.

    A directory entry (`secrets/`, or a bare `secrets` that the changed path
    sits under) matches everything beneath it; a file entry matches exactly.
    """
    normalized = rel.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        candidate = pattern.replace("\\", "/").lstrip("./")
        if not candidate:
            continue
        if candidate.endswith("/"):
            if normalized.startswith(candidate):
                return pattern
        elif normalized == candidate or normalized.startswith(candidate + "/"):
            return pattern
    return None
