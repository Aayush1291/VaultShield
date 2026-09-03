"""Antigravity_Adapter: the Agent_Adapter implementation for the
Antigravity (`agy`) CLI agent.

Implements the `Antigravity_Adapter` concrete adapter described in
design.md's "Agent_Adapter interface and implementations (`adapters/`)"
section (Task 20.1: the invocation; Task 20.2: the clean-exit/empty-output
failure check), built on the `Agent_Adapter` ABC, `AgentEvent` TypedDict
and `AdapterError` exception defined in `base.py` (Task 6.1). Wiring
`--agent=antigravity` into `run`'s adapter dispatch table is Task 20.4's
own concern, not this module's.

Covers Requirement 16.1: "WHEN `vaultshield run --agent=antigravity`
executes, THE Antigravity_Adapter SHALL invoke the `agy` CLI using
`agy -p "<prompt>" --non-interactive`."
And Requirement 16.2: "THE Antigravity_Adapter SHALL rely on the Monitor's
PTY wrapper to satisfy the `agy` process's `isatty()` check."
And Requirement 16.3 (Task 20.2): "IF the `agy` process exits cleanly but
produces empty output, THEN THE Antigravity_Adapter SHALL report a failure
rather than a successful Run."

design.md's own bullet for this adapter: "builds `agy -p "<prompt>"
--non-interactive`. Relies entirely on the Monitor's PTY wrapper to make
`isatty()` return true for the `agy` process (no separate pty allocation
in the adapter itself). After the process exits, if the exit code is 0 but
the captured output is empty or whitespace-only, raises `AdapterError`
(reported as a failed Run) instead of reporting success."

### Requirements 16.2 and 16.3 pull in opposite directions

Task 20.1 deferred exactly one question to this task: where "the captured
output" comes from. It is not a detail, because the two halves of this
adapter's contract are in direct tension:

- Requirement 16.2 needs the `agy` process's `isatty()` check to pass.
  `isatty()` is `tcgetattr()` succeeding, so it is true for exactly one
  kind of file: a terminal. A pipe is not one, which is why the sibling
  adapters' `stdout=PIPE` approach is ruled out here (Task 20.1 said as
  much, and Task 20.1's own test asserts a terminal on all three of the
  child's stdio fds).
- Requirement 16.3 needs this adapter to know whether the process wrote
  anything. Task 20.1 spawned the child with no stdio redirection at all,
  so its bytes went straight through the Monitor's pty slave — inherited
  via this process's fd 0/1/2 — to the Monitor's own reader thread. That
  reader is in a different component holding the only handle to the pty
  master; nothing in this module can see those bytes, and racing it for
  them would tear the Event_Log in half rather than duplicate it.

So "a terminal, whose other end this module can read" is the only shape
that satisfies both, and that means one pty per `launch()`:

    child stdout/stderr ──▶ tap pty slave
                            tap pty master ──▶ reader thread ──▶ "output"
                                                                 events
    child stdin ──────────▶ inherited fd 0 (the Monitor's pty slave)

`_open_output_tap` allocates the pair, `_spawn` points the child's stdout
AND stderr at the slave, and `_pump_output` drains the master on its own
thread, turning it into the `"output"` events `_stream_events` yields.
Consequences, all of them deliberate:

- **The child still sees a terminal on all three of its stdio fds.**
  fd 1 and fd 2 are the tap's slave; fd 0 is untouched, so it is whatever
  the Monitor put there — its pty slave — which is Requirement 16.2's
  mechanism operating exactly as written. `_spawn` passes no `stdin`
  argument for that reason, and must not gain one: `stdin=DEVNULL` would
  make `isatty(0)` false.
- **Every captured byte still reaches the Event_Log exactly once**, now
  via the yielded events rather than directly: `monitor.py`'s
  `PtyOutputCapture` writes each yielded payload to fd 1, i.e. through
  its own pty, where its reader timestamps it into the Event_Log. The
  double-emission `monitor.py` warns about (output captured once through
  an inherited pty and again as an event payload) does not arise, because
  the child's own writes no longer touch the Monitor's pty at all. This
  is the same arrangement `Codex_Adapter` and `Kiro_Adapter` already use;
  only the transport differs (a pty here, pipes there).
- **stdout and stderr arrive merged**, in the order the child wrote them,
  because one terminal is one stream. That is what Requirement 16.3's
  unqualified "output" and Property 25's "the captured output" describe,
  and what an interactive `agy` user sees; nothing here needs to tell the
  two apart, unlike Requirement 8.2's demand on `Codex_Adapter`.
- **`agy` inherits the Monitor's pty on fd 0, so a read from stdin would
  block.** Nothing ever writes to that pty's master, so an agent that
  decided to prompt interactively would wait forever. `--non-interactive`
  (Requirement 16.1's own argv) is what rules that out, and it is the
  agent's contract to honor it.

### Where this diverges from design.md, and why

design.md's bullet says "no separate pty allocation in the adapter
itself" — and its very next sentence requires the empty-output check.
Both cannot be literally true at once, for the reason above: with the
child's stdio wholly inherited, this module has nothing to check. The
resolution taken here keeps the *behavior* both requirements exist to
protect and pays for it in the mechanism note:

- the agent still gets a terminal on every stdio fd, so nothing about how
  `agy` behaves changes (Requirement 16.2's purpose, and Task 20.1's
  `test_agent_process_inherits_the_monitors_pty`, both hold);
- the Monitor's pty is still what carries the output into the Event_Log,
  and is still what satisfies `isatty()` on fd 0 (Requirement 16.2's
  letter, on the one fd left to it);
- a clean-but-silent Run now fails (Requirement 16.3).

The alternative — implementing the check against output this module never
sees — would make Requirement 16.3 either a no-op in production or a
guaranteed failure of every real Run, which is worse than a documented
mechanism divergence.

### Judgment calls this module has to make (none settled by design.md)

1. **`launch()` is not itself a generator function.** A function
   containing `yield` returns a generator whose body does not run until it
   is first iterated, which would defer the empty-prompt check to some
   arbitrary later moment. `launch` is a plain function that validates the
   prompt, opens the tap, spawns the process, and returns a separate
   generator (`_stream_events`) for the event stream — so `AdapterError`
   is raised at the `launch(...)` call itself, before any Sandbox process
   exists, per `base.py`'s contract. Same shape as `Codex_Adapter` and
   `Kiro_Adapter`.

2. **No credential is injected.** Requirement 8.1 names `CODEX_API_KEY`
   and Requirement 15.1 names `KIRO_API_KEY`, so those two adapters carry
   an `api_key` parameter and an env-injection step. Requirement 16.1
   names no credential for `agy` at all, so this adapter has neither —
   the child simply inherits this process's environment (`_spawn` passes
   no `env`), which is what keeps its PATH, HOME and terminal settings
   intact. Inventing an `ANTIGRAVITY_API_KEY` the spec never mentions
   would mean failing Runs over an unset variable nothing asked for. If a
   given `agy` build needs credentials, they are already in the
   environment `vaultshield run` inherits.

3. **A non-zero exit status yields an in-band `"error"` event AND then
   raises `AdapterError`.** Requirements 16.1/16.2 say nothing about exit
   codes, but `base.py`'s contract — binding on every adapter — is
   explicit that a Run is never reported successful when the process did
   not produce a usable result. Yielding the event first puts the exit
   status in the Event_Log (the Monitor writes each yielded payload
   through the pty before it ever sees the exception); raising afterwards
   is what fails the Run at its call site. Same treatment, same order, as
   both siblings.

4. **Captured lines are yielded verbatim as `type="output"` events, one
   per line.** `AgentEvent` has no "progress" variant, and `base.py`
   documents `"output"` as "ordinary progress/log output". Line-at-a-time
   rather than chunk-at-a-time because the Monitor appends its own
   newline per payload, so a chunk boundary landing mid-line would inject
   a newline the agent never printed. No payload carries a stream tag,
   which costs nothing here: the tap merges stdout and stderr before this
   module ever sees them, so there is no tag left to drop.

5. **Output post-processing is disabled on the tap** (`_disable_output_
   postprocessing`). A fresh pty rewrites `\n` as `\r\n` on the way out
   (and, on some platforms, expands tabs), so a tap left at its defaults
   would hand the Monitor text the agent did not write — and the Monitor's
   own pty would then apply the same rewrite a second time. Clearing
   `OPOST` makes the tap byte-transparent, so the Event_Log records
   exactly what today's inherited-stdio arrangement records.

6. **Emptiness is judged over the whole captured stream, and
   whitespace-only counts as empty.** Requirement 16.3 says "empty
   output" without naming a stream, design.md spells it "empty or
   whitespace-only", and the tap merges both streams anyway — so
   `captured_output_is_empty` asks one question of everything the process
   wrote. A run whose entire contribution was a blank line produced
   nothing for a reviewer to read, which is the case Requirement 16.3 and
   `base.py`'s "never conflate exit 0 with useful output" rule exist for.
   Note the deliberate contrast with `Kiro_Adapter`, whose Requirement
   15.3 asks for a *parseable final message* and therefore judges stdout
   alone: here, any visible byte from either stream is enough.

7. **The exit-status check runs before the emptiness check.** Both
   failures can hold at once (a crash that printed nothing), and the exit
   status is the more specific, more actionable diagnosis — "exited with
   status 1" tells the reviewer more than "produced empty output". So a
   non-zero exit raises with its own message and the emptiness check is
   reached only on a clean exit, which also keeps the `AdapterError`
   message unambiguous about which condition failed the Run. Mirrors
   `Kiro_Adapter`'s judgment call 10.

This module deliberately does not share `codex.py`/`kiro.py`'s streaming
helpers: design.md gives each adapter its own module, the transport here
is a pty rather than two pipes (one reader, not two; no stream labels),
and factoring the plumbing out would mean rewriting two already-working
adapters inside a task scoped to this one.
"""

from __future__ import annotations

import codecs
import os
import queue
import subprocess
import termios
import threading
from datetime import datetime, timezone
from typing import Iterator, Sequence

from vaultshield.adapters.base import (
    AdapterError,
    Agent_Adapter,
    AgentEvent,
    SandboxInvocation,
)

# The Antigravity CLI executable, resolved on PATH inside the environment
# `vaultshield run` runs in. Not configurable per Run: Requirement 16.1
# fixes the invocation. Read by `build_antigravity_argv` at call time
# (rather than baked into a module-level argv tuple) so a test can point
# it at a stand-in binary without reaching into the argv builder itself.
ANTIGRAVITY_EXECUTABLE = "agy"

# Bytes requested per `os.read()` against the tap's master fd. Only bounds
# how much of an already-arrived burst one call returns; chunk boundaries
# are invisible downstream, since `_pump_output` reassembles lines across
# them. Matches `monitor.py`'s own pty read size.
_READ_CHUNK_SIZE = 4096

# Safety-net timeout for joining the tap reader thread once the process
# has exited (or been killed) and every fd referencing the tap's slave is
# closed. The reader observes end-of-stream and returns almost immediately
# at that point; this bound only guards against an unexpected platform
# quirk turning that into an indefinite hang.
_TAP_READER_JOIN_TIMEOUT = 5.0


def build_antigravity_argv(prompt: str) -> list[str]:
    """Builds the exact `agy` invocation Requirement 16.1 mandates:
    `agy -p "<prompt>" --non-interactive`.

    Element order matches that requirement literally — `-p` and its value,
    then `--non-interactive` — rather than being normalized into some
    other flag order that would be equivalent to `agy` but no longer the
    command the spec names.

    `prompt` is passed as a single argv element rather than interpolated
    into a shell string, so no quoting/escaping of its content is needed
    (and no shell is involved at all) — the double quotes in Requirement
    16.1's rendering are shell notation for "one argument", which is what
    this list already expresses.

    Raises `AdapterError` for a missing/whitespace-only prompt: an empty
    prompt cannot form a valid invocation, and `base.py`'s contract wants
    that reported as an adapter-level error before any Sandbox process
    starts. Whitespace-only counts as empty because it would otherwise
    produce a syntactically valid command asking the agent to do nothing —
    the same rule both sibling adapters apply.

    Kept module-level and public so an exact-argv check can exercise
    invocation construction on its own, without spawning a process.
    """
    if not prompt.strip():
        raise AdapterError(
            "Antigravity_Adapter cannot construct a valid invocation: prompt "
            "is empty. Pass a non-empty --prompt."
        )
    return [ANTIGRAVITY_EXECUTABLE, "-p", prompt, "--non-interactive"]


def captured_output_is_empty(captured_lines: Sequence[str]) -> bool:
    """Requirement 16.3's test, and the single definition of "empty
    output" this module has: `True` when nothing the `agy` process wrote
    carries a visible character.

    Whitespace-only output counts as empty (judgment call 6 in the module
    docstring, and design.md's own "empty or whitespace-only" wording), so
    a process whose whole contribution was blank lines fails a Run exactly
    as one that printed nothing at all does.

    Judges everything captured, stdout and stderr alike — they arrive
    already merged through the tap, so there is no per-stream choice to
    make here (unlike `Kiro_Adapter._extract_final_message`, which
    deliberately looks at stdout only).

    Kept module-level and public so the verdict can be exercised directly,
    without spawning a process.
    """
    return not any(line.strip() for line in captured_lines)


class Antigravity_Adapter(Agent_Adapter):
    """Agent_Adapter implementation for the Antigravity (`agy`) CLI agent
    (Requirements 16.1, 16.2, 16.3).

    Takes no constructor arguments — unlike `Codex_Adapter` and
    `Kiro_Adapter`, there is no credential to override (judgment call 2 in
    the module docstring), and `run`'s dispatch table instantiates every
    adapter as `_AGENT_ADAPTERS[agent]()` anyway.

    Holds no state at all: each `launch()` builds its argv from scratch,
    opens its own output tap and spawns an independent process, so
    repeated calls on one instance are as independent as calls on two
    fresh instances.
    """

    def launch(self, prompt: str, workdir: str) -> Iterator[AgentEvent]:
        """Starts `agy -p "<prompt>" --non-interactive` in `workdir` with
        its stdout and stderr on a pty tap this adapter reads, and its
        stdin inherited from this process — the Monitor's pty, so the
        agent's `isatty()` check passes on all three fds (Requirement
        16.2) — and returns an iterator over the resulting `AgentEvent`s.

        The stream carries one `"output"` event per captured line as it
        arrives (the Monitor writes each payload through its own pty into
        the Event_Log), then, on either post-exit failure, one `"error"`
        event.

        Raises `AdapterError` — before any agent process is started, per
        `base.py`'s contract — when the prompt is empty, when the tap
        cannot be opened, or when the process cannot be spawned at all (no
        `agy` on PATH, unusable `workdir`). This method is intentionally
        not a generator function, so those checks run at the call itself
        rather than at first iteration (judgment call 1 in the module
        docstring).

        The returned iterator raises `AdapterError` too, at the end of the
        stream, for the two post-exit failures `_stream_events` documents:
        a non-zero exit status, and a clean exit that produced empty
        output (Requirement 16.3).
        """
        argv = build_antigravity_argv(prompt)
        tap = _open_output_tap()
        try:
            process = _spawn(argv, workdir, tap)
        except BaseException:
            # Nothing was started, so the tap has no reader and no writer;
            # closing both ends here is what keeps a failed `launch` from
            # leaking two fds per attempt.
            tap.close()
            raise

        # Hand the slave over to the child completely: while this process
        # still holds a copy, the tap can never reach end-of-stream, so the
        # reader below would keep waiting long after the agent exited.
        tap.close_slave()
        return _stream_events(process, tap)

    def sandbox_invocation(self, prompt: str, workdir: str) -> SandboxInvocation:
        """Describes `agy -p "<prompt>" --non-interactive` for in-Sandbox
        execution.

        Same argv as the host path: `build_antigravity_argv` remains the single
        definition of Requirement 16.1's invocation, and it raises
        `AdapterError` for an empty prompt before anything starts.

        `env` is empty. Requirement 16.1 names no credential for `agy` (this
        adapter deliberately invents none — see judgment call 2 in the module
        docstring), so there is nothing to export, and `SandboxInvocation`
        forbids copying the host environment in.

        Requirement 16.2's `isatty()` check is satisfied by the Sandbox rather
        than by this adapter: `Sandbox.exec_stream` allocates a pseudo-terminal
        for the process by default, so `agy` sees a TTY on its stdio without
        this adapter opening a tap of its own. That is why no `_OutputTap` is
        involved here — the pty now lives on the container side of the boundary.

        Requires the `agy` executable in the Sandbox image; the default
        `python:3.11` base image does not have it, so `--agent=antigravity`
        needs `--base-image` pointing at one that does.
        """
        return SandboxInvocation(argv=build_antigravity_argv(prompt), env={})


class _OutputTap:
    """The pty pair whose slave the `agy` process writes to and whose
    master this adapter reads — the mechanism that makes Requirements 16.2
    and 16.3 hold at the same time (see the module docstring).

    Owns nothing but two file descriptors and the bookkeeping to close
    each exactly once: `close_slave()` on the ordinary path (right after
    the spawn, so the child holds the only remaining reference) and
    `close()` from `_cleanup`/`launch`'s failure path. Both are idempotent
    so the two callers never have to coordinate, and `slave_fd`/`master_fd`
    read as `-1` once closed.
    """

    def __init__(self, master_fd: int, slave_fd: int) -> None:
        self.master_fd = master_fd
        self.slave_fd = slave_fd

    def close_slave(self) -> None:
        """Drops this process's reference to the pty slave. No-op if
        already dropped."""
        if self.slave_fd != -1:
            os.close(self.slave_fd)
            self.slave_fd = -1

    def close(self) -> None:
        """Drops both references. Called only once no reader can still be
        blocked on the master (see `_cleanup`)."""
        self.close_slave()
        if self.master_fd != -1:
            os.close(self.master_fd)
            self.master_fd = -1


def _open_output_tap() -> _OutputTap:
    """Allocates one pty pair per `launch()` and returns it as an
    `_OutputTap`, with output post-processing disabled so the tap is
    byte-transparent (judgment call 5 in the module docstring).

    An `OSError` here means the platform refused to allocate a pty (fd
    exhaustion, a container without `/dev/ptmx`), which is an invocation
    that could not be constructed — so it surfaces as `AdapterError`,
    before any process is started, exactly like a missing executable does.
    """
    try:
        master_fd, slave_fd = os.openpty()
    except OSError as exc:
        raise AdapterError(
            "Antigravity_Adapter cannot allocate a pseudo-terminal for the "
            f"agent's output: {exc}."
        ) from exc

    try:
        _disable_output_postprocessing(slave_fd)
    except BaseException:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    return _OutputTap(master_fd, slave_fd)


def _disable_output_postprocessing(slave_fd: int) -> None:
    """Clears `OPOST` on the tap's slave, so the terminal passes the
    agent's bytes through untouched instead of rewriting `\\n` as `\\r\\n`
    (and, on some platforms, expanding tabs).

    Applied to the slave — the terminal the child will write to — and to
    nothing else: the Monitor's pty on fd 0 keeps its own settings, since
    a tap has no business reconfiguring a terminal it does not own.

    A `termios` failure is not worth failing a Run over: the tap still
    works, output just arrives with `\\r\\n` line endings, which
    `_pump_output` strips anyway. So this is best-effort by design.
    """
    try:
        attributes = termios.tcgetattr(slave_fd)
        attributes[1] &= ~termios.OPOST  # index 1 is c_oflag
        termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
    except termios.error:
        pass


def _spawn(argv: list[str], workdir: str, tap: _OutputTap) -> subprocess.Popen[bytes]:
    """Starts the `agy` process in `workdir` with its stdout and stderr on
    `tap`'s pty slave — a terminal, so `isatty()` is true for both, and
    one this adapter can read the other end of — and its stdin left
    inherited from this process, i.e. the Monitor's pty slave for the
    duration of `launch()` (Requirement 16.2).

    The absence of a `stdin` argument is load-bearing and nothing may be
    added that redirects it: `stdin=DEVNULL` would make `isatty(0)` false.
    `env` is likewise omitted so the child inherits this process's
    environment unchanged (judgment call 2 in the module docstring).

    Both streams are pointed at the SAME slave fd, which is what merges
    them into one chronological stream (see the module docstring).

    Any `OSError` from the spawn itself — most plausibly no `agy` on PATH,
    or a `workdir` that does not exist or is not a directory — is an
    invocation that could not be constructed, so it surfaces as
    `AdapterError`. Nothing has been started at that point, so there is no
    process to clean up; `launch` closes the tap.
    """
    try:
        return subprocess.Popen(
            argv,
            cwd=workdir,
            stdout=tap.slave_fd,
            stderr=tap.slave_fd,
        )
    except OSError as exc:
        raise AdapterError(
            f"Antigravity_Adapter cannot start {argv[0]!r} in {workdir!r}: "
            f"{exc}. Check that {argv[0]!r} is installed and on PATH and that "
            "the working directory exists."
        ) from exc


def _stream_events(
    process: subprocess.Popen[bytes], tap: _OutputTap
) -> Iterator[AgentEvent]:
    """Yields one `"output"` event per line captured from the tap as it
    arrives, then the events that describe how the process ended.

    Two post-exit conditions fail the Run, each yielding an `"error"`
    event describing it and then raising `AdapterError` — judgment call 3
    in the module docstring explains why both, and in that order:

    - a non-zero exit status; or
    - a clean (status 0) exit whose captured output was empty or
      whitespace-only (Requirement 16.3). Judgment call 7 explains why
      this one is checked second.

    A clean exit WITH captured output is the only path on which the
    iterator ends normally, and is therefore the only one the caller
    reports as a successful Run.

    The `finally` block runs on every exit path, including the caller
    abandoning the iterator early (`GeneratorExit`), so the process is
    never left running and the tap's fds are always closed.
    """
    lines: "queue.Queue[str | None]" = queue.Queue()
    reader = _start_output_reader(tap, lines)
    captured_lines: list[str] = []

    try:
        # The reader pushes exactly one `None` sentinel when the tap
        # reaches end-of-stream, which happens once the child (and
        # anything it spawned) has dropped the slave.
        while True:
            line = lines.get()
            if line is None:
                break
            captured_lines.append(line)
            yield AgentEvent(
                type="output", timestamp=_now_iso8601(), payload=line
            )

        exit_status = process.wait()

        if exit_status != 0:
            failure = f"agy exited with status {exit_status}"
            yield AgentEvent(
                type="error", timestamp=_now_iso8601(), payload=failure
            )
            raise AdapterError(f"Antigravity_Adapter: {failure}")

        # Requirement 16.3: a clean exit is not evidence of a usable
        # result. Reached only when `exit_status == 0`, since the branch
        # above raises otherwise (judgment call 7).
        if captured_output_is_empty(captured_lines):
            failure = "agy exited cleanly (status 0) but produced empty output"
            yield AgentEvent(
                type="error", timestamp=_now_iso8601(), payload=failure
            )
            raise AdapterError(f"Antigravity_Adapter: {failure}")
    finally:
        _cleanup(process, reader, tap)


def _start_output_reader(
    tap: _OutputTap, sink: "queue.Queue[str | None]"
) -> threading.Thread:
    """Starts the daemon thread that drains the tap's master fd into
    `sink`.

    One thread, not two: the tap merges stdout and stderr into a single
    terminal, so there is only one stream to read — the deadlock-avoidance
    dance `Kiro_Adapter` needs for its two pipes does not arise here.
    """
    reader = threading.Thread(
        target=_pump_output,
        args=(tap.master_fd, sink),
        name="vaultshield-antigravity-output",
        daemon=True,
    )
    reader.start()
    return reader


def _pump_output(master_fd: int, sink: "queue.Queue[str | None]") -> None:
    """Reader-thread body: reassembles the tap's byte stream into lines,
    pushing each onto `sink`, then exactly one `None` sentinel on the way
    out.

    Reads with `os.read` and treats both of a pty's end-of-stream
    spellings as ordinary: `os.read` raises `OSError` (typically Linux,
    errno EIO) or returns `b""` (typically macOS/BSD) once every fd
    referencing the slave is closed. Neither is a failure worth
    reporting — the exit status is the signal that matters — which is the
    same reading `monitor.py`'s own pty reader takes.

    Chunk boundaries are not line boundaries, so lines are assembled
    across reads and any unterminated remainder is flushed at
    end-of-stream (an agent that ends without a trailing newline still
    contributes its last line). Decoding runs through an incremental
    decoder so a multi-byte character split across two reads survives
    intact, with `errors="replace"` so a stray non-UTF-8 byte in agent
    output can never abort a Run. Only line terminators are stripped
    (`\\r` included, in case `OPOST` could not be cleared); every other
    character reaches the Event_Log as the agent wrote it.

    The sentinel is pushed from a `finally` block so the consumer's loop
    can never hang waiting on a reader that died.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    try:
        while True:
            try:
                chunk = os.read(master_fd, _READ_CHUNK_SIZE)
            except OSError:
                break
            if not chunk:
                break
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, _, pending = pending.partition("\n")
                sink.put(line.rstrip("\r"))
    finally:
        pending += decoder.decode(b"", True)
        if pending:
            sink.put(pending.rstrip("\r"))
        sink.put(None)


def _cleanup(
    process: subprocess.Popen[bytes], reader: threading.Thread, tap: _OutputTap
) -> None:
    """Ensures the process is not left running and the tap's fds are
    closed.

    On the ordinary path the process has already exited and the reader has
    already finished, so every step here is a no-op. It matters when the
    caller abandons the iterator early: killing the process is what drops
    the last reference to the tap's slave, which lets the reader observe
    end-of-stream and return — so the kill precedes the join, and the
    join precedes closing the master fd nobody may still be reading.

    The child's stdin is this process's own inherited fd 0, which belongs
    to the Monitor and is deliberately left exactly as it was found.
    """
    if process.poll() is None:
        process.kill()
        process.wait()

    tap.close_slave()
    reader.join(timeout=_TAP_READER_JOIN_TIMEOUT)
    tap.close()


def _now_iso8601() -> str:
    """Returns the current UTC time as an ISO-8601 string, matching the
    `AgentEvent.timestamp` format documented in `base.py` and produced by
    `Mock_Adapter`, `Codex_Adapter` and `Kiro_Adapter`.
    """
    return datetime.now(timezone.utc).isoformat()
