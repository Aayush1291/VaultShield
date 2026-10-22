"""Kiro backstops: `fs_read` deny rules and a `PreToolUse` hook.

From the docs (FINDINGS.md, K-rows), two layers are CONFIRMED:

* `fs_read` deny rules in the user file `~/.kiro/settings/permissions.yaml`. A
  deny there wins over any allow, and the IDE and the CLI both read it. The match
  pattern for an absolute path is not shown in the V3 docs (K4), so `doctor` flags
  the match format as UNCONFIRMED. The rule keys themselves are confirmed.
* A `PreToolUse` hook. The user-level hook directory `~/.kiro/hooks/` is read by
  the Kiro CLI V3 only; the IDE reads a project's `.kiro/hooks/` instead, so the
  IDE hook is deliberately NOT installed (it would mean writing into the repo),
  and `doctor` reports that surface as not available at the user level. The field
  of the `execute_bash` payload that holds the command is not documented (K11), so
  the hook evaluates every string it finds in the tool input rather than guessing.

Whether these rules or the hook constrain shell commands is UNCONFIRMED (K8): it
holds only with the local command sandbox, which is off by default. The
placeholders remain the real protection.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import yaml

from vaultshield.protect.agents import agentdoctor, hookutil, yamledit
from vaultshield.protect.agents.agentdoctor import ACTIVE, NOT_ACTIVE, NOT_AVAILABLE, AgentReport, Check, Surface
from vaultshield.protect.agents.agentinstall import Edit
from vaultshield.protect.agents.antigravity import REASON_UNPARSABLE, Verdict
from vaultshield.protect.agents.doctor import check_placeholders
from vaultshield.protect.agents.install import vaultshield_command
from vaultshield.protect.manifest import Manifest

AGENT = "kiro"
AGENT_LABEL = "Kiro"
HOOK_SUBCOMMAND = "kiro"

PERMISSIONS = Path(".kiro/settings/permissions.yaml")
HOOK_FILE = Path(".kiro/hooks/vaultshield-protect.json")
HOOK_NAME = "vaultshield-protect"
MATCHER = "^(execute_bash|execute_pwsh)$"

HOOK_COMMAND = "vaultshield hook kiro"  # shown by render; install writes the absolute-python form
REINSTALL = "run `agents install kiro` again"

INSTALL_NOTES = [
    "Two files under ~/.kiro are written: settings/permissions.yaml (fs_read deny rules)",
    "and hooks/vaultshield-protect.json (the PreToolUse hook, read by Kiro CLI V3).",
    "The Kiro IDE reads hooks only from a project's .kiro/hooks, so the IDE hook is not installed;",
    "the placeholders still protect it. The absolute-path match format for fs_read is not documented.",
]


def deny_paths(root: Path, manifest: Manifest) -> list[str]:
    """The absolute path of every protected and detected file (the match values)."""
    base = root.resolve()
    return [str(base / rel) for rel in manifest.all_paths()]


def hook_object(command: str = HOOK_COMMAND) -> dict:
    return {
        "version": "v1",
        "hooks": [
            {
                "name": HOOK_NAME,
                "trigger": "PreToolUse",
                "matcher": MATCHER,
                "action": {"type": "command", "command": command},
                "timeout": 10,
            }
        ],
    }


def hook_command() -> str:
    return shlex.join([*vaultshield_command(), "hook", HOOK_SUBCOMMAND])


def _hook_text(command: str) -> str:
    return json.dumps(hook_object(command), indent=2) + "\n"


# --- render ------------------------------------------------------------------


def render(root: Path, manifest: Manifest) -> str:
    paths = deny_paths(root, manifest)
    rules = {"rules": [{"capability": "fs_read", "effect": "deny", "match": [p]} for p in paths]}
    return "\n".join(
        [
            "# VaultShield for Kiro.",
            "# This only PRINTS the configuration. Nothing was installed or changed.",
            "",
            "## 1. fs_read deny rules (merge into ~/.kiro/settings/permissions.yaml)",
            "",
            yaml.safe_dump(rules, sort_keys=False, default_flow_style=False).rstrip(),
            "",
            "A deny rule wins over any allow, in the IDE and the CLI. The absolute-path match",
            "format is not documented for V3; it is the most precise option available.",
            "",
            "## 2. PreToolUse hook (save as ~/.kiro/hooks/vaultshield-protect.json, Kiro CLI V3)",
            "",
            _hook_text(HOOK_COMMAND).rstrip(),
            "",
            "The Kiro IDE reads hooks from a project's .kiro/hooks instead; this file covers the CLI.",
            "`vaultshield` must be on Kiro's PATH (install writes the absolute path of the Python that ran it).",
            "",
        ]
    )


# --- install / uninstall -----------------------------------------------------


def _permissions_edit(home: Path, paths: list[str]) -> Edit:
    def apply(old: str) -> str:
        return yamledit.add_fs_read_denies(old, paths)

    def describe(old: str) -> str:
        try:
            parsed = yaml.safe_load(old) if old.strip() else None
        except yaml.YAMLError:
            parsed = None
        added = len([p for p in paths if not yamledit.already_denied(parsed, p)])
        return f"{added} fs_read deny rule{'s' if added != 1 else ''} added"

    return Edit(
        "deny",
        "Deny rules",
        home / PERMISSIONS,
        home / ".kiro",
        "Kiro is not set up here (no ~/.kiro)",
        apply,
        describe,
        yamledit.external_change,
    )


def _hook_edit(home: Path, command: str) -> Edit:
    text = _hook_text(command)

    def external_change(original: str, current: str) -> bool:
        # we own this file; keep a copy only if it is not exactly what we wrote
        try:
            return json.loads(current) != json.loads(text)
        except ValueError:
            return True

    return Edit(
        "hook",
        "Hook",
        home / HOOK_FILE,
        home / ".kiro",
        "Kiro is not set up here (no ~/.kiro)",
        lambda old: text,
        lambda old: "hook",
        external_change,
    )


def install_edits(home: Path, root: Path, manifest: Manifest) -> list[Edit]:
    return [_permissions_edit(home, deny_paths(root, manifest)), _hook_edit(home, hook_command())]


def uninstall_edits(home: Path) -> list[Edit]:
    return [_permissions_edit(home, []), _hook_edit(home, hook_command())]


# --- the hook ----------------------------------------------------------------


def handle_hook(stdin_text: str) -> hookutil.HookOutput:
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("tool_name") not in ("execute_bash", "execute_pwsh"):
            return hookutil.HookOutput(0)  # not a shell call: allow silently
        strings: list[str] = []
        hookutil.collect_strings(payload.get("tool_input"), strings)
        if not strings:
            raise ValueError  # a shell call we cannot read is denied (fail closed)
        command = "\n".join(strings)
        cwd = payload.get("cwd")
    except (ValueError, KeyError, TypeError):
        return _deny(Verdict(False, REASON_UNPARSABLE))

    verdict = hookutil.decide(command, [cwd])
    if verdict.allowed:
        return hookutil.HookOutput(0)
    hookutil.log_violation(AGENT, verdict)
    return _deny(verdict)


def _deny(verdict) -> hookutil.HookOutput:
    # Kiro has no JSON decision: a non-zero exit blocks the tool and stderr reaches
    # the agent. stdout would be added to the agent's context, so an allow is silent.
    return hookutil.HookOutput(2, stderr=hookutil.reason_text(verdict))


# --- doctor ------------------------------------------------------------------


def diagnose(home: Path, root: Path, manifest: Manifest) -> AgentReport:
    placeholders = check_placeholders(root, manifest)
    expected = deny_paths(root, manifest)
    deny = _deny_check(home / PERMISSIONS, expected)
    hook_cli = _hook_check(home / HOOK_FILE)
    hook_ide = Check(NOT_AVAILABLE, "the IDE reads hooks from a project's .kiro/hooks, not ~/.kiro; not installed")
    surface = Surface(
        "Kiro",
        _shown(home / PERMISSIONS),
        [
            ("placeholders", placeholders),
            ("deny rules", deny),
            ("hook (CLI)", hook_cli),
            ("hook (IDE)", hook_ide),
        ],
    )
    notes = [
        "The hook file is " + _shown(home / HOOK_FILE) + " (Kiro CLI V3 reads ~/.kiro/hooks).",
        "fs_read deny rules and the hook constrain shell commands only with the local command",
        "sandbox, which is off by default; the placeholders are the real protection. The absolute",
        "match format for fs_read is not documented for V3 (treated as the most precise option).",
    ]
    return AgentReport(AGENT, [surface], notes, not_covered=sorted(manifest.detected))


def _deny_check(path: Path, expected: list[str]) -> Check:
    if not path.is_file():
        return Check(NOT_ACTIVE, f"permissions.yaml not found; {REINSTALL}")
    try:
        parsed = yaml.safe_load(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return Check(NOT_ACTIVE, "permissions.yaml is not valid YAML")
    have = [p for p in expected if yamledit.already_denied(parsed, p)]
    return agentdoctor.deny_rules_check(have, expected, REINSTALL)


def _hook_check(path: Path) -> Check:
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return Check(NOT_ACTIVE, f"no VaultShield hook; {REINSTALL}")
    for hook in data.get("hooks", []) if isinstance(data, dict) else []:
        if not isinstance(hook, dict) or hook.get("name") != HOOK_NAME:
            continue
        action = hook.get("action") if isinstance(hook.get("action"), dict) else {}
        command = action.get("command")
        if isinstance(command, str):
            try:
                program = shlex.split(command)[0]
            except (ValueError, IndexError):
                program = ""
            if program and os.path.isabs(program) and not os.path.exists(program):
                return Check(NOT_ACTIVE, f"the hook points at a program that no longer exists; {REINSTALL}")
        return Check(ACTIVE, "PreToolUse on the shell tools (Kiro CLI V3)")
    return Check(NOT_ACTIVE, f"no VaultShield hook; {REINSTALL}")


def _shown(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)
