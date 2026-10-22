"""`protect` and `unprotect`: move values between project files and the Keychain.

`protect` order for one file, so a failure can never lose a value:

  1. parse the file; refuse it (changing nothing) if it cannot be parsed
     cleanly;
  2. store every value in the Keychain and READ EACH ONE BACK and compare;
  3. write the manifest;
  4. only then atomically replace the file with `NAME=vs_protected` lines.

If step 2, 3 or 4 fails, everything written so far is rolled back and the file
is untouched. Nothing in this module prints, logs or raises with a value.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from vaultshield.credentials import parse_env_file
from vaultshield.protect import PLACEHOLDER, manifest as manifest_mod
from vaultshield.protect.discovery import KIND_ENV, KIND_SECRET, discover
from vaultshield.protect.envfile import (
    Line,
    encode_record,
    is_empty,
    join_lines,
    split_lines,
)
from vaultshield.protect.fsutil import atomic_write_bytes
from vaultshield.protect.manifest import Manifest
from vaultshield.protect.vault import Vault, VaultError

STATUS_PROTECTED = "protected"
STATUS_UNCHANGED = "unchanged"
STATUS_REFUSED = "refused"


@dataclass
class FileResult:
    """What happened to one env file. Names and line numbers only."""

    path: str
    status: str
    names: list[str] = field(default_factory=list)
    reason: str = ""
    lines: list[int] = field(default_factory=list)


@dataclass
class ProtectReport:
    files: list[FileResult] = field(default_factory=list)
    detected: list[str] = field(default_factory=list)  # reported, not moved

    @property
    def refused(self) -> list[FileResult]:
        return [f for f in self.files if f.status == STATUS_REFUSED]


@dataclass
class Analysis:
    """A file read and understood, without changing anything."""

    lines: list[Line] = field(default_factory=list)
    real: dict[str, list[int]] = field(default_factory=dict)  # name -> line indexes
    placeholders: dict[str, list[int]] = field(default_factory=dict)
    refusal: str = ""
    refusal_lines: list[int] = field(default_factory=list)


def analyze(path: Path) -> Analysis:
    """Reads an env file and groups its variables. Sets `refusal` (and the
    offending line numbers) if the file cannot be protected safely."""
    result = Analysis()
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result.refusal = "the file is not valid UTF-8"
        return result
    except OSError:
        result.refusal = "the file could not be read"
        return result

    lines = split_lines(text)
    result.lines = lines
    bad = [i + 1 for i, line in enumerate(lines) if line.bad]
    if bad:
        result.refusal = (
            "some lines are not KEY=value (for example `export KEY=...` or a "
            "multi-line value), so real values could stay on disk"
        )
        result.refusal_lines = bad
        return result

    # Cross-check against the existing parser the rest of VaultShield uses.
    mine = [(line.key, line.value) for line in lines if line.key is not None]
    try:
        theirs = parse_env_file(path)
    except (OSError, UnicodeDecodeError):
        result.refusal = "the file could not be read"
        return result
    if mine != theirs:
        result.refusal = "the file contains unusual line separators and cannot be parsed safely"
        return result

    for index, line in enumerate(lines):
        if line.key is None or line.value is None or is_empty(line.value):
            continue  # comments, blanks, and empty values (nothing to protect)
        target = result.placeholders if line.value == PLACEHOLDER else result.real
        target.setdefault(line.key, []).append(index)

    mixed = sorted(set(result.real) & set(result.placeholders))
    if mixed:
        result.refusal = (
            "these variables are partly protected and partly not, so it is "
            "unclear which value wins: " + ", ".join(mixed)
        )
    return result


def _rollback_items(vault: Vault, rel: str, previous: dict[str, str | None]) -> None:
    """Puts each Keychain item back as it was before this file's attempt."""
    for name, old in previous.items():
        try:
            if old is None:
                vault.remove(rel, name)
            else:
                vault.store(rel, name, old)
        except VaultError:
            pass  # best effort; the file was never changed


def _protect_env_file(root: Path, rel: str, vault: Vault, manifest: Manifest) -> FileResult:
    path = root / rel
    analysis = analyze(path)
    if analysis.refusal:
        return FileResult(rel, STATUS_REFUSED, reason=analysis.refusal, lines=analysis.refusal_lines)
    if not analysis.real:
        return FileResult(rel, STATUS_UNCHANGED, names=list(manifest.protected.get(rel, [])))

    names = list(analysis.real)  # order of first appearance in the file
    records = {
        name: encode_record([analysis.lines[i].text for i in analysis.real[name]]) for name in names
    }

    # 2. store every value and read each one back before the file is touched.
    previous: dict[str, str | None] = {}
    try:
        for name in names:
            previous[name] = vault.fetch(rel, name)
            vault.store_verified(rel, name, records[name])
    except VaultError as exc:
        _rollback_items(vault, rel, previous)
        return FileResult(rel, STATUS_REFUSED, reason=f"{exc}; the file was not changed")

    # 3. the manifest (names and paths only).
    manifest_before = manifest_mod.load(root)
    updated = copy.deepcopy(manifest)
    existing = updated.protected.get(rel, [])
    updated.protected[rel] = existing + [n for n in names if n not in existing]
    # 4. the file itself.
    new_lines = list(analysis.lines)
    for name in names:
        for i in analysis.real[name]:
            old = new_lines[i]
            new_lines[i] = Line(f"{name}={PLACEHOLDER}", old.ending, name, PLACEHOLDER)
    new_bytes = join_lines(new_lines).encode("utf-8")
    try:
        manifest_mod.save(root, updated)
        atomic_write_bytes(path, new_bytes)
    except OSError:
        if manifest_before is None:
            manifest_mod.delete(root)
        else:
            manifest_mod.save(root, manifest_before)
        _rollback_items(vault, rel, previous)
        return FileResult(rel, STATUS_REFUSED, reason="writing the file failed; nothing was changed")

    manifest.protected[rel] = updated.protected[rel]
    return FileResult(rel, STATUS_PROTECTED, names=names)


def protect_project(root: Path) -> ProtectReport:
    """Protects every env file under `root` and records the secret files it
    only detects. Safe to run again: protected files are left alone."""
    found = discover(root)
    existing = manifest_mod.load(root)
    on_disk = existing.to_dict() if existing else None  # `manifest` below is mutated
    manifest = existing or Manifest(project_id=str(uuid.uuid4()))
    vault = Vault(manifest.project_id)

    report = ProtectReport()
    for item in found:
        if item.kind == KIND_ENV:
            report.files.append(_protect_env_file(root, item.path, vault, manifest))

    # Files that stay on disk with real values are only reported: key files and
    # the like, plus env files that had to be refused.
    detected = {f.path for f in found if f.kind == KIND_SECRET}
    detected |= {f.path for f in report.refused}
    manifest.detected = sorted(detected)
    report.detected = list(manifest.detected)

    # A manifest is created only when there is something to record, but an
    # existing one is always kept current (a detected file may have been removed).
    changed = on_disk != manifest.to_dict()
    worth_saving = on_disk is not None or bool(manifest.protected or manifest.detected)
    if changed and worth_saving:
        manifest_mod.save(root, manifest)
    return report
