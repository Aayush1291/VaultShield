"""`.vaultshield/protect.json`: which files are protected.

Holds names and paths only: the project id, each protected file's path with
its variable NAMES, and the paths of secret files that were detected but not
moved. Never a value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from vaultshield.protect.fsutil import atomic_write_bytes

MANIFEST_DIR = ".vaultshield"
MANIFEST_NAME = "protect.json"
VIOLATIONS_NAME = "violations.jsonl"
MANIFEST_VERSION = 1


class ManifestError(Exception):
    """`protect.json` exists but is not a valid manifest."""


@dataclass
class Manifest:
    project_id: str
    # project-relative posix path -> variable names, in file order
    protected: dict[str, list[str]] = field(default_factory=dict)
    # secret files that were found but not moved (key files, credentials, and
    # env files that could not be parsed and so still hold real values)
    detected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": MANIFEST_VERSION,
            "project_id": self.project_id,
            "protected": {k: list(v) for k, v in sorted(self.protected.items())},
            "detected": sorted(self.detected),
        }

    def all_paths(self) -> list[str]:
        """Every file the manifest covers, sorted and without duplicates."""
        return sorted(set(self.protected) | set(self.detected))


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_DIR / MANIFEST_NAME


def violations_path(root: Path) -> Path:
    return root / MANIFEST_DIR / VIOLATIONS_NAME


def _safe_relative(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts


def parse_manifest(data: object) -> Manifest:
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        raise ManifestError("unsupported or malformed manifest")
    project_id = data.get("project_id")
    protected = data.get("protected")
    detected = data.get("detected")
    if not isinstance(project_id, str) or not project_id:
        raise ManifestError("manifest has no project id")
    if not isinstance(protected, dict) or not isinstance(detected, list):
        raise ManifestError("manifest is malformed")
    for path, names in protected.items():
        if not _safe_relative(path) or not isinstance(names, list):
            raise ManifestError("manifest has an invalid protected entry")
        if not all(isinstance(n, str) and n for n in names):
            raise ManifestError("manifest has an invalid variable name")
    if not all(isinstance(p, str) and _safe_relative(p) for p in detected):
        raise ManifestError("manifest has an invalid detected path")
    return Manifest(project_id, {k: list(v) for k, v in protected.items()}, list(detected))


def load(root: Path) -> Manifest | None:
    """The manifest at `root`, or None if there is none."""
    path = manifest_path(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return parse_manifest(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ManifestError("manifest is not valid JSON") from None


def save(root: Path, manifest: Manifest) -> None:
    text = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n"
    atomic_write_bytes(manifest_path(root), text.encode("utf-8"), mode=0o644)


def delete(root: Path) -> None:
    """Removes the manifest file (used to roll back a manifest that was never
    on disk before)."""
    manifest_path(root).unlink(missing_ok=True)
    try:
        (root / MANIFEST_DIR).rmdir()  # only if now empty
    except OSError:
        pass


def find_root(start: Path) -> Path | None:
    """The nearest directory at or above `start` that has a manifest."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if manifest_path(candidate).is_file():
            return candidate
    return None
