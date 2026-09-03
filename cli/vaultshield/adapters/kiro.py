"""Kiro_Adapter: the Agent_Adapter implementation for the Kiro CLI agent.

Implements the `Kiro_Adapter` concrete adapter described in design.md's
"Agent_Adapter interface and implementations (`adapters/`)" section
(Task 19.1), built on the `Agent_Adapter` ABC, `AgentEvent` TypedDict and
`AdapterError` exception defined in `base.py` (Task 6.1). Wiring
`--agent=kiro` into `run`'s adapter dispatch table is Task 19.5's own
concern, not this module's.

Covers Requirement 15.1: "WHEN `vaultshield run --agent=kiro` executes,
THE Kiro_Adapter SHALL invoke the Kiro CLI using
`kiro-cli chat --no-interactive` and SHALL authenticate using the
`KIRO_API_KEY` value."
And Requirement 15.2: "THE Kiro_Adapter SHALL treat every Run as a fresh,
non-resumable session and SHALL NOT attempt to resume a prior Kiro
session."
And Requirement 15.3 (Task 19.3): "IF the `kiro-cli` process exits cleanly
but produces no parseable final message, THEN THE Kiro_Adapter SHALL
report a failure rather than a successful Run."

design.md's own bullet for this adapter: "builds `kiro-cli chat
--no-interactive`, injects `KIRO_API_KEY`. Never persists or passes a
session id between runs — each `launch()` call is a fresh CLI invocation
with no `--resume`/session-file argument. After the process exits, if the
exit code is 0 but no parseable final message can be extracted from the
captured output, raises `AdapterError` (reported as a failed Run) rather
than reporting success."

### How Requirement 15.2 is satisfied, structurally

The Kiro CLI does support resuming: it saves a chat session per directory
and prints a `--resume-id <ID>`-style command for picking one back up
([Kiro CLI session management](https://kiro.dev/docs/cli/chat/session-management/);
content was rephrased for compliance with licensing restrictions).
Requirement 15.2 is the decision never to use that, and it holds here for
three independent reasons rather than one:

- The argv is built fresh on every `launch()` from `build_kiro_argv`,
  which appends nothing but the prompt to a fixed `kiro-cli chat
  --no-interactive` prefix. There is no code path — no option, no
  parameter, no config lookup — that can add a resume or session-file
  argument to it.
- `Kiro_Adapter` instances carry no session state. The only instance
  attribute is the optional API key override; nothing derived from a
  previous `launch()` is stored, so repeated calls on one instance are as
  independent as calls on two fresh instances.
- Nothing is written to disk. The adapter persists no session id, cache,
  or transcript of its own, so a later Run has nothing to resume from
  even if a future caller wanted it to.

Whether the Kiro CLI writes its own per-directory session record as a
side effect is outside this adapter's control; what Requirement 15.2 asks
for, and what this module guarantees, is that VaultShield never *asks* to
resume one.

### Why the subprocess's stdout/stderr are piped rather than inherited

Same reasoning as `Codex_Adapter`, and it applies unchanged here:
`monitor.py`'s PTY wrapper swaps this process's fd 0/1/2 onto a pty slave
for the duration of `launch()`, so a subprocess started with no
redirection of its own would write straight into the pty AND have its
output yielded again as `AgentEvent` payloads — the double-emission
`monitor.py`'s docstring warns about. Piping both streams keeps the
subprocess's own bytes out of the pty, so every line reaches the
Event_Log exactly once, via the events yielded below. stdin stays
inherited (the Monitor's pty), so `kiro-cli` still sees a terminal on
fd 0 rather than a closed pipe.

This module deliberately does not share `codex.py`'s streaming helpers.
design.md gives each adapter its own module, and factoring the plumbing
into a shared module would mean rewriting the already-working
`Codex_Adapter` as part of a task that is only about Kiro. The
duplication is the cheaper trade for now; the natural moment to
reconsider is Task 20 (`Antigravity_Adapter`), when a third copy would
have to justify itself.

### Judgment calls this module has to make (none settled by design.md)

1. **The prompt is appended after `--no-interactive`.** Requirement 15.1
   fixes the command as `kiro-cli chat --no-interactive` but says nothing
   about where `launch`'s `prompt` goes, and the agent obviously has to
   receive it. `kiro-cli chat` takes the prompt as a positional argument
   after its options, so the argv is exactly the mandated prefix plus one
   trailing element. Nothing is interpolated into a shell string and no
   shell is involved, so the prompt needs no quoting or escaping.

2. **No flags beyond the mandated ones.** No `--resume-id` (Requirement
   15.2, above), and equally no tool-trust/auto-approve flag: Requirement
   15.1 fixes this invocation, and VaultShield's containment story is the
   Sandbox, not the agent's own permission prompts. Adding flags the spec
   did not name is not this task's call.

3. **`launch()` is not itself a generator function.** A function
   containing `yield` returns a generator whose body does not run until
   first iterated, which would defer the prompt and API-key checks to
   some arbitrary later moment. `launch` is a plain function that
   validates, spawns, and returns a separate generator
   (`_stream_events`), so `AdapterError` is raised at the `launch(...)`
   call itself — before any Sandbox process exists, per `base.py`'s
   contract.

4. **stdout and stderr are drained by two reader threads feeding one
   queue.** Reading one stream to EOF before the other risks deadlocking
   on a full pipe buffer for the unread one. Two threads pushing onto a
   single `queue.Queue` keep both drained and preserve arrival order
   across streams, so events are yielded as they occur rather than only
   after the process exits.

5. **stderr lines are yielded verbatim as `type="output"` events.**
   `AgentEvent` has no "progress" variant, and `base.py` documents
   `"output"` as "ordinary progress/log output". Payloads carry no
   stream tag: the Monitor writes each payload through the pty into the
   Event_Log, and a terminal running `kiro-cli chat` interleaves both
   streams untagged, so tagging here would put text in the reviewer's
   transcript that the real command never printed. The cost is that the
   Event_Log does not record which stream a line came from; nothing in
   requirements.md or design.md asks it to.

6. **A non-zero exit status yields an in-band `"error"` event AND then
   raises `AdapterError`.** `base.py`'s contract forbids reporting a Run
   as successful when the process did not produce a usable result.
   Yielding the event first puts the exit status in the Event_Log (the
   Monitor writes each payload before it ever sees the exception);
   raising afterwards is what fails the Run at its call site. This is
   distinct from Requirement 15.3, which is about the exit-0 case.

7. **`KIRO_API_KEY` is injected under exactly that name.** Requirement
   15.1 and design.md both name `KIRO_API_KEY`, so that is the variable
   set in the child environment. If a given `kiro-cli` build reads its
   credential from a differently-named variable, that deployment also
   needs it set in the environment `vaultshield run` inherits — renaming
   the spec's variable is not this task's call.

### How Requirement 15.3 is satisfied (Task 19.3)

An exit-0 process whose output carries no parseable final message is
reported as a failed Run rather than a success. `_extract_final_message`
is the single place that decides what "parseable" means — currently "the
last non-empty stdout line", the same rule design.md gives for
`Codex_Adapter` — and it returns `None` when captured stdout offered no
such line. `_stream_events`'s post-exit block turns that `None` into an
in-band `"error"` event plus an `AdapterError`, exactly as it already does
for a non-zero exit status. Three further judgment calls this check needs
(none of them settled by design.md):

8. **"Parseable final message" is judged against stdout only, so an exit-0
   run that printed nothing but stderr fails.** `_extract_final_message`
   considers only stdout lines, per design.md's extraction rule, and this
   check reuses it rather than introducing a second notion of "output". A
   `kiro-cli` run whose entire contribution was progress chatter on stderr
   produced no result to review, which is precisely the case Requirement
   15.3 and `base.py`'s "never conflate exit 0 with useful output" rule
   are about. Every stderr line is still in the Event_Log as an
   `"output"` event, so the reviewer sees why the Run failed.

9. **Whitespace-only stdout counts as no final message.** Requirement
   15.3 says "no parseable final message" and Requirement 16.3 spells the
   analogous Antigravity case out as "empty or whitespace-only";
   `_extract_final_message`'s existing `line.strip()` test already treats
   a stdout of blank lines as carrying nothing, so nothing extra is
   needed to align the two adapters.

10. **The exit-status check runs before this one.** Both failures can hold
    at once (a crash that printed nothing), and the exit status is the
    more specific, more actionable diagnosis — "exited with status 1" tells
    the reviewer more than "produced no final message". So a non-zero exit
    raises with its own message and this check is reached only on a clean
    exit, which also keeps the `AdapterError` message unambiguous about
    which of the two conditions failed the Run.

This is the difference between the two clean-exit outcomes: stdout with a
parseable line yields a `"final_message"` event and the iterator ends
normally; stdout without one yields an `"error"` event and raises. Unlike
`Codex_Adapter`, which has no equivalent requirement and simply yields no
`final_message` event in that case, this adapter fails the Run.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from datetime import datetime, timezone
from typing import IO, Iterator

from vaultshield.adapters.base import AdapterError, Agent_Adapter, AgentEvent

# The Kiro CLI executable, resolved on PATH inside the environment
# `vaultshield run` runs in. Not configurable per Run: Requirement 15.1
# fixes the invocation, and Task 19.2's unit test asserts the exact argv.
# Note this is `kiro-cli` (the terminal agent), not the `kiro` IDE
# launcher — a machine can have one without the other.
KIRO_EXECUTABLE = "kiro-cli"

# The fixed, mandated part of the invocation: `kiro-cli chat
# --no-interactive` (Requirement 15.1). `build_kiro_argv` appends only the
# prompt to this. Kept as a module-level constant so the argv can never be
# assembled from anything Run-specific other than the prompt itself —
# which is how Requirement 15.2's "no `--resume`/session-file argument"
# guarantee stays structural rather than a matter of remembering.
_KIRO_BASE_ARGV = (KIRO_EXECUTABLE, "chat", "--no-interactive")

# Environment variable carrying the Kiro credential, per Requirement 15.1
# ("SHALL authenticate using the `KIRO_API_KEY` value"). Read from this
# process's own environment at `launch()` time and injected into the
# subprocess environment — see judgment call 7 in the module docstring.
KIRO_API_KEY_ENV = "KIRO_API_KEY"

# Safety-net timeout for joining a stdout/stderr reader thread once the
# process has exited (or been killed) and its pipe is at EOF. Both threads
# return almost immediately at that point; this bound only guards against
# an unexpected platform quirk turning that into an indefinite hang.
_STREAM_READER_JOIN_TIMEOUT = 5.0

# Stream labels used on the internal reader queue. Only `_STDOUT` is
# load-bearing (it selects the lines `_extract_final_message` considers);
# both streams' lines are yielded identically, per judgment call 5.
_STDOUT = "stdout"
_STDERR = "stderr"


def build_kiro_argv(prompt: str) -> list[str]:
    """Builds the exact Kiro CLI invocation Requirement 15.1 mandates:
    `kiro-cli chat --no-interactive` with `prompt` as the trailing
    positional argument (judgment call 1 in the module docstring).

    Carries no session/resume argument, by construction: the fixed part
    comes from `_KIRO_BASE_ARGV` and the only appended element is the
    prompt (Requirement 15.2).

    Raises `AdapterError` for a missing/whitespace-only prompt: an empty
    prompt cannot form a valid invocation, and `base.py`'s contract wants
    that reported as an adapter-level error before any Sandbox process
    starts. Whitespace-only counts as empty because it would otherwise
    produce a syntactically valid command asking the agent to do nothing.

    Kept module-level and public so Task 19.2's exact-argv/no-resume check
    can exercise invocation construction on its own, without spawning a
    process.
    """
    if not prompt.strip():
        raise AdapterError(
            "Kiro_Adapter cannot construct a valid invocation: prompt is "
            "empty. Pass a non-empty --prompt."
        )
    return [*_KIRO_BASE_ARGV, prompt]


class Kiro_Adapter(Agent_Adapter):
    """Agent_Adapter implementation for the Kiro CLI agent
    (Requirements 15.1, 15.2, 15.3).

    Every `launch()` call is a fresh, non-resumable session: the instance
    holds no session state and the argv carries no resume/session
    argument — see "How Requirement 15.2 is satisfied, structurally" in
    the module docstring.

    `api_key` overrides the `KIRO_API_KEY` value that would otherwise be
    read from this process's environment at `launch()` time. It exists so
    a caller (and Task 19.2's unit test) can supply a credential without
    mutating the process environment; `None` — the default, and what
    `run`'s dispatch table produces, since it instantiates adapters with
    no arguments — means "read `KIRO_API_KEY` from the environment".

    Reading the environment at `launch()` rather than at construction
    matters for the pipeline in `cli.py`, which instantiates the adapter
    early (before the Sandbox exists) and launches it later: the check
    then reflects the credential state at the moment the invocation is
    actually built.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def launch(self, prompt: str, workdir: str) -> Iterator[AgentEvent]:
        """Starts `kiro-cli chat --no-interactive "<prompt>"` in `workdir`
        and returns an iterator of `AgentEvent`s: one `"output"` event per
        captured stdout/stderr line as it arrives, then one
        `"final_message"` event carrying the message parsed from stdout.

        Raises `AdapterError` — before any agent process is started, per
        `base.py`'s contract — when the prompt is empty, when
        `KIRO_API_KEY` is unset/blank, or when the process cannot be
        spawned at all (no `kiro-cli` on PATH, unusable `workdir`). This
        method is intentionally not a generator function, so those checks
        run at the call itself rather than at first iteration (judgment
        call 3 in the module docstring).

        The returned iterator raises `AdapterError` too, at the end of the
        stream, for the two post-exit failures `_stream_events` documents:
        a non-zero exit status, and a clean exit carrying no parseable
        final message (Requirement 15.3).
        """
        argv = build_kiro_argv(prompt)
        env = self._build_env()
        process = _spawn(argv, env, workdir)
        return _stream_events(process)

    def _build_env(self) -> dict[str, str]:
        """Returns the subprocess environment: this process's environment
        with `KIRO_API_KEY` injected (Requirement 15.1).

        Raises `AdapterError` when no usable credential is available, for
        the same reason `Codex_Adapter` does: failing at invocation
        construction gives a clear adapter-level error instead of an
        opaque failure from the CLI later, and it happens before any
        Sandbox process exists. A blank/whitespace-only value is treated
        as unset — it authenticates nothing.

        The rest of the environment is inherited so `kiro-cli` keeps the
        PATH, HOME, and terminal settings it needs to run at all. Nothing
        session-related is added (Requirement 15.2).
        """
        api_key = (
            self._api_key
            if self._api_key is not None
            else os.environ.get(KIRO_API_KEY_ENV, "")
        )
        if not api_key.strip():
            raise AdapterError(
                "Kiro_Adapter cannot construct a valid invocation: "
                f"{KIRO_API_KEY_ENV} is not set. Export {KIRO_API_KEY_ENV} "
                "before running --agent=kiro."
            )

        env = dict(os.environ)
        env[KIRO_API_KEY_ENV] = api_key
        return env


def _spawn(argv: list[str], env: dict[str, str], workdir: str) -> subprocess.Popen[str]:
    """Starts the Kiro CLI process with stdout and stderr on separate
    pipes (see "Why the subprocess's stdout/stderr are piped" in the
    module docstring) and stdin inherited.

    Line-buffered text mode (`text=True, bufsize=1`) so each reader thread
    below sees whole lines as the process emits them, decoded with
    `errors="replace"` so a stray non-UTF-8 byte in agent output can never
    abort a Run.

    Any `OSError` from the spawn itself — most plausibly no `kiro-cli` on
    PATH, or a `workdir` that does not exist or is not a directory — is an
    invocation that could not be constructed, so it surfaces as
    `AdapterError`. Nothing has been started at that point, so there is no
    process to clean up.
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
            f"Kiro_Adapter cannot start {argv[0]!r} in {workdir!r}: {exc}. "
            f"Check that {argv[0]!r} is installed and on PATH and that the "
            "working directory exists."
        ) from exc


def _stream_events(process: subprocess.Popen[str]) -> Iterator[AgentEvent]:
    """Yields one `"output"` event per captured stdout/stderr line as it
    arrives, then the `"final_message"` event parsed from stdout.

    Two post-exit conditions fail the Run, each yielding an `"error"`
    event describing it and then raising `AdapterError` — judgment call 6
    in the module docstring explains why both, and in that order:

    - a non-zero exit status; or
    - a clean (status 0) exit from which `_extract_final_message` could
      parse no final message (Requirement 15.3). Judgment call 10 explains
      why this one is checked second.

    A clean exit WITH a parseable final message is the only path on which
    the iterator ends normally, and is therefore the only one the caller
    reports as a successful Run.

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
            failure = f"kiro-cli chat exited with status {exit_status}"
            yield AgentEvent(
                type="error", timestamp=_now_iso8601(), payload=failure
            )
            raise AdapterError(f"Kiro_Adapter: {failure}")

        # Requirement 15.3: a clean exit is not evidence of a usable
        # result. Reached only when `exit_status == 0`, since the branch
        # above raises otherwise (judgment call 10).
        if final_message is None:
            failure = (
                "kiro-cli chat exited cleanly (status 0) but produced no "
                "parseable final message"
            )
            yield AgentEvent(
                type="error", timestamp=_now_iso8601(), payload=failure
            )
            raise AdapterError(f"Kiro_Adapter: {failure}")
    finally:
        _cleanup(process, readers)


def _start_stream_readers(
    process: subprocess.Popen[str], sink: "queue.Queue[tuple[str, str] | None]"
) -> list[threading.Thread]:
    """Starts one daemon reader thread per captured stream, both pushing
    onto `sink` (judgment call 4 in the module docstring).

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
            name=f"vaultshield-kiro-{stream_name}",
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
    """Returns the agent's final message parsed from captured stdout — the
    last non-empty stdout line, the same rule design.md gives for
    `Codex_Adapter` and the only one available with the plain-text output
    the mandated argv produces — or `None` when stdout carried no
    parseable content at all.

    `None` is what fails the Run on a clean exit (Requirement 15.3): this
    function is the sole definition of "parseable final message" that
    `_stream_events`'s post-exit check consults, so changing the rule here
    changes both which text becomes the `final_message` payload and which
    clean exits count as failures — deliberately, since they are the same
    question asked twice.

    Known limitation, inherited from the "last non-empty line" rule: a
    final message spanning several lines contributes only its last line as
    the payload. Nothing is lost from the Event_Log — every stdout line is
    also yielded as an `"output"` event — and this function is the single
    place a better rule would go.
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
    `Mock_Adapter` and `Codex_Adapter`.
    """
    return datetime.now(timezone.utc).isoformat()
