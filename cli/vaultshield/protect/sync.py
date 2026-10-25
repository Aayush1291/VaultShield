"""`vaultshield protect sync`: push unsynced Protect and Orchestrate activity to
the dashboard as one Run, so a human can see it next to the sandbox runs.

What it does, reusing the existing `vaultshield run` machinery unchanged:

* reads `.vaultshield/violations.jsonl` (the protect hook's denials) and
  `.vaultshield/orchestrator.jsonl` (the orchestrator's call ledger) in the
  current project;
* takes only the entries not yet synced, tracked as per-file byte offsets in
  `.vaultshield/sync.json` (atomic write), advanced ONLY after a full success;
* if there is anything new: creates one Run (agent "protect") through
  `uploader.create_run`, uploads the mapped events through `uploader.upload_run`,
  and moves it to `awaiting_review` through `uploader.complete_run`.

Mapping (each keeps its own timestamp):
* a violation line -> event_type "violation", payload as logged (it already
  holds no value);
* an orchestrator call -> event_type "output", payload
  "orchestrator: <provider> <model> <outcome> (<request_bytes> bytes)".

Before uploading, every payload is scrubbed with the project's vault values
(a `SecretSet`, in host memory), and a final check refuses to upload if any
vault value still appears. No value is ever written to disk or sent.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vaultshield import uploader
from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect.envfile import decode_record, value_of
from vaultshield.protect.fsutil import atomic_write_bytes
from vaultshield.protect.vault import Vault, VaultError
from vaultshield.run_directory import event_log_path, generate_run_id
from vaultshield.token_hygiene import SecretSet

AGENT = "protect"
SYNC_FILE = "sync.json"
VIOLATIONS_FILE = manifest_mod.VIOLATIONS_NAME  # "violations.jsonl"
ORCHESTRATOR_FILE = "orchestrator.jsonl"
_SYNC_VERSION = 1


class SyncError(Exception):
    """Building the sync failed before anything was uploaded. Never holds a value."""


@dataclass
class NewEntries:
    events: list[dict]
    offsets: dict[str, int]  # file -> the byte offset to store once the upload succeeds


@dataclass
class SyncOutcome:
    nothing: bool
    run_id: str | None
    event_count: int


def project_dir(start: Path) -> Path:
    """The project whose `.vaultshield/` holds the logs: the nearest protected
    root at or above `start`, else `start` itself (matching how the orchestrator
    chose the directory it wrote its ledger into)."""
    return manifest_mod.find_root(start) or start.resolve()


def _vaultshield_dir(root: Path) -> Path:
    return root / manifest_mod.MANIFEST_DIR


def _sync_path(root: Path) -> Path:
    return _vaultshield_dir(root) / SYNC_FILE


# --- offsets -----------------------------------------------------------------


def load_offsets(root: Path) -> dict[str, int]:
    try:
        data = json.loads(_sync_path(root).read_bytes())
    except (OSError, ValueError):
        return {}
    offsets = data.get("offsets") if isinstance(data, dict) else None
    if not isinstance(offsets, dict):
        return {}
    return {k: v for k, v in offsets.items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0}


def save_offsets(root: Path, offsets: dict[str, int]) -> None:
    text = json.dumps({"version": _SYNC_VERSION, "offsets": offsets}, indent=2) + "\n"
    atomic_write_bytes(_sync_path(root), text.encode("utf-8"))


def _consume(path: Path, offset: int) -> tuple[list[str], int]:
    """The complete lines in `path` after byte `offset`, and the new offset to
    store. Only whole lines (ending in a newline) are consumed, so a line still
    being appended is left for next time. An offset past the end (the file was
    truncated or replaced) restarts at 0."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], offset
    except OSError as exc:
        raise SyncError(f"cannot read {path.name} ({type(exc).__name__})") from None
    start = offset if 0 <= offset <= len(data) else 0
    chunk = data[start:]
    newline = chunk.rfind(b"\n")
    if newline == -1:
        return [], start
    consumed = newline + 1
    text = chunk[:consumed].decode("utf-8", "replace")
    lines = [line for line in text.split("\n") if line.strip()]
    return lines, start + consumed


# --- mapping -----------------------------------------------------------------


def _as_object(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _violation_event(line: str) -> dict | None:
    obj = _as_object(line)
    if obj is None:
        return None
    timestamp, payload = obj.get("timestamp"), obj.get("payload")
    if not isinstance(timestamp, str) or not isinstance(payload, str):
        return None
    # A blank payload says nothing and the backend rejects it; skipped like
    # an unparsable line, so it neither counts nor creates an empty Run.
    if not payload.strip():
        return None
    return {"event_type": "violation", "timestamp": timestamp, "payload": payload}


def _orchestrator_event(line: str) -> dict | None:
    obj = _as_object(line)
    if obj is None:
        return None
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    payload = (
        f"orchestrator: {obj.get('provider')} {obj.get('model')} "
        f"{obj.get('outcome')} ({obj.get('request_bytes')} bytes)"
    )
    return {"event_type": "output", "timestamp": timestamp, "payload": payload}


def collect(root: Path) -> NewEntries:
    """Every not-yet-synced entry from both logs, mapped to events, with the
    offsets that record how far each log was consumed."""
    offsets = load_offsets(root)
    directory = _vaultshield_dir(root)
    v_lines, v_offset = _consume(directory / VIOLATIONS_FILE, offsets.get(VIOLATIONS_FILE, 0))
    o_lines, o_offset = _consume(directory / ORCHESTRATOR_FILE, offsets.get(ORCHESTRATOR_FILE, 0))

    events: list[dict] = []
    for line in v_lines:
        event = _violation_event(line)
        if event is not None:
            events.append(event)
    for line in o_lines:
        event = _orchestrator_event(line)
        if event is not None:
            events.append(event)

    new_offsets = dict(offsets)
    new_offsets[VIOLATIONS_FILE] = v_offset
    new_offsets[ORCHESTRATOR_FILE] = o_offset
    return NewEntries(events, new_offsets)


# --- scrubbing ---------------------------------------------------------------


def build_secrets(root: Path) -> SecretSet:
    """A `SecretSet` of every value this project keeps in the vault, so a payload
    can be scrubbed and checked against them. Values stay in host memory only."""
    secrets = SecretSet()
    try:
        manifest = manifest_mod.load(root)
    except manifest_mod.ManifestError:
        return secrets
    if manifest is None:
        return secrets
    vault = Vault(manifest.project_id)
    for rel, names in manifest.protected.items():
        for name in names:
            try:
                record = vault.fetch(rel, name)
            except VaultError as exc:
                raise SyncError(f"cannot read the vault ({exc})") from None
            if record is None:
                continue
            try:
                for raw in decode_record(record):
                    value = value_of(raw)
                    if value:
                        secrets.add_needle(value.encode("utf-8"))
            except ValueError:
                continue
    return secrets


def scrub_events(events: list[dict], secrets: SecretSet) -> list[dict]:
    """Every event with its payload scrubbed. Refuses (raising `SyncError`) if a
    vault value survives, so nothing is uploaded when scrubbing has not held."""
    scrubbed: list[dict] = []
    for event in events:
        payload = secrets.scrub(event["payload"])
        if secrets.contains_secret(payload.encode("utf-8")):
            raise SyncError("a secret value survived scrubbing; nothing was uploaded")
        scrubbed.append({**event, "payload": payload})
    return scrubbed


# --- the upload --------------------------------------------------------------


def _write_events(run_dir: Path, events: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(event) + "\n" for event in events)
    event_log_path(run_dir).write_text(body, encoding="utf-8")


def perform(root: Path, project: str, backend_url: str, token: str) -> SyncOutcome:
    """Collects, scrubs, and uploads one Run. Raises `uploader.UploadError` if the
    backend cannot be reached or the token is bad, or `SyncError` if scrubbing or
    the vault read fails; in either case the sync offsets are NOT advanced, so the
    next `protect sync` resends the same entries."""
    entries = collect(root)
    if not entries.events:
        return SyncOutcome(nothing=True, run_id=None, event_count=0)

    secrets = build_secrets(root)
    scrubbed = scrub_events(entries.events, secrets)

    run_id = generate_run_id()
    uploader.create_run(backend_url, token, project=project, run_id=run_id, agent=AGENT)
    staging = Path(tempfile.mkdtemp(prefix="vs-sync-"))
    try:
        _write_events(staging, scrubbed)
        # The same event-upload code `run` uses, with the same scrub as a second
        # layer behind the check above. upload_run skips the absent diff/screenshots.
        uploader.upload_run(staging, run_id, backend_url, token, scrub=secrets.scrub)
        uploader.complete_run(backend_url, token, run_id)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Only now, after the Run is complete, is the progress recorded.
    save_offsets(root, entries.offsets)
    return SyncOutcome(nothing=False, run_id=run_id, event_count=len(scrubbed))
