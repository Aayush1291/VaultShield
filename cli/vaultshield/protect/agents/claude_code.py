"""Claude Code backstops: `Read` deny rules and a `PreToolUse` hook on `Bash`.

Both go into one file, the user settings `~/.claude/settings.json`, which the CLI,
the VS Code extension and the desktop app all read (FINDINGS.md C3). Secondary to
the placeholders, as everywhere in Protect.

What the docs say and this module relies on (FINDINGS.md, C-rows):

* A `Read(//<absolute path>)` deny rule blocks Claude's file tools and also the
  file-read commands Claude Code recognises in Bash (`cat`, `head`, `tail`, `sed`,
  `tee`) and redirection targets. It does NOT catch a script that opens the file
  itself, so the hook is still a backstop. The `//` prefix is Claude Code's form
  for an absolute path; a single leading `/` would be read relative to the
  settings file.
* The `PreToolUse` hook on `Bash` gets the tool call as JSON on stdin and answers
  with `hookSpecificOutput.permissionDecision`, or blocks by exiting 2. A plain
  hook that crashes or times out fails OPEN, so the handler is installed with
  `onFailure: "block"` (Claude Code v2.1.295+) and the handler also exits 2 on any
  error it catches.

The hook reuses the Antigravity decision logic unchanged (`hookutil.decide`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from vaultshield.protect.agents import agentdoctor, hookutil, jsonedit
from vaultshield.protect.agents.agentdoctor import ACTIVE, NOT_ACTIVE, AgentReport, Check, Surface
from vaultshield.protect.agents.agentinstall import Edit
from vaultshield.protect.agents.doctor import check_placeholders
from vaultshield.protect.agents.install import vaultshield_command
from vaultshield.protect.manifest import Manifest

AGENT = "claude-code"
AGENT_LABEL = "Claude Code"
HOOK_SUBCOMMAND = "claude-code"
INSTALL_NOTES = [
    "One file is written: ~/.claude/settings.json (shared by the CLI, VS Code and the desktop app).",
    "The hook uses onFailure:block, which needs Claude Code v2.1.295 or later to fail closed.",
]

SETTINGS = Path(".claude/settings.json")
DENY_PATH = ["permissions", "deny"]
HOOK_PATH = ["hooks", "PreToolUse"]

HOOK_COMMAND = "vaultshield hook claude-code"  # shown by `render`; install writes the absolute-python form
REINSTALL = "run `agents install claude-code` again"

# gitignore metacharacters, escaped so a literal path that contains one is matched literally
_SPECIAL = re.compile(r"([\\*?\[\]!#])")


def _escape(path_str: str) -> str:
    return _SPECIAL.sub(r"\\\1", path_str)


def deny_rules(root: Path, manifest: Manifest) -> list[str]:
    """`Read(//<absolute path>)` for every protected and detected file."""
    base = root.resolve()
    return [f"Read(/{_escape(str(base / rel))})" for rel in manifest.all_paths()]


def hook_group(command: str = HOOK_COMMAND) -> dict:
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": command, "timeout": 10, "onFailure": "block"}],
    }


def hook_command() -> str:
    return shlex.join([*vaultshield_command(), "hook", HOOK_SUBCOMMAND])


# --- render ------------------------------------------------------------------


def render(root: Path, manifest: Manifest) -> str:
    rules = deny_rules(root, manifest)
    return "\n".join(
        [
            "# VaultShield for Claude Code.",
            "# This only PRINTS the configuration. Nothing was installed or changed.",
            "",
            "## 1. Deny rules (absolute paths; `//` is Claude Code's absolute form)",
            "",
            json.dumps({"permissions": {"deny": rules}}, indent=2),
            "",
            'Merge the "deny" list into ~/.claude/settings.json (read by the CLI, the VS Code extension',
            "and the desktop app). A Read deny rule also blocks Edit/Write and the file-read commands",
            "cat/head/tail/sed/tee and redirections; it does not catch a script that opens the file.",
            "",
            "## 2. PreToolUse hook on Bash (a backstop for the terminal route)",
            "",
            json.dumps({"hooks": {"PreToolUse": [hook_group()]}}, indent=2),
            "",
            "Merge into the same settings.json. `onFailure: block` makes a hook crash or timeout block",
            "the call; it needs Claude Code v2.1.295 or later. `vaultshield` must be on Claude Code's PATH",
            "(install writes the absolute path of the Python that ran it instead).",
            "",
        ]
    )


# --- reading the parsed file -------------------------------------------------


def _parsed(text: str) -> dict:
    data = json.loads(text) if text.strip() else {}
    return data if isinstance(data, dict) else {}


def _deny_in(data: dict) -> list[str]:
    node: object = data
    for key in DENY_PATH:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return [r for r in node if isinstance(r, str)] if isinstance(node, list) else []


def _groups_in(data: dict) -> list[dict]:
    node: object = data
    for key in HOOK_PATH:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return [g for g in node if isinstance(g, dict)] if isinstance(node, list) else []


def _is_our_group(group: dict) -> bool:
    for handler in group.get("hooks") or []:
        command = handler.get("command") if isinstance(handler, dict) else None
        if isinstance(command, str):
            try:
                words = shlex.split(command)
            except ValueError:
                continue
            if words[-2:] == ["hook", HOOK_SUBCOMMAND]:
                return True
    return False


# --- install / uninstall -----------------------------------------------------


def _describe(rules: list[str]):
    def describe(old: str) -> str:
        data = _parsed(old)
        have = set(_deny_in(data))
        added = len([r for r in rules if r not in have])
        parts = [f"{added} deny rule{'s' if added != 1 else ''}"]
        if not any(_is_our_group(g) for g in _groups_in(data)):
            parts.append("hook")
        return ", ".join(parts) + " added"

    return describe


def _external_change(original: str, current: str) -> bool:
    """Whether `current` holds anything beyond the original plus our own deny
    rules and hook group. Mirrors the Antigravity uninstall check: drop the deny
    rules that were not in the original, drop our hook group, compare the rest."""
    try:
        orig = _parsed(original)
        reduced = json.loads(current)
    except ValueError:
        return True
    if not isinstance(reduced, dict):
        return True
    orig_deny = set(_deny_in(orig))
    perm = reduced.get("permissions")
    if isinstance(perm, dict) and isinstance(perm.get("deny"), list):
        perm["deny"] = [r for r in perm["deny"] if r in orig_deny]
        if not perm["deny"]:
            perm.pop("deny")
        if perm == {}:
            reduced.pop("permissions")
    hooks = reduced.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get("PreToolUse"), list):
        hooks["PreToolUse"] = [g for g in hooks["PreToolUse"] if not (isinstance(g, dict) and _is_our_group(g))]
        if not hooks["PreToolUse"]:
            hooks.pop("PreToolUse")
        if hooks == {}:
            reduced.pop("hooks")
    return reduced != orig


def _edit(home: Path, apply, describe) -> Edit:
    return Edit(
        "settings",
        "Settings (deny rules + hook)",
        home / SETTINGS,
        home / SETTINGS.parent,
        "Claude Code is not set up here (no ~/.claude)",
        apply,
        describe,
        _external_change,
    )


def install_edits(home: Path, root: Path, manifest: Manifest) -> list[Edit]:
    rules = deny_rules(root, manifest)
    group = hook_group(hook_command())

    def apply(old: str) -> str:
        text = jsonedit.add_to_string_array(old, DENY_PATH, rules)
        return jsonedit.add_object_to_array(text, HOOK_PATH, group)

    return [_edit(home, apply, _describe(rules))]


def uninstall_edits(home: Path) -> list[Edit]:
    # uninstall restores from the backup; only the path and external_change are used.
    return [_edit(home, lambda old: old, lambda old: "")]


# --- the hook ----------------------------------------------------------------


def handle_hook(stdin_text: str) -> hookutil.HookOutput:
    """Reads the PreToolUse payload on stdin, returns the decision. Never raises;
    any failure to read the payload is a denial (fail closed)."""
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("tool_name") != "Bash":
            return hookutil.HookOutput(0)  # not a shell call: allow silently
        command = payload["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError
        cwd = payload.get("cwd")
    except (ValueError, KeyError, TypeError):
        return _deny(hookutil.Verdict(False, hookutil.REASON_UNPARSABLE))

    verdict = hookutil.decide(command, [cwd])
    if verdict.allowed:
        return hookutil.HookOutput(0)
    hookutil.log_violation(AGENT, verdict)
    return _deny(verdict)


def _deny(verdict) -> hookutil.HookOutput:
    reason = hookutil.reason_text(verdict)
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    # exit 2 blocks the call on every version; the JSON carries the clean reason.
    return hookutil.HookOutput(2, stdout=json.dumps(decision), stderr=reason)


# --- doctor ------------------------------------------------------------------


def diagnose(home: Path, root: Path, manifest: Manifest) -> AgentReport:
    placeholders = check_placeholders(root, manifest)
    expected = deny_rules(root, manifest)
    path = home / SETTINGS
    deny, hook = _check_file(path, expected)
    surface = Surface(
        "Claude Code",
        _shown(path),
        [("placeholders", placeholders), ("deny rules", deny), ("hook", hook)],
    )
    notes = [
        "One settings file is read by the CLI, the VS Code extension and the desktop app.",
        "The hook is installed with onFailure:block, which needs Claude Code v2.1.295 or later;",
        "on an older version a hook crash or timeout would not block the command.",
    ]
    return AgentReport(AGENT, [surface], notes, not_covered=sorted(manifest.detected))


def _check_file(path: Path, expected: list[str]) -> tuple[Check, Check]:
    if not path.is_file():
        note = "file not found; " + REINSTALL
        return Check(NOT_ACTIVE, note), Check(NOT_ACTIVE, note)
    try:
        data = _parsed(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        bad = Check(NOT_ACTIVE, "file is not valid JSON")
        return bad, bad
    deny = agentdoctor.deny_rules_check(_deny_in(data), expected, REINSTALL)
    return deny, _hook_check(data)


def _hook_check(data: dict) -> Check:
    for group in _groups_in(data):
        if not _is_our_group(group):
            continue
        for handler in group.get("hooks") or []:
            command = handler.get("command") if isinstance(handler, dict) else None
            if isinstance(command, str):
                try:
                    program = shlex.split(command)[0]
                except (ValueError, IndexError):
                    continue
                if os.path.isabs(program) and not os.path.exists(program):
                    return Check(NOT_ACTIVE, f"the hook points at a program that no longer exists; {REINSTALL}")
        return Check(ACTIVE, "PreToolUse on Bash, onFailure:block")
    return Check(NOT_ACTIVE, f"no VaultShield hook; {REINSTALL}")


def _shown(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)
