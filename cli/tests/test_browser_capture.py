"""Unit tests for Browser_Capture_Module's app-start, readiness polling, and
per-route screenshot capture (Tasks 11.1 and 11.2, Requirements 9.1-9.4).

Property 14 ("Readiness polling stops exactly at success or timeout") and
Property 15 ("Every configured route is screenshotted exactly once on
success") are Tasks 11.3 and 11.4's own property tests and are deliberately
NOT written here. These are the fixed-scenario checks: the loop's
termination behavior at the two boundaries it is easy to get wrong (the
first 200, and the deadline), the URL join, the `NetworkSettings` parsing
that turns a container into a host-reachable origin, the guards on calling
either new Sandbox capability before a container exists, and the
route -> filename rules plus failure behavior of the screenshot half.

Three levels of realism are used on purpose:

- The loop tests inject a scripted probe plus a fake clock. That is not a
  stand-in for the code under test — the loop itself is real — it is the
  only way to drive an exact status-code sequence and exact elapsed time
  without waiting in real seconds for a real server to misbehave.
- The probe tests use a REAL HTTP server on a real socket, and a real
  closed port for the unreachable case, so `_http_probe`'s actual
  `httpx` behavior (status codes, and `RequestError` -> `None`) is
  verified against sockets rather than asserted about.
- The screenshot tests inject a browser session and write to a real
  `tmp_path`. Only the browser itself is stood in for; the slugging,
  ordering, de-duplication, collision suffixes, `mkdir`, and error wrapping
  under test are all real code writing real files. A real Chromium is not
  used because the browser binary is a separate per-machine install
  (`playwright install chromium`) that CI for this package does not
  require.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from vaultshield.browser_capture import (
    READY_STATUS,
    ReadinessTimeout,
    ScreenshotError,
    _build_url,
    _container_ip,
    _http_probe,
    _published_host_port,
    _screenshot_targets,
    capture_route_screenshots,
    resolve_base_url,
    route_slug,
    start_and_capture,
    wait_until_ready,
)
from vaultshield.config import ProjectConfig
from vaultshield.run_directory import screenshots_dir
from vaultshield.sandbox import Sandbox, SandboxError

# ---------------------------------------------------------------------------
# Test doubles for the clock and the probe. Neither replaces any VaultShield
# code: `wait_until_ready`'s loop is the real thing under test.
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only advances when `sleep` is called.

    Makes elapsed time exact and the tests instant: a 60-second readiness
    budget costs no real waiting, and `now` after the fact is exactly how
    far the loop let the clock run — which is how the "never probes past the
    deadline" assertion below is made.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class ScriptedProbe:
    """Returns each outcome of `script` in turn, then repeats the last one.

    Records the URL of every call, so a test can assert both how many
    probes happened and that they all targeted the resolved `ready_path`.
    `None` entries stand for a transport-level failure (connection
    refused), matching `_http_probe`'s own `None` return.
    """

    def __init__(self, script: list[int | None]) -> None:
        self._script = script
        self.calls: list[str] = []

    def __call__(self, url: str) -> int | None:
        index = min(len(self.calls), len(self._script) - 1)
        self.calls.append(url)
        return self._script[index]


@contextmanager
def _local_server() -> Iterator[str]:
    """Runs a real HTTP server on an ephemeral port, answering `200` for
    `/ready` and `503` for every other path. Yields its origin.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self.send_response(READY_STATUS if self.path == "/ready" else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silences the handler's default stderr access log."""


def _closed_port() -> int:
    """Returns a port number nothing is listening on: bound to find a free
    one, then released.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeBrowserSession:
    """Stands in for `_playwright_session`: records every `(url, path)` it
    was asked to capture and writes a real (tiny) file at `path`, so the
    tests assert against files on disk rather than against calls alone.

    `fail_on` makes the capture of one URL raise, which is how the
    all-or-nothing failure behavior is exercised without a flaky browser.
    """

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.captured: list[tuple[str, Path]] = []
        self.entered = 0
        self.exited = 0
        self._fail_on = fail_on

    @contextmanager
    def __call__(self) -> Iterator[object]:
        self.entered += 1
        try:
            yield self._capture
        finally:
            self.exited += 1

    def _capture(self, url: str, path: Path) -> None:
        if url == self._fail_on:
            raise RuntimeError("Timeout 30000ms exceeded")
        # A real write, so "was a screenshot saved here?" is answered by the
        # filesystem. The bytes are the PNG magic number rather than a whole
        # image: nothing under test reads them back.
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.captured.append((url, path))


class FakeContainer:
    """The two pieces of a `docker-py` container `resolve_base_url` reads:
    `reload()` and `attrs["NetworkSettings"]`, with `container_port`
    published to `host_port`.
    """

    def __init__(self, container_port: int, host_port: int) -> None:
        self.attrs = {
            "NetworkSettings": {
                "Ports": {
                    f"{container_port}/tcp": [
                        {"HostIp": "0.0.0.0", "HostPort": str(host_port)}
                    ]
                }
            }
        }

    def reload(self) -> None:
        """No-op: the fake's `attrs` are already current."""


class FakeSandbox:
    """A Sandbox with a published port and a recording `exec_detached`, so
    `start_and_capture` can be driven end to end against a real local HTTP
    server with no Docker daemon involved.
    """

    def __init__(self, container_port: int, host_port: int) -> None:
        self.run_id = "20250614-091530-deadbeefdeadbeef"
        self.container = FakeContainer(container_port, host_port)
        self.started: list[tuple[list[str], dict[str, str] | None]] = []

    def exec_detached(
        self, cmd: list[str], *, env: dict[str, str] | None = None
    ) -> None:
        self.started.append((cmd, env))


def _config(**overrides: object) -> ProjectConfig:
    fields: dict[str, object] = {
        "start_command": "npm run dev",
        "port": 3000,
        "ready_path": "/ready",
        "agent": "mock",
        "routes": ["/"],
        "off_limits_paths": [],
    }
    fields.update(overrides)
    return ProjectConfig(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Readiness polling (Requirements 9.2, 9.4)
# ---------------------------------------------------------------------------


def test_polling_stops_at_the_first_200_without_sleeping():
    clock = FakeClock()
    probe = ScriptedProbe([READY_STATUS])

    url = wait_until_ready(
        "http://127.0.0.1:5001",
        "/healthz",
        60,
        probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert url == "http://127.0.0.1:5001/healthz"
    # One probe, issued immediately: an application that is already up
    # costs no wait, and nothing is polled after the 200.
    assert probe.calls == [url]
    assert clock.slept == []


def test_polling_retries_non_200_and_connection_failures_then_succeeds():
    clock = FakeClock()
    # Connection refused (still binding), then a 503 (up but not healthy),
    # then ready. Both non-ready outcomes must be retried, not treated as
    # failures.
    probe = ScriptedProbe([None, 503, READY_STATUS])

    url = wait_until_ready(
        "http://127.0.0.1:5001",
        "/healthz",
        60,
        poll_interval_s=1.0,
        probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert len(probe.calls) == 3
    assert set(probe.calls) == {url}
    assert clock.slept == [1.0, 1.0]


def test_polling_raises_readiness_timeout_and_never_probes_past_the_deadline():
    clock = FakeClock()
    probe = ScriptedProbe([500])

    with pytest.raises(ReadinessTimeout) as excinfo:
        wait_until_ready(
            "http://127.0.0.1:5001",
            "/healthz",
            3.0,
            poll_interval_s=1.0,
            probe=probe,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    # Probes at t=0,1,2,3 — the last one lands exactly on the deadline, and
    # the timeout is raised immediately after it rather than after another
    # sleep or another probe.
    assert len(probe.calls) == 4
    assert clock.slept == [1.0, 1.0, 1.0]
    assert clock.now == 3.0

    # Requirement 9.4: the failure names what was polled and the budget
    # that elapsed, as attributes rather than only as prose.
    assert excinfo.value.url == "http://127.0.0.1:5001/healthz"
    assert excinfo.value.timeout_s == 3.0


def test_polling_truncates_the_final_sleep_to_the_deadline():
    clock = FakeClock()
    probe = ScriptedProbe([500])

    with pytest.raises(ReadinessTimeout):
        wait_until_ready(
            "http://127.0.0.1:5001",
            "/healthz",
            2.5,
            poll_interval_s=1.0,
            probe=probe,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    # The last sleep is shortened to the 0.5s left rather than overshooting
    # the budget by half a second.
    assert clock.slept == [1.0, 1.0, 0.5]
    assert clock.now == 2.5


def test_polling_probes_once_even_with_a_zero_timeout():
    clock = FakeClock()
    ready = ScriptedProbe([READY_STATUS])
    assert wait_until_ready(
        "http://127.0.0.1:5001",
        "/",
        0,
        probe=ready,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert len(ready.calls) == 1

    not_ready = ScriptedProbe([500])
    with pytest.raises(ReadinessTimeout):
        wait_until_ready(
            "http://127.0.0.1:5001",
            "/",
            0,
            probe=not_ready,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert len(not_ready.calls) == 1


# ---------------------------------------------------------------------------
# The default probe, against real sockets
# ---------------------------------------------------------------------------


def test_probe_reports_real_status_codes():
    with _local_server() as base_url:
        assert _http_probe(f"{base_url}/ready") == READY_STATUS
        assert _http_probe(f"{base_url}/starting") == 503


def test_probe_returns_none_for_an_unreachable_port():
    assert _http_probe(f"http://127.0.0.1:{_closed_port()}/ready") is None


def test_polling_against_a_real_server_succeeds_and_times_out():
    with _local_server() as base_url:
        # Real probe, real sleeps, real socket: the default path works
        # end to end.
        assert wait_until_ready(base_url, "/ready", 5, poll_interval_s=0.01) == (
            f"{base_url}/ready"
        )

        # A server that answers, but never with a 200, still times out.
        with pytest.raises(ReadinessTimeout):
            wait_until_ready(base_url, "/starting", 0.2, poll_interval_s=0.05)


# ---------------------------------------------------------------------------
# URL joining and address resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        ("http://127.0.0.1:5001", "/healthz", "http://127.0.0.1:5001/healthz"),
        ("http://127.0.0.1:5001/", "/healthz", "http://127.0.0.1:5001/healthz"),
        # A `ready_path` written without a leading slash stays a path on the
        # origin rather than being resolved relative to it.
        ("http://127.0.0.1:5001", "healthz", "http://127.0.0.1:5001/healthz"),
        ("http://127.0.0.1:5001", "/", "http://127.0.0.1:5001/"),
        ("http://127.0.0.1:5001", "", "http://127.0.0.1:5001/"),
    ],
)
def test_build_url_joins_origin_and_path(base_url, path, expected):
    assert _build_url(base_url, path) == expected


def test_published_host_port_is_read_from_network_settings():
    # Shape as Docker reports it: one binding entry per host interface,
    # both carrying the same host port.
    network = {
        "Ports": {
            "3000/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "54321"},
                {"HostIp": "::", "HostPort": "54321"},
            ]
        }
    }
    assert _published_host_port(network, 3000) == 54321

    # A container port with no published binding, and an unpublished
    # container altogether.
    assert _published_host_port({"Ports": {"3000/tcp": None}}, 3000) is None
    assert _published_host_port({"Ports": {}}, 3000) is None
    assert _published_host_port({}, 3000) is None


def test_container_ip_covers_default_and_user_defined_networks():
    assert _container_ip({"IPAddress": "172.17.0.2"}) == "172.17.0.2"
    # A user-defined network leaves the top-level field empty and reports
    # the address per network instead.
    assert (
        _container_ip(
            {"IPAddress": "", "Networks": {"vaultshield": {"IPAddress": "172.20.0.5"}}}
        )
        == "172.20.0.5"
    )
    assert _container_ip({"IPAddress": "", "Networks": {}}) is None


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        # The route almost every project configures first must not become
        # `.png`.
        ("/", "root"),
        ("", "root"),
        ("/dashboard", "dashboard"),
        ("/users/42", "users-42"),
        # Query strings, mixed case, and repeated/edge separators all fold
        # into the same safe character set.
        ("/users/42?tab=activity", "users-42-tab-activity"),
        ("/Admin/Settings", "admin-settings"),
        ("/a//b/", "a-b"),
        ("/search?q=hello world", "search-q-hello-world"),
        ("///", "root"),
    ],
)
def test_route_slug_is_filesystem_safe_and_recognisable(route, expected):
    assert route_slug(route) == expected


def test_route_slug_truncates_long_routes_without_a_trailing_separator():
    slug = route_slug("/" + "segment/" * 40)
    assert len(slug) <= 80
    assert not slug.endswith("-")
    assert slug.startswith("segment-")


# ---------------------------------------------------------------------------
# Per-route screenshot capture (Requirement 9.3)
# ---------------------------------------------------------------------------


def test_capture_saves_one_png_per_route_into_the_run_directory(tmp_path):
    session = FakeBrowserSession()
    routes = ["/", "/dashboard", "/users/42?tab=activity"]

    saved = capture_route_screenshots(
        "http://127.0.0.1:54321", routes, tmp_path, browser_session=session
    )

    # Requirement 9.3: one screenshot per configured route, saved into the
    # Run_Directory at design.md's `<run_dir>/screenshots/<route-slug>.png`.
    assert saved == [
        screenshots_dir(tmp_path) / "root.png",
        screenshots_dir(tmp_path) / "dashboard.png",
        screenshots_dir(tmp_path) / "users-42-tab-activity.png",
    ]
    assert all(path.is_file() for path in saved)

    # Each capture targeted the route's own URL on the ready origin, in
    # configured order, and exactly one browser was launched and closed for
    # the whole list.
    assert [url for url, _ in session.captured] == [
        "http://127.0.0.1:54321/",
        "http://127.0.0.1:54321/dashboard",
        "http://127.0.0.1:54321/users/42?tab=activity",
    ]
    assert (session.entered, session.exited) == (1, 1)


def test_capture_gives_slug_colliding_routes_their_own_files(tmp_path):
    session = FakeBrowserSession()

    # Three distinct routes that all slugify to `a-b`: each must still get
    # its own screenshot rather than overwriting the previous one.
    saved = capture_route_screenshots(
        "http://127.0.0.1:54321",
        ["/a/b", "/a-b", "/A/B"],
        tmp_path,
        browser_session=session,
    )

    assert [path.name for path in saved] == ["a-b.png", "a-b-2.png", "a-b-3.png"]
    assert len({path for path in saved}) == 3


def test_capture_treats_a_route_listed_twice_as_one_route(tmp_path):
    session = FakeBrowserSession()

    saved = capture_route_screenshots(
        "http://127.0.0.1:54321",
        ["/dashboard", "/", "/dashboard"],
        tmp_path,
        browser_session=session,
    )

    assert [path.name for path in saved] == ["dashboard.png", "root.png"]
    assert len(session.captured) == 2


def test_capture_with_no_configured_routes_writes_nothing_at_all(tmp_path):
    session = FakeBrowserSession()

    # `config.py` writes `routes: []` as an `init`-time placeholder, so this
    # is the common case for a not-yet-filled-in project: no screenshots, no
    # browser launched, and no empty `screenshots/` directory left behind.
    assert (
        capture_route_screenshots(
            "http://127.0.0.1:54321", [], tmp_path, browser_session=session
        )
        == []
    )
    assert session.entered == 0
    assert not screenshots_dir(tmp_path).exists()


def test_capture_failure_names_the_route_and_keeps_earlier_screenshots(tmp_path):
    session = FakeBrowserSession(fail_on="http://127.0.0.1:54321/broken")

    with pytest.raises(ScreenshotError) as excinfo:
        capture_route_screenshots(
            "http://127.0.0.1:54321",
            ["/", "/broken", "/never-reached"],
            tmp_path,
            browser_session=session,
        )

    # design.md: a screenshot failure is a distinct failure from a startup
    # timeout, and it names what could not be captured.
    assert not isinstance(excinfo.value, ReadinessTimeout)
    assert "/broken" in str(excinfo.value)

    # The screenshot taken before the failure is preserved on disk and
    # reported on the exception, and the browser was still shut down.
    root_png = screenshots_dir(tmp_path) / "root.png"
    assert excinfo.value.saved == [root_png]
    assert root_png.is_file()
    assert not (screenshots_dir(tmp_path) / "never-reached.png").exists()
    assert session.exited == 1


# ---------------------------------------------------------------------------
# `start_and_capture` end to end (Requirements 9.1-9.4)
# ---------------------------------------------------------------------------


def test_start_and_capture_starts_the_app_then_screenshots_every_route(tmp_path):
    session = FakeBrowserSession()

    with _local_server() as base_url:
        host_port = int(base_url.rsplit(":", 1)[1])
        sandbox = FakeSandbox(container_port=3000, host_port=host_port)
        config = _config(routes=["/", "/dashboard"])

        saved = start_and_capture(
            sandbox,  # type: ignore[arg-type]
            config,
            tmp_path,
            5,
            poll_interval_s=0.01,
            browser_session=session,
        )

    # Requirement 9.1: the configured `start_command` was started inside the
    # Sandbox, bound to the configured port.
    assert sandbox.started == [
        (
            ["sh", "-c", "npm run dev"],
            {"PORT": "3000", "HOST": "0.0.0.0", "HOSTNAME": "0.0.0.0"},
        )
    ]

    # Requirements 9.2/9.3: readiness was established against the real
    # server, and both routes were then captured at that same origin.
    assert [path.name for path in saved] == ["root.png", "dashboard.png"]
    assert all(path.is_file() for path in saved)
    assert [url for url, _ in session.captured] == [
        f"{base_url}/",
        f"{base_url}/dashboard",
    ]


def test_start_and_capture_timeout_captures_nothing(tmp_path):
    session = FakeBrowserSession()

    with _local_server() as base_url:
        host_port = int(base_url.rsplit(":", 1)[1])
        sandbox = FakeSandbox(container_port=3000, host_port=host_port)
        # `/starting` answers 503 forever, so readiness never succeeds.
        config = _config(ready_path="/starting", routes=["/", "/dashboard"])

        with pytest.raises(ReadinessTimeout):
            start_and_capture(
                sandbox,  # type: ignore[arg-type]
                config,
                tmp_path,
                0.2,  # type: ignore[arg-type]
                poll_interval_s=0.05,
                browser_session=session,
            )

    # Requirement 9.4: zero screenshots for a timed-out Run — no browser was
    # launched, and nothing was written under the Run_Directory.
    assert session.entered == 0
    assert session.captured == []
    assert not screenshots_dir(tmp_path).exists()


def test_screenshot_targets_is_pure(tmp_path):
    # The path resolution creates nothing on its own: the `mkdir` belongs to
    # the capture step, once there is something to write.
    targets = _screenshot_targets(
        "http://127.0.0.1:54321/", ["/", "/a"], screenshots_dir(tmp_path)
    )

    assert targets == [
        ("http://127.0.0.1:54321/", screenshots_dir(tmp_path) / "root.png"),
        ("http://127.0.0.1:54321/a", screenshots_dir(tmp_path) / "a.png"),
    ]
    assert not screenshots_dir(tmp_path).exists()


def test_sandbox_capabilities_refuse_to_run_without_a_container():
    # Both of Task 11.1's Sandbox-side entry points must fail clearly rather
    # than with an AttributeError when no container exists — the same guard
    # `Sandbox.exec` already applies. No Docker daemon is involved: the
    # client is never touched before a container exists.
    sandbox = Sandbox("20250614-091530-deadbeefdeadbeef", client=None)

    with pytest.raises(SandboxError, match="no container has been created"):
        sandbox.exec_detached(["sh", "-c", "npm start"])

    with pytest.raises(SandboxError, match="no container has been created"):
        resolve_base_url(sandbox, 3000)
