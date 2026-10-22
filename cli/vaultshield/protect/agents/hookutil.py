"""Shared pieces for the Claude Code, Kiro and Codex PreToolUse hooks.

The decision about a shell command (does it name a protected file, dump the
environment, or read the Keychain?) is the same for every agent. It lives in
`antigravity.evaluate` and is reused here unchanged. What differs per agent is
only the payload shape on stdin and the response shape on stdout, which each
agent module builds around `decide` and `HookOutput`.

Nothing here reads the Keychain, and no value is ever put in a message: a reason
carries a file's path or a fixed command word, never user text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vaultshield.monitor import EventLogRecord, _append_event_log, _now_iso8601
from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect.agents import antigravity
from vaultshield.protect.agents.antigravity import REASON_MANIFEST, REASON_UNPARSABLE, Verdict
from vaultshield.protect.manifest import ManifestError


@dataclass(frozen=True)
class HookOutput:
    """What a hook command writes and the status it exits with. `stdout` and
    `stderr` are already-formatted text; `exit_code` is 0 to allow, 2 to block."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


def projects_for_starts(starts: list[object]) -> list[tuple[Path, "manifest_mod.Manifest"]]:
    """Every protected project found at or above each start directory, de-duped.
    Mirrors `antigravity._projects_for`, but takes plain directory strings so it
    works for any agent's payload shape."""
    projects: list[tuple[Path, manifest_mod.Manifest]] = []
    seen: set[Path] = set()
    for start in starts:
        if not isinstance(start, str) or not start:
            continue
        root = manifest_mod.find_root(Path(start))
        if root is None or root in seen:
            continue
        seen.add(root)
        manifest = manifest_mod.load(root)
        if manifest is not None:
            projects.append((root, manifest))
    return projects


def decide(command: str, starts: list[object]) -> Verdict:
    """Allow or deny `command` for the protected projects at `starts`. Never
    raises: any failure to work out the projects or to evaluate the command is
    turned into a denial (fail closed)."""
    try:
        projects = projects_for_starts(starts)
    except ManifestError:
        return Verdict(False, REASON_MANIFEST)
    except (OSError, RuntimeError, ValueError):
        return Verdict(False, REASON_UNPARSABLE)
    try:
        return antigravity.evaluate(command, projects)
    except Exception:  # fail closed: an unexpected error must never become an allow
        return Verdict(False, REASON_UNPARSABLE)


def reason_text(verdict: Verdict) -> str:
    """The human-readable reason for a denial, prefixed with VaultShield. Reuses
    the Antigravity message table so every agent says the same thing."""
    return "VaultShield: " + antigravity._MESSAGES[verdict.reason]


def collect_strings(value: object, out: list[str]) -> None:
    """Every string anywhere inside `value` (an agent's tool input), in order.
    Kiro does not document the field that holds a shell command, so the Kiro
    hook feeds every string it finds to the evaluator rather than guessing a
    key."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            collect_strings(item, out)
    elif isinstance(value, list):
        for item in value:
            collect_strings(item, out)


def log_violation(agent: str, verdict: Verdict) -> None:
    """Records a denial in the project's violations log. A reason code and a
    file path only, never the command and never a value. Failing to log never
    turns a denial into an allow."""
    if verdict.root is None:
        return
    detail = f" {verdict.detail}" if verdict.detail else ""
    record = EventLogRecord("violation", _now_iso8601(), f"{agent} shell denied: {verdict.reason}{detail}")
    try:
        _append_event_log(manifest_mod.violations_path(verdict.root), [record])
    except OSError:
        pass
