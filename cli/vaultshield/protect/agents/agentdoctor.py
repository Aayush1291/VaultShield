"""A uniform report shape for `agents doctor` across Claude Code, Kiro and Codex.

Antigravity keeps its own richer report (`doctor.py`); this is the shared shape
for the three agents added on Day 3, and the thing `agents doctor all` prints for
every agent in one format. It reuses `doctor`'s placeholder check and `Check`
type so the placeholder line reads the same everywhere.

Read-only: nothing here changes a file. No value is ever printed; a line names a
file, a rule count, or a layer's state, never a secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vaultshield.protect.agents.doctor import ACTIVE, NOT_ACTIVE, PARTIAL, Check

NOT_AVAILABLE = "N/A"  # a layer that this agent/surface does not offer; never counts as a failure

__all__ = ["ACTIVE", "PARTIAL", "NOT_ACTIVE", "NOT_AVAILABLE", "Check", "Surface", "AgentReport", "deny_rules_check"]


@dataclass
class Surface:
    name: str
    path: str  # a display path such as "~/.claude/settings.json"
    rows: list[tuple[str, Check]]


@dataclass
class AgentReport:
    agent: str
    surfaces: list[Surface]
    notes: list[str] = field(default_factory=list)
    not_covered: list[str] = field(default_factory=list)  # detected files still holding real values

    @property
    def ok(self) -> bool:
        return all(check.state in (ACTIVE, NOT_AVAILABLE) for surface in self.surfaces for _, check in surface.rows)


def deny_rules_check(have: list[str], expected: list[str], reinstall_hint: str) -> Check:
    """How many of the `expected` deny rules are present in `have`."""
    present = [rule for rule in expected if rule in set(have)]
    if not expected:
        return Check(NOT_ACTIVE, "no files to protect yet")
    if len(present) == len(expected):
        return Check(ACTIVE, f"{len(present)} of {len(expected)} exact-path rules present")
    if present:
        return Check(PARTIAL, f"{len(present)} of {len(expected)} rules present; {reinstall_hint}")
    return Check(NOT_ACTIVE, f"no VaultShield rules; {reinstall_hint}")


def render(report: AgentReport) -> list[str]:
    """The report as printable lines (the command adds a heading and footer)."""
    lines: list[str] = []
    for surface in report.surfaces:
        lines.append("")
        lines.append(f"{surface.name}  {surface.path}")
        for label, check in surface.rows:
            lines.append(f"  {label:<13} {check.state:<11} {check.detail}")
    for note in report.notes:
        lines.append("")
        lines.append(note)
    if report.not_covered:
        lines.append("")
        lines.append(
            "Not covered by placeholders (detected, not moved; their real contents are still on disk): "
            + ", ".join(report.not_covered)
        )
    return lines
