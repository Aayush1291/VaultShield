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

from vaultshield.adapters.base import Agent_Adapter, AgentEvent

# Deterministic SHAPE: every `launch()` call yields exactly this many
# "output" events, followed by exactly one "final_message" event.
_OUTPUT_EVENT_COUNT = 3


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


def _now_iso8601() -> str:
    """Returns the current UTC time as an ISO-8601 string, matching the
    `AgentEvent.timestamp` format documented in `base.py`.
    """
    return datetime.now(timezone.utc).isoformat()
