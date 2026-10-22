"""Install and remove the Antigravity backstops: exact-path deny rules in the CLI
and App settings, and one PreToolUse hook in the global hooks.json.

Merge-only. Every file is edited by inserting text (see `jsonedit`), so every
other key and every existing grant stays byte for byte identical. Before a file
is first changed, a copy is kept next to it:

    <file>.vaultshield.bak      the original bytes
    <file>.vaultshield.absent   marker: the file did not exist (uninstall removes it)

Nothing here reads or writes outside `home`. Callers pass the home directory, and
the tests always pass a temporary one.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from vaultshield.protect.agents import antigravity, jsonedit
from vaultshield.protect.fsutil import DEFAULT_MODE, atomic_write_bytes
from vaultshield.protect.manifest import Manifest

CLI_SETTINGS = Path(".gemini/antigravity-cli/settings.json")
APP_CONFIG = Path(".gemini/config/config.json")
GLOBAL_HOOKS = Path(".gemini/config/hooks.json")

CLI_DENY_PATH = ["permissions", "deny"]
APP_DENY_PATH = ["userSettings", "globalPermissionGrants", "deny"]

BACKUP_SUFFIX = ".vaultshield.bak"
ABSENT_SUFFIX = ".vaultshield.absent"

KIND_CLI, KIND_APP, KIND_HOOK = "cli", "app", "hook"


class InstallError(Exception):
    """Nothing was changed (or everything was rolled back). No file content in the message."""


@dataclass(frozen=True)
class Target:
    kind: str
    label: str
    path: Path
    create_in: Path | None  # a missing file is created only if this directory exists
    missing_note: str


@dataclass
class Action:
    target: Target
    status: str  # created | updated | unchanged | skipped | restored | removed | nothing
    detail: str = ""
    added: int = 0


def targets(home: Path) -> list[Target]:
    return [
        Target(
            KIND_CLI,
            "CLI settings",
            home / CLI_SETTINGS,
            home / CLI_SETTINGS.parent,
            "the Antigravity CLI is not set up here (no ~/.gemini/antigravity-cli)",
        ),
        # The App owns config.json, so a missing one is not invented.
        Target(KIND_APP, "App config", home / APP_CONFIG, None, "config.json not found; the App is not set up here"),
        Target(
            KIND_HOOK,
            "Hook",
            home / GLOBAL_HOOKS,
            home / GLOBAL_HOOKS.parent,
            "Antigravity is not set up here (no ~/.gemini/config)",
        ),
    ]


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def absent_marker(path: Path) -> Path:
    return path.with_name(path.name + ABSENT_SUFFIX)


def vaultshield_command() -> list[str]:
    """How to run VaultShield from a hook: an absolute path, because the process
    that runs hooks may not have the shell's PATH."""
    sibling = Path(sys.executable).parent / "vaultshield"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    found = shutil.which("vaultshield")
    if found:
        return [os.path.abspath(found)]
    return [sys.executable, "-m", "vaultshield.cli"]


def hook_command() -> str:
    return shlex.join([*vaultshield_command(), "hook", "antigravity"])


# --- planning ----------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise InstallError(f"{path.name} is not valid UTF-8; nothing was changed") from None
    except OSError:
        raise InstallError(f"{path.name} could not be read; nothing was changed") from None


def _deny_path(kind: str) -> list[str]:
    return CLI_DENY_PATH if kind == KIND_CLI else APP_DENY_PATH


def existing_rules(text: str, kind: str) -> list[str]:
    """The deny rules already in `text` (empty if there are none or it is not JSON)."""
    try:
        node = json.loads(text) if text.strip() else {}
        for key in _deny_path(kind):
            node = node[key]
    except (ValueError, KeyError, TypeError):
        return []
    return [r for r in node if isinstance(r, str)] if isinstance(node, list) else []


def _expected(old: object, kind: str, rules: list[str], entry: dict) -> object:
    """What the file should mean after the edit, computed on parsed data, to
    check the text edit against."""
    data = json.loads(json.dumps(old))
    if kind == KIND_HOOK:
        data[antigravity.HOOK_NAME] = entry
        return data
    path = _deny_path(kind)
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    deny = node.setdefault(path[-1], [])
    deny.extend(r for r in dict.fromkeys(rules) if r not in deny)
    return data


def _edit(target: Target, text: str, rules: list[str], entry: dict) -> str:
    try:
        if target.kind == KIND_HOOK:
            new = jsonedit.set_member(text, [], antigravity.HOOK_NAME, entry)
        else:
            new = jsonedit.add_to_string_array(text, _deny_path(target.kind), rules)
    except jsonedit.JsonEditError as exc:
        raise InstallError(f"{target.path.name}: {exc}; nothing was changed") from None
    old = json.loads(text) if text.strip() else {}
    if json.loads(new) != _expected(old, target.kind, rules, entry):
        raise InstallError(f"{target.path.name}: the edit did not come out as planned; nothing was changed")
    return new


def install(home: Path, root: Path, manifest: Manifest) -> list[Action]:
    """Writes the deny rules and the hook under `home`. All files are planned and
    checked before any is written; if a write fails, the ones already written are
    put back."""
    rules = antigravity.deny_rules(root, manifest)
    entry = antigravity.hook_config(hook_command())[antigravity.HOOK_NAME]

    plan: list[tuple[Target, str | None, str]] = []  # (target, old text or None if absent, new text)
    actions: list[Action] = []
    for target in targets(home):
        if target.path.is_file():
            old: str | None = _read(target.path)
        elif target.create_in is not None and target.create_in.is_dir():
            old = None
        else:
            actions.append(Action(target, "skipped", target.missing_note))
            continue
        new = _edit(target, old or "", rules, entry)
        if old is not None and new == old:
            actions.append(Action(target, "unchanged"))
            continue
        plan.append((target, old, new))
        added = 0
        if target.kind != KIND_HOOK:
            have = set(existing_rules(old or "", target.kind))
            added = len([r for r in dict.fromkeys(rules) if r not in have])
        actions.append(Action(target, "created" if old is None else "updated", added=added))

    written: list[tuple[Target, bytes | None, list[Path]]] = []
    try:
        for target, old, new in plan:
            made: list[Path] = []
            backup, marker = backup_path(target.path), absent_marker(target.path)
            if not backup.exists() and not marker.exists():
                if old is not None:
                    shutil.copy2(target.path, backup)
                    made.append(backup)
                else:
                    marker.write_bytes(b"")
                    made.append(marker)
            original = target.path.read_bytes() if old is not None else None
            written.append((target, original, made))
            atomic_write_bytes(target.path, new.encode("utf-8"), mode=None if old is not None else DEFAULT_MODE)
    except OSError:
        clean = True
        for target, original, made in reversed(written):
            try:
                if original is None:
                    target.path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(target.path, original)
                for extra in made:
                    extra.unlink(missing_ok=True)
            except OSError:
                clean = False
        raise InstallError(
            "writing failed; every file was put back as it was"
            if clean
            else "writing failed and some files could not be put back; the .vaultshield.bak copies next to them hold the originals"
        ) from None
    return actions
