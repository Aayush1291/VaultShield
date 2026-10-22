"""Find the files in a project that hold secrets.

Two kinds are reported:

* `env`    — `.env` and `.env.*` files. `protect` can move their values into
             the Keychain.
* `secret` — keys, certificates and credential files in other formats
             (`*.pem`, `*.key`, `id_rsa*`, `credentials.json`, `.npmrc`,
             `.pypirc`, `.netrc`, `.aws/credentials`). These are only
             detected and reported; `protect` never moves them.

Discovery reads directory entries only. It never opens a file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

KIND_ENV = "env"
KIND_SECRET = "secret"

# Directories never searched. `.vaultshield` is skipped because the Docker
# pipeline writes dummy `.env.sandbox` files under `.vaultshield/runs/`.
SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", ".vaultshield"})

# `.env.example` and `.env.sample` are templates, not secrets. `.env.template`
# is deliberately NOT skipped: only example and sample are named as templates.
_TEMPLATE_SUFFIXES = (".example", ".sample")

_SECRET_NAMES = frozenset({"credentials.json", ".npmrc", ".pypirc", ".netrc"})


@dataclass(frozen=True, order=True)
class Discovered:
    """One file found. `path` is relative to the project root, with `/`."""

    path: str
    kind: str


def classify(rel_path: str) -> str | None:
    """The kind of secret file `rel_path` (posix, relative) is, or None."""
    name = rel_path.rsplit("/", 1)[-1]
    if name == ".env" or name.startswith(".env."):
        return None if name.endswith(_TEMPLATE_SUFFIXES) else KIND_ENV
    if rel_path == ".aws/credentials" or rel_path.endswith("/.aws/credentials"):
        return KIND_SECRET
    if name.endswith((".pem", ".key")):
        return KIND_SECRET
    # `id_rsa.pub` is a public key, not a secret.
    if name.startswith("id_rsa") and not name.endswith(".pub"):
        return KIND_SECRET
    if name in _SECRET_NAMES:
        return KIND_SECRET
    return None


def discover(root: Path) -> list[Discovered]:
    """Every secret file under `root`, sorted by path.

    Symlinks (to files or directories) are never followed or reported.
    """
    found: list[Discovered] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = Path(os.path.relpath(full, root)).as_posix()
            kind = classify(rel)
            if kind is not None:
                found.append(Discovered(rel, kind))
    return sorted(found)
