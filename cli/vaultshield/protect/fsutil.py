"""Atomic file replacement."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

DEFAULT_MODE = 0o644


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Replaces `path` with `data` in one step: write a temp file in the same
    directory, then `os.replace` it over `path`. A crash leaves either the old
    file or the new one, never a partial file. The temp file is removed on any
    error.

    `mode` defaults to the existing file's permissions (or 0644 for a new one).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else DEFAULT_MODE
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
