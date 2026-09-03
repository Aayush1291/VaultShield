"""Codex_Adapter: the Agent_Adapter implementation for the Codex CLI agent.

Implements the `Codex_Adapter` concrete adapter described in design.md's
"Agent_Adapter interface and implementations (`adapters/`)" section
(Task 10.1), built on the `Agent_Adapter` ABC, `AgentEvent` TypedDict and
`AdapterError` exception defined in `base.py` (Task 6.1). Wiring
`--agent=codex` into `run`'s adapter dispatch table is Task 10.5's own
concern, not this module's.

Covers Requirement 8.1: "WHEN `vaultshield run --agent=codex` executes,
THE Codex_Adapter SHALL invoke the Codex CLI using
`codex exec "<prompt>" --full-auto --ephemeral` and SHALL authenticate
using the `CODEX_API_KEY` value."
And Requirement 8.2: "WHEN the Codex CLI process produces output, THE
Codex_Adapter SHALL parse standard output for the agent's final message
and SHALL treat standard error output as progress information rather than
failure."
And Requirement 8.3: "IF the Codex_Adapter cannot construct a valid
invocation of the Codex CLI, THEN THE Codex_Adapter SHALL report an
adapter-level error without starting a Sandbox process."

design.md's own bullet for this adapter: "builds
`codex exec "<prompt>" --full-auto --ephemeral`, injects `CODEX_API_KEY`
into the subprocess environment. Reads stdout to find the final message
(last non-empty stdout line, or a designated marker if the Codex CLI emits
one); treats all stderr content as progress/log output, never as a failure
signal by itself. If `prompt` is empty or `CODEX_API_KEY` is unset, raises
`AdapterError` before any Sandbox process is created."

That split matches the Codex CLI's own documented non-interactive
behavior: [`codex exec` streams progress to stderr and prints the final
agent message to stdout](https://developers.openai.com/codex/noninteractive)
(content rephrased for compliance with licensing restrictions), which is
why stdout is the stream parsed for a result and stderr is never read as a
failure signal.

### Why the subprocess's stdout/stderr are piped rather than inherited

`monitor.py`'s PTY-wrapper mechanism swaps this process's fd 0/1/2 onto a
pty slave for the duration of `launch()`, so a subprocess started here
with no redirection of its own would inherit that pty as its stdio — and
`monitor.py`'s own docstring flags the consequence: an adapter that spawns
a real subprocess AND yields `AgentEvent`s describing that subprocess's
output "would see that output captured twice ... it is for that future
adapter's own task to avoid double-emission if it matters." This IS that
task, and the two concerns resolve together: Requirement 8.2 requires
stdout and stderr to be told apart (one is parsed for the final message,
the other is progress), which is only possible with separate pipes, and
piping them is exactly what keeps the subprocess's own bytes out of the
pty. Every line therefore reaches the Event_Log exactly once, via the
`AgentEvent` payloads yielded below.

stdin is deliberately left inherited (i.e. the Monitor's pty), so the
Codex CLI still sees a terminal on fd 0 rather than a closed/empty pipe.

### Judgment calls this module has to make (none settled by design.md)

1. **`launch()` is not itself a generator function.** A function
   containing `yield` returns a generator whose body does not run until it
   is first iterated, which would defer Requirement 8.3's fail-fast
   validation to some arbitrary later moment. `launch` is therefore a
   plain function that validates the prompt and the API key, spawns the
   process, and returns a separate generator (`_stream_events`) for the
   event stream — so `AdapterError` is raised at the `launch(...)` call
   itself, before any agent process exists.

2. **stdout and stderr are drained by two reader threads feeding one
   queue.** Reading one stream to EOF before the other risks deadlocking
   on a full pipe buffer for whichever stream is left unread, and
   `codex exec` writes to both throughout a run. Two threads pushing onto
   a single `queue.Queue` keep both drained and preserve arrival order
   across streams, which is what lets `_stream_events` yield events as
   they occur rather than only after the process exits. (Same shape as
   `monitor.py`'s "two independent event producers feeding a single
   thread-safe buffer", for the same reason.)

3. **stderr lines are yielded verbatim as `type="output"` events.**
   `AgentEvent` has no "progress" variant, and `base.py` documents
   `"output"` as "ordinary progress/log output" — so progress on stderr
   belongs in that bucket, which is precisely Requirement 8.2's "rather
   than failure". The payload is not prefixed with a stream tag: the
   Monitor writes every payload through the pty into the Event_Log, and a
   terminal running `codex exec` interleaves both streams untagged, so
   tagging here would put text on the reviewer's terminal transcript that
   the real command never printed. The cost is that the Event_Log does
   not record which stream a given line came from; nothing in
   requirements.md or design.md asks it to.

4. **A non-zero exit status yields an in-band `"error"` event AND then
   raises `AdapterError`.** design.md's Codex bullet says nothing about
   exit codes — it only rules out treating stderr *content* as failure —
   but `base.py`'s contract is explicit that an adapter never reports a
   Run as successful when the process did not produce a usable result.
   Yielding the `"error"` event first puts the exit status in the
   Event_Log (the Monitor writes each yielded payload before it ever sees
   the exception), and raising afterwards is what makes the Run fail at
   its call site. Requirement 8.2 is untouched by this: the trigger is
   the exit status, never the presence of stderr content.

5. **A clean exit with no final message is NOT an error here.**
   design.md assigns the clean-exit-but-no-usable-output check to the
   Kiro_Adapter (Requirement 15.3) and the Antigravity_Adapter
   (Requirement 16.3), and deliberately not to this one; Requirement
   8.2's own wording is conditional ("WHEN the Codex CLI process produces
   output"). So an exit-0 run whose stdout was empty simply yields no
   `final_message` event, rather than failing a Run over a check this
   adapter was not given.

6. **`CODEX_API_KEY` is injected under exactly that name.** Requirement
   8.1 and design.md both name `CODEX_API_KEY`, so that is the variable
   set in the child environment. The real Codex CLI resolves credentials
   its own way (saved CLI auth, or its own documented env var), so a
   deployment may additionally need that variable set in the environment
   `vaultshield run` inherits — renaming or duplicating the spec's
   variable is not this task's call to make.

### Known limitation: multi-line final messages

design.md's extraction rule is "last non-empty stdout line", which this
module implements literally. A final agent message spanning several lines
therefore contributes only its last line as the `final_message` payload.
Nothing is lost from the Event_Log — every stdout line is also yielded as
an `"output"` event — and `_extract_final_message` is the single place a
better rule would go if a future task needs one. design.md's alternative,
"a designated marker if the Codex CLI emits one", stays unimplemented on
purpose: with the fixed argv Requirement 8.1 mandates (no `--json`),
`codex exec` writes a plain final message to stdout with no marker to key
off, so a marker branch here would be dead code guessing at a format that
does not exist.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from datetime import datetime, timezone
from typing import IO, Iterator

from vaultshield.adapters.base import (
    AdapterError,
    Agent_Adapter,
    AgentEvent,
    SandboxInvocation,
)

# The Codex CLI executable, resolved on PATH inside the environment
# `vaultshield run` runs in. Not configurable per Run: Requirement 8.1
# fixes the invocation, and Task 10.4's unit test asserts the exact argv.
CODEX_EXECUTABLE = "codex"

# Environment variable carrying the Codex credential, per Requirement 8.1
# ("SHALL authenticate using the `CODEX_API_KEY` value"). Read from this
# process's own environment at `launch()` time and injected into the
# subprocess environment — see judgment call 6 in the module docstring.
CODEX_API_KEY_ENV = "CODEX_API_KEY"

# Safety-net timeout for joining a stdout/stderr reader thread once the
# process has exited (or been killed) and its pipe is at EOF. Both threads
# return almost immediately at that point; this bound only guards against
# an unexpected platform quirk turning that into an indefinite hang — the
# same role `monitor.py`'s `_READER_JOIN_TIMEOUT` plays for its pty reader.
_STREAM_READER_JOIN_TIMEOUT = 5.0

# Stream labels used on the internal reader queue. Only `_STDOUT` is
# load-bearing (it selects the lines `_extract_final_message` considers);
# both streams' lines are yielded identically, per judgment call 3.
_STDOUT = "stdout"
_STDERR = "stderr"


def build_codex_argv(prompt: str) -> list[str]:
    """Builds the exact Codex CLI invocation Requirement 8.1 mandates:
    `codex exec "<prompt>" --full-auto --ephemeral`.

    `prompt` is passed as a single argv element rather than interpolated
    into a shell string, so no quoting/escaping of its content is needed
    (and no shell is involved at all) — the double quotes in Requirement
    8.1's and design.md's rendering are shell notation for "one argument",
    which is what this list already expresses.

    Raises `AdapterError` for a missing/whitespace-only prompt: an empty
    prompt cannot form a valid invocation, and Requirement 8.3 wants that
    reported as an adapter-level error. Whitespace-only counts as empty
    because it would otherwise produce a syntactically valid command that
    asks the agent to do nothing.

    Kept module-level and public so the exact-argv check (Task 10.4) and
    the fail-fast property test (Task 10.3) can exercise invocation
    construction on its own, without spawning a process.
    """
    if not prompt.strip():
        raise AdapterError(
            "Codex_Adapter cannot construct a valid invocation: prompt is "
            "empty. Pass a non-empty --prompt."
        )
    return [CODEX_EXECUTABLE, "exec", prompt, "--full-auto", "--ephemeral"]


class Codex_Adapter(Agent_Adapter):
    """Agent_Adapter implementation for the Codex CLI agent
    (Requirements 8.1-8.3).

    `api_key` overrides the `CODEX_API_KEY` value that would otherwise be
    read from this process's environment at `launch()` time. It exists so
    a caller (and Task 10.2/10.3's property tests) can supply a credential
    without mutating the process environment; `None` — the default, and
    what `run`'s dispatch table produces, since it instantiates adapters
    with no arguments — means "read `CODEX_API_KEY` from the environment".

    Reading the environment at `launch()` rather than at construction
    matters for the pipeline in `cli.py`, which instantiates the adapter
    early (before the Sandbox exists) and launches it later: the check
    then reflects the credential state at the moment the invocation is
    actually built.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def launch(self, prompt: str, workdir: str) -> Iterator[AgentEvent]:
        """Starts `codex exec "<prompt>" --full-auto --ephemeral` in
        `workdir` and returns an iterator of `AgentEvent`s: one `"output"`
        event per captured stdout/stderr line as it arrives, then one
        `"final_message"` event carrying the message parsed from stdout.

        Raises `AdapterError` — before any agent process is started, per
        Requirement 8.3 — when the prompt is empty, when `CODEX_API_KEY`
        is unset/blank, or when the process cannot be spawned at all (no
        `codex` on PATH, unusable `workdir`). This method is intentionally
        not a generator function, so those checks run at the call itself
        rather than at first iteration (judgment call 1 in the module
        docstring).
        """
        argv = build_codex_argv(prompt)
        env = self._build_env()
        process = _spawn(argv, env, workdir)
        return _stream_events(process)

    def _build_env(self) -> dict[str, str]:
        """Returns the subprocess environment: this process's environment
        with `CODEX_API_KEY` injected (Requirement 8.1).

        Raises `AdapterError` when no usable credential is available —
        design.md: "If `prompt` is empty or `CODEX_API_KEY` is unset,
        raises `AdapterError` before any Sandbox process is created." A
        blank/whitespace-only value is treated as unset: it authenticates
        nothing, and failing here gives a clear adapter-level error
        instead of an opaque failure from the Codex CLI later.

        The rest of the environment is inherited so the Codex CLI keeps
        the PATH, HOME, and terminal settings it needs to run at all.
        """
        api_key = (
            self._api_key
            if self._api_key is not None
            else os.environ.get(CODEX_API_KEY_ENV, "")
        )
        if not api_key.strip():
            raise AdapterError(
                "Codex_Adapter cannot construct a valid invocation: "
                f"{CODEX_API_KEY_ENV} is not set. Export {CODEX_API_KEY_ENV} "
                "before running --agent=codex."
            )

        env = dict(os.environ)
        env[CODEX_API_KEY_ENV] = api_key
        return env

    def _resolve_api_key(self) -> str:
        """The API key for this Run, or `AdapterError` if there isn't one.

        Split out of `_build_env` so `sandbox_invocation` can reuse the
        resolution and its error message without also inheriting
        `_build_env`'s copy of `os.environ`, which must not cross into the
        container.
        """
        api_key = (
            self._api_key
            if self._api_key is not None
            else os.environ.get(CODEX_API_KEY_ENV, "")
        )
        if not api_key.strip():
            raise AdapterError(
                "Codex_Adapter cannot construct a valid invocation: "
                f"{CODEX_API_KEY_ENV} is not set. Export {CODEX_API_KEY_ENV} "
                "before running --agent=codex."
            )
        return api_key

    def sandbox_invocation(self, prompt: str, workdir: str) -> SandboxInvocation:
        """Describes the Codex CLI invocation for in-Sandbox execution.

        Same argv as the host path: `build_codex_argv` remains the single
        definition of Requirement 8.1's invocation, so what runs in the
        container is what the spec names.

        Only `CODEX_API_KEY` is exported, unlike `_build_env`'s copy of
        `os.environ`. That difference is the security point — a host subprocess
        needs the developer's PATH/HOME to run at all, a container process gets
        those from its image, and copying the developer's environment across the
        boundary would hand the Sandbox every secret in their shell.

        Requires the `codex` executable in the Sandbox image; the default
        `python:3.11` base image does not have it, so `--agent=codex` needs
        `--base-image` pointing at one that does.
        """
        return SandboxInvocation(
            argv=build_codex_argv(prompt),
            env={CODEX_API_KEY_ENV: self._resolve_api_key()},
        )


def _spawn(argv: list[str], env: dict[str, str], workdir: str) -> subprocess.Popen[str]:
    """Starts the Codex CLI process with stdout and stderr on separate
    pipes (see "Why the subprocess's stdout/stderr are piped" in the
    module docstring) and stdin inherited.

    Line-buffered text mode (`text=True, bufsize=1`) so each reader thread
    below sees whole lines as the process emits them, decoded with
    `errors="replace"` so a stray non-UTF-8 byte in agent output can never
    abort a Run.

    Any `OSError` from the spawn itself — most plausibly no `codex` on
    PATH, or a `workdir` that does not exist or is not a directory — is an
    invocation that could not be constructed, so it surfaces as
    `AdapterError` per Requirement 8.3. Nothing has been started at that
    point, so there is no process to clean up.
    """
    try:
        return subprocess.Popen(
            argv,
            cwd=workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise AdapterError(
            f"Codex_Adapter cannot start {argv[0]!r} in {workdir!r}: {exc}. "
            f"Check that {argv[0]!r} is installed and on PATH and that the "
            "working directory exists."
        ) from exc


def _stream_events(process: subprocess.Popen[str]) -> Iterator[AgentEvent]:
    """Yields one `"output"` event per captured stdout/stderr line as it
    arrives, then the `"final_message"` event parsed from stdout.

    On a non-zero exit status, yields an `"error"` event describing it and
    then raises `AdapterError` — judgment call 4 in the module docstring
    explains why both, and in that order.

    The `finally` block runs on every exit path, including the caller
    abandoning the iterator early (`GeneratorExit`), so the process is
    never left running and its pipes are always closed.
    """
    lines: "queue.Queue[tuple[str, str] | None]" = queue.Queue()
    readers = _start_stream_readers(process, lines)
    stdout_lines: list[str] = []

    try:
        # One `None` sentinel arrives per reader thread when its stream
        # reaches EOF; the loop ends once both have reported.
        open_streams = len(readers)
        while open_streams:
            item = lines.get()
            if item is None:
                open_streams -= 1
                continue

            stream_name, line = item
            if stream_name == _STDOUT:
                stdout_lines.append(line)
            yield AgentEvent(
                type="output", timestamp=_now_iso8601(), payload=line
            )

        exit_status = process.wait()

        final_message = _extract_final_message(stdout_lines)
        if final_message is not None:
            yield AgentEvent(
                type="final_message",
                timestamp=_now_iso8601(),
                payload=final_message,
            )

        if exit_status != 0:
            failure = (
                f"codex exec exited with status {exit_status} "
                f"(stderr content alone is never treated as a failure; this is "
                "the process's own exit status)"
            )
            yield AgentEvent(
                type="error", timestamp=_now_iso8601(), payload=failure
            )
            raise AdapterError(f"Codex_Adapter: {failure}")
    finally:
        _cleanup(process, readers)


def _start_stream_readers(
    process: subprocess.Popen[str], sink: "queue.Queue[tuple[str, str] | None]"
) -> list[threading.Thread]:
    """Starts one daemon reader thread per captured stream, both pushing
    onto `sink` (judgment call 2 in the module docstring).

    A stream is only read if `Popen` actually created it, which is always
    the case here — both are `subprocess.PIPE` — but is checked anyway
    since `Popen.stdout`/`.stderr` are `Optional` by type.
    """
    readers: list[threading.Thread] = []
    for stream, stream_name in ((process.stdout, _STDOUT), (process.stderr, _STDERR)):
        if stream is None:
            continue
        reader = threading.Thread(
            target=_pump_stream,
            args=(stream, stream_name, sink),
            name=f"vaultshield-codex-{stream_name}",
            daemon=True,
        )
        reader.start()
        readers.append(reader)
    return readers


def _pump_stream(
    stream: IO[str],
    stream_name: str,
    sink: "queue.Queue[tuple[str, str] | None]",
) -> None:
    """Reader-thread body: pushes `(stream_name, line)` for each line read
    from `stream`, then exactly one `None` sentinel on the way out.

    The trailing newline is stripped (and only that): the Monitor appends
    its own newline when writing a payload through the pty, and every
    other character of agent output is preserved as-is.

    The sentinel is pushed from a `finally` block so the consumer's loop
    can never hang waiting on a reader that died: an `OSError`/`ValueError`
    from reading a pipe closed under it (the process was killed, or the
    consumer abandoned the iterator) is ordinary end-of-stream here, not a
    failure worth reporting — the exit status is the signal that matters.
    """
    try:
        for line in stream:
            sink.put((stream_name, line.rstrip("\n")))
    except (OSError, ValueError):
        pass
    finally:
        sink.put(None)


def _extract_final_message(stdout_lines: list[str]) -> str | None:
    """Returns the agent's final message parsed from captured stdout —
    design.md's rule: the last non-empty stdout line — or `None` when
    stdout carried no content at all.

    `None` (rather than an empty `final_message` event, or an error) is
    what makes an exit-0 run with empty stdout a Run with no final
    message; see judgment calls 5 and the multi-line limitation in the
    module docstring.
    """
    for line in reversed(stdout_lines):
        if line.strip():
            return line.strip()
    return None


def _cleanup(process: subprocess.Popen[str], readers: list[threading.Thread]) -> None:
    """Ensures the process is not left running and its pipes are closed.

    On the ordinary path the process has already exited and both readers
    have already finished, so every step here is a no-op. It matters when
    the caller abandons the iterator early: killing the process is what
    lets the readers hit EOF and return, which is why the kill precedes
    the joins.
    """
    if process.poll() is None:
        process.kill()
        process.wait()

    for reader in readers:
        reader.join(timeout=_STREAM_READER_JOIN_TIMEOUT)

    for stream in (process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def _now_iso8601() -> str:
    """Returns the current UTC time as an ISO-8601 string, matching the
    `AgentEvent.timestamp` format documented in `base.py` and produced by
    `Mock_Adapter`.
    """
    return datetime.now(timezone.utc).isoformat()
