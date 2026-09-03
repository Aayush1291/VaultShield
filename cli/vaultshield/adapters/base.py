"""Agent_Adapter interface: the common contract every AI coding agent
integration implements.

Implements the `Agent_Adapter` ABC, the `AgentEvent` TypedDict, and the
`AdapterError` exception described in design.md's "Agent_Adapter interface
and implementations (`adapters/`)" section (Task 6.1). This module defines
ONLY the interface/contract; concrete adapters — `Mock_Adapter` (Task 6.2),
`Codex_Adapter` (Task 10.1), `Kiro_Adapter` (Task 19.1), and
`Antigravity_Adapter` (Task 20.1) — each live in their own sibling module
and implement `Agent_Adapter` against the contract documented here.

Covers Requirement 5.1: "THE VaultShield_CLI SHALL define an Agent_Adapter
interface exposing a `launch(prompt, workdir) -> event_stream` operation."

design.md's Error Handling section states a behavioral contract every
concrete adapter must uphold. That contract belongs to the interface
itself, not to any one adapter, so it is documented in full on
`Agent_Adapter` and `AdapterError` below rather than repeated later in
each concrete adapter module:

    Agent_Adapter: invocation-construction failures (bad/missing prompt,
    missing API key) raise AdapterError *before* a Sandbox exists for that
    Run, so no container is ever created for a Run that can't start.
    Adapters never conflate "process exited 0" with "produced useful
    output" (see Antigravity's empty-output check).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, Literal, TypedDict


class AgentEvent(TypedDict):
    """One item in an Agent_Adapter's event stream, as yielded by `launch`.

    Mirrors design.md's `AgentEvent` exactly:
    - `type`: `"output"` for ordinary progress/log output, `"final_message"`
      for the agent's concluding response, or `"error"` for an error
      surfaced as part of the event stream itself. This is distinct from
      an `AdapterError` raised by `launch` — see `Agent_Adapter`'s
      docstring for the raise-vs-yield distinction; design.md does not
      further specify how each concrete adapter populates `"error"`
      events, so that choice is left to each adapter's own task.
    - `timestamp`: ISO-8601, per design.md's inline comment. Generating
      this value is each concrete adapter's responsibility (e.g. via
      `datetime.now(timezone.utc).isoformat()`); this module only defines
      the shape, not how any adapter populates it.
    - `payload`: the event's text content — captured output for
      `"output"`, the concluding text for `"final_message"`, or an error
      description for `"error"`.

    All three fields are required (TypedDict's default `total=True`):
    design.md's reference declares no optional fields, and a partially
    populated event would leave the Monitor (Task 7) and any downstream
    consumer unable to rely on a consistent shape.
    """

    type: Literal["output", "final_message", "error"]
    timestamp: str  # ISO-8601
    payload: str


class Agent_Adapter(ABC):
    """Common interface implemented by every AI coding agent integration.

    Satisfies Requirement 5.1's `launch(prompt, workdir) -> event_stream`
    operation. `launch` is the ONLY operation this interface defines:
    design.md's reference `Agent_Adapter` declares no other abstract
    method, and Sandbox lifecycle, monitoring, and run-directory concerns
    are each owned by their own component (`Sandbox`, `Monitor`,
    `run_directory.py`) rather than by the adapter itself.

    Behavioral contract for every concrete implementation (from design.md's
    Error Handling section — documented here, once, rather than repeated
    in each concrete adapter module):

    - A failure to even construct a valid invocation — a missing/empty
      `prompt`, a missing API key/credential the adapter needs, or any
      other reason `launch` cannot start the agent at all — MUST be
      raised as `AdapterError` BEFORE any Sandbox process is started for
      that Run. This guarantees no container is ever created for a Run
      that was never going to be able to start, and lets the caller
      distinguish "this Run never really began" from a failure that
      happens after a Sandbox already exists.
    - Ordinary agent output (including output on stderr, or an agent-
      reported in-band error) is communicated by yielding an `AgentEvent`
      from the returned iterator, never by raising. `launch` raising once
      the event stream has started is reserved for a genuine adapter/
      transport failure, not for the agent simply doing its job.
    - A clean process exit (exit code 0) must not, by itself, be treated
      as "the agent produced useful output". A concrete adapter that can
      detect a clean-but-empty/unparseable result (see the Kiro_Adapter's
      clean-exit/no-final-message check and the Antigravity_Adapter's
      clean-exit/empty-output check, both added in later tasks) reports
      that case as an `AdapterError` too, rather than a successful Run.
    """

    @abstractmethod
    def launch(self, prompt: str, workdir: str) -> Iterator[AgentEvent]:
        """Starts the agent process (or simulation) and yields AgentEvent
        items as they occur. Must not raise for ordinary agent output;
        adapter-level construction errors (bad invocation, missing auth) are
        raised BEFORE any Sandbox process is started, as an AdapterError.
        """


class AdapterError(Exception):
    """Raised by an Agent_Adapter for either of two distinct failure cases
    (see `Agent_Adapter`'s docstring above for the full contract):

    1. Invocation-construction failure — raised BEFORE `launch` starts any
       Sandbox process for the Run (e.g. an empty `prompt`, or a required
       API key that isn't set).
    2. Post-execution validation failure — raised AFTER the agent process
       has already exited cleanly (exit code 0), when a concrete adapter
       determines the process nonetheless produced no usable result (an
       empty or unparseable final message). A clean exit code alone is
       never sufficient evidence of a successful Run.

    Both cases are surfaced by the caller as a failed Run. A single
    exception type covers both so that every caller of `launch` only ever
    needs to catch one exception type from any Agent_Adapter, regardless
    of which of the two cases actually occurred.
    """
