"""A generic, merge-only installer for the Claude Code, Kiro and Codex backstops.

The Antigravity installer (`install.py`) is left untouched. This module does the
same job for the other three agents, driven by a list of `Edit`s that each agent
builds: one per file it touches. The machinery here is the same as `install.py`'s
and reuses its backup suffixes and its `InstallError`:

* before a file is first changed, the original bytes are copied to
  `<file>.vaultshield.bak`, or a `<file>.vaultshield.absent` marker is written if
  the file did not exist;
* every file is planned and checked before any is written; a failed write rolls
  the written ones back;
* `uninstall` restores each file from its copy (byte for byte) and removes the
  copy. If the file changed since the install in a way that is not just our own
  additions, the current version is first kept as `<file>.vaultshield.before-uninstall`.

Each `Edit` carries the small, agent-specific pieces: how to splice the file
(`apply`), how to describe what changed (`describe`), and how to tell whether
anything beyond our own additions changed since the install (`external_change`).
"""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vaultshield.protect.agents.install import (
    ABSENT_SUFFIX,
    BACKUP_SUFFIX,
    SAVED_SUFFIX,
    InstallError,
    absent_marker,
    backup_path,
)
from vaultshield.protect.fsutil import DEFAULT_MODE, atomic_write_bytes

__all__ = ["Edit", "Action", "InstallError", "install", "uninstall"]


@dataclass(frozen=True)
class Edit:
    kind: str  # "deny" | "hook" | "profile" ... used only for reporting
    label: str
    path: Path
    create_in: Path | None  # a missing file is created only if this directory exists
    missing_note: str
    apply: Callable[[str], str]  # old text ("" if absent) -> new text; raises InstallError
    describe: Callable[[str], str]  # old text -> what will change, for the report
    external_change: Callable[[str, str], bool]  # (original text, current text) -> something besides ours changed


@dataclass
class Action:
    kind: str
    label: str
    path: Path
    status: str  # created | updated | unchanged | skipped | restored | removed | nothing
    detail: str = ""


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise InstallError(f"{path.name} is not valid UTF-8; nothing was changed") from None
    except OSError:
        raise InstallError(f"{path.name} could not be read; nothing was changed") from None


def install(edits: list[Edit]) -> list[Action]:
    """Applies every edit. All files are planned and checked first; if a write
    fails, the ones already written are put back."""
    plan: list[tuple[Edit, str | None, str]] = []
    actions: list[Action] = []
    for edit in edits:
        if edit.path.is_file():
            old: str | None = _read(edit.path)
        elif edit.create_in is not None and edit.create_in.is_dir():
            old = None
        else:
            actions.append(Action(edit.kind, edit.label, edit.path, "skipped", edit.missing_note))
            continue
        new = edit.apply(old or "")
        if old is not None and new == old:
            actions.append(Action(edit.kind, edit.label, edit.path, "unchanged"))
            continue
        plan.append((edit, old, new))
        actions.append(Action(edit.kind, edit.label, edit.path, "created" if old is None else "updated", edit.describe(old or "")))

    written: list[tuple[Edit, bytes | None, list[Path]]] = []
    try:
        for edit, old, new in plan:
            made: list[Path] = []
            backup, marker = backup_path(edit.path), absent_marker(edit.path)
            if not backup.exists() and not marker.exists():
                if old is not None:
                    shutil.copy2(edit.path, backup)
                    made.append(backup)
                else:
                    marker.write_bytes(b"")
                    made.append(marker)
            original = edit.path.read_bytes() if old is not None else None
            written.append((edit, original, made))
            atomic_write_bytes(edit.path, new.encode("utf-8"), mode=None if old is not None else DEFAULT_MODE)
    except OSError:
        clean = True
        for edit, original, made in reversed(written):
            try:
                if original is None:
                    edit.path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(edit.path, original)
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


def _keep_before_uninstall(path: Path, current: bytes) -> Path:
    saved = path.with_name(path.name + SAVED_SUFFIX)
    number = 1
    while saved.exists():
        saved = path.with_name(f"{path.name}{SAVED_SUFFIX}.{number}")
        number += 1
    saved.write_bytes(current)
    return saved


def uninstall(edits: list[Edit]) -> list[Action]:
    """Puts every touched file back exactly as it was before the first install,
    then removes the copies. Takes the same edit list as `install` so it knows
    which files to look at and how to tell an external change from our own."""
    actions: list[Action] = []
    for edit in edits:
        backup, marker = backup_path(edit.path), absent_marker(edit.path)
        if not backup.is_file() and not marker.is_file():
            actions.append(Action(edit.kind, edit.label, edit.path, "nothing", "no VaultShield install found"))
            continue

        original_bytes = backup.read_bytes() if backup.is_file() else b""
        note = ""
        if edit.path.is_file():
            current = edit.path.read_bytes()
            try:
                original_text = original_bytes.decode("utf-8")
                current_text = current.decode("utf-8")
                changed = edit.external_change(original_text, current_text)
            except UnicodeDecodeError:
                changed = current != original_bytes
            if changed:
                saved = _keep_before_uninstall(edit.path, current)
                note = f"it had changed since the install; that version is kept as {saved.name}"

        if backup.is_file():
            atomic_write_bytes(edit.path, original_bytes, mode=stat.S_IMODE(backup.stat().st_mode))
            if edit.path.read_bytes() != original_bytes:
                raise InstallError(f"{edit.path.name} did not read back correctly; the copy was kept")
            backup.unlink()
            actions.append(Action(edit.kind, edit.label, edit.path, "restored", note))
        else:
            edit.path.unlink(missing_ok=True)
            marker.unlink()
            actions.append(Action(edit.kind, edit.label, edit.path, "removed", note or "it did not exist before the install"))
    return actions
