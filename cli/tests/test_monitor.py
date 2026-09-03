"""Unit tests for Task 7.1 (the PTY-wrapper thread, `PtyOutputCapture`),
Task 7.2 (the inotify-watcher thread, `FileChangeWatcher`) and Task 7.3
(`Monitor.supervise`'s merge step and JSONL Event_Log writer) in
`cli/monitor.py`, plus the `Monitor` class scaffold shared by all three.

Scoped deliberately narrowly to these tasks' own behavior: Requirement
6.1 (wrap the process in a PTY, capture terminal output with a timestamp
per chunk), Requirement 6.2 (watch the working directory, record a
timestamped event per detected file create/modify/delete) and
Requirements 6.3/6.4 (merge both sources into one append-only,
timestamp-ordered Event_Log fed by nothing else). `off_limits_paths`
enforcement (Task 7.7 / Requirement 4.5) is implemented in `monitor.py`
but has no tests here: its unit test is Task 7.8's own scope. The
dedicated property tests for ordered terminal-output capture (Property 9),
file-change capture (Property 10) and the lossless timestamp-sorted merge
(Property 11) are Tasks 7.4-7.6, also not attempted here -- everything
below is an ordinary unit test over fixed examples.

Exercises the PTY capture against `Mock_Adapter` (per design.md, "the
Monitor's first integration target") for the ordinary-output and
timestamp-shape checks, a fake adapter that raises `AdapterError`
immediately for the failure-propagation path, and a fake adapter that
spawns a real subprocess with no stdout redirection of its own to
confirm the Antigravity_Adapter design constraint: such a subprocess
must inherit a working pty as its stdio without allocating one itself.

Exercises the inotify watcher against a real filesystem under `tmp_path`,
for each of create/modify/delete, plus its `stop()`/`join()` shutdown
contract. Filesystem-watcher notifications arrive asynchronously relative
to the operation that triggers them -- unlike `PtyOutputCapture`, which
these tests simply `join()` and then inspect once already finished -- so
these tests poll the shared queue with a bounded timeout via
`_wait_until` rather than draining it once synchronously via `_drain`.

Exercises `supervise` end to end against a real filesystem too, using an
adapter that both writes a file and yields output so a single call
produces records from both producers, plus direct tests of the merge and
writer helpers for the ordering/append-only guarantees that are awkward
to force through a real timing-dependent run.

`supervise`'s re-raise of `FileChangeWatcher.error` has no test here on
purpose: that error is only ever set when starting the watch itself fails,
and on this project's dev platform (macOS/FSEvents) watchdog accepts both
a nonexistent path and a plain file without raising, so there is no way to
trigger it for real. Forcing it with a stubbed watcher would only assert
that a fake was wired up, not that the branch behaves correctly against
the real library, so the branch is left to the Linux/inotify environment
where it can actually fire.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vaultshield.adapters.base import AdapterError, Agent_Adapter, AgentEvent
from vaultshield.adapters.mock import Mock_Adapter
from vaultshield.monitor import (
    EVENT_LOG_FILENAME,
    EventLogRecord,
    FileChangeWatcher,
    Monitor,
    PtyOutputCapture,
    _append_event_log,
    _merge_events,
)


class _FailingAdapter(Agent_Adapter):
    """Fake Agent_Adapter that raises AdapterError immediately, exercising
    PtyOutputCapture's failure-propagation path (see monitor.py's class
    docstring) without needing a real Codex/Kiro/Antigravity adapter.
    """

    def launch(self, prompt: str, workdir: str):
        raise AdapterError("boom: invocation failed")


class _SubprocessAdapter(Agent_Adapter):
    """Fake Agent_Adapter simulating the Antigravity_Adapter's documented
    reliance on the Monitor's PTY: spawns a REAL subprocess via plain
    `subprocess.run` with no stdout/stderr redirection of its own, so it
    inherits whatever fd 1/2 currently are. Prints its own `isatty()`
    result so the test can confirm the subprocess actually observed a
    pty, without the adapter allocating one itself.
    """

    def launch(self, prompt: str, workdir: str):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; print('subprocess-isatty=' + str(sys.stdout.isatty()))",
            ],
            check=True,
        )
        return iter(())


class _FileTouchingAdapter(Agent_Adapter):
    """Fake Agent_Adapter that writes a file inside `workdir` and then
    yields ordinary output, so one `supervise` call produces records from
    BOTH producers (a file-change event and output events) and the merge
    step has something from each source to interleave.

    Sleeps briefly after the write so the watcher's asynchronous
    notification lands while this adapter is still running -- the test
    then asserts the merge, not `supervise`'s post-run settle delay.
    """

    def __init__(self, filename: str = "agent_created.txt") -> None:
        self._filename = filename

    def launch(self, prompt: str, workdir: str):
        (Path(workdir) / self._filename).write_text("written by the fake agent")
        time.sleep(0.5)
        yield AgentEvent(
            type="output",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload="fake agent output",
        )


class _FailsAfterOutputAdapter(Agent_Adapter):
    """Fake Agent_Adapter that yields real output and only then raises
    `AdapterError`, so a test can check `supervise` still wrote the
    already-captured output to the Event_Log before re-raising.
    """

    def launch(self, prompt: str, workdir: str):
        yield AgentEvent(
            type="output",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload="output-before-failure",
        )
        raise AdapterError("boom: failed after producing output")


def _read_event_log(path: Path) -> list[dict]:
    """Parses a JSONL Event_Log into one dict per line, asserting each
    line is independently valid JSON carrying exactly the three
    `EventLogRecord` fields.
    """
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        assert set(record) == {"event_type", "timestamp", "payload"}
        records.append(record)
    return records


def _drain(events: "queue.Queue[EventLogRecord]") -> list[EventLogRecord]:
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except queue.Empty:
            break
    return items


# Filesystem-watcher notifications are asynchronous relative to the
# filesystem operation that triggers them (unlike PtyOutputCapture, whose
# completion `_drain` above can rely on having already been `join()`-ed),
# so tests below poll for a matching record rather than draining once
# immediately after the triggering operation. 5s comfortably exceeds the
# platform-backend latencies observed in practice (well under 100ms on
# both Linux inotify and macOS FSEvents) without making a genuinely
# missing event slow to detect as a test failure.
_WATCHER_EVENT_TIMEOUT = 5.0


def _wait_until_file_change(
    events: "queue.Queue[EventLogRecord]",
    predicate,
    timeout: float = _WATCHER_EVENT_TIMEOUT,
) -> EventLogRecord:
    """Polls `events`, returning the first drained record matching
    `predicate`. Non-matching records are discarded (they're incidental
    platform-backend noise irrelevant to the specific change each test
    below waits for -- e.g. some backends report a spurious extra
    `modified` alongside a file's own `created` event). Raises
    `AssertionError` if `timeout` elapses with no match.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if predicate(record):
            return record
    raise AssertionError(f"timed out after {timeout}s waiting for a matching EventLogRecord")


def test_monitor_init_stores_workdir_and_run_id(tmp_path: Path):
    monitor = Monitor(tmp_path, "run-123")
    assert monitor.workdir == tmp_path
    assert monitor.run_id == "run-123"


def test_pty_capture_records_mock_adapter_output_with_iso8601_timestamps(tmp_path: Path):
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    capture = PtyOutputCapture(tmp_path, Mock_Adapter(), "do the thing", events)

    capture.start()
    capture.join(timeout=10)

    assert not capture.is_alive()
    assert capture.error is None

    records = _drain(events)
    assert records, "expected at least one captured output chunk"

    combined = ""
    for record in records:
        assert record.event_type == "output"
        # Must be parseable ISO-8601, matching AgentEvent's own timestamp
        # format (adapters/base.py).
        datetime.fromisoformat(record.timestamp)
        assert record.payload
        combined += record.payload

    # Mock_Adapter yields 3 "output" events (step=0,1,2) followed by one
    # "final_message" event (see adapters/mock.py); every one of those
    # payloads should have reached the pty, in order, regardless of how
    # the OS happened to chunk the underlying reads.
    expected_markers = ["step=0", "step=1", "step=2", "simulated final message"]
    positions = [combined.index(marker) for marker in expected_markers]
    assert positions == sorted(positions)


def test_pty_capture_stores_adapter_error_without_raising(tmp_path: Path):
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    capture = PtyOutputCapture(tmp_path, _FailingAdapter(), "do the thing", events)

    capture.start()
    capture.join(timeout=10)

    assert not capture.is_alive()
    assert isinstance(capture.error, AdapterError)
    assert "boom" in str(capture.error)


def test_pty_capture_gives_real_subprocess_a_working_tty(tmp_path: Path):
    """Confirms the Antigravity_Adapter design constraint this thread must
    satisfy: a real subprocess spawned inside `launch()` with no explicit
    stdout redirection inherits a pty as its stdio (its own `isatty()`
    check passes), without the adapter allocating a pty of its own.
    """
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    capture = PtyOutputCapture(tmp_path, _SubprocessAdapter(), "do the thing", events)

    capture.start()
    capture.join(timeout=10)

    assert not capture.is_alive()
    assert capture.error is None

    combined = "".join(record.payload for record in _drain(events))
    assert "subprocess-isatty=True" in combined


def test_monitor_start_pty_capture_wires_workdir_through_to_adapter(tmp_path: Path):
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    monitor = Monitor(tmp_path, "run-abc")

    capture = monitor._start_pty_capture(Mock_Adapter(), "do the thing", events)
    capture.join(timeout=10)

    assert capture.error is None
    combined = "".join(record.payload for record in _drain(events))
    assert str(tmp_path) in combined


def test_inotify_watcher_records_file_create(tmp_path: Path):
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    watcher = FileChangeWatcher(tmp_path, events)
    watcher.start()
    try:
        target = tmp_path / "created.txt"
        target.write_text("hello")

        record = _wait_until_file_change(
            events, lambda r: r.event_type == "file_change" and str(target) in r.payload
        )
        assert record.event_type == "file_change"
        datetime.fromisoformat(record.timestamp)  # parseable ISO-8601
        assert str(target) in record.payload
    finally:
        watcher.stop()
        watcher.join(timeout=10)

    assert watcher.error is None


def test_inotify_watcher_records_file_modify(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("original content")

    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    watcher = FileChangeWatcher(tmp_path, events)
    watcher.start()
    try:
        # Give the watcher a moment to finish starting up before the
        # triggering write, so this modify isn't racing the watcher's own
        # startup (a plain sleep is acceptable here: correctness is
        # asserted below via polling with a real timeout, not via this
        # delay's exact duration).
        time.sleep(0.3)
        target.write_text("modified content")

        record = _wait_until_file_change(
            events, lambda r: r.event_type == "file_change" and str(target) in r.payload
        )
        assert record.event_type == "file_change"
        datetime.fromisoformat(record.timestamp)
        assert str(target) in record.payload
    finally:
        watcher.stop()
        watcher.join(timeout=10)

    assert watcher.error is None


def test_inotify_watcher_records_file_delete(tmp_path: Path):
    target = tmp_path / "to_delete.txt"
    target.write_text("will be deleted")

    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    watcher = FileChangeWatcher(tmp_path, events)
    watcher.start()
    try:
        time.sleep(0.3)
        target.unlink()

        record = _wait_until_file_change(
            events, lambda r: r.event_type == "file_change" and str(target) in r.payload
        )
        assert record.event_type == "file_change"
        datetime.fromisoformat(record.timestamp)
        assert str(target) in record.payload
    finally:
        watcher.stop()
        watcher.join(timeout=10)

    assert watcher.error is None


def test_inotify_watcher_stop_terminates_promptly(tmp_path: Path):
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    watcher = FileChangeWatcher(tmp_path, events)
    watcher.start()
    time.sleep(0.2)
    assert watcher.is_alive()

    watcher.stop()
    watcher.join(timeout=10)

    assert not watcher.is_alive()
    assert watcher.error is None


def test_inotify_watcher_signals_once_its_watch_is_established(tmp_path: Path):
    """`wait_until_watching()` is what lets `supervise` launch the adapter
    only after the watch is live: once it returns, a change made with no
    settling delay at all is still captured.
    """
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    watcher = FileChangeWatcher(tmp_path, events)
    watcher.start()
    try:
        assert watcher.wait_until_watching(timeout=10) is True
        assert watcher.error is None

        target = tmp_path / "immediately.txt"
        target.write_text("content")

        record = _wait_until_file_change(
            events, lambda r: r.event_type == "file_change" and str(target) in r.payload
        )
        assert str(target) in record.payload
    finally:
        watcher.stop()
        watcher.join(timeout=10)

    assert watcher.error is None


def test_monitor_start_inotify_watcher_wires_workdir_through(tmp_path: Path):
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    monitor = Monitor(tmp_path, "run-abc")

    watcher = monitor._start_inotify_watcher(events)
    try:
        target = tmp_path / "wired.txt"
        target.write_text("content")

        record = _wait_until_file_change(
            events, lambda r: r.event_type == "file_change" and str(target) in r.payload
        )
        assert str(tmp_path) in record.payload
    finally:
        watcher.stop()
        watcher.join(timeout=10)

    assert watcher.error is None


def test_supervise_writes_timestamp_ordered_event_log_from_both_producers(tmp_path: Path):
    """Requirements 6.3/6.4: one Event_Log at `<run_dir>/events.jsonl`,
    containing both producers' events, ordered by timestamp, and nothing
    from any other source.
    """
    monitor = Monitor(tmp_path, "run-merge-1")

    log_path = monitor.supervise(_FileTouchingAdapter(), "do the thing")

    assert log_path == tmp_path / ".vaultshield" / "runs" / "run-merge-1" / EVENT_LOG_FILENAME
    assert log_path.exists()

    records = _read_event_log(log_path)
    assert records, "expected a non-empty Event_Log"

    event_types = {record["event_type"] for record in records}
    assert event_types == {"output", "file_change"}, (
        "expected records from both the PTY producer and the file-change producer, "
        f"got {event_types}"
    )

    timestamps = [datetime.fromisoformat(record["timestamp"]) for record in records]
    assert timestamps == sorted(timestamps), "Event_Log is not ordered by timestamp"

    combined_output = "".join(
        record["payload"] for record in records if record["event_type"] == "output"
    )
    assert "fake agent output" in combined_output

    file_changes = [record for record in records if record["event_type"] == "file_change"]
    assert any("agent_created.txt" in record["payload"] for record in file_changes)


def test_supervise_honours_explicit_run_dir(tmp_path: Path):
    """Task 8.2 passes the canonical Run_Directory in explicitly rather
    than relying on the default derived from `workdir`/`run_id`.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    run_dir = tmp_path / "elsewhere" / "run-explicit"
    monitor = Monitor(workdir, "run-explicit")

    log_path = monitor.supervise(Mock_Adapter(), "do the thing", run_dir=run_dir)

    assert log_path == run_dir / EVENT_LOG_FILENAME
    assert _read_event_log(log_path)
    assert not (workdir / ".vaultshield").exists()


def test_supervise_appends_to_an_existing_event_log(tmp_path: Path):
    """Requirement 6.3's "append-only": a second supervise call into the
    same Run_Directory adds lines, never truncating or rewriting the ones
    already there.
    """
    monitor = Monitor(tmp_path, "run-append")

    first_path = monitor.supervise(Mock_Adapter(), "first prompt")
    first_lines = first_path.read_text(encoding="utf-8").splitlines()
    assert first_lines

    second_path = monitor.supervise(Mock_Adapter(), "second prompt")
    second_lines = second_path.read_text(encoding="utf-8").splitlines()

    assert second_path == first_path
    assert second_lines[: len(first_lines)] == first_lines
    assert len(second_lines) > len(first_lines)


def test_supervise_writes_event_log_before_reraising_adapter_error(tmp_path: Path):
    """A failed Run still leaves its captured evidence on disk: the
    Event_Log is written first, then the adapter's error propagates.
    """
    monitor = Monitor(tmp_path, "run-failed")

    with pytest.raises(AdapterError, match="boom"):
        monitor.supervise(_FailsAfterOutputAdapter(), "do the thing")

    log_path = tmp_path / ".vaultshield" / "runs" / "run-failed" / EVENT_LOG_FILENAME
    records = _read_event_log(log_path)
    combined = "".join(record["payload"] for record in records)
    assert "output-before-failure" in combined


def test_merge_events_drains_everything_and_sorts_by_timestamp():
    """The merge step is lossless and timestamp-ordered, and stable for
    records sharing one timestamp (Property 9's relative-order guarantee).
    """
    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    later = EventLogRecord("output", "2024-01-01T00:00:02+00:00", "later output")
    tie_first = EventLogRecord("output", "2024-01-01T00:00:01+00:00", "tie A")
    tie_second = EventLogRecord("file_change", "2024-01-01T00:00:01+00:00", "tie B")
    earliest = EventLogRecord("file_change", "2024-01-01T00:00:00+00:00", "earliest change")
    for record in (later, tie_first, tie_second, earliest):
        events.put(record)

    merged = _merge_events(events)

    assert merged == [earliest, tie_first, tie_second, later]
    assert events.empty(), "merge step left records behind in the buffer"


def test_append_event_log_keeps_one_record_per_line_for_multiline_payloads(tmp_path: Path):
    """Terminal output routinely contains newlines; a record must still
    occupy exactly one JSONL line, with the payload round-tripping intact.
    """
    log_path = tmp_path / "nested" / EVENT_LOG_FILENAME
    payload = "first line\nsecond line\r\n\ttabbed"

    _append_event_log(
        log_path,
        [
            EventLogRecord("output", "2024-01-01T00:00:00+00:00", payload),
            EventLogRecord("file_change", "2024-01-01T00:00:01+00:00", "created: a.txt"),
        ],
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["payload"] == payload


# --- F-2: any agent exception must cross the thread boundary -----------------
#
# `PtyOutputCapture` drives `adapter.launch()` on its own thread. `run()` is the
# top of that thread's stack, so anything escaping it goes to
# `threading.excepthook` and is LOST: the joining caller in `supervise` sees a
# clean join, writes the Event_Log, finds `capture.error is None`, and reports a
# successful Run.
#
# Before the fix only `AdapterError` was captured, so an agent that died with any
# other exception — a `RuntimeError` from its own code, a `KeyError` while parsing
# a response — was indistinguishable from one that finished. The Run was reported
# as successful with a truncated transcript and (in the pipeline) an empty diff,
# which is the worst possible failure mode: a reviewer sees a green Run and
# approves it.


class _CrashingAdapter(Agent_Adapter):
    """Emits one normal event, then dies with a non-AdapterError exception."""

    def launch(self, prompt, workdir):
        yield AgentEvent(
            type="output",
            timestamp="2026-01-01T00:00:00+00:00",
            payload="one normal event before the crash",
        )
        raise RuntimeError("simulated agent crash")


class _AdapterErroringAdapter(Agent_Adapter):
    """Emits one normal event, then raises `AdapterError`."""

    def launch(self, prompt, workdir):
        yield AgentEvent(
            type="output",
            timestamp="2026-01-01T00:00:00+00:00",
            payload="one normal event before the adapter error",
        )
        raise AdapterError("simulated adapter failure")


class TestAgentExceptionsFailTheRun:
    """Both exception families must fail the Run, and neither may be silent."""

    def test_a_runtime_error_from_the_agent_fails_the_run(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(RuntimeError, match="simulated agent crash"):
            Monitor(tmp_path, "20260101-000000-aaaaaaaaaaaaaaaa").supervise(
                _CrashingAdapter(), "prompt", run_dir=run_dir
            )

    def test_an_adapter_error_from_the_agent_fails_the_run(self, tmp_path):
        """The case that already worked; asserted alongside so a future change
        cannot fix one family by breaking the other."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(AdapterError, match="simulated adapter failure"):
            Monitor(tmp_path, "20260101-000000-bbbbbbbbbbbbbbbb").supervise(
                _AdapterErroringAdapter(), "prompt", run_dir=run_dir
            )

    @pytest.mark.parametrize(
        "adapter_class,marker",
        [
            (_CrashingAdapter, "one normal event before the crash"),
            (_AdapterErroringAdapter, "one normal event before the adapter error"),
        ],
    )
    def test_the_partial_transcript_survives_the_failure(
        self, tmp_path, adapter_class, marker
    ):
        """The Event_Log is written BEFORE the exception is re-raised, which is
        what makes the crash diagnosable rather than merely reported."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises((RuntimeError, AdapterError)):
            Monitor(tmp_path, "20260101-000000-cccccccccccccccc").supervise(
                adapter_class(), "prompt", run_dir=run_dir
            )

        log = run_dir / "events.jsonl"
        assert log.is_file(), "no Event_Log was written for the failed Run"
        assert marker in log.read_text(), (
            "the event the agent emitted before failing is missing from the "
            "Event_Log, so the transcript does not show how far it got"
        )

    def test_a_crash_is_never_reported_as_a_successful_run(self, tmp_path):
        """The regression stated directly: `supervise` must not return.

        Returning a log path here is exactly the false success F-2 describes.
        """
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        returned = None
        try:
            returned = Monitor(
                tmp_path, "20260101-000000-dddddddddddddddd"
            ).supervise(_CrashingAdapter(), "prompt", run_dir=run_dir)
        except RuntimeError:
            pass

        assert returned is None, (
            "supervise() returned normally for a crashed agent: the Run would be "
            "reported as successful"
        )
