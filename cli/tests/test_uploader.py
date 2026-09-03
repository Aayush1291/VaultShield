"""Unit tests for `uploader.login`/token storage (Task 18.1) and
`uploader.upload_run` (Task 18.2).

Covers Requirement 14.1's "obtain and store a dashboard authentication
token" and the client side of Requirement 27.1's `POST /api/auth/login/`
contract: the credential that is posted, the endpoint it is posted to, the
token that comes back, where it is stored, and its file permissions — plus
every failure path (unreachable backend, rejected credential, tokenless
response), each of which must leave no token on disk.

The backend is a REAL local HTTP server on an ephemeral port, following
`test_browser_capture.py`'s own `_local_server` pattern, rather than a
mocked HTTP client: `login` issues a real `httpx.post`, the request body is
parsed by a real server, and the response is real JSON, so what is under
test is the whole flow instead of an assertion about a call. Requirement
27.1's response shape (`{"token": ...}`) is what the handler answers with,
which is also what `accounts.views.LoginView` inherits from DRF.

Storage is exercised against `tmp_path` via `login`'s `home` argument, so
the assertions are about a real file with real permission bits.

The `upload_run` tests below (Requirements 14.2-14.4) use the same
real-server approach against a second handler standing in for the three
artifact-ingest endpoints: real `httpx` requests, real multipart encoding,
real files in a real Run_Directory. What each test asserts is what the
backend would have received — the endpoint, the auth header, the batch
contents and their order, the multipart fields — rather than that some
function was called.

Task 18.3 (the upload-failure property test) and Task 18.4 (the
token-separation unit test) are separate, optional tasks and are
deliberately not duplicated here.
"""

from __future__ import annotations

import base64
import json
import socket
import stat
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator

import pytest

from vaultshield.run_directory import (
    diff_path,
    event_log_path,
    screenshots_dir,
)
from vaultshield.uploader import (
    DIFF_PATH_TEMPLATE,
    EVENTS_PATH_TEMPLATE,
    LOGIN_PATH,
    SCREENSHOTS_PATH_TEMPLATE,
    TOKEN_FILE_MODE,
    UploadError,
    load_token,
    login,
    store_token,
    token_path,
    upload_run,
)

# A token shaped like DRF's: `rest_framework.authtoken` issues a 40-character
# hex string, so the tests assert against something the real backend could
# actually have returned.
_TOKEN = "0123456789abcdef0123456789abcdef01234567"

_USERNAME = "dev"
_PASSWORD = "correct horse battery staple"


class _LoginHandler(BaseHTTPRequestHandler):
    """Stands in for `accounts.views.LoginView` on a real socket.

    Answers `POST /api/auth/login/` the way DRF's `ObtainAuthToken` does:
    `200 {"token": ...}` for the one credential it knows, and
    `400 {"non_field_errors": [...]}` for anything else (Requirement 27.2).
    Every request is recorded on the server object so a test can assert what
    was actually sent over the wire.
    """

    # Set per-server by `_login_server`; see its docstring.
    status_override: int | None = None
    body_override: bytes | None = None

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        self.server.requests.append((self.path, payload))  # type: ignore[attr-defined]

        override = self.server.response_override  # type: ignore[attr-defined]
        if override is not None:
            status, body = override
        elif (
            isinstance(payload, dict)
            and payload.get("username") == _USERNAME
            and payload.get("password") == _PASSWORD
        ):
            status, body = 200, json.dumps({"token": _TOKEN}).encode()
        else:
            status, body = 400, json.dumps(
                {"non_field_errors": ["Unable to log in with provided credentials."]}
            ).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silences the handler's default stderr access log."""


@contextmanager
def _login_server(
    response_override: tuple[int, bytes] | None = None,
) -> Iterator[ThreadingHTTPServer]:
    """Runs `_LoginHandler` on an ephemeral port and yields the server.

    `response_override` forces one fixed `(status, body)` for every request,
    which is how the "backend answered 2xx with no token" and other
    off-contract responses are exercised without teaching the handler about
    them. The yielded server carries `requests` (every `(path, json_body)`
    it received, in order) and `origin`.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoginHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.response_override = response_override  # type: ignore[attr-defined]
    server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _closed_port() -> int:
    """Returns a port number nothing is listening on: bound to find a free
    one, then released.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _file_mode(path: Path) -> int:
    """Returns just the permission bits of `path`."""
    return stat.S_IMODE(path.stat().st_mode)


# --- token_path ------------------------------------------------------------


def test_token_path_is_the_token_file_under_the_home_vaultshield_directory(
    tmp_path: Path,
):
    assert token_path(tmp_path) == tmp_path / ".vaultshield" / "token"


# --- login: the happy path (Requirements 14.1, 27.1) -----------------------


def test_login_stores_the_returned_token_at_the_canonical_path(tmp_path: Path):
    with _login_server() as server:
        login(
            server.origin,
            username=_USERNAME,
            password=_PASSWORD,
            home=tmp_path,
        )

    stored = token_path(tmp_path)
    assert stored.is_file()
    assert stored.read_text().strip() == _TOKEN


def test_login_posts_the_credential_to_the_login_endpoint(tmp_path: Path):
    with _login_server() as server:
        login(
            server.origin,
            username=_USERNAME,
            password=_PASSWORD,
            home=tmp_path,
        )
        requests = list(server.requests)

    assert requests == [(LOGIN_PATH, {"username": _USERNAME, "password": _PASSWORD})]


def test_login_tolerates_a_trailing_slash_on_the_backend_url(tmp_path: Path):
    with _login_server() as server:
        login(
            f"{server.origin}/",
            username=_USERNAME,
            password=_PASSWORD,
            home=tmp_path,
        )
        paths = [path for path, _ in server.requests]

    # Not `//api/auth/login/`, which the backend would 404.
    assert paths == [LOGIN_PATH]
    assert token_path(tmp_path).read_text().strip() == _TOKEN


def test_login_stores_the_token_readable_only_by_its_owner(tmp_path: Path):
    with _login_server() as server:
        login(
            server.origin,
            username=_USERNAME,
            password=_PASSWORD,
            home=tmp_path,
        )

    stored = token_path(tmp_path)
    assert _file_mode(stored) == TOKEN_FILE_MODE == 0o600
    assert _file_mode(stored.parent) == 0o700


def test_login_tightens_the_permissions_of_a_pre_existing_token_file(
    tmp_path: Path,
):
    # A token file left world-readable by an earlier tool (or by a
    # permissive umask) must not survive as-is: `os.open`'s mode argument
    # does not apply to an existing file, which is why `store_token` chmods
    # unconditionally.
    stored = token_path(tmp_path)
    stored.parent.mkdir(parents=True)
    stored.write_text("stale-token\n")
    stored.chmod(0o644)

    with _login_server() as server:
        login(
            server.origin,
            username=_USERNAME,
            password=_PASSWORD,
            home=tmp_path,
        )

    assert _file_mode(stored) == 0o600
    assert stored.read_text().strip() == _TOKEN


# --- login: failure paths --------------------------------------------------


def test_login_rejects_an_invalid_credential_and_stores_nothing(tmp_path: Path):
    with _login_server() as server:
        with pytest.raises(UploadError) as excinfo:
            login(
                server.origin,
                username=_USERNAME,
                password="wrong password",
                home=tmp_path,
            )

    # The message names the endpoint and the user, and quotes the backend's
    # own detail — but never the password that was sent.
    message = str(excinfo.value)
    assert "wrong password" not in message
    assert "Unable to log in with provided credentials." in message
    assert not token_path(tmp_path).exists()


def test_login_reports_an_unreachable_backend_and_stores_nothing(tmp_path: Path):
    origin = f"http://127.0.0.1:{_closed_port()}"

    with pytest.raises(UploadError) as excinfo:
        login(
            origin,
            username=_USERNAME,
            password=_PASSWORD,
            home=tmp_path,
        )

    assert origin in str(excinfo.value)
    assert not token_path(tmp_path).exists()


def test_login_rejects_a_successful_response_carrying_no_token(tmp_path: Path):
    with _login_server(response_override=(200, b'{"detail": "ok"}')) as server:
        with pytest.raises(UploadError) as excinfo:
            login(
                server.origin,
                username=_USERNAME,
                password=_PASSWORD,
                home=tmp_path,
            )

    assert "token" in str(excinfo.value)
    assert not token_path(tmp_path).exists()


def test_login_rejects_a_successful_response_that_is_not_json(tmp_path: Path):
    with _login_server(response_override=(200, b"<html>hello</html>")) as server:
        with pytest.raises(UploadError):
            login(
                server.origin,
                username=_USERNAME,
                password=_PASSWORD,
                home=tmp_path,
            )

    assert not token_path(tmp_path).exists()


def test_login_reports_a_server_error_and_stores_nothing(tmp_path: Path):
    with _login_server(response_override=(500, b"boom")) as server:
        with pytest.raises(UploadError) as excinfo:
            login(
                server.origin,
                username=_USERNAME,
                password=_PASSWORD,
                home=tmp_path,
            )

    assert "500" in str(excinfo.value)
    assert not token_path(tmp_path).exists()


# --- store_token -----------------------------------------------------------


def test_store_token_creates_the_directory_and_returns_the_path(tmp_path: Path):
    path = store_token(_TOKEN, tmp_path)

    assert path == token_path(tmp_path)
    assert path.read_text() == f"{_TOKEN}\n"
    assert _file_mode(path) == 0o600


def test_store_token_replaces_an_existing_token_completely(tmp_path: Path):
    store_token("a-much-longer-previous-token-value", tmp_path)
    path = store_token(_TOKEN, tmp_path)

    assert path.read_text() == f"{_TOKEN}\n"


def test_store_token_reports_an_unwritable_location(tmp_path: Path):
    # A file where `~/.vaultshield/` should be: the directory cannot be
    # created, so storage fails with the path named rather than with a bare
    # OSError.
    (tmp_path / ".vaultshield").write_text("not a directory")

    with pytest.raises(UploadError) as excinfo:
        store_token(_TOKEN, tmp_path)

    assert str(token_path(tmp_path)) in str(excinfo.value)

# --- load_token ------------------------------------------------------------


def test_load_token_reads_back_what_store_token_wrote(tmp_path: Path):
    store_token(_TOKEN, tmp_path)

    # Not `f"{_TOKEN}\n"`: the trailing newline must not reach an
    # `Authorization` header.
    assert load_token(tmp_path) == _TOKEN


def test_load_token_reports_a_missing_token_by_naming_the_login_command(
    tmp_path: Path,
):
    with pytest.raises(UploadError) as excinfo:
        load_token(tmp_path)

    message = str(excinfo.value)
    assert str(token_path(tmp_path)) in message
    assert "vaultshield login" in message


def test_load_token_rejects_an_empty_token_file(tmp_path: Path):
    stored = token_path(tmp_path)
    stored.parent.mkdir(parents=True)
    stored.write_text("\n")

    with pytest.raises(UploadError):
        load_token(tmp_path)


# --- upload_run ------------------------------------------------------------

_RUN_ID = "20250614-091530-3f9a2b7c8d1e4f60"

# A real 1x1 PNG. Real bytes rather than a placeholder because the backend's
# `Screenshot.image` is an `ImageField` that decodes what it is sent, so a
# test that uploaded arbitrary bytes would be exercising a request the real
# endpoint would reject.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH"
    "/q842iQAAAABJRU5ErkJggg=="
)

# What each ingest endpoint's path is for `_RUN_ID`, so the assertions name
# the same URLs `RunViewSet`'s routes serve.
_EVENTS_PATH = EVENTS_PATH_TEMPLATE.format(run_id=_RUN_ID)
_DIFF_PATH = DIFF_PATH_TEMPLATE.format(run_id=_RUN_ID)
_SCREENSHOTS_PATH = SCREENSHOTS_PATH_TEMPLATE.format(run_id=_RUN_ID)


class _IngestRequest:
    """One request the ingest server received, in the terms the assertions
    are written in: which endpoint, how it was authenticated, and its body
    (parsed as JSON when it was JSON, raw bytes when it was multipart).
    """

    def __init__(self, path: str, headers, body: bytes) -> None:
        self.path = path
        self.authorization = headers.get("Authorization")
        self.content_type = headers.get("Content-Type") or ""
        self.body = body

    @property
    def json(self):
        return json.loads(self.body)


class _IngestHandler(BaseHTTPRequestHandler):
    """Stands in for `RunViewSet`'s three ingest actions on a real socket.

    Answers `201` with an empty JSON object by default — the status DRF
    returns for a created event batch, diff, or screenshot — and records
    every request so a test can assert what actually went over the wire. A
    per-server `responder` can override the answer for any single request,
    which is how the rejected-batch path is exercised.
    """

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        received = self.server.received  # type: ignore[attr-defined]
        index = len(received)
        received.append(_IngestRequest(self.path, self.headers, body))

        status, payload = self.server.responder(index, self.path)  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Silences the handler's default stderr access log."""


def _created(index: int, path: str) -> tuple[int, bytes]:
    """The default answer: `201 {}` for every ingest request."""
    return 201, b"{}"


@contextmanager
def _ingest_server(
    responder: Callable[[int, str], tuple[int, bytes]] = _created,
) -> Iterator[ThreadingHTTPServer]:
    """Runs `_IngestHandler` on an ephemeral port and yields the server,
    carrying `received` (every request, in order) and `origin`.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IngestHandler)
    server.received = []  # type: ignore[attr-defined]
    server.responder = responder  # type: ignore[attr-defined]
    server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_event_log(run_dir: Path, count: int) -> list[dict]:
    """Writes `count` events to `<run_dir>/events.jsonl` in the shape
    `monitor.py` writes them, and returns them in file order.
    """
    events = [
        {
            "event_type": "output",
            "timestamp": f"2025-06-14T09:15:{second:02d}+00:00",
            # Distinguishable per event, so an assertion about order is an
            # assertion about these specific events and not just their count.
            "payload": f"line {second}\n",
        }
        for second in range(count)
    ]
    run_dir.mkdir(parents=True, exist_ok=True)
    event_log_path(run_dir).write_text(
        "".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8"
    )
    return events


def _write_diff(run_dir: Path) -> dict:
    """Writes `<run_dir>/diff.json` in the shape `git_capture.py` writes it,
    and returns its contents.
    """
    diff = {"files": [{"path": "src/app.py", "additions": 12, "deletions": 3}]}
    run_dir.mkdir(parents=True, exist_ok=True)
    diff_path(run_dir).write_text(json.dumps(diff), encoding="utf-8")
    return diff


def _write_screenshots(run_dir: Path, *stems: str) -> None:
    """Writes one real PNG per `stems` entry into `<run_dir>/screenshots/`,
    named the way `browser_capture.route_slug` names them.
    """
    directory = screenshots_dir(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (directory / f"{stem}.png").write_bytes(_PNG)


def _snapshot(run_dir: Path) -> dict[str, bytes]:
    """Every file under `run_dir`, by relative path, with its bytes — so a
    failed upload can be shown to have left the artifacts alone.
    """
    return {
        str(path.relative_to(run_dir)): path.read_bytes()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }


def _complete_run_dir(tmp_path: Path) -> Path:
    """A Run_Directory holding all three artifacts."""
    run_dir = tmp_path / "runs" / _RUN_ID
    _write_event_log(run_dir, 3)
    _write_diff(run_dir)
    _write_screenshots(run_dir, "root", "users")
    return run_dir


# --- upload_run: the happy path (Requirements 14.2, 14.3) ------------------


def test_upload_run_posts_every_artifact_to_its_own_endpoint(tmp_path: Path):
    run_dir = _complete_run_dir(tmp_path)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        paths = [request.path for request in server.received]

    # One events batch, one diff, one POST per screenshot, in design.md's
    # order (events, diff, screenshots).
    assert paths == [_EVENTS_PATH, _DIFF_PATH, _SCREENSHOTS_PATH, _SCREENSHOTS_PATH]


def test_upload_run_authenticates_every_request_with_the_stored_token(
    tmp_path: Path,
):
    run_dir = _complete_run_dir(tmp_path)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        authorizations = {request.authorization for request in server.received}

    # DRF's `TokenAuthentication` keyword, not `Bearer`.
    assert authorizations == {f"Token {_TOKEN}"}


def test_upload_run_posts_the_event_log_in_file_order_across_batches(
    tmp_path: Path,
):
    run_dir = tmp_path / "runs" / _RUN_ID
    events = _write_event_log(run_dir, 5)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN, batch_size=2)
        batches = [request.json for request in server.received]

    # Split into 2 + 2 + 1, posted in that order, and the concatenation is
    # the Event_Log's own line order — which is what the backend numbers
    # `sequence` from.
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert [event for batch in batches for event in batch] == events


def test_upload_run_posts_events_without_inventing_a_sequence_field(
    tmp_path: Path,
):
    run_dir = tmp_path / "runs" / _RUN_ID
    events = _write_event_log(run_dir, 2)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        batch = server.received[0].json

    # The Event_Log's lines, untranslated: `sequence` is read-only on the
    # ingest serializer and assigned server-side from list position.
    assert batch == events
    assert all("sequence" not in event for event in batch)


def test_upload_run_posts_the_diff_verbatim(tmp_path: Path):
    run_dir = tmp_path / "runs" / _RUN_ID
    diff = _write_diff(run_dir)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        request = server.received[0]

    assert request.path == _DIFF_PATH
    assert request.json == diff


def test_upload_run_posts_each_screenshot_as_multipart_with_its_route(
    tmp_path: Path,
):
    run_dir = tmp_path / "runs" / _RUN_ID
    _write_screenshots(run_dir, "root", "users-42")

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        requests = list(server.received)

    assert len(requests) == 2
    for request, stem in zip(requests, ["root", "users-42"]):
        assert request.path == _SCREENSHOTS_PATH
        assert request.content_type.startswith("multipart/form-data")
        # The `route` field carries the filename stem (the only record of the
        # route that survives `route_slug`), and the `image` field carries
        # the real PNG bytes.
        assert b'name="route"' in request.body
        assert stem.encode() in request.body
        assert _PNG in request.body


def test_upload_run_skips_artifacts_a_run_never_produced(tmp_path: Path):
    # A Run whose git capture failed and which had no configured routes:
    # only an Event_Log exists. The upload still submits what there is.
    run_dir = tmp_path / "runs" / _RUN_ID
    _write_event_log(run_dir, 1)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        paths = [request.path for request in server.received]

    assert paths == [_EVENTS_PATH]


def test_upload_run_sends_nothing_for_a_run_with_no_artifacts(tmp_path: Path):
    run_dir = tmp_path / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        received = list(server.received)

    # An empty Run_Directory is not a failure, and an empty event batch is
    # not worth a request.
    assert received == []


def test_upload_run_tolerates_a_trailing_slash_on_the_backend_url(tmp_path: Path):
    run_dir = tmp_path / "runs" / _RUN_ID
    _write_diff(run_dir)

    with _ingest_server() as server:
        upload_run(run_dir, _RUN_ID, f"{server.origin}/", _TOKEN)
        paths = [request.path for request in server.received]

    # Not `//api/runs/...`, which the backend would 404.
    assert paths == [_DIFF_PATH]


# --- upload_run: failure paths (Requirement 14.4) --------------------------


def test_upload_run_reports_an_unreachable_backend_and_preserves_artifacts(
    tmp_path: Path,
):
    run_dir = _complete_run_dir(tmp_path)
    before = _snapshot(run_dir)
    origin = f"http://127.0.0.1:{_closed_port()}"

    with pytest.raises(UploadError) as excinfo:
        upload_run(run_dir, _RUN_ID, origin, _TOKEN)

    assert origin in str(excinfo.value)
    assert _snapshot(run_dir) == before


def test_upload_run_reports_a_rejected_batch_naming_the_stage_and_preserves_artifacts(
    tmp_path: Path,
):
    run_dir = _complete_run_dir(tmp_path)
    before = _snapshot(run_dir)

    def reject_everything(index: int, path: str) -> tuple[int, bytes]:
        return 400, json.dumps({"detail": "nope"}).encode()

    with _ingest_server(reject_everything) as server:
        with pytest.raises(UploadError) as excinfo:
            upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        received = list(server.received)

    message = str(excinfo.value)
    assert "events" in message
    assert "400" in message
    assert "nope" in message
    # Stopped at the first failure rather than pressing on through the diff
    # and the screenshots.
    assert len(received) == 1
    assert _snapshot(run_dir) == before


def test_upload_run_stops_at_the_first_failing_screenshot(tmp_path: Path):
    run_dir = tmp_path / "runs" / _RUN_ID
    _write_screenshots(run_dir, "a-route", "b-route", "c-route")
    before = _snapshot(run_dir)

    def reject_the_second(index: int, path: str) -> tuple[int, bytes]:
        return (500, b"boom") if index == 1 else (201, b"{}")

    with _ingest_server(reject_the_second) as server:
        with pytest.raises(UploadError) as excinfo:
            upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        received = list(server.received)

    # The message names the specific screenshot that failed, not just "the
    # upload".
    assert "b-route.png" in str(excinfo.value)
    assert len(received) == 2
    assert _snapshot(run_dir) == before


def test_upload_run_reports_a_corrupt_event_log_without_touching_it(tmp_path: Path):
    run_dir = tmp_path / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)
    event_log_path(run_dir).write_text('{"event_type": "output"}\nnot json\n')
    before = _snapshot(run_dir)

    with _ingest_server() as server:
        with pytest.raises(UploadError) as excinfo:
            upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)
        received = list(server.received)

    # Line 2 is named, and nothing was posted: a malformed Event_Log fails
    # the upload rather than being silently dropped from the review.
    assert "line 2" in str(excinfo.value)
    assert received == []
    assert _snapshot(run_dir) == before


def test_upload_run_reports_a_corrupt_diff_without_touching_it(tmp_path: Path):
    run_dir = tmp_path / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)
    diff_path(run_dir).write_text("{ truncated")
    before = _snapshot(run_dir)

    with _ingest_server() as server:
        with pytest.raises(UploadError) as excinfo:
            upload_run(run_dir, _RUN_ID, server.origin, _TOKEN)

    assert str(diff_path(run_dir)) in str(excinfo.value)
    assert _snapshot(run_dir) == before


def test_upload_run_rejects_an_unsafe_run_id_before_any_request(tmp_path: Path):
    run_dir = _complete_run_dir(tmp_path)

    with _ingest_server() as server:
        with pytest.raises(UploadError) as excinfo:
            upload_run(run_dir, "../projects", server.origin, _TOKEN)
        received = list(server.received)

    assert "../projects" in str(excinfo.value)
    assert received == []
