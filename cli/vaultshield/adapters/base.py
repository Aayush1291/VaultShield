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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class SandboxInvocation:
    """Everything needed to start an agent as a real process INSIDE the Sandbox.

    Returned by `Agent_Adapter.sandbox_invocation`. The Monitor hands this to
    `Sandbox.exec_stream`, so `argv` is executed by the container's own kernel
    with the cloned repository as its working directory.

    ### Why the adapter returns a description instead of spawning

    `launch()` yields events by spawning a process in *this* (host) process.
    That is the wrong side of the boundary: a process started here runs on the
    developer's machine with the developer's filesystem and credentials, which
    is precisely the isolation failure this type exists to remove. An adapter
    therefore describes its invocation and lets the Sandbox be the thing that
    runs it.

    ### `env` is a whitelist, never a copy of the host environment

    `env` must contain ONLY what the agent genuinely needs — in practice its
    API key and nothing else. It is injected into the container process's
    environment, so anything put here crosses into the Sandbox. Copying
    `os.environ` (which the host-side `launch()` path does, legitimately, to
    preserve PATH/HOME for a local process) would export every secret in the
    developer's shell into the container and defeat the credential
    substitution that `.env.sandbox` performs. The container supplies its own
    PATH/HOME from its image.

    Attributes:
        argv: the command to run, already fully constructed and validated.
        env: extra environment variables for that process only. Merged over
            the container's own environment by the Docker daemon; keys absent
            here keep whatever the image defines.
    """

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


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

        NOTE ON EXECUTION LOCUS: this method runs in the *calling* process. It
        is retained as the adapter-level contract Requirement 5.1 names, and is
        used directly by unit tests and by any tooling that deliberately wants
        a local run. `vaultshield run` does NOT use it — it uses
        `sandbox_invocation` below, so that the agent process runs inside the
        Sandbox rather than on the developer's machine.
        """

    def sandbox_invocation(self, prompt: str, workdir: str) -> SandboxInvocation:
        """Describes how to start this agent as a process INSIDE the Sandbox.

        This is the operation `vaultshield run` uses. Returning a description
        rather than a running process is what lets the Sandbox — not the host —
        be the thing that executes the agent.

        Args:
            prompt: the developer's prompt, validated the same way `launch`
                validates it.
            workdir: the agent's working directory *inside the container*
                (`Sandbox.REPO_DIR`). Passed for adapters that need to name it
                in their arguments; the Sandbox sets it as the process's
                working directory regardless.

        Returns:
            A `SandboxInvocation` whose `argv` the container will execute and
            whose `env` is a minimal whitelist — see that class on why it must
            never be a copy of the host environment.

        Raises:
            AdapterError: for an invocation that cannot be constructed (empty
                prompt, missing API key), exactly as `launch` does and for the
                same reason — it happens before any process starts, so a Run
                that could never begin never gets one.

        The default implementation refuses rather than falling back to
        `launch()`. A silent fallback would run the agent on the host, which is
        the exact failure mode this method was introduced to eliminate, so an
        adapter that has not been taught to run in a Sandbox must fail loudly.
        """
        raise AdapterError(
            f"{type(self).__name__} does not support running inside a Sandbox: "
            "it implements no sandbox_invocation(). `vaultshield run` executes "
            "agents inside the container and will not fall back to running one "
            "on the host."
        )

    def auth_invocation(self) -> SandboxInvocation | None:
        """How to authenticate this agent inside the Sandbox, or `None`.

        `None` — the default, and the answer for every adapter except
        Antigravity — means "this agent needs no in-Sandbox sign-in step". An
        adapter whose credential is an API key returns `None` because its key
        travels in `sandbox_invocation().env` and needs no interaction.

        An adapter returns an invocation here only when its agent cannot be
        authenticated any other way. Antigravity is the case that forced this to
        exist: it authenticates by Google OAuth alone, reads no API-key variable,
        and persists its session as a file inside the container. There is no value
        `sandbox_invocation().env` could carry that would authenticate it.

        Returned separately from `sandbox_invocation` because the two run under
        different rules. The monitored invocation's every byte becomes an
        Event_Log record; this one exchanges an authorization code with a human
        and must be recorded nowhere. `vaultshield run` executes this through
        `Sandbox.exec_interactive` and the monitored one through
        `Sandbox.exec_stream`, and only the latter is wired to the Event_Log.

        Only ever run when the developer passes `--interactive-auth`. Absent that
        flag the auth phase does not happen at all, so the default behaviour of
        every agent — including Antigravity — is exactly what it was before this
        method existed.
        """
        return None

    def credential_paths(self) -> tuple[str, ...]:
        """Container paths holding this agent's own credential (Batch 3 FR5).

        `token_hygiene.snapshot` reads them through the Docker API and the
        host scans everything that leaves the machine for their bytes. Empty —
        the default — means the agent keeps no credential file in the Sandbox
        (an API key in the exec env is not covered; see the design's out of
        scope list).
        """
        return ()


class AuthError(Exception):
    """Raised when an agent's in-Sandbox authentication phase failed.

    Distinct from `AdapterError` because the two need different messages and
    happen at different points. `AdapterError` from `sandbox_invocation` means the
    Run never began; `AdapterError` from the monitored run means the agent failed
    with an Event_Log already on disk. This means the Sandbox exists, the repo is
    cloned, and the agent was never started — so there is no Event_Log to point
    the developer at, and the fix is to sign in again rather than to inspect
    artifacts.

    NOT a subclass of `AdapterError`, deliberately: `cli.py`'s `AdapterError`
    handler appends "(Event_Log preserved at ...)" to its message, which would be
    a lie here.

    Never carries an authorization code or a session token. The auth exchange
    happens on a socket, and nothing that crosses it is interpolated into this
    message — see `Sandbox.exec_interactive`.
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
