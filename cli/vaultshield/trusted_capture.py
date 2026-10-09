"""Trusted_Capture_Module: build the run branch and `diff.json` with a git the
agent cannot tamper with (Batch 3 FR5, D9 option b, design §5.4.1).

The agent runs as root in the Sandbox, so everything in it is untrusted: its
`git` binary, its `.git` (config, hooks, refs, `info/*`, alternates, index,
replace refs, grafts) and every process it leaves behind. This module therefore
uses the Sandbox only as a data source read through the Docker API
(`container.get_archive`), so no in-container binary runs during capture:

* **5-base** (`record_trusted_base`), before any agent code (including the
  `agy` sign-in): the base commit id from `.git/HEAD` and its ref, the object
  format, and a filtered snapshot of `.git/objects`.
* **step 8**: `kill_sandbox` stops the Sandbox (no file can grow mid-archive
  and nothing can change between the last credential snapshot and the
  archive), `archive_sandbox_repo` streams the work tree out through
  `split_archive`'s fail-closed filter, and a fresh **capture helper**
  container (`TrustedCapture`, same image ID, `network_mode="none"`, no
  capabilities) rebuilds the repository from the 5-base objects plus the
  filtered work tree. `git_capture`'s unchanged functions then run on the
  helper, which has `Sandbox.exec`'s contract.

Nothing is extracted on the host: archives pass through as tar streams,
spooled to anonymous temp files (memory first), so non-UTF-8 and case-variant
names survive on a macOS host (APFS cannot store them).

Every Docker error is mapped to `GitCaptureError` (or `SandboxError` for
`TrustedCapture.exec`), so the developer sees one line, never a traceback.
"""

from __future__ import annotations

import io
import posixpath
import re
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, Iterator

import click
import docker
import requests
import urllib3

from vaultshield.git_capture import (
    _GIT_ENV,
    _UNSAFE_CONFIG_REGEX,
    GitCaptureError,
    UnsafeGitConfig,
    _git,
)
from vaultshield.monitor import EventLogRecord, _now_iso8601
from vaultshield.sandbox import (
    KEEP_ALIVE_CMD,
    REPO_DIR,
    WORKDIR,
    ExecResult,
    SandboxError,
    _decode,
)

# Size limits (§5.4.1 step 3). Each applies to each stream on its own.
TRUSTED_CAPTURE_MAX_STREAM_BYTES = 4 << 30
TRUSTED_CAPTURE_MAX_MEMBERS = 1_000_000
TRUSTED_CAPTURE_MAX_MEMBER_BYTES = 1 << 30
TRUSTED_CAPTURE_MAX_CONFIG_BYTES = 64 << 10
_HEAD_MAX_BYTES = 4 << 10
_REF_MAX_BYTES = 4 << 10
_PACKED_REFS_MAX_BYTES = 1 << 20
_SPOOL_MEMORY_BYTES = 64 << 20

MIN_GIT_VERSION = (2, 32)
CAPTURE_CONTAINER_PREFIX = "vaultshield-capture-"
UNTRUSTED_CONFIG_NAME = "untrusted-config"
UNTRUSTED_CONFIG_PATH = f"/tmp/{UNTRUSTED_CONFIG_NAME}"

_HEAD_REF = re.compile(rb"ref: (refs/heads/[A-Za-z0-9._/-]{1,200})\n?\Z")
_BARE_OID = re.compile(rb"([0-9a-f]{40}|[0-9a-f]{64})\n?\Z")
_PACKED_LINE = re.compile(rb"\A([0-9a-f]{40}|[0-9a-f]{64}) (\S+)\Z")
_PEEL_LINE = re.compile(rb"\A\^([0-9a-f]{40}|[0-9a-f]{64})\Z")
_OBJ_SECTION = re.compile(r"^\s*\[\s*extensions\s*\]\s*$", re.IGNORECASE)
_ANY_SECTION = re.compile(r"^\s*\[")
_OBJ_FORMAT = re.compile(r"^\s*objectformat\s*=\s*(\S+)\s*$", re.IGNORECASE)
_GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)")
_OID_LENGTH = {"sha1": 40, "sha256": 64}

# The base's object store as snapshotted at 5-base: loose objects and pack
# pairs only. `objects/info/*` (alternates, http-alternates, commit-graph) is
# never taken.
_OBJECT_PATTERNS = {
    "sha1": re.compile(r"\Aobjects/[0-9a-f]{2}/[0-9a-f]{38}\Z"),
    "sha256": re.compile(r"\Aobjects/[0-9a-f]{2}/[0-9a-f]{62}\Z"),
}
_PACK_PATTERN = re.compile(r"\Aobjects/pack/pack-[0-9a-f]{40,64}\.(pack|idx)\Z")
_OBJECT_DIR_PATTERN = re.compile(r"\Aobjects(/[0-9a-f]{2}|/pack)?\Z")

_DOCKER_ERRORS = (
    docker.errors.DockerException,
    requests.RequestException,
    urllib3.exceptions.HTTPError,
)


def _fmt_bytes(n: int) -> str:
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if n >= (1 << shift) and n % (1 << shift) == 0:
            return f"{n >> shift} {unit}"
    return f"{n} bytes"


def _sanitise(name: str, limit: int = 200) -> str:
    """A member name made safe for a message: raw bytes back, printable ASCII
    only (others → `?`), at most `limit` characters."""
    raw = name.encode("utf-8", "surrogateescape") if isinstance(name, str) else bytes(name)
    text = "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in raw)
    return text[:limit]


def _docker_failure(step: str, exc: BaseException) -> GitCaptureError:
    return GitCaptureError(f"trusted capture: {step} failed: {type(exc).__name__}: {exc}")


def _gone() -> GitCaptureError:
    return GitCaptureError("trusted capture: the Sandbox container is gone")


@dataclass(frozen=True)
class TrustedCaptureCaps:
    """The limits one capture applies (tests pass small ones)."""

    stream: int = TRUSTED_CAPTURE_MAX_STREAM_BYTES
    members: int = TRUSTED_CAPTURE_MAX_MEMBERS
    member: int = TRUSTED_CAPTURE_MAX_MEMBER_BYTES
    config: int = TRUSTED_CAPTURE_MAX_CONFIG_BYTES


class _ChunkReader(io.RawIOBase):
    """File adapter over docker-py's `get_archive` chunk iterator that
    enforces the stream cap. Docker/transport errors raised while pulling a
    chunk are mapped (§5.4.1 "Docker exception mapping")."""

    def __init__(
        self,
        chunks: Iterable[bytes],
        cap: int,
        *,
        error_prefix: str = "trusted capture: ",
        what: str = "Sandbox archive",
        step: str = "archive the Sandbox repo",
    ):
        super().__init__()
        self._chunks: Iterator[bytes] = iter(chunks)
        self._buf = b""
        self._pos = 0
        self._cap = cap
        self._prefix = error_prefix
        self._what = what
        self._step = step
        self.total = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while self._pos >= len(self._buf):
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
            except _DOCKER_ERRORS as exc:
                raise GitCaptureError(
                    f"{self._prefix}{self._step} failed: {type(exc).__name__}: {exc}"
                ) from exc
            self._pos = 0
        n = min(len(b), len(self._buf) - self._pos)
        b[:n] = self._buf[self._pos : self._pos + n]
        self._pos += n
        self.total += n
        if self.total > self._cap:
            raise GitCaptureError(
                f"{self._prefix}{self._what} exceeds {_fmt_bytes(self._cap)} "
                f"({self.total}+ bytes)"
            )
        return n


class _MalformedArchive(Exception):
    """A header that is garbage, truncated or missing where tarfile would
    otherwise quietly treat it as the end of the archive."""


class _StrictTarInfo(tarfile.TarInfo):
    """Only a zero block ends an archive. tarfile's stream mode treats a
    garbage or truncated header after the first member as a silent end, which
    would drop every later member without a word."""

    @classmethod
    def fromtarfile(cls, tf):  # type: ignore[override]
        try:
            return super(_StrictTarInfo, cls).fromtarfile(tf)
        except tarfile.EOFHeaderError:
            raise
        except (
            tarfile.EmptyHeaderError,
            tarfile.TruncatedHeaderError,
            tarfile.InvalidHeaderError,
        ) as exc:
            raise _MalformedArchive(str(exc)) from exc


def _open_input(reader) -> tarfile.TarFile:
    return tarfile.open(
        fileobj=reader,
        mode="r|",
        encoding="utf-8",
        errors="surrogateescape",
        tarinfo=_StrictTarInfo,
    )


def _new_spool():
    return tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_BYTES)


def _open_output(spool) -> tarfile.TarFile:
    return tarfile.open(
        fileobj=spool,
        mode="w",
        format=tarfile.PAX_FORMAT,
        encoding="utf-8",
        errors="surrogateescape",
    )


def _malformed() -> GitCaptureError:
    return GitCaptureError("trusted capture: the Sandbox archive stream was truncated or malformed")


def _check_name(name: str, display: str, root: str) -> list[str]:
    """The shared name rules; returns the components. Raises GitCaptureError."""
    if "\x00" in name:
        raise GitCaptureError(f"trusted capture: unsafe archive member {display}: NUL in name")
    if name.startswith("/"):
        raise GitCaptureError(f"trusted capture: unsafe archive member {display}: absolute name")
    components = name.split("/")
    if any(c in ("", ".", "..") for c in components):
        raise GitCaptureError(
            f"trusted capture: unsafe archive member {display}: empty, `.` or `..` component"
        )
    if components[0] != root:
        raise GitCaptureError(
            f"trusted capture: unsafe archive member {display}: outside {root}/"
        )
    return components


def _normalised(member: tarfile.TarInfo, name: str) -> tarfile.TarInfo:
    """A fresh header with normalised metadata (§5.4.1 "Metadata")."""
    info = tarfile.TarInfo(name)
    info.type = member.type
    info.mtime = member.mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if member.isdir():
        info.mode = 0o755
    elif member.issym():
        info.mode = 0o777
        info.linkname = member.linkname
    elif member.islnk():
        info.mode = 0o755 if member.mode & 0o111 else 0o644
        info.linkname = member.linkname
    else:
        info.mode = 0o755 if member.mode & 0o111 else 0o644
        info.size = member.size
    return info


class _TarFilter:
    """Per-stream state for the fail-closed member rules (§5.4.1 step 2)."""

    def __init__(self, root: str, caps: TrustedCaptureCaps):
        self.root = root
        self.caps = caps
        self.count = 0
        self.seen: set[str] = set()
        self.symlinks: set[str] = set()
        self.emitted_regular: set[str] = set()

    def check(self, member: tarfile.TarInfo) -> list[str]:
        """Count, name, duplicate, type, size and symlinked-parent rules.
        Returns the name's components."""
        self.count += 1
        if self.count > self.caps.members:
            raise GitCaptureError(
                f"trusted capture: archive member count exceeds {self.caps.members} "
                f"({self.count})"
            )
        name = member.name
        display = _sanitise(name)
        components = _check_name(name, display, self.root)
        if name in self.seen:
            raise GitCaptureError(f"trusted capture: unsafe archive member {display}: duplicate name")
        self.seen.add(name)
        if member.type == tarfile.CONTTYPE or member.issparse():
            raise GitCaptureError(
                f"trusted capture: unsafe archive member {display}: unsupported type"
            )
        if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
            raise GitCaptureError(
                f"trusted capture: unsafe archive member {display}: device, FIFO or "
                "unknown type"
            )
        if member.isreg() and member.size > self.caps.member:
            raise GitCaptureError(
                f"trusted capture: archive member {display} exceeds "
                f"{_fmt_bytes(self.caps.member)} ({member.size} bytes)"
            )
        for depth in range(1, len(components)):
            if "/".join(components[:depth]) in self.symlinks:
                raise GitCaptureError(
                    f"trusted capture: unsafe archive member {display}: under a symlink"
                )
        return components

    def check_hardlink(self, member: tarfile.TarInfo) -> None:
        display = _sanitise(member.name)
        target = member.linkname
        bad = GitCaptureError(
            f"trusted capture: unsafe archive member {display}: hardlink to "
            f"{_sanitise(target)} outside its archive"
        )
        try:
            _check_name(target, _sanitise(target), self.root)
        except GitCaptureError:
            raise bad from None
        if target not in self.emitted_regular:
            raise bad


def _drain(tf: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    """Reads a regular member's data in full (so a truncated stream raises
    even for a member that is dropped)."""
    handle = tf.extractfile(member)
    return handle.read() if handle is not None else b""


@dataclass
class SplitArchive:
    """The filtered step-8 archive: the work-tree tar (no `.git` anywhere),
    the untrusted `.git/config` as a one-member tar (or `None`), and the
    non-failing nested-`.git` notices."""

    worktree_tar: object
    untrusted_config: object | None
    notices: list[str] = field(default_factory=list)

    def close(self) -> None:
        for spool in (self.worktree_tar, self.untrusted_config):
            if spool is None:
                continue
            try:
                spool.close()
            except Exception:  # noqa: BLE001 - idempotent, never raises
                pass


def split_archive(reader, *, caps: TrustedCaptureCaps = TrustedCaptureCaps()) -> SplitArchive:
    """Filters the Sandbox's `get_archive(REPO_DIR)` stream (§5.4.1 steps 1-3).

    Emits every accepted member outside `repo/.git/` and outside any nested
    `.git` into a PAX work-tree tar with normalised metadata; copies only
    `repo/.git/config` (≤ 64 KiB, regular) into a one-member tar named
    `untrusted-config`; drops everything else under `repo/.git/` and every
    nested `.git` (recorded in `notices`). Symlinks are copied as symlinks and
    never followed; a member below one is rejected.

    Raises:
        GitCaptureError: an unsafe member, a cap exceeded, a non-directory
            `repo`, or a truncated/malformed stream.
        UnsafeGitConfig: `repo/.git` is not a directory or is missing, or
            `repo/.git/config` is not a regular file.
    """
    state = _TarFilter("repo", caps)
    worktree = _new_spool()
    config_spool = None
    notices: list[str] = []
    saw_root = saw_git = False
    try:
        out = _open_output(worktree)
        try:
            tf = _open_input(reader)
            for member in tf:
                components = state.check(member)
                name = member.name
                if len(components) == 1:
                    if not member.isdir():
                        raise GitCaptureError(
                            "trusted capture: the Sandbox repo directory is not a directory"
                        )
                    saw_root = True
                    out.addfile(_normalised(member, name))
                    continue
                rel = components[1:]
                if rel[0] == ".git":
                    if len(rel) == 1:
                        if not member.isdir():
                            raise UnsafeGitConfig(
                                "trusted capture: the Sandbox repo's .git is not a "
                                "directory (a gitfile or a symlink)"
                            )
                        saw_git = True
                    elif rel == [".git", "config"]:
                        if not member.isreg():
                            raise UnsafeGitConfig(
                                "trusted capture: the Sandbox .git/config is not a regular file"
                            )
                        if member.size > caps.config:
                            raise GitCaptureError(
                                f"trusted capture: .git/config exceeds "
                                f"{_fmt_bytes(caps.config)} ({member.size} bytes)"
                            )
                        config_spool = _config_tar(_drain(tf, member))
                    elif member.isreg():
                        _drain(tf, member)
                    continue
                if ".git" in rel[1:]:
                    index = rel.index(".git", 1)
                    nested = "/".join(rel[:index])
                    if nested not in notices:
                        notices.append(nested)
                    if member.isreg():
                        _drain(tf, member)
                    continue
                if member.islnk():
                    state.check_hardlink(member)
                    out.addfile(_normalised(member, name))
                elif member.issym():
                    state.symlinks.add(name)
                    out.addfile(_normalised(member, name))
                elif member.isdir():
                    out.addfile(_normalised(member, name))
                else:
                    data = _drain(tf, member)
                    out.addfile(_normalised(member, name), io.BytesIO(data))
                    state.emitted_regular.add(name)
        finally:
            out.close()
    except (tarfile.TarError, EOFError, _MalformedArchive) as exc:
        worktree.close()
        if config_spool is not None:
            config_spool.close()
        raise _malformed() from exc
    except BaseException:
        worktree.close()
        if config_spool is not None:
            config_spool.close()
        raise
    if not saw_root:
        worktree.close()
        raise GitCaptureError("trusted capture: the Sandbox repo directory is not a directory")
    if not saw_git:
        worktree.close()
        if config_spool is not None:
            config_spool.close()
        raise UnsafeGitConfig("trusted capture: the Sandbox repo has no .git directory")
    worktree.seek(0)
    return SplitArchive(worktree_tar=worktree, untrusted_config=config_spool, notices=notices)


def _config_tar(content: bytes):
    """The untrusted config as a PAX tar with exactly one member."""
    spool = _new_spool()
    out = _open_output(spool)
    info = tarfile.TarInfo(UNTRUSTED_CONFIG_NAME)
    info.size = len(content)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    out.addfile(info, io.BytesIO(content))
    out.close()
    spool.seek(0)
    return spool


def notices_to_events(notices: Iterable[str]) -> list[EventLogRecord]:
    """One non-failing `violation` event per nested `.git` (§5.4.1 step 2)."""
    return [
        EventLogRecord(
            "violation",
            _now_iso8601(),
            f"trusted capture: nested git repository at {_sanitise(path)} was "
            "captured as plain files",
        )
        for path in notices
    ]


# --- 5-base ------------------------------------------------------------------


@dataclass
class TrustedBase:
    """The pre-agent base: object format, base commit (or `None` for an
    unborn clone), and the filtered `.git/objects` snapshot."""

    object_format: str
    commit_oid: str | None
    objects_tar: object

    def close(self) -> None:
        try:
            self.objects_tar.close()
        except Exception:  # noqa: BLE001 - idempotent, never raises
            pass


def _valid_branch_ref(name: str) -> bool:
    comps = name.split("/")
    return not (
        ".." in name
        or "//" in name
        or "@{" in name
        or name.endswith("/")
        or name.endswith(".")
        or any(c == "" or c.startswith(".") or c.endswith(".lock") for c in comps)
    )


def _get_archive(container, path: str, step: str):
    """`container.get_archive(path)`, NotFound propagated, other Docker
    errors mapped."""
    try:
        return container.get_archive(path)
    except docker.errors.NotFound:
        raise
    except _DOCKER_ERRORS as exc:
        raise _docker_failure(step, exc) from exc


def _read_single_file(container, path: str, max_bytes: int, step: str) -> bytes:
    """The content of one regular file read through `get_archive`.
    `docker.errors.NotFound` propagates."""
    stream, _stat = _get_archive(container, path, step)
    reader = io.BufferedReader(
        _ChunkReader(stream, max_bytes + 64 * 1024, step=step, what=f"archive of {path}")
    )
    base_error = f"trusted capture: cannot read the pre-agent base commit: {posixpath.basename(path)}"
    try:
        tf = _open_input(reader)
        member = tf.next()
        if member is None:
            raise GitCaptureError(f"{base_error} is missing from its archive")
        if not member.isreg():
            raise GitCaptureError(f"{base_error} is not a regular file")
        if member.size > max_bytes:
            raise GitCaptureError(f"{base_error} exceeds {_fmt_bytes(max_bytes)} ({member.size} bytes)")
        return _drain(tf, member)
    except (tarfile.TarError, EOFError, _MalformedArchive) as exc:
        raise GitCaptureError(f"{base_error} archive is malformed") from exc


def _object_format(config: bytes | None) -> str:
    if config is None:
        return "sha1"
    value = None
    inside = False
    for line in config.decode("utf-8", "replace").splitlines():
        if _OBJ_SECTION.match(line):
            inside = True
            continue
        if _ANY_SECTION.match(line):
            inside = False
            continue
        if inside:
            match = _OBJ_FORMAT.match(line)
            if match:
                value = match.group(1)
    if value is None:
        return "sha1"
    lowered = value.lower()
    if lowered not in _OID_LENGTH:
        raise GitCaptureError(
            "trusted capture: cannot read the pre-agent base commit: unsupported "
            f"extensions.objectformat {_sanitise(value, 40)}"
        )
    return lowered


def _snapshot_objects(container, fmt: str, caps: TrustedCaptureCaps):
    """The filtered `.git/objects` snapshot (§5.4.1 5-base item 4)."""
    step = "archive .git/objects"
    try:
        stream, _stat = _get_archive(container, f"{REPO_DIR}/.git/objects", step)
    except docker.errors.NotFound as exc:
        raise GitCaptureError(
            "trusted capture: cannot read the pre-agent base commit: .git/objects is missing"
        ) from exc
    reader = io.BufferedReader(_ChunkReader(stream, caps.stream, step=step, what="object archive"))
    state = _TarFilter("objects", caps)
    loose = _OBJECT_PATTERNS[fmt]
    spool = _new_spool()
    try:
        out = _open_output(spool)
        try:
            tf = _open_input(reader)
            for member in tf:
                state.check(member)
                name = member.name
                if member.isdir():
                    if _OBJECT_DIR_PATTERN.match(name):
                        out.addfile(_normalised(member, name))
                    continue
                if not (loose.match(name) or _PACK_PATTERN.match(name)):
                    if member.isreg():
                        _drain(tf, member)
                    continue
                if member.islnk():
                    state.check_hardlink(member)
                    out.addfile(_normalised(member, name))
                elif member.isreg():
                    data = _drain(tf, member)
                    out.addfile(_normalised(member, name), io.BytesIO(data))
                    state.emitted_regular.add(name)
                # A symlink with an object's name is dropped.
        finally:
            out.close()
    except (tarfile.TarError, EOFError, _MalformedArchive) as exc:
        spool.close()
        raise GitCaptureError(
            "trusted capture: cannot read the pre-agent base commit: the object "
            "archive was truncated or malformed"
        ) from exc
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool


def record_trusted_base(sandbox, *, caps: TrustedCaptureCaps = TrustedCaptureCaps()) -> TrustedBase:
    """5-base (§5.4.1): record the base commit and snapshot its objects, with
    Docker API reads only, before any agent code runs.

    Raises:
        GitCaptureError: malformed, oversized or wrong-type base files, an
            unsupported object format, or a mapped Docker error.
    """
    container = sandbox.container
    if container is None:
        raise _gone()
    prefix = "trusted capture: cannot read the pre-agent base commit: "
    try:
        head = _read_single_file(container, f"{REPO_DIR}/.git/HEAD", _HEAD_MAX_BYTES, "read .git/HEAD")
    except docker.errors.NotFound as exc:
        raise GitCaptureError(prefix + ".git/HEAD is missing") from exc
    try:
        config = _read_single_file(
            container, f"{REPO_DIR}/.git/config", caps.config, "read .git/config"
        )
    except docker.errors.NotFound:
        config = None
    fmt = _object_format(config)
    oid_len = _OID_LENGTH[fmt]

    commit: str | None
    symbolic = _HEAD_REF.match(head)
    bare = _BARE_OID.match(head)
    if symbolic and _valid_branch_ref(symbolic.group(1).decode("ascii")):
        ref = symbolic.group(1).decode("ascii")
        commit = _resolve_ref(container, ref, oid_len, prefix)
    elif bare and len(bare.group(1)) == oid_len:
        commit = bare.group(1).decode("ascii")
    else:
        raise GitCaptureError(prefix + ".git/HEAD is not a branch ref or an object id")

    objects = _snapshot_objects(container, fmt, caps)
    return TrustedBase(object_format=fmt, commit_oid=commit, objects_tar=objects)


def _resolve_ref(container, ref: str, oid_len: int, prefix: str) -> str | None:
    try:
        content = _read_single_file(container, f"{REPO_DIR}/.git/{ref}", _REF_MAX_BYTES, "read the HEAD ref")
    except docker.errors.NotFound:
        content = None
    if content is not None:
        match = _BARE_OID.match(content)
        if not match or len(match.group(1)) != oid_len:
            raise GitCaptureError(prefix + "the HEAD ref does not hold an object id")
        return match.group(1).decode("ascii")
    try:
        packed = _read_single_file(
            container, f"{REPO_DIR}/.git/packed-refs", _PACKED_REFS_MAX_BYTES, "read .git/packed-refs"
        )
    except docker.errors.NotFound:
        return None
    lines = packed.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    for line in lines:
        if line.startswith(b"#") or _PEEL_LINE.match(line):
            continue
        match = _PACKED_LINE.match(line)
        if not match:
            raise GitCaptureError(prefix + ".git/packed-refs is malformed")
        name = match.group(2)
        if name == ref.encode("ascii"):
            if len(match.group(1)) != oid_len:
                raise GitCaptureError(prefix + "the HEAD ref does not hold an object id")
            return match.group(1).decode("ascii")
    return None


# --- step 8 ------------------------------------------------------------------


def kill_sandbox(sandbox) -> None:
    """Step 8, step 0: stop every Sandbox process through the Docker API. A
    409 "is not running" counts as success."""
    container = sandbox.container
    if container is None:
        raise _gone()
    try:
        container.kill()
    except docker.errors.NotFound as exc:
        raise _gone() from exc
    except docker.errors.APIError as exc:
        status = getattr(exc, "status_code", None)
        if status == 409 or "is not running" in str(exc):
            return
        raise _docker_failure("kill the Sandbox", exc) from exc
    except _DOCKER_ERRORS as exc:
        raise _docker_failure("kill the Sandbox", exc) from exc


def sandbox_image_id(sandbox) -> str:
    """The immutable image ID the Sandbox was created from (no API call)."""
    container = sandbox.container
    if container is None:
        raise _gone()
    try:
        return container.attrs["Image"]
    except (KeyError, TypeError) as exc:
        raise GitCaptureError("trusted capture: the Sandbox has no image id") from exc


def archive_sandbox_repo(sandbox, *, caps: TrustedCaptureCaps = TrustedCaptureCaps()) -> SplitArchive:
    """Step 8: `get_archive(REPO_DIR)` on the (killed) Sandbox, filtered by
    `split_archive`."""
    container = sandbox.container
    if container is None:
        raise _gone()
    try:
        stream, _stat = _get_archive(container, REPO_DIR, "archive the Sandbox repo")
    except docker.errors.NotFound as exc:
        raise _gone() from exc
    return split_archive(io.BufferedReader(_ChunkReader(stream, caps.stream)), caps=caps)


def capture_container_name(run_id: str) -> str:
    return f"{CAPTURE_CONTAINER_PREFIX}{run_id}"


class TrustedCapture:
    """The capture helper container (§5.4.1): same image ID as the Sandbox,
    no network, no capabilities, no volumes, no env. Exposes `exec` with
    `Sandbox.exec`'s contract so `git_capture` runs on it unchanged."""

    def __init__(self, run_id: str, client, *, image_id: str):
        self.run_id = run_id
        self._client = client
        self.image_id = image_id
        self.container = None

    def start(self) -> None:
        """Create and start the helper, then require git ≥ 2.32 (the first
        release that honours `GIT_CONFIG_GLOBAL`)."""
        try:
            self.container = self._client.containers.create(
                self.image_id,
                command=KEEP_ALIVE_CMD,
                name=capture_container_name(self.run_id),
                working_dir=WORKDIR,
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                mem_limit="1g",
                pids_limit=256,
                labels={"vaultshield.run_id": self.run_id, "vaultshield.role": "capture"},
            )
        except _DOCKER_ERRORS as exc:
            raise _docker_failure("create the capture helper", exc) from exc
        try:
            self.container.start()
        except _DOCKER_ERRORS as exc:
            raise _docker_failure("start the capture helper", exc) from exc
        version = self._exec(["git", "version"], workdir=WORKDIR)
        match = _GIT_VERSION.search(version.stdout)
        found = version.stdout.strip() or "(no output)"
        if version.exit_code != 0 or not match:
            raise GitCaptureError(
                f"trusted capture: the Sandbox image's git {_sanitise(found, 60)} is older "
                "than 2.32 or unusable"
            )
        if (int(match.group(1)), int(match.group(2))) < MIN_GIT_VERSION:
            raise GitCaptureError(
                f"trusted capture: the Sandbox image's git {match.group(1)}.{match.group(2)} "
                "is older than 2.32"
            )

    def _exec(self, cmd: list[str], *, workdir: str = REPO_DIR, env: dict | None = None) -> ExecResult:
        if self.container is None:
            raise SandboxError(f"trusted capture exec failed for run {self.run_id}: no helper")
        try:
            exit_code, (stdout, stderr) = self.container.exec_run(
                cmd, workdir=workdir, demux=True, environment=env
            )
        except Exception as exc:
            raise SandboxError(f"trusted capture exec failed for run {self.run_id}: {exc}") from exc
        return ExecResult(
            exit_code=exit_code,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
            stdout_bytes=stdout or b"",
        )

    def exec(self, cmd: list[str], *, env: dict[str, str] | None = None) -> ExecResult:
        """`Sandbox.exec`'s contract, in the helper's repository."""
        return self._exec(cmd, workdir=REPO_DIR, env=env)

    def _put(self, path: str, spool, step: str) -> None:
        spool.seek(0)
        try:
            ok = self.container.put_archive(path, spool.read())
        except _DOCKER_ERRORS as exc:
            raise _docker_failure(step, exc) from exc
        if ok is False:
            raise GitCaptureError(f"trusted capture: {step} failed: the daemon refused it")

    def _git_ok(self, *args: str, step: str) -> ExecResult:
        result = self.exec(_git(*args), env={**_GIT_ENV, "GIT_NO_REPLACE_OBJECTS": "1"})
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "no output captured").strip()
            raise GitCaptureError(
                f"trusted capture: {step} failed (exit code {result.exit_code}): {detail[:300]}"
            )
        return result

    def load(self, split: SplitArchive, base: TrustedBase) -> None:
        """Lay down the filtered work tree, init a fresh repository, add the
        5-base objects, run the untrusted-config check, and detach HEAD at the
        base with the base tree in the index (§5.4.1 steps 4-5)."""
        self._put(WORKDIR, split.worktree_tar, "put_archive work tree")
        self._git_ok(
            "init", "-q", "--template=", f"--object-format={base.object_format}", ".",
            step="initialise the capture repository",
        )
        self._put(f"{REPO_DIR}/.git", base.objects_tar, "put_archive base objects")
        if split.untrusted_config is not None:
            self._put("/tmp", split.untrusted_config, "put_archive untrusted config")
            check = self.exec(
                _git("config", "--file", UNTRUSTED_CONFIG_PATH, "--get-regexp", _UNSAFE_CONFIG_REGEX),
                env=_GIT_ENV,
            )
            if check.exit_code == 0 and check.stdout.strip():
                raise UnsafeGitConfig(
                    "trusted capture: the Sandbox .git/config defines a filter or diff driver"
                )
            if check.exit_code not in (0, 1):
                raise UnsafeGitConfig(
                    "trusted capture: the Sandbox .git/config could not be parsed "
                    f"(git config exit {check.exit_code})"
                )
        if base.commit_oid is None:
            return
        oid = base.commit_oid
        incomplete = GitCaptureError(
            f"trusted capture: the pre-agent object snapshot is incomplete for base {oid}"
        )
        for args in (("cat-file", "-e", f"{oid}^{{tree}}"), ("rev-list", "--quiet", "--objects", oid)):
            result = self.exec(_git(*args), env={**_GIT_ENV, "GIT_NO_REPLACE_OBJECTS": "1"})
            if result.exit_code != 0:
                raise incomplete
        self._git_ok("update-ref", "--no-deref", "HEAD", oid, step="detach HEAD at the base")
        self._git_ok("read-tree", oid, step="read the base tree")

    def remove(self) -> None:
        """Remove the helper. Never raises; one warning on failure."""
        if self.container is None:
            return
        try:
            self.container.remove(v=True, force=True)
        except docker.errors.NotFound:
            pass
        except BaseException as exc:  # noqa: BLE001
            click.echo(
                f"Warning: could not remove the capture helper for run {self.run_id}: "
                f"{type(exc).__name__}",
                err=True,
            )
        finally:
            self.container = None


def remove_capture_helper(run_id: str, client) -> bool:
    """Remove `vaultshield-capture-<run_id>` by name; `False` if absent.
    Docker refusals propagate (callers wrap them)."""
    try:
        container = client.containers.get(capture_container_name(run_id))
    except docker.errors.NotFound:
        return False
    try:
        container.remove(v=True, force=True)
    except docker.errors.NotFound:
        return False
    return True
