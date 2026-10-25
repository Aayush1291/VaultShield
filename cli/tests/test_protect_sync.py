"""`vaultshield protect sync` (Day 3b step 1).

The "mocked transport" is a real local HTTP server on an ephemeral port, the same
approach as test_uploader.py, so these exercise the real uploader code end to end
and assert on what actually went over the wire. HOME is a temporary directory and
the Keychain is the in-memory fake (the `protect_project` fixture); no real token,
Keychain, or backend is touched.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from vaultshield import uploader
from vaultshield.cli import cli
from vaultshield.protect import commands, sync as sync_mod

KEY_VALUE = "sk-test-LEAKCANARY-1111-2222-3333"
TOKEN = "0123456789abcdef0123456789abcdef01234567"


# --- a local backend that records what sync sent ----------------------------


class _Request:
    def __init__(self, method: str, path: str, headers, body: bytes) -> None:
        self.method = method
        self.path = path
        self.authorization = headers.get("Authorization")
        self.body = body

    @property
    def json(self):
        return json.loads(self.body) if self.body else None


class _Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        received = self.server.received  # type: ignore[attr-defined]
        received.append(_Request(self.command, self.path, self.headers, body))
        status = self.server.fail_status_for(self.command, self.path)  # type: ignore[attr-defined]
        payload = b"{}" if status < 400 else json.dumps({"detail": "nope"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = _handle
    do_PATCH = _handle

    def log_message(self, *args: object) -> None:
        """Silence the access log."""


@contextmanager
def backend(fail: dict[str, int] | None = None) -> Iterator[ThreadingHTTPServer]:
    """A backend recording every request. `fail` maps a path suffix
    ("events/", "/", "runs/") to the status to answer it with."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.received = []  # type: ignore[attr-defined]
    fails = fail or {}

    def fail_status_for(method: str, path: str) -> int:
        if method == "POST" and path.endswith("/events/"):
            return fails.get("events", 201)
        if method == "POST" and path == "/api/runs/":
            return fails.get("create", 201)
        if method == "PATCH":
            return fails.get("complete", 200)
        return 201

    server.fail_status_for = fail_status_for  # type: ignore[attr-defined]
    server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- helpers -----------------------------------------------------------------


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def write_token(home: Path) -> None:
    path = home / ".vaultshield" / "token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TOKEN)


def vdir(root: Path) -> Path:
    directory = root / ".vaultshield"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_violation(root: Path, payload: str, *, timestamp: str = "2026-01-01T00:00:00+00:00") -> None:
    line = json.dumps({"event_type": "violation", "timestamp": timestamp, "payload": payload})
    with (vdir(root) / "violations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_orchestrator(root: Path, *, provider="gemini", model="gemini-2.5-flash", outcome="ok", request_bytes=512,
                       timestamp="2026-01-01T00:00:01+00:00") -> None:
    line = json.dumps(
        {"timestamp": timestamp, "provider": provider, "model": model, "request_bytes": request_bytes, "outcome": outcome}
    )
    with (vdir(root) / "orchestrator.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def events_posted(server) -> list[dict]:
    """Every event in the order the events endpoint received them."""
    out: list[dict] = []
    for request in server.received:
        if request.method == "POST" and request.path.endswith("/events/"):
            out.extend(request.json)
    return out


def creates(server) -> list[_Request]:
    return [r for r in server.received if r.method == "POST" and r.path == "/api/runs/"]


@pytest.fixture
def home(protect_project):
    """The temp HOME the `protect_project` fixture set, with a login token."""
    home_dir = Path.cwd().parent / "home"
    write_token(home_dir)
    return home_dir


# --- the happy path ----------------------------------------------------------


def test_new_entries_are_uploaded_once_as_one_protect_run(protect_project, home):
    write_violation(protect_project, "antigravity shell denied: protected_path .env")
    write_orchestrator(protect_project, outcome="ok", request_bytes=512)

    with backend() as server:
        outcome = sync_mod.perform(protect_project, "proj-abc", server.origin, TOKEN)

    assert outcome.nothing is False and outcome.event_count == 2
    # exactly one Run created, agent protect, the resolved project
    [create] = creates(server)
    assert create.json == {"project": "proj-abc", "run_id": outcome.run_id, "agent": "protect"}
    assert create.authorization == f"Token {TOKEN}"
    # the mapped events, each keeping its own type and timestamp
    posted = events_posted(server)
    assert [e["event_type"] for e in posted] == ["violation", "output"]
    assert posted[0]["payload"] == "antigravity shell denied: protected_path .env"
    assert posted[0]["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert posted[1]["payload"] == "orchestrator: gemini gemini-2.5-flash ok (512 bytes)"
    # moved to awaiting_review
    patches = [r for r in server.received if r.method == "PATCH"]
    assert patches and patches[-1].json == {"status": "awaiting_review"}


def test_a_second_sync_uploads_nothing(protect_project, home):
    write_violation(protect_project, "denied once")
    with backend() as first:
        sync_mod.perform(protect_project, "proj-abc", first.origin, TOKEN)

    with backend() as second:
        outcome = sync_mod.perform(protect_project, "proj-abc", second.origin, TOKEN)
        assert outcome.nothing is True and outcome.run_id is None
        assert second.received == []  # no create, no events, no PATCH


def test_nothing_to_sync_creates_no_run(protect_project, home):
    with backend() as server:
        outcome = sync_mod.perform(protect_project, "proj-abc", server.origin, TOKEN)
    assert outcome.nothing is True
    assert server.received == []


def test_offsets_are_not_advanced_when_the_upload_fails(protect_project, home):
    write_violation(protect_project, "denied, will be resent")

    with backend(fail={"events": 500}) as broken:
        with pytest.raises(uploader.UploadError):
            sync_mod.perform(protect_project, "proj-abc", broken.origin, TOKEN)

    # sync.json must not record progress, so the next attempt resends the same entry
    assert not (protect_project / ".vaultshield" / "sync.json").exists()
    with backend() as good:
        outcome = sync_mod.perform(protect_project, "proj-abc", good.origin, TOKEN)
    assert outcome.event_count == 1
    assert [e["payload"] for e in events_posted(good)] == ["denied, will be resent"]


def test_a_blank_violation_payload_is_neither_uploaded_nor_counted(protect_project, home):
    write_violation(protect_project, "")
    write_violation(protect_project, "antigravity shell denied: protected_path .env")
    write_violation(protect_project, "   ")
    with backend() as server:
        outcome = sync_mod.perform(protect_project, "proj-abc", server.origin, TOKEN)
    assert outcome.event_count == 1
    assert [e["payload"] for e in events_posted(server)] == ["antigravity shell denied: protected_path .env"]

    # Only blank lines: nothing new, so no Run is created at all.
    write_violation(protect_project, "")
    with backend() as again:
        assert sync_mod.perform(protect_project, "proj-abc", again.origin, TOKEN).nothing is True
    assert again.received == []


def test_a_rejected_token_does_not_advance_offsets(protect_project, home):
    write_violation(protect_project, "denied")
    with backend(fail={"create": 401}) as server:
        with pytest.raises(uploader.UploadError):
            sync_mod.perform(protect_project, "proj-abc", server.origin, TOKEN)
    assert not (protect_project / ".vaultshield" / "sync.json").exists()


# --- scrubbing ---------------------------------------------------------------


def test_a_vault_value_in_a_payload_is_scrubbed_before_upload(protect_project, home):
    # Protect a real (fake) value, then plant it in a violation payload. Nothing
    # logs a value in practice, but the scrubber is the guarantee, not the logger.
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    assert run("protect").exit_code == 0
    write_violation(protect_project, f"someone logged {KEY_VALUE} by mistake")

    with backend() as server:
        sync_mod.perform(protect_project, "proj-abc", server.origin, TOKEN)

    posted = events_posted(server)
    whole = json.dumps(posted)
    assert KEY_VALUE not in whole  # positive control: the value never leaves
    assert "***" in posted[0]["payload"]


def test_sync_refuses_to_upload_if_a_value_survives_scrubbing(protect_project, home, monkeypatch):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    assert run("protect").exit_code == 0
    write_violation(protect_project, f"logged {KEY_VALUE}")

    # A scrub that does nothing stands in for a scrubbing bug: the final check
    # must catch it and upload nothing.
    monkeypatch.setattr(sync_mod.SecretSet, "scrub", lambda self, text: text)
    with backend() as server:
        with pytest.raises(sync_mod.SyncError):
            sync_mod.perform(protect_project, "proj-abc", server.origin, TOKEN)
        assert creates(server) == []  # nothing was created or uploaded


# --- project resolution (command level) --------------------------------------


def test_project_resolution_prefers_the_flag(protect_project):
    assert commands._resolve_sync_project("proj-flag", protect_project) == "proj-flag"


def test_project_resolution_reads_the_config(protect_project):
    (protect_project / ".vaultshield.yml").write_text(
        "start_command: node app.js\nport: 3000\nready_path: /\nagent: ''\nroutes: []\noff_limits_paths: []\nproject: proj-cfg\n"
    )
    assert commands._resolve_sync_project(None, protect_project) == "proj-cfg"


def test_project_resolution_errors_when_nothing_names_a_project(protect_project):
    (protect_project / ".vaultshield.yml").write_text(
        "start_command: node app.js\nport: 3000\nready_path: /\nagent: ''\nroutes: []\noff_limits_paths: []\n"
    )
    with pytest.raises(Exception) as info:
        commands._resolve_sync_project(None, protect_project)
    assert "No project for this sync" in str(info.value)


# --- command level: messages and token ---------------------------------------


def test_the_command_says_nothing_to_sync_and_makes_no_request(protect_project, home):
    with backend() as server:
        result = run("protect", "sync", "--project", "proj-abc", "--backend-url", server.origin)
        assert result.exit_code == 0, result.output
        assert "nothing to sync" in result.output
        assert server.received == []


def test_the_command_uploads_and_reports_the_run(protect_project, home):
    write_violation(protect_project, "denied")
    with backend() as server:
        result = run("protect", "sync", "--project", "proj-abc", "--backend-url", server.origin)
    assert result.exit_code == 0, result.output
    assert "awaiting review" in result.output
    assert "run " in result.output


def test_a_missing_token_is_a_clear_error(protect_project, monkeypatch, tmp_path):
    # Point HOME at a directory with no token file.
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    write_violation(protect_project, "denied")
    result = run("protect", "sync", "--project", "proj-abc", "--backend-url", "http://127.0.0.1:9")
    assert result.exit_code != 0
    assert "vaultshield login" in result.output


def test_an_unreachable_backend_does_not_advance_offsets(protect_project, home):
    write_violation(protect_project, "denied")
    # Nothing is listening on this port.
    result = run("protect", "sync", "--project", "proj-abc", "--backend-url", "http://127.0.0.1:9")
    assert result.exit_code != 0
    assert "resends" in result.output
    assert not (protect_project / ".vaultshield" / "sync.json").exists()
