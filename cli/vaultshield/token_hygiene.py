"""Token_Hygiene_Module: keep the agent's credential out of everything that
leaves the machine (Batch 3 FR5, D9, design §5.5).

The agent's own credential (Antigravity's OAuth session under `/root/.gemini`)
lives in the Sandbox, readable by the agent. Four ways it could leave the
machine are checked on the HOST, on bytes the host holds:

* (a) `diff.json` (every entry's `path` and `patch`);
* (b) `events.jsonl` (every payload: a `cat` of the token to stdout);
* (c) the capture branch's changed files, read by blob oid from the trusted
  capture helper (path-independent, so a non-UTF-8 name is still read);
* (d) symlinks the Run added or retargeted that point outside the repository.

The needles come from `snapshot`, a Docker-API read (`get_archive`) of the
credential root, so no in-container binary is involved. They live only in a
`SecretSet` in host memory: never written to disk, never logged, never put in
an exception message, event payload or warning (NFR5).

Path literals (the absolute root, and `~/`-form first-level children such as
`~/.gemini/antigravity-cli`) are findings in (a) and (c) — a changed file that
names the credential path (§9 item 5). They are not checked in (b): an agent's
terminal output may legitimately mention its own config directory, and only
credential BYTES there are a leak.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import docker

from vaultshield.git_capture import _GIT_ENV, GitCaptureError, _git
from vaultshield.monitor import EventLogRecord, _append_event_log, _now_iso8601
from vaultshield.run_directory import diff_path, event_log_path
from vaultshield.trusted_capture import (
    _DOCKER_ERRORS,
    _ChunkReader,
    _MalformedArchive,
    _drain,
    _open_input,
)

REDACTED = "***"  # the same marker as `sandbox._REDACTED`
SNAPSHOT_PREFIX = "token hygiene: credential snapshot failed: "
CREDENTIAL_SNAPSHOT_MAX_STREAM_BYTES = 64 << 20
CREDENTIAL_MEMBER_MAX_BYTES = 1 << 20
CREDENTIAL_TOTAL_MAX_BYTES = 8 << 20
WHOLE_FILE_NEEDLE_MIN = 32
WHOLE_FILE_NEEDLE_MAX = 8 << 10
CHANGED_FILES_SCAN_CAP = 32 << 20
QUARANTINED_DIFF_FILENAME = "diff.quarantined.json"

# New files after snapshot #1 are needle sources only if their basename looks
# like a credential and no directory component is session/transcript state
# (§5.5, §9 item 14).
_CREDENTIAL_BASENAME = re.compile(
    r"(token|oauth|credential|cred|secret|auth|session|\.pbtxt$|\.json$)", re.IGNORECASE
)
_EXCLUDED_DIRS = frozenset({"tmp", "logs", "history", "brain", "conversations", "cache"})
_TOKEN_RUN = re.compile(rb"[A-Za-z0-9._~+/=-]{32,}")
_ZERO_OID = re.compile(rb"\A0+\Z")

FINDING_KINDS = ("credential_bytes", "credential_path", "symlink_outside_repo", "unsafe_git_config")


def _token_shaped(run: bytes) -> bool:
    return (
        any(48 <= b <= 57 for b in run)
        and any(65 <= b <= 90 for b in run)
        and any(97 <= b <= 122 for b in run)
    )


class SecretSet:
    """Credential needles for one Run, in host memory only. `repr` shows
    counts, never values."""

    def __init__(self) -> None:
        self._needles: set[bytes] = set()
        self._paths: set[bytes] = set()
        self.roots: list[str] = []

    def add_needle(self, needle: bytes) -> None:
        if needle:
            self._needles.add(bytes(needle))

    def add_path_literal(self, literal: str) -> None:
        if literal:
            self._paths.add(literal.encode("utf-8"))

    def add_root(self, root: str) -> None:
        if root not in self.roots:
            self.roots.append(root)

    def add(self, other: "SecretSet") -> None:
        self._needles |= other._needles
        self._paths |= other._paths
        for root in other.roots:
            self.add_root(root)

    def __bool__(self) -> bool:
        return bool(self._needles or self._paths)

    def __repr__(self) -> str:
        return f"SecretSet(needles={len(self._needles)}, path_literals={len(self._paths)})"

    __str__ = __repr__

    def contains_secret(self, data: bytes) -> bool:
        return any(needle in data for needle in self._needles)

    def contains_path(self, data: bytes) -> bool:
        return any(literal in data for literal in self._paths)

    def scrub(self, text: str) -> str:
        """`text` with every UTF-8-decodable needle replaced by `***`,
        longest first."""
        if not isinstance(text, str) or not self._needles:
            return text
        for needle in sorted(self._needles, key=len, reverse=True):
            try:
                needle_text = needle.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if needle_text:
                text = text.replace(needle_text, REDACTED)
        return text


@dataclass(frozen=True)
class Snapshot:
    """One credential snapshot: its needles, the credential file set (defined
    by snapshot #1, passed back to later ones) and how many selected files
    were too large to scan."""

    secrets: SecretSet
    credential_files: frozenset[str]
    skipped: int = 0


def _home_form(path: str) -> str:
    return "~/" + path[len("/root/"):] if path.startswith("/root/") else path


def _literal_root(path: str) -> str:
    """`/root/<top>` for a path under `/root`, else the path itself: the
    credential root a path-literal finding names (§9 item 5)."""
    parts = path.split("/")
    if path.startswith("/root/") and len(parts) > 2:
        return "/".join(parts[:3])
    return path


def _root_literals(path: str) -> list[str]:
    """Path literals for one credential path: the absolute root
    (`/root/.gemini`) and, for a path below it, its first-level child in
    `~/` form (`~/.gemini/antigravity-cli`). For a file path this is the same
    literal set a whole-root snapshot derives, so narrowing to files keeps
    §9 item 5's behaviour."""
    root = _literal_root(path)
    out = [root]
    rest = path[len(root):].strip("/").split("/")
    if root != path and len(rest) > 1:
        out.append(_home_form(f"{root}/{rest[0]}"))
    return out


def _relative(name: str, single_file: bool) -> str:
    parts = name.split("/", 1)
    if len(parts) == 1:
        return parts[0] if single_file else ""
    return parts[1]


def _selected(rel: str, credential_files: frozenset[str] | None) -> bool:
    if credential_files is None or rel in credential_files:
        return True
    directories = rel.split("/")[:-1]
    if any(part.lower() in _EXCLUDED_DIRS for part in directories):
        return False
    return bool(_CREDENTIAL_BASENAME.search(posixpath.basename(rel)))


def snapshot(sandbox, roots: Iterable[str], *, credential_files: frozenset[str] | None = None) -> Snapshot:
    """Reads each credential root through `get_archive` and extracts needles
    (§5.5). `credential_files=None` (snapshot #1) defines the credential file
    set; later snapshots pass it back and only re-read those files plus new
    credential-named files outside session/transcript directories.

    Raises:
        GitCaptureError: a Docker/transport error, an over-64-MiB stream, or a
            malformed archive (message prefix `token hygiene: credential
            snapshot failed: `). A missing root is an empty result.
    """
    secrets = SecretSet()
    defined: set[str] = set()
    skipped = 0
    total = 0
    container = sandbox.container
    for root in roots:
        if container is None:
            raise GitCaptureError(SNAPSHOT_PREFIX + "the Sandbox container is gone")
        try:
            stream, _stat = container.get_archive(root)
        except docker.errors.NotFound:
            continue
        except _DOCKER_ERRORS as exc:
            raise GitCaptureError(f"{SNAPSHOT_PREFIX}{type(exc).__name__}: {exc}") from exc
        secrets.add_root(_literal_root(root))
        for literal in _root_literals(root):
            secrets.add_path_literal(literal)
        reader = io.BufferedReader(
            _ChunkReader(
                stream,
                CREDENTIAL_SNAPSHOT_MAX_STREAM_BYTES,
                error_prefix=SNAPSHOT_PREFIX,
                what=f"archive of {root}",
                step=f"reading {root}",
            )
        )
        try:
            tf = _open_input(reader)
            members = []
            for member in tf:
                members.append(member)
                single = len(members) == 1 and member.isreg() and "/" not in member.name
                rel = _relative(member.name, single)
                if member.isdir() and rel and "/" not in rel:
                    secrets.add_path_literal(_home_form(posixpath.join(root, rel)))
                # Symlinks (e.g. agy's `cli.log`), hardlinks and anything else
                # are never followed or read: only regular members' own bytes
                # inside the archive are needle sources.
                if not member.isreg():
                    continue
                if credential_files is None:
                    defined.add(rel)
                if not _selected(rel, credential_files):
                    _drain(tf, member)
                    continue
                if member.size > CREDENTIAL_MEMBER_MAX_BYTES or total >= CREDENTIAL_TOTAL_MAX_BYTES:
                    _drain(tf, member)
                    skipped += 1
                    continue
                data = _drain(tf, member)
                total += len(data)
                if WHOLE_FILE_NEEDLE_MIN <= len(data) <= WHOLE_FILE_NEEDLE_MAX:
                    secrets.add_needle(data)
                for match in _TOKEN_RUN.finditer(data):
                    if _token_shaped(match.group(0)):
                        secrets.add_needle(match.group(0))
        except (tarfile.TarError, EOFError, _MalformedArchive) as exc:
            raise GitCaptureError(f"{SNAPSHOT_PREFIX}the archive of {root} is malformed") from exc
    files = frozenset(defined) if credential_files is None else credential_files
    return Snapshot(secrets=secrets, credential_files=files, skipped=skipped)


def skipped_snapshot_event(n: int) -> EventLogRecord:
    """The non-failing event for credential files too large to scan."""
    return EventLogRecord(
        "violation",
        _now_iso8601(),
        f"token hygiene: {n} credential-root files not scanned for needles "
        "(over 1 MiB or past the 8 MiB cap)",
    )


@dataclass(frozen=True)
class Finding:
    """One hygiene finding. Never carries the matched bytes."""

    kind: str
    artifact: str
    repo_path: str | None = None


class TokenHygieneViolation(Exception):
    """Credential material (or an escaping symlink) would have left the
    machine. Its message never contains the needle."""

    def __init__(self, findings: list[Finding], roots: Iterable[str] = ()):
        self.findings = list(findings)
        artifacts = ", ".join(dict.fromkeys(f.artifact for f in self.findings))
        root = ", ".join(roots) or "the credential root"
        super().__init__(
            f"credential material from {root} found in {artifacts}; nothing but "
            "the scrubbed timeline was uploaded"
        )


def violation_payload(finding: Finding) -> str:
    return (
        f"token hygiene violation: {finding.kind} in {finding.artifact} "
        f"({finding.repo_path or '-'}); the Run's diff and screenshots were not uploaded"
    )


def _display(path: bytes) -> str:
    return path.decode("utf-8", "replace")


def _parse_raw_diff(output: bytes) -> list[tuple[bytes, bytes, bytes, bytes, bytes]]:
    """`git diff --raw -z --no-renames --no-abbrev` → (old mode, new mode,
    old oid, new oid, path) per record. Raises GitCaptureError when malformed."""
    records = []
    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields = fields[:-1]
    if len(fields) % 2:
        raise GitCaptureError("token hygiene: the capture branch's raw diff is malformed")
    for i in range(0, len(fields), 2):
        head, path = fields[i], fields[i + 1]
        parts = head.split(b" ")
        if len(parts) != 5 or not head.startswith(b":") or not path:
            raise GitCaptureError("token hygiene: the capture branch's raw diff is malformed")
        old_mode, new_mode, old_oid, new_oid, _status = parts
        records.append((old_mode[1:], new_mode, old_oid, new_oid, path))
    return records


def _blob(capture, oid: bytes) -> bytes:
    result = capture.exec(_git("cat-file", "blob", oid.decode("ascii")), env=_GIT_ENV)
    if result.exit_code != 0:
        raise GitCaptureError(
            f"token hygiene: could not read a changed file's blob (exit code {result.exit_code})"
        )
    return result.stdout_bytes or b""


def _escapes(link: bytes, target: bytes) -> bool:
    if target.startswith(b"/"):
        return True
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
    return joined == b".." or joined.startswith(b"../")


def check_run_outputs(capture, secrets: SecretSet, run_dir: Path, branch: str, base_ref: str) -> None:
    """Scans (a)-(d) (§5.5). On any finding: one `violation` event per
    finding, `diff.json` renamed `diff.quarantined.json`, and
    `TokenHygieneViolation` raised. A count of changed files past the 32 MiB
    scan cap is recorded as one non-failing event.

    Raises:
        TokenHygieneViolation: a finding.
        GitCaptureError: the raw diff could not be produced or parsed, or a
            blob could not be read (never a silent skip).
    """
    findings: list[Finding] = []
    notes: list[EventLogRecord] = []

    def note(kind: str, artifact: str, path: str | None) -> None:
        # A path can itself carry the needle (`leak-<token>.txt`), so it is
        # scrubbed before it reaches a finding, an event or a message.
        finding = Finding(kind, artifact, secrets.scrub(path) if path else path)
        if finding not in findings:
            findings.append(finding)

    # (a) diff.json
    diff_file = diff_path(run_dir)
    if diff_file.exists():
        payload = json.loads(diff_file.read_text(encoding="utf-8"))
        for entry in payload.get("files", []):
            path = str(entry.get("path", ""))
            for text in (path, entry.get("patch") or ""):
                data = str(text).encode("utf-8", "surrogateescape")
                if secrets.contains_secret(data):
                    note("credential_bytes", "diff.json", path)
                elif secrets.contains_path(data):
                    note("credential_path", "diff.json", path)

    # (b) events.jsonl
    log = event_log_path(run_dir)
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = str(record.get("payload", "")).encode("utf-8", "surrogateescape")
            if secrets.contains_secret(data):
                note("credential_bytes", "events.jsonl", None)

    # (c) + (d) the capture branch, by oid
    raw = capture.exec(
        _git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base_ref, branch), env=_GIT_ENV
    )
    if raw.exit_code != 0:
        raise GitCaptureError(
            f"token hygiene: could not list the capture branch's changes (exit code {raw.exit_code})"
        )
    scanned = 0
    not_scanned = 0
    for _old_mode, new_mode, _old_oid, new_oid, path in _parse_raw_diff(raw.stdout_bytes or b""):
        if _ZERO_OID.match(new_oid):
            continue
        if new_mode == b"120000":
            target = _blob(capture, new_oid)
            if _escapes(path, target):
                note("symlink_outside_repo", "branch", _display(path))
            elif secrets.contains_secret(target):
                note("credential_bytes", "branch", _display(path))
            continue
        if new_mode not in (b"100644", b"100755") or not secrets:
            continue
        data = _blob(capture, new_oid)
        if scanned + len(data) > CHANGED_FILES_SCAN_CAP:
            not_scanned += 1
            continue
        scanned += len(data)
        if secrets.contains_secret(data):
            note("credential_bytes", "branch", _display(path))
        elif secrets.contains_path(data):
            note("credential_path", "branch", _display(path))
    if not_scanned:
        notes.append(
            EventLogRecord(
                "violation",
                _now_iso8601(),
                f"token hygiene: {not_scanned} changed files not scanned (over 32 MiB)",
            )
        )

    events = notes + [EventLogRecord("violation", _now_iso8601(), violation_payload(f)) for f in findings]
    if events:
        _append_event_log(log, events)
    if findings:
        if diff_file.exists():
            diff_file.replace(run_dir / QUARANTINED_DIFF_FILENAME)
        raise TokenHygieneViolation(findings, secrets.roots)


def record_unsafe_git_config(run_dir: Path) -> None:
    """The `violation` event for an `UnsafeGitConfig` (handled like a
    hygiene finding by `cli.py`)."""
    _append_event_log(
        event_log_path(run_dir),
        [EventLogRecord("violation", _now_iso8601(), violation_payload(Finding("unsafe_git_config", "branch")))],
    )
