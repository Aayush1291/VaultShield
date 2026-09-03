"""Mock_Adapter: a pure-Python simulation of the Agent_Adapter interface.

Implements the `Mock_Adapter` concrete adapter described in design.md's
"Agent_Adapter interface and implementations (`adapters/`)" section
(Task 6.2), built directly on the `Agent_Adapter` ABC and `AgentEvent`
TypedDict defined in `base.py` (Task 6.1).

Covers Requirement 5.2: "THE Mock_Adapter SHALL implement the
Agent_Adapter interface and SHALL produce a well-formed event stream
without invoking any real AI coding agent process."

Per design.md, `Mock_Adapter` "yields a small, deterministic-shape but
content-varying sequence of `AgentEvent`s (e.g. simulated `output` events
followed by one `final_message`) without spawning any real agent
process." Concretely, here:

- Deterministic SHAPE: every `launch()` call yields exactly
  `_OUTPUT_EVENT_COUNT` `"output"` events followed by exactly one
  `"final_message"` event, in that order, regardless of `prompt`/
  `workdir` content. Property 8 (Task 6.3, optional) exercises this
  against arbitrary non-empty `prompt`/`workdir` values.
- Content-VARYING: each event's `payload` incorporates `prompt`,
  `workdir`, the event's own position in the sequence, and a
  per-instance call counter, so distinct calls — whether with different
  arguments, or repeated calls on the same instance with identical
  arguments — never produce byte-identical payloads.

Unlike `Codex_Adapter`/`Kiro_Adapter`/`Antigravity_Adapter` (later tasks),
`Mock_Adapter` has no real invocation-construction failure mode: there is
no real CLI invocation, API key, or subprocess to fail to construct. Per
`Agent_Adapter`'s contract in `base.py`, `launch` here never raises
`AdapterError` for ordinary operation — it is a pure simulation that
always succeeds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from vaultshield.adapters.base import (
    Agent_Adapter,
    AdapterError,
    AgentEvent,
    SandboxInvocation,
)

# Deterministic SHAPE: every `launch()` call yields exactly this many
# "output" events, followed by exactly one "final_message" event.
_OUTPUT_EVENT_COUNT = 3

# The in-Sandbox body of `sandbox_invocation`. Runs as a REAL process inside the
# container (see that method), which is what makes Mock_Adapter usable as an
# end-to-end smoke test of the sandboxed execution path rather than only of the
# adapter interface.
#
# Written against the standard library alone: the Sandbox's base image is a
# plain `python:*` image with none of VaultShield installed, so this program
# cannot import anything from `vaultshield`.
#
# It mirrors `launch()`'s shape (N output lines then one final message) and, so
# that the file-change half of the Monitor has something real to observe, writes
# one file into the repository it was given. `flush=True` on every print keeps
# the transcript ordered with respect to the file write when stdout is a pipe.
_SANDBOX_PROGRAM = '''
import os, sys, time
prompt, workdir, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.chdir(workdir)
print("[mock-sandbox] cwd=%s" % os.getcwd(), flush=True)
for step in range(count):
    print("[mock-sandbox step=%d] simulated output for prompt=%r" % (step, prompt), flush=True)
    time.sleep(0.05)
with open("vaultshield_mock_agent.txt", "w") as fh:
    fh.write("written by Mock_Adapter inside the Sandbox\\n")
print("[mock-sandbox] wrote vaultshield_mock_agent.txt", flush=True)
print("[mock-sandbox] simulated final message for prompt=%r" % prompt, flush=True)
'''


class Mock_Adapter(Agent_Adapter):
    """Agent_Adapter implementation used only for testing the Agent_Adapter
    interface contract, without invoking a real AI coding agent (see the
    Glossary in requirements.md).

    Tracks a per-instance call counter so content varies not only across
    different `prompt`/`workdir` arguments, but also across repeated
    `launch()` calls on the same instance with identical arguments —
    "content-varying" per design.md, not merely a pure function of its
    inputs.
    """

    def __init__(self) -> None:
        self._call_count = 0

    def launch(self, prompt: str, workdir: str) -> Iterator[AgentEvent]:
        """Yields `_OUTPUT_EVENT_COUNT` `"output"` events followed by
        exactly one `"final_message"` event — the deterministic shape
        described in the module docstring.

        Never spawns a process, never makes a network call, and never
        raises for ordinary operation (see the module docstring for why
        `Mock_Adapter` has no invocation-construction failure mode).

        Each event's `payload` incorporates `prompt`, `workdir`, the
        event's index within this call, and this call's position in this
        instance's call history, so content varies across events, across
        arguments, and across repeated calls alike.
        """
        call_index = self._call_count
        self._call_count += 1

        for step in range(_OUTPUT_EVENT_COUNT):
            yield AgentEvent(
                type="output",
                timestamp=_now_iso8601(),
                payload=(
                    f"[mock call={call_index} step={step}] simulated output "
                    f"for prompt={prompt!r} workdir={workdir!r}"
                ),
            )

        yield AgentEvent(
            type="final_message",
            timestamp=_now_iso8601(),
            payload=(
                f"[mock call={call_index}] simulated final message for "
                f"prompt={prompt!r}"
            ),
        )

    def sandbox_invocation(self, prompt: str, workdir: str) -> SandboxInvocation:
        """Runs the simulation as a real `python` process inside the Sandbox.

        `launch()` above is a generator in the calling process, which cannot
        satisfy `vaultshield run`'s requirement that the agent execute inside
        the container. This returns an actual command instead, so `--agent=mock`
        exercises the same execution path a real agent takes: a container
        process, its output streamed back through the Sandbox, and a file it
        writes observed by the Monitor's in-container watcher.

        `python` is guaranteed present — the Sandbox's default base image is
        `python:3.11` and the program is passed with `-c` so nothing has to be
        installed or written to disk first. `env` is empty: the simulation needs
        no credential, and `SandboxInvocation` forbids copying the host
        environment in.

        Raises `AdapterError` for an empty prompt, matching every other
        adapter's invocation-construction contract, so the failure happens
        before a Sandbox process starts.
        """
        if not prompt.strip():
            raise AdapterError(
                "Mock_Adapter cannot construct a valid invocation: prompt is "
                "empty. Pass a non-empty --prompt."
            )
        return SandboxInvocation(
            argv=[
                "python",
                "-c",
                _SANDBOX_PROGRAM,
                prompt,
                workdir,
                str(_OUTPUT_EVENT_COUNT),
            ],
            env={},
        )


def _now_iso8601() -> str:
    """Returns the current UTC time as an ISO-8601 string, matching the
    `AgentEvent.timestamp` format documented in `base.py`.
    """
    return datetime.now(timezone.utc).isoformat()
