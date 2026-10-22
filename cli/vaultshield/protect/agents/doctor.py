"""`agents doctor antigravity`: is each backstop active, per surface?

Read-only. It reads the manifest, the project's files, the Antigravity settings
files and the hooks files, and changes nothing. It never prints a value: file
names, variable names, rule counts and grant positions only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from vaultshield.protect import PLACEHOLDER, protector
from vaultshield.protect.agents import antigravity
from vaultshield.protect.agents.install import APP_CONFIG, CLI_SETTINGS, GLOBAL_HOOKS, KIND_APP, KIND_CLI, existing_rules
from vaultshield.protect.manifest import Manifest

ACTIVE = "ACTIVE"
PARTIAL = "PARTIAL"
NOT_ACTIVE = "NOT ACTIVE"


@dataclass
class Check:
    state: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.state == ACTIVE


@dataclass
class Surface:
    name: str
    path: Path
    placeholders: Check
    deny: Check
    hook: Check

    @property
    def ok(self) -> bool:
        return self.placeholders.ok and self.deny.ok and self.hook.ok


@dataclass
class Report:
    root: Path
    surfaces: list[Surface]
    not_covered: list[str] = field(default_factory=list)  # detected files that still hold real contents
    credential_grants: tuple[int, list[int]] | None = None  # (all grants, indexes that look like credentials)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.surfaces)


# --- placeholders ------------------------------------------------------------


def check_placeholders(root: Path, manifest: Manifest) -> Check:
    """Whether every protected variable still holds the placeholder on disk."""
    if not manifest.protected:
        return Check(NOT_ACTIVE, "no file has been protected yet (only detected files)")
    holding = 0
    real: list[str] = []
    unreadable: list[str] = []
    for rel, names in sorted(manifest.protected.items()):
        analysis = protector.analyze(root / rel)
        if analysis.refusal:
            unreadable.append(rel)
            continue
        for name in names:
            if name in analysis.real:
                real.append(f"{rel}: {name}")
            elif name in analysis.placeholders:
                holding += 1
    if real or unreadable:
        parts = []
        if real:
            parts.append("real values on disk for " + ", ".join(real))
        if unreadable:
            parts.append("cannot be read: " + ", ".join(unreadable))
        return Check(NOT_ACTIVE, "; ".join(parts))
    files = len(manifest.protected)
    return Check(ACTIVE, f"{files} file{'s' if files != 1 else ''}, {holding} variable{'s' if holding != 1 else ''} hold {PLACEHOLDER}")


# --- deny rules --------------------------------------------------------------


def check_deny(path: Path, kind: str, expected: list[str]) -> Check:
    if not path.is_file():
        return Check(NOT_ACTIVE, "file not found")
    try:
        text = path.read_bytes().decode("utf-8")
        json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return Check(NOT_ACTIVE, "file is not valid JSON")
    have = set(existing_rules(text, kind))
    present = [r for r in expected if r in have]
    if len(present) == len(expected):
        return Check(ACTIVE, f"{len(present)} of {len(expected)} exact-path rules present")
    if present:
        return Check(PARTIAL, f"{len(present)} of {len(expected)} exact-path rules present; run `agents install` again")
    return Check(NOT_ACTIVE, "no VaultShield deny rules; run `agents install antigravity`")


# --- hook --------------------------------------------------------------------


def _matches_run_command(matcher: object) -> bool:
    if not isinstance(matcher, str) or matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, "run_command") is not None
    except re.error:
        return False


def _vaultshield_hook_commands(data: object) -> list[str]:
    """Every command, in an enabled hook with a PreToolUse group that matches
    run_command, that ends in `hook antigravity`."""
    found: list[str] = []
    if not isinstance(data, dict):
        return found
    for hook in data.values():
        if not isinstance(hook, dict) or hook.get("enabled") is False:
            continue
        for group in hook.get("PreToolUse") or []:
            if not isinstance(group, dict) or not _matches_run_command(group.get("matcher")):
                continue
            for handler in group.get("hooks") or []:
                command = handler.get("command") if isinstance(handler, dict) else None
                if isinstance(command, str):
                    try:
                        words = shlex.split(command)
                    except ValueError:
                        continue
                    if words[-2:] == ["hook", "antigravity"]:
                        found.append(command)
    return found


def check_hook(home: Path, root: Path) -> Check:
    places = [(home / GLOBAL_HOOKS, "~/" + GLOBAL_HOOKS.as_posix()), (root / ".agents" / "hooks.json", ".agents/hooks.json (this project)")]
    for path, label in places:
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        for command in _vaultshield_hook_commands(data):
            program = shlex.split(command)[0]
            if os.path.isabs(program) and not os.path.exists(program):
                return Check(NOT_ACTIVE, f"{label} points at a program that no longer exists; run `agents install antigravity` again")
            return Check(ACTIVE, label)
    return Check(NOT_ACTIVE, "no VaultShield hook found; run `agents install antigravity`")


# --- credentials in command grants -------------------------------------------

_USERINFO_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@'\"]+:[^/\s@'\"]+@")
_KNOWN_TOKENS = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_\-]{30,}"
    r"|\bya29\.[0-9A-Za-z_\-]{20,}"
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
_BEARER = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/\-]{20,}", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b[A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key)"
    r"[A-Za-z0-9_]*\s*[=:]\s*['\"]?(?!\$)[^\s'\";&|)]{8,}",
    re.IGNORECASE,
)


def looks_like_a_credential(grant: str) -> bool:
    """A heuristic, on purpose conservative: obvious token shapes, a user:password
    in a URL, a private key header, or SECRET=long-value style assignments."""
    return bool(
        _USERINFO_URL.search(grant)
        or _KNOWN_TOKENS.search(grant)
        or _BEARER.search(grant)
        or _SECRET_ASSIGNMENT.search(grant)
    )


def credential_grants(config_path: Path) -> tuple[int, list[int]] | None:
    """(number of grants, positions of the command grants that look like they
    contain a credential), or None if the file is missing or unreadable. Positions
    are 0-based in `userSettings.globalPermissionGrants.allow`."""
    try:
        data = json.loads(config_path.read_bytes().decode("utf-8"))
        allow = data["userSettings"]["globalPermissionGrants"]["allow"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(allow, list):
        return None
    flagged = [
        i for i, grant in enumerate(allow) if isinstance(grant, str) and grant.startswith("command(") and looks_like_a_credential(grant)
    ]
    return len(allow), flagged


# --- the report --------------------------------------------------------------


def diagnose(home: Path, root: Path, manifest: Manifest) -> Report:
    expected = antigravity.deny_rules(root, manifest)
    placeholders = check_placeholders(root, manifest)
    hook = check_hook(home, root)
    surfaces = [
        Surface("CLI", home / CLI_SETTINGS, placeholders, check_deny(home / CLI_SETTINGS, KIND_CLI, expected), hook),
        Surface("App", home / APP_CONFIG, placeholders, check_deny(home / APP_CONFIG, KIND_APP, expected), hook),
    ]
    return Report(
        root=root,
        surfaces=surfaces,
        not_covered=sorted(manifest.detected),
        credential_grants=credential_grants(home / APP_CONFIG),
    )
