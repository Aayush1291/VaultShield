"""Codex backstops: a filesystem deny profile and a `PreToolUse` hook on `Bash`.

From the docs (FINDINGS.md, X-rows), both layers are CONFIRMED:

* A beta permission profile `[permissions.vaultshield]` in `~/.codex/config.toml`
  that extends `:workspace` and denies reads of each protected file by absolute
  path. On macOS the Seatbelt sandbox enforces it for commands Codex runs. The
  profile only takes effect once it is selected with `default_permissions`, and
  it is ignored if `sandbox_mode` is set anywhere. Per the task, install writes
  the profile but NEVER sets `default_permissions` or touches `sandbox_mode`;
  `doctor` reports it as defined-not-selected and prints the one line to turn it
  on.
* A `PreToolUse` hook on `Bash` in `~/.codex/hooks.json`. It blocks by exiting 2
  with the reason on stderr. Codex has no fail-closed flag, so a hook that crashes
  or times out lets the command through (X11, fail-open); the handler still exits
  2 on any error it can catch.

The hook reuses the Antigravity decision logic unchanged (`hookutil.decide`).
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from vaultshield.protect.agents import agentdoctor, hookutil, jsonedit, tomledit
from vaultshield.protect.agents.agentdoctor import ACTIVE, NOT_ACTIVE, AgentReport, Check, Surface
from vaultshield.protect.agents.agentinstall import Edit
from vaultshield.protect.agents.antigravity import REASON_UNPARSABLE, Verdict
from vaultshield.protect.agents.doctor import check_placeholders
from vaultshield.protect.agents.install import vaultshield_command
from vaultshield.protect.manifest import Manifest

AGENT = "codex"
AGENT_LABEL = "Codex"
HOOK_SUBCOMMAND = "codex"

CONFIG = Path(".codex/config.toml")
HOOKS = Path(".codex/hooks.json")
HOOK_PATH = ["hooks", "PreToolUse"]

ENABLE_LINE = 'default_permissions = "vaultshield"'
HOOK_COMMAND = "vaultshield hook codex"  # shown by render; install writes the absolute-python form
REINSTALL = "run `agents install codex` again"

INSTALL_NOTES = [
    "Two files under ~/.codex are written: config.toml (the permission profile) and hooks.json (the hook).",
    "The profile is defined but NOT selected, so it does nothing until you add this line to",
    f"~/.codex/config.toml:  {ENABLE_LINE}",
    "It is also ignored while sandbox_mode is set anywhere in your Codex config (doctor checks this).",
]


def deny_paths(root: Path, manifest: Manifest) -> list[str]:
    base = root.resolve()
    return [str(base / rel) for rel in manifest.all_paths()]


def hook_group(command: str = HOOK_COMMAND) -> dict:
    return {"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": 10}]}


def hook_command() -> str:
    return shlex.join([*vaultshield_command(), "hook", HOOK_SUBCOMMAND])


# --- render ------------------------------------------------------------------


def render(root: Path, manifest: Manifest) -> str:
    paths = deny_paths(root, manifest)
    return "\n".join(
        [
            "# VaultShield for Codex.",
            "# This only PRINTS the configuration. Nothing was installed or changed.",
            "",
            "## 1. Permission profile (merge into ~/.codex/config.toml)",
            "",
            tomledit.profile_body(paths, "\n").rstrip(),
            "",
            "This defines the profile but does NOT select it. To turn it on, add to ~/.codex/config.toml:",
            f"    {ENABLE_LINE}",
            "The profile is ignored while sandbox_mode is set anywhere in your config.",
            "",
            "## 2. PreToolUse hook on Bash (save/merge into ~/.codex/hooks.json)",
            "",
            json.dumps({"hooks": {"PreToolUse": [hook_group()]}}, indent=2),
            "",
            "Codex trusts a new hook only after you review it with /hooks. It has no fail-closed flag:",
            "a hook that errors or times out lets the command through. `vaultshield` must be on Codex's PATH.",
            "",
        ]
    )


# --- install / uninstall -----------------------------------------------------


def _groups_in(data: object) -> list[dict]:
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


def _profile_edit(home: Path, paths: list[str]) -> Edit:
    def apply(old: str) -> str:
        return tomledit.set_profile(old, paths)

    def describe(old: str) -> str:
        have = tomledit.profile_denies(old)
        added = len([p for p in paths if have.get(p) != "deny"])
        return f"{added} deny entr{'ies' if added != 1 else 'y'} in the profile"

    return Edit(
        "profile",
        "Permission profile",
        home / CONFIG,
        home / ".codex",
        "Codex is not set up here (no ~/.codex)",
        apply,
        describe,
        tomledit.external_change,
    )


def _hook_edit(home: Path, command: str) -> Edit:
    group = hook_group(command)

    def apply(old: str) -> str:
        return jsonedit.add_object_to_array(old, HOOK_PATH, group)

    def describe(old: str) -> str:
        return "hook added" if not any(_is_our_group(g) for g in _groups_in(_loads(old))) else "unchanged"

    def external_change(original: str, current: str) -> bool:
        try:
            orig = _loads(original)
            reduced = json.loads(current)
        except ValueError:
            return True
        if not isinstance(reduced, dict):
            return True
        hooks = reduced.get("hooks")
        if isinstance(hooks, dict) and isinstance(hooks.get("PreToolUse"), list):
            hooks["PreToolUse"] = [g for g in hooks["PreToolUse"] if not (isinstance(g, dict) and _is_our_group(g))]
            if not hooks["PreToolUse"]:
                hooks.pop("PreToolUse")
            if hooks == {}:
                reduced.pop("hooks")
        return reduced != (orig if isinstance(orig, dict) else {})

    return Edit(
        "hook",
        "Hook",
        home / HOOKS,
        home / ".codex",
        "Codex is not set up here (no ~/.codex)",
        apply,
        describe,
        external_change,
    )


def _loads(text: str) -> dict:
    data = json.loads(text) if text.strip() else {}
    return data if isinstance(data, dict) else {}


def install_edits(home: Path, root: Path, manifest: Manifest) -> list[Edit]:
    return [_profile_edit(home, deny_paths(root, manifest)), _hook_edit(home, hook_command())]


def uninstall_edits(home: Path) -> list[Edit]:
    return [_profile_edit(home, []), _hook_edit(home, hook_command())]


# --- the hook ----------------------------------------------------------------


def handle_hook(stdin_text: str) -> hookutil.HookOutput:
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
        return _deny(Verdict(False, REASON_UNPARSABLE))

    verdict = hookutil.decide(command, [cwd])
    if verdict.allowed:
        return hookutil.HookOutput(0)
    hookutil.log_violation(AGENT, verdict)
    return _deny(verdict)


def _deny(verdict) -> hookutil.HookOutput:
    # Codex blocks on exit 2 with the reason on stderr.
    return hookutil.HookOutput(2, stderr=hookutil.reason_text(verdict))


# --- doctor ------------------------------------------------------------------


def diagnose(home: Path, root: Path, manifest: Manifest) -> AgentReport:
    placeholders = check_placeholders(root, manifest)
    expected = deny_paths(root, manifest)
    config_path = home / CONFIG
    deny, selection, sandbox = _profile_check(config_path, expected)
    hook = _hook_check(home / HOOKS)
    surface = Surface(
        "Codex",
        _shown(config_path),
        [("placeholders", placeholders), ("deny profile", deny), ("hook", hook)],
    )
    notes = [selection]
    if sandbox:
        notes.append(sandbox)
    notes.append(
        "The hook file is " + _shown(home / HOOKS) + "; Codex trusts a new hook only after /hooks review,"
    )
    notes.append("and it has no fail-closed flag, so a hook crash or timeout lets the command through.")
    return AgentReport(AGENT, [surface], notes, not_covered=sorted(manifest.detected))


def _profile_check(path: Path, expected: list[str]) -> tuple[Check, str, str]:
    if not path.is_file():
        return Check(NOT_ACTIVE, f"config.toml not found; {REINSTALL}"), f"To turn the profile on later: {ENABLE_LINE}", ""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        bad = Check(NOT_ACTIVE, "config.toml is not valid UTF-8")
        return bad, "", ""
    denies = tomledit.profile_denies(text)
    have = [p for p in expected if denies.get(p) == "deny"]
    deny = agentdoctor.deny_rules_check(have, expected, REINSTALL)
    selected = tomledit.selected_profile(text)
    if selected == "vaultshield":
        selection = "The profile is selected (default_permissions = \"vaultshield\"); it is in force."
    else:
        selection = f'The profile is defined, not selected. To turn it on, add to config.toml:  {ENABLE_LINE}'
    sandbox = ""
    if tomledit.has_sandbox_mode(text):
        sandbox = "WARNING: sandbox_mode is set, so Codex ignores permission profiles; remove it to use this one."
    return deny, selection, sandbox


def _hook_check(path: Path) -> Check:
    try:
        data = _loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return Check(NOT_ACTIVE, f"no VaultShield hook; {REINSTALL}")
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
        return Check(ACTIVE, "PreToolUse on Bash (trust it once with /hooks)")
    return Check(NOT_ACTIVE, f"no VaultShield hook; {REINSTALL}")


def _shown(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)
