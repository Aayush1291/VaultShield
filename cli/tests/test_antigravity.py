"""Unit tests for Task 20.1 (`Antigravity_Adapter` argv construction and
its reliance on the Monitor's PTY) and Task 20.2 (the clean-exit/
empty-output failure check) in `cli/vaultshield/adapters/antigravity.py`.

Scoped to those tasks' three requirements: Requirement 16.1 (invoke `agy`
as `agy -p "<prompt>" --non-interactive`), Requirement 16.2 (rely on the
Monitor's PTY wrapper to satisfy the `agy` process's `isatty()` check) and
Requirement 16.3 (a clean exit that produced empty output is a failed Run,
not a successful one). Property 25 is Task 20.3's scope, so the property
itself is not asserted across generated inputs here — these are fixed
examples of each branch.

The `agy` CLI is not installed on a dev machine, so the tests that need a
real process substitute a stand-in executable (`_install_stub_agy`) for
`ANTIGRAVITY_EXECUTABLE` and run the real adapter, the real
`subprocess.Popen` call and — for the `isatty()` check — the real
`PtyOutputCapture` against it. Nothing about the adapter itself is mocked
or stubbed: what varies is only which binary gets executed.
"""

from __future__ import annotations

import queue
from pathlib import Path

import pytest

from vaultshield.adapters.antigravity import (
    ANTIGRAVITY_EXECUTABLE,
    Antigravity_Adapter,
    build_antigravity_argv,
    captured_output_is_empty,
)
from vaultshield.adapters.base import AdapterError
from vaultshield.monitor import EventLogRecord, PtyOutputCapture

# Bound on joining the `PtyOutputCapture` thread in the pty test below.
# The stand-in binary prints three lines and exits, so the thread finishes
# almost immediately; this only stops a genuine hang from stalling the
# suite indefinitely.
_CAPTURE_JOIN_TIMEOUT = 30.0


def _install_stub_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Writes an executable stand-in for `agy` and points
    `ANTIGRAVITY_EXECUTABLE` at it, returning its path.

    `body` is POSIX shell, so the stub can report `[ -t fd ]` per fd and
    pick its own exit status. Shell rather than Python because a `#!`
    shebang cannot name an interpreter whose path contains a space, and
    this repository's checkout path does.

    The stub ignores its own argv — the mandated `-p` /
    `--non-interactive` flags mean nothing to it — which is fine, because
    what these tests exercise is how the adapter spawns a process, not how
    `agy` parses its options.
    """
    stub = tmp_path / "agy-stub"
    stub.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(
        "vaultshield.adapters.antigravity.ANTIGRAVITY_EXECUTABLE", str(stub)
    )
    return stub


def _drain(events: "queue.Queue[EventLogRecord]") -> list[EventLogRecord]:
    items: list[EventLogRecord] = []
    while True:
        try:
            items.append(events.get_nowait())
        except queue.Empty:
            return items


def test_argv_is_exactly_what_requirement_16_1_mandates():
    """`agy -p "<prompt>" --non-interactive`, in that element order, with
    the prompt as one argv element rather than a shell-quoted string.
    """
    assert build_antigravity_argv("add a health check endpoint") == [
        "agy",
        "-p",
        "add a health check endpoint",
        "--non-interactive",
    ]
    assert ANTIGRAVITY_EXECUTABLE == "agy"


def test_argv_carries_prompt_verbatim_without_quoting():
    """A prompt containing quotes, spaces and newlines is passed through
    untouched: no shell is involved, so nothing needs escaping.
    """
    prompt = 'fix the "user  name" bug\nand add a test'
    assert build_antigravity_argv(prompt)[2] == prompt


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t "])
def test_empty_prompt_is_rejected_before_any_process_starts(
    prompt: str, tmp_path: Path
):
    """An empty or whitespace-only prompt cannot form a valid invocation,
    so `launch` raises `AdapterError` at the call itself — per `base.py`'s
    contract, before any agent process exists. `launch` deliberately is
    not a generator function, which is what makes the raise happen here
    rather than at first iteration.
    """
    with pytest.raises(AdapterError, match="prompt is empty"):
        build_antigravity_argv(prompt)

    with pytest.raises(AdapterError, match="prompt is empty"):
        Antigravity_Adapter().launch(prompt, str(tmp_path))


def test_missing_executable_is_reported_as_adapter_error(tmp_path: Path):
    """No `agy` on PATH is an invocation that could not be constructed, so
    it surfaces as `AdapterError` (naming the executable) rather than as a
    raw `FileNotFoundError` the caller would have to know to catch.
    """
    with pytest.raises(AdapterError, match="cannot start"):
        Antigravity_Adapter().launch("do the thing", str(tmp_path))


def test_agent_process_inherits_the_monitors_pty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Requirement 16.2: run under `PtyOutputCapture`, the spawned process
    sees a terminal on all three of its own stdio fds — without the
    adapter allocating a pty itself.

    The stub reports its own `isatty()` per fd on stdout; those bytes reach
    the shared event queue only by travelling through the pty the Monitor
    set up, so the assertions cover both halves of the mechanism: the
    child inherited the pty, and its output is captured by the Monitor
    rather than re-yielded by the adapter.
    """
    _install_stub_agy(
        tmp_path,
        monkeypatch,
        'if [ -t 0 ]; then echo "stdin-isatty=yes"; else echo "stdin-isatty=no"; fi\n'
        'if [ -t 1 ]; then echo "stdout-isatty=yes"; else echo "stdout-isatty=no"; fi\n'
        'if [ -t 2 ]; then echo "stderr-isatty=yes"; else echo "stderr-isatty=no"; fi\n',
    )

    events: "queue.Queue[EventLogRecord]" = queue.Queue()
    capture = PtyOutputCapture(tmp_path, Antigravity_Adapter(), "do the thing", events)
    capture.start()
    capture.join(timeout=_CAPTURE_JOIN_TIMEOUT)

    assert not capture.is_alive()
    assert capture.error is None

    captured = "".join(record.payload for record in _drain(events))
    assert "stdin-isatty=yes" in captured
    assert "stdout-isatty=yes" in captured
    assert "stderr-isatty=yes" in captured


def test_nonzero_exit_yields_an_error_event_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`base.py`'s contract: a failed process is never reported as a
    successful Run. The exit status goes into the event stream first (so
    the Monitor writes it to the Event_Log) and only then fails the Run by
    raising.
    """
    _install_stub_agy(tmp_path, monkeypatch, "exit 3\n")

    collected = []
    with pytest.raises(AdapterError, match="status 3"):
        for event in Antigravity_Adapter().launch("do the thing", str(tmp_path)):
            collected.append(event)

    assert [event["type"] for event in collected] == ["error"]
    assert "status 3" in collected[0]["payload"]
    assert collected[0]["timestamp"]


def test_clean_exit_with_output_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A process that did its job and exited 0 produces no `"error"` event
    and no `AdapterError`.

    Asserted as "no error", not as "no events at all", so it still holds
    once Task 20.2 adds Requirement 16.3's exit-0 empty-output check —
    which fails only the empty case, never this one.
    """
    _install_stub_agy(tmp_path, monkeypatch, 'echo "done: added the endpoint"\n')

    events = list(Antigravity_Adapter().launch("do the thing", str(tmp_path)))

    assert all(event["type"] != "error" for event in events)


def test_captured_output_is_yielded_as_output_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The output tap that makes Requirement 16.3's check possible turns
    the agent's writes into `"output"` events — stdout and stderr merged
    into one chronological stream, since one terminal is one stream.

    This is also what keeps the Event_Log complete under the Monitor: the
    child's bytes no longer reach the Monitor's pty directly, so they get
    there by being yielded here and written back out by `PtyOutputCapture`.
    """
    _install_stub_agy(
        tmp_path,
        monkeypatch,
        'echo "step one"\n'
        'echo "warming up" >&2\n'
        'printf "no trailing newline"\n',
    )

    events = list(Antigravity_Adapter().launch("do the thing", str(tmp_path)))

    assert [event["type"] for event in events] == ["output", "output", "output"]
    assert [event["payload"] for event in events] == [
        "step one",
        "warming up",
        "no trailing newline",
    ]
    assert all(event["timestamp"] for event in events)


def test_clean_exit_with_empty_output_yields_an_error_event_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Requirement 16.3: exit code 0 is not evidence of a usable result.
    A process that wrote nothing at all fails the Run — the reason goes
    into the event stream first (so the Monitor records it), then the
    `AdapterError` fails the Run at the call site.
    """
    _install_stub_agy(tmp_path, monkeypatch, "exit 0\n")

    collected = []
    with pytest.raises(AdapterError, match="produced empty output"):
        for event in Antigravity_Adapter().launch("do the thing", str(tmp_path)):
            collected.append(event)

    assert [event["type"] for event in collected] == ["error"]
    assert "status 0" in collected[0]["payload"]


def test_clean_exit_with_whitespace_only_output_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """design.md spells Requirement 16.3's "empty output" as "empty or
    whitespace-only", so blank lines and spaces count as nothing: they
    leave a reviewer exactly as much to read as no output at all.
    """
    _install_stub_agy(tmp_path, monkeypatch, 'printf "   \\n\\t\\n\\n"\n')

    with pytest.raises(AdapterError, match="produced empty output"):
        list(Antigravity_Adapter().launch("do the thing", str(tmp_path)))


def test_nonzero_exit_is_reported_ahead_of_empty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Both post-exit failures can hold at once (a crash that printed
    nothing). The exit status is the more actionable diagnosis, so it is
    the one reported — the empty-output branch is reached only on a clean
    exit.
    """
    _install_stub_agy(tmp_path, monkeypatch, "exit 4\n")

    with pytest.raises(AdapterError, match="status 4"):
        list(Antigravity_Adapter().launch("do the thing", str(tmp_path)))


@pytest.mark.parametrize(
    "captured_lines, expected",
    [
        ([], True),
        ([""], True),
        (["", "   ", "\t"], True),
        (["done"], False),
        (["", "  ", "done: added the endpoint"], False),
    ],
)
def test_empty_output_verdict(captured_lines: list[str], expected: bool):
    """The single definition of "empty output" this adapter has, exercised
    directly: any visible character on either stream is enough to make a
    clean exit a successful Run.
    """
    assert captured_output_is_empty(captured_lines) is expected
