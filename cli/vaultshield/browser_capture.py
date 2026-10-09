"""Browser_Capture_Module: start the Run's application inside the Sandbox,
wait for it to become ready, then screenshot each configured route.

THIS FILE implements Tasks 11.1 and 11.2 — the whole of design.md's
`start_and_capture`: app start, `ready_path` polling, the
`ReadinessTimeout` it raises, and the Playwright screenshot of every
configured route. Task 11.5 (wiring this module into `cli.py`'s `run`
pipeline) is still open, and nothing here reads or touches `cli.py`.

Covers Requirement 9.1: "WHEN Browser_Capture_Module starts after agent
execution, THE Browser_Capture_Module SHALL start the target application
inside the Sandbox using the resolved `start_command` and `port` from the
Project_Config_File."
And Requirement 9.2: "WHILE the target application is starting, THE
Browser_Capture_Module SHALL poll the resolved `ready_path` until a 200
response is received or a timeout elapses."
And Requirement 9.3: "WHEN the resolved `ready_path` returns a 200
response, THE Browser_Capture_Module SHALL capture a screenshot of each
configured route using Playwright and SHALL save each screenshot into the
Run_Directory."
And Requirement 9.4: "IF the readiness timeout elapses without a 200
response from `ready_path`, THEN THE Browser_Capture_Module SHALL report a
startup timeout failure and SHALL NOT capture screenshots for that Run."

`httpx` is the HTTP client, per design.md's tech stack ("`httpx` for the
Uploader/Submit HTTP calls") — the same client for every outbound HTTP
call the CLI makes, rather than a second one just for readiness polling.

### Starting the application (Requirement 9.1)

`config.start_command` is run through `sh -c` inside the Sandbox, exactly
like `sandbox.py`'s dependency-install step, so an ordinary shell-shaped
command from `.vaultshield.yml` (`npm run dev`, `cd api && flask run`) works
verbatim. It is started via `Sandbox.exec_detached` (added by this task —
see `sandbox.py`'s module docstring): a dev server runs for the rest of the
Run, so the blocking `Sandbox.exec` cannot start it, and nothing here waits
on it. Whether the application actually came up is answered by the
readiness poll below, never by inspecting the exec — which is also why a
`start_command` that exits immediately (a typo, a missing dependency) is
reported as a `ReadinessTimeout` rather than as its own distinct failure.

"Bound to `port`" (Requirement 9.1's wording) is expressed two ways at
once, because `.vaultshield.yml` carries `start_command` and `port` as
independent fields and there is no general way to rewrite an arbitrary
command's own port argument:

1. `PORT` is injected into the started process's environment, which is the
   convention nearly every Node/Flask/Rails-style dev server reads. A
   `start_command` that already names the port itself (`config.py`'s
   Django detection emits `python manage.py runserver 0.0.0.0:8000`) simply
   ignores it, and the two agree because both come from the same config.
2. `HOST`/`HOSTNAME` are injected as `0.0.0.0`. This matters more than it
   looks: a server bound to `127.0.0.1` *inside* the container is not
   reachable through a published port at all, so a default-loopback dev
   server would be unreachable from the host no matter how the port is
   published. Frameworks that read neither variable still need their
   `start_command` to bind `0.0.0.0` themselves.

### Reaching the application from the host (`resolve_base_url`)

design.md says the poll goes "through the sandbox's published port", so
`resolve_base_url` reads the host port Docker assigned to
`<config.port>/tcp` off the container and returns
`http://127.0.0.1:<host_port>`. That binding exists when the Sandbox was
created with `Sandbox.create(..., publish_port=config.port)` (Task 11.5's
one-line job when it wires this module in).

If the port was not published, the container's own IP on its Docker
network is used instead (`http://<container_ip>:<config.port>`). That is a
documented partial fallback, not an equivalent path: container IPs are
routable from the host on Linux, where production Sandboxes run, but not
through Docker Desktop's VM on macOS/Windows. If neither a published port
nor a container IP is available, `SandboxError` is raised naming both
misses — the same exception type every other Sandbox-level failure in this
CLI already surfaces as, so `cli.py`'s existing `except SandboxError`
handler covers it unchanged.

A host-side base URL (rather than polling from inside the container with
`curl`) is what Task 11.2 needs anyway: Playwright drives a browser on the
host, so the readiness check and the screenshots must agree on one origin,
and checking readiness at the origin the screenshots will actually use is
the check worth making.

### Polling termination (Requirements 9.2, 9.4 / Property 14)

`wait_until_ready` is deliberately a separate, public function with an
injectable probe, clock, and sleep. Property 14 ("Readiness polling stops
exactly at success or timeout", Task 11.3) is a statement about this loop
alone, and injecting those three lets it be tested against a scripted
sequence of status codes with no Docker daemon, no real server, and no
real elapsed time — while `start_and_capture` keeps design.md's exact
signature.

The loop, precisely, so the property test has something exact to assert:

    1. Probe immediately, before any sleep. An application that is already
       up costs zero wait.
    2. A 200 stops the loop at once and returns. No further probe is
       issued, and no sleep happens after it.
    3. Any other outcome — any non-200 status, or a transport-level failure
       (`httpx.RequestError`: connection refused while the server is still
       binding, a read timeout, a DNS failure) — counts as "not ready yet"
       and is retried. A refused connection is the *expected* state for the
       first second or so of a Run, so treating it as a failure would fail
       essentially every Run.
    4. After a not-ready outcome, the deadline is checked. Once reached,
       `ReadinessTimeout` is raised immediately — no further probe, and no
       sleep before raising.
    5. Otherwise sleep `poll_interval_s`, truncated so it never overshoots
       the deadline, and go back to step 1.

Two consequences worth stating outright, since both are the kind of thing
a property test pins down: at least one probe always happens (even with
`ready_timeout_s=0`), and the last probe can land exactly on the deadline
but never after it.

The clock is `time.monotonic`, not `time.time`: a wall-clock adjustment
(NTP step, DST) mid-Run must not extend or truncate a readiness timeout.

### Screenshotting the routes (Requirement 9.3 / Property 15)

`capture_route_screenshots` is reached only once the poll above returned,
so a timed-out Run never gets here and Requirement 9.4's "SHALL NOT
capture screenshots" holds by control flow rather than by a check. Each
route is fetched at `_build_url(base_url, route)` — the same origin whose
readiness was just established, which is the point of resolving a
host-side origin at all — and saved as
`<run_dir>/screenshots/<route-slug>.png`, per design.md.

Playwright's *synchronous* API is used (`playwright.sync_api`): this is a
CLI with no event loop of its own, and `sync_playwright` cannot be used
from inside a running asyncio loop anyway. One Chromium instance and one
page are launched for the whole route list and reused across routes,
rather than one browser per route — browser startup dominates the cost of
a screenshot, and nothing about one route's capture should leak into
another beyond ordinary same-origin browser state.

The import of `playwright` is deliberately *inside*
`_playwright_session`, not at module scope. Two reasons: importing this
module (which `cli.py` does on every command, including `init`) must not
pay for Playwright's import, and a machine without the package installed
must still be able to run `vaultshield init`/`config` rather than failing
at import time. A missing package or a missing browser binary surfaces as
a `ScreenshotError` naming `playwright install chromium`, because that —
not `pip install` — is the step people actually forget: the Python
package pulls no browser with it.

Route -> filename (`route_slug`), in order:

    1. Lowercase, then replace every character outside `[a-z0-9]` with
       `-`, collapse runs of `-`, and strip leading/trailing `-`. That
       folds `/`, `?`, `&`, `=`, `:` and whitespace — every character a
       route may legally contain and a filename may not — into one safe
       separator, so `/users/42?tab=activity` becomes
       `users-42-tab-activity`.
    2. `/` (and anything that reduces to nothing) becomes `root`, since
       `.png` alone is not a usable filename.
    3. Truncate to `_MAX_SLUG_LENGTH` characters, keeping the *front* of
       the route: the leading path segments are what identify a route, and
       255 bytes is the filename ceiling on ext4/APFS.
    4. If that slug is already taken by an earlier route in the same Run,
       append `-2`, `-3`, ... . Steps 1-3 are lossy — `/a/b` and `/a-b`
       both slugify to `a-b` — and Property 15 requires each configured
       route to produce its own screenshot, so a collision must become a
       second file rather than silently overwriting the first.

Two behaviors of the route list itself, stated because they are exactly
what Property 15 ("Every configured route is screenshotted exactly once
on success") pins down:

- A route listed twice in `.vaultshield.yml` is one route. Duplicates are
  dropped, keeping first-occurrence order, so the returned list never
  contains the same path twice and no capture overwrites another.
- `config.routes` defaulting to `[]` (which is what `config.py` writes as
  a placeholder at `init` time) is not an error: zero routes means zero
  screenshots, an empty returned list, and no `screenshots/` directory
  created at all. The directory is created only when there is something
  to put in it, matching `run_directory.py`'s convention that the writer
  owns the `mkdir`.

Failure is all-or-nothing per Run, never silently partial: if any route
cannot be captured, `ScreenshotError` is raised naming that route.
design.md requires this to be a *distinct* exception from
`ReadinessTimeout` so a caller can tell "the app never came up" from "the
app came up but a screenshot failed". PNGs already written are left on
disk rather than deleted, and the exception carries them as `.saved`, so
Task 11.5 can report or upload the partial evidence instead of having to
rediscover it.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, ContextManager, Iterable, Iterator

import httpx

from vaultshield.config import ProjectConfig
from vaultshield.run_directory import screenshots_dir
from vaultshield.sandbox import Sandbox, SandboxError, container_path_under_repo

# The only status code that means "ready" (Requirements 9.2, 9.4 and
# design.md both say "a 200 response", not "any 2xx"). A 204 or a 301 from
# a `ready_path` means the resolved path is not the health endpoint the
# developer thinks it is, which is worth surfacing as a timeout rather than
# papering over.
READY_STATUS = 200

# design.md's own default for `start_and_capture(..., ready_timeout_s=60)`,
# named here so `cli.py` (Task 11.5) can expose it as a `--ready-timeout`
# default without restating the number.
DEFAULT_READY_TIMEOUT_S = 60

# Delay between readiness probes. One second is short enough that a fast
# application (a Flask dev server binds in well under a second) is detected
# almost immediately, and long enough that a 60-second timeout costs ~61
# requests rather than thousands of tight-loop connection attempts.
_POLL_INTERVAL_S = 1.0

# Per-probe HTTP timeout, bounding a single request so one unresponsive
# socket cannot swallow the entire readiness budget in a single hang. A
# probe that exceeds it raises `httpx.RequestError`, which the loop already
# treats as "not ready yet" and retries — so this is a cap on one attempt,
# never on the poll as a whole (`ready_timeout_s` is that).
_PROBE_REQUEST_TIMEOUT_S = 5.0

# Host address used with a published port. `127.0.0.1` rather than
# `localhost` to avoid a DNS/`/etc/hosts` lookup per probe and to pin the
# probe to IPv4 — Docker publishes on `0.0.0.0` by default, and resolving
# `localhost` to `::1` first is a known source of connection-refused
# confusion on macOS.
_HOST_LOOPBACK = "127.0.0.1"

# Environment variables injected into the started application's process, so
# a framework that reads them binds to the port/interface this module then
# polls. See the module docstring's "Starting the application" section.
_PORT_ENV_VAR = "PORT"
_HOST_ENV_VARS = ("HOST", "HOSTNAME")
_BIND_ALL_INTERFACES = "0.0.0.0"

# Screenshot filename parts. The extension is PNG because design.md fixes
# it (`<run_dir>/screenshots/<route-slug>.png`), and it is the lossless
# format Playwright produces by default — a review screenshot should not
# have JPEG artifacts smeared across the text of a diffed UI.
_SCREENSHOT_SUFFIX = ".png"

# Slug for a route that reduces to nothing — `/` above all, which is the
# route almost every project configures first. `<run_dir>/screenshots/.png`
# would be a hidden, extension-only file on POSIX.
_ROOT_SLUG = "root"

# Every character that is not an ASCII lowercase letter or digit becomes the
# separator, and runs of separators collapse. See the module docstring's
# route -> filename rules for why the safe set is this small.
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_SLUG_SEPARATOR = "-"

# Maximum slug length before the `.png` suffix and any `-2`/`-3` collision
# suffix. Filenames are capped at 255 bytes on ext4 and APFS, so this
# leaves ample room for both while still keeping enough of a route to
# recognise it.
_MAX_SLUG_LENGTH = 80

# Browser viewport for every capture. Fixed, and fixed here rather than
# read from config, so two Runs of the same Project produce comparable
# screenshots: a reviewer comparing a Run against its predecessor should be
# looking at UI changes, not at a different window size. 1280x800 is the
# conventional desktop review size and matches Playwright's own default
# aspect ratio closely enough to avoid surprising layout breakpoints.
_VIEWPORT_WIDTH = 1280
_VIEWPORT_HEIGHT = 800

# Per-route navigation budget. Bounds one `goto` so a single hanging route
# cannot stall a Run indefinitely; a route that exceeds it raises, which
# `capture_route_screenshots` reports as a `ScreenshotError` naming that
# route. Generous relative to a local dev server (which answers in
# milliseconds) because a first request can trigger a dev-server bundle
# build that legitimately takes seconds.
_NAVIGATION_TIMEOUT_MS = 30_000

# Extra settle time after a route's `load` event, before the shutter. A
# client-rendered app has often only just mounted when `load` fires, and
# `networkidle` is not a usable alternative here — Playwright's own docs
# discourage it, and a dev server with a websocket/HMR channel open never
# reaches it at all. A fixed short wait is the honest trade: it costs half
# a second per route and captures rendered UI instead of an empty root
# element.
_RENDER_SETTLE_MS = 500

# One readiness probe: given a URL, return the HTTP status code observed,
# or `None` if the request could not complete at all (connection refused,
# read timeout, DNS failure). `None` and a non-200 status are handled
# identically by the loop — both are "not ready yet" — but they stay
# distinguishable so a caller/test can tell "the server said no" from "no
# server answered".
Probe = Callable[[str], int | None]

# One capture: render `url` in a browser and write a PNG to `path`. This is
# the whole of what `capture_route_screenshots` needs from Playwright, so it
# is the whole of what a test has to stand in for.
CaptureFn = Callable[[str, Path], None]

# A browser session: a context manager that launches a browser, yields a
# `CaptureFn` bound to it, and shuts it down on exit. Injectable for the
# same reason `Probe` is (see `wait_until_ready`): Property 15 (Task 11.4)
# is a statement about slugging, ordering, and one-file-per-route, none of
# which needs a real browser to be verified — while everything else in
# `capture_route_screenshots` (the paths, the `mkdir`, the dedupe, the
# collision suffixes, the error wrapping) stays real code under test.
BrowserSession = Callable[[], ContextManager[CaptureFn]]


class ReadinessTimeout(Exception):
    """Raised by `start_and_capture` when the readiness timeout elapsed
    without a 200 response from the resolved `ready_path` (Requirement 9.4).

    This exception IS the "report a startup timeout failure" half of
    Requirement 9.4, and raising it is what guarantees the other half: it
    propagates from `wait_until_ready` *before* `start_and_capture` reaches
    the screenshot step at all, so zero screenshots exist for that Run
    rather than a partial set. design.md is explicit that this is a
    distinct exception from a Playwright screenshot failure — that is
    `ScreenshotError` below — precisely so a caller can tell "the app never
    came up" from "the app came up but a screenshot failed".

    `url` is the fully-resolved URL that was polled and `timeout_s` the
    budget that elapsed; both are kept as attributes, not just interpolated
    into the message, matching `OffLimitsViolation`'s convention in
    `monitor.py` so a caller can act on them without re-parsing prose.
    """

    def __init__(self, url: str, timeout_s: float) -> None:
        super().__init__(
            f"Application readiness timed out after {timeout_s}s: "
            f"no {READY_STATUS} response from {url}"
        )
        self.url = url
        self.timeout_s = timeout_s


class ScreenshotError(Exception):
    """Raised when a configured route could not be screenshotted, or when
    no browser could be launched to screenshot with (Requirement 9.3).

    Deliberately NOT a subclass of `ReadinessTimeout` nor of a shared base:
    design.md requires the two to be distinguishable ("`ReadinessTimeout`
    is a distinct exception from a Playwright screenshot failure"), because
    they mean opposite things about the Run. A `ReadinessTimeout` says the
    agent's application never came up; a `ScreenshotError` says it came up
    and answered a 200, and only the visual capture failed — so the Run's
    Event_Log and diff are still worth reviewing.

    `saved` carries the screenshots that *were* written before the failure,
    in capture order, matching `ReadinessTimeout`'s convention of exposing
    its context as attributes rather than only in prose. Those files are
    left on disk: partial visual evidence is more useful to a reviewer than
    none, and nothing in this module is a way to lose an artifact a Run
    already produced.
    """

    def __init__(self, message: str, *, saved: list[Path] | None = None) -> None:
        super().__init__(message)
        self.saved: list[Path] = list(saved or ())


def start_and_capture(
    sandbox: Sandbox,
    config: ProjectConfig,
    run_dir: Path,
    ready_timeout_s: int = DEFAULT_READY_TIMEOUT_S,
    *,
    poll_interval_s: float = _POLL_INTERVAL_S,
    browser_session: BrowserSession | None = None,
    published_by=None,
) -> list[Path]:
    """Starts `config.start_command` inside `sandbox`, bound to
    `config.port`, polls `config.ready_path` until a 200 response or
    `ready_timeout_s` elapses, then screenshots each route in
    `config.routes` into `<run_dir>/screenshots/` and returns the saved
    paths in capture order.

    This is design.md's full `start_and_capture` contract: Requirement 9.1
    (start the application), 9.2 (poll until 200 or timeout), 9.3 (one
    Playwright screenshot per configured route, saved into the
    Run_Directory), and 9.4 (a startup timeout is the `ReadinessTimeout`
    raised out of this call — before `run_dir` is touched at all, so a
    timed-out Run has zero screenshots rather than a partial set).

    `poll_interval_s` and `browser_session` are keyword-only additions to
    design.md's reference signature, which stays valid for every positional
    caller; see `wait_until_ready` and `capture_route_screenshots` for what
    each one feeds. `published_by` (Batch 3 D4) is passed to
    `resolve_base_url`: with egress enforced, the egress proxy, not the
    Sandbox, publishes the app port.

    Raises `ReadinessTimeout` if the application never answers with a 200
    in time, `ScreenshotError` if it did but a route could not be captured,
    or `SandboxError` if the application could not be started at all or the
    Sandbox's address could not be resolved.
    """
    # Address first, application second: Docker assigns the published host
    # port when the container starts, so this is answerable before the
    # application exists, and an unreachable Sandbox is worth failing on
    # before a dev server has been started that nothing could ever poll.
    if not config.routes:
        # Nothing to capture, so nothing to start. `capture_route_screenshots`
        # already returned `[]` for empty routes, but it did so three steps
        # later — after the application had been started and after readiness had
        # been polled to a 200 or to a `ReadinessTimeout`. That made a Run with
        # `routes: []` depend on the application coming up in order to capture
        # zero screenshots, and a project whose `start_command` was a
        # placeholder failed a Run whose agent work had already succeeded.
        #
        # Returning here keeps the observable no-routes result identical (an
        # empty list, no `screenshots/` directory) while removing the readiness
        # requirement that had no purpose.
        return []

    base_url = resolve_base_url(sandbox, config.port, published_by=published_by)
    _start_app(sandbox, config)
    wait_until_ready(
        base_url,
        config.ready_path,
        ready_timeout_s,
        poll_interval_s=poll_interval_s,
    )
    return capture_route_screenshots(
        base_url, config.routes, run_dir, browser_session=browser_session
    )


def wait_until_ready(
    base_url: str,
    ready_path: str,
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    *,
    poll_interval_s: float = _POLL_INTERVAL_S,
    probe: Probe | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Polls `<base_url>/<ready_path>` until it answers `200`, and returns
    the polled URL (Requirement 9.2). Raises `ReadinessTimeout` naming that
    URL if `ready_timeout_s` elapses first (Requirement 9.4).

    The loop's exact termination behavior — first probe immediately, stop on
    the first 200, retry every non-200 and every transport failure, raise as
    soon as the deadline is reached, never probe past it — is spelled out
    step by step in the module docstring's "Polling termination" section,
    which is what Property 14 (Task 11.3) asserts against.

    `probe`, `monotonic`, and `sleep` are injectable for exactly that
    property test: the default probe issues a real `httpx` GET, and the
    defaults for the other two are the real clock and the real sleep, so
    ordinary callers pass none of them. A test supplies a scripted probe
    plus a fake clock to drive any status sequence and any elapsed time
    deterministically, with no Docker daemon and no real waiting.
    """
    url = _build_url(base_url, ready_path)
    request = probe if probe is not None else _http_probe
    deadline = monotonic() + ready_timeout_s

    while True:
        if request(url) == READY_STATUS:
            return url

        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ReadinessTimeout(url, ready_timeout_s)

        sleep(min(poll_interval_s, remaining))


def resolve_base_url(sandbox: Sandbox, port: int, *, published_by=None) -> str:
    """Returns the `http://host:port` origin at which `sandbox`'s
    application is reachable *from the host*.

    Prefers the host port Docker published for `<port>/tcp` (i.e. the
    Sandbox was created with `publish_port=port`), and falls back to the
    container's own IP on its Docker network. See the module docstring's
    "Reaching the application from the host" section for why the fallback
    is Linux-only in practice and why this is still worth having.

    `published_by` (keyword-only, Batch 3 D4) is the container that publishes
    the port instead of the Sandbox: the egress proxy, which forwards it to an
    internal-only Sandbox. When given, its published port is read and the
    container-IP fallback is skipped, because an internal-only Sandbox's IP
    is never host-reachable.

    Raises `SandboxError` if `sandbox` has no container (never created, or
    already torn down), if Docker cannot be asked about it, or if neither a
    published port nor a container IP could be found.
    """
    container = sandbox.container if published_by is None else published_by
    if container is None:
        raise SandboxError(
            f"Cannot resolve the application address for run {sandbox.run_id}: "
            "no container has been created (call create() first, or it "
            "failed/was already torn down)"
        )

    try:
        # The port bindings and the container IP are both assigned when the
        # container starts, so a `create()`-time snapshot of `attrs` may
        # predate them; `reload()` refetches the live state.
        container.reload()
        network = container.attrs.get("NetworkSettings") or {}
    except Exception as exc:
        raise SandboxError(
            f"Cannot resolve the application address for run {sandbox.run_id}: "
            f"{exc}"
        ) from exc

    host_port = _published_host_port(network, port)
    if host_port is not None:
        return f"http://{_HOST_LOOPBACK}:{host_port}"

    if published_by is not None:
        raise SandboxError(
            f"Cannot resolve the application address for run {sandbox.run_id}: "
            f"the egress proxy does not publish port {port}/tcp"
        )

    container_ip = _container_ip(network)
    if container_ip:
        return f"http://{container_ip}:{port}"

    raise SandboxError(
        f"Cannot resolve the application address for run {sandbox.run_id}: "
        f"container port {port}/tcp is not published to the host and the "
        "container has no reachable IP address. Create the Sandbox with "
        "`publish_port=<port>` so the application can be reached."
    )


def _start_app(sandbox: Sandbox, config: ProjectConfig) -> None:
    """Starts `config.start_command` inside `sandbox`, detached, with the
    port/host environment variables set (Requirement 9.1).

    Run through `sh -c` so a shell-shaped `start_command` works verbatim,
    matching `sandbox.py`'s own dependency-install step. Returns as soon as
    the process is started: readiness is established by polling, not here.
    """
    env = {_PORT_ENV_VAR: str(config.port)}
    for host_var in _HOST_ENV_VARS:
        env[host_var] = _BIND_ALL_INTERFACES

    sandbox.exec_detached(
        ["sh", "-c", config.start_command],
        env=env,
        # The application root, which is the clone root unless `project_dir`
        # names a subdirectory. `npm run dev` has to run where `package.json`
        # is.
        workdir=container_path_under_repo(config.project_dir),
    )


def capture_route_screenshots(
    base_url: str,
    routes: Iterable[str],
    run_dir: Path,
    *,
    browser_session: BrowserSession | None = None,
) -> list[Path]:
    """Screenshots each route in `routes` at `base_url` with Playwright,
    saving one PNG per route into `<run_dir>/screenshots/`, and returns the
    saved paths in capture order (Requirement 9.3).

    Each route is fetched at `_build_url(base_url, route)` and saved as
    `<route-slug>.png`; the slug rules, the duplicate-route and
    empty-`routes` behavior, and why a failure is all-or-nothing are all in
    the module docstring's "Screenshotting the routes" section, which is
    what Property 15 (Task 11.4) asserts against.

    Called by `start_and_capture` only after readiness succeeded, so it
    never runs for a timed-out Run (Requirement 9.4). Safe to call
    directly too — it makes no assumption that a Sandbox is involved, only
    that something is serving `base_url`.

    `browser_session` defaults to a real headless Chromium via
    `playwright.sync_api`; a test injects its own to exercise the path
    logic without a browser binary.

    Raises `ScreenshotError` if no browser could be launched or if any
    route could not be captured, carrying whatever was saved first.
    """
    targets = _screenshot_targets(base_url, routes, screenshots_dir(run_dir))
    if not targets:
        # Nothing configured to capture: no browser launch, and no empty
        # `screenshots/` directory left behind. See the module docstring.
        return []

    session = browser_session if browser_session is not None else _playwright_session
    saved: list[Path] = []
    with session() as capture:
        # Created here — after the browser is up, and only once there is
        # something to write into it — per `run_directory.py`'s convention
        # that the writer owns the `mkdir`. A Run that could not launch a
        # browser at all therefore leaves no empty `screenshots/` behind.
        screenshots_dir(run_dir).mkdir(parents=True, exist_ok=True)

        for url, path in targets:
            try:
                capture(url, path)
            except Exception as exc:
                # Every Playwright failure mode (navigation timeout, a
                # crashed page, an unwritable path) lands here as itself;
                # naming the route is what makes the message actionable,
                # and `saved` keeps the partial evidence addressable.
                raise ScreenshotError(
                    f"Failed to capture a screenshot of {url}: {exc}", saved=saved
                ) from exc
            saved.append(path)

    return saved


def route_slug(route: str) -> str:
    """Turns one configured route into the filename stem of its screenshot:
    lowercased, with every unsafe character folded to `-`, runs collapsed,
    ends trimmed, truncated to `_MAX_SLUG_LENGTH`, and `root` for anything
    that reduces to nothing.

    Pure and total: every string in, one non-empty filesystem-safe stem
    out. Lossy by design (`/a/b` and `/a-b` agree), which is why
    `_screenshot_targets` de-collides the results rather than trusting them
    to be distinct — see the module docstring's route -> filename rules.
    """
    slug = _SLUG_UNSAFE.sub(_SLUG_SEPARATOR, route.lower()).strip(_SLUG_SEPARATOR)
    if not slug:
        return _ROOT_SLUG
    # Truncation can re-expose a trailing separator (`/a/bbb...` cut mid-run),
    # so trim again rather than emitting `some-route-.png`.
    return slug[:_MAX_SLUG_LENGTH].strip(_SLUG_SEPARATOR) or _ROOT_SLUG


def _screenshot_targets(
    base_url: str, routes: Iterable[str], target_dir: Path
) -> list[tuple[str, Path]]:
    """Resolves `routes` into the exact `(url, png_path)` pairs to capture,
    in first-occurrence order, with duplicate routes dropped and colliding
    slugs given `-2`/`-3` suffixes.

    Pure: computes paths, creates nothing. Separated from the capture loop
    because "which file does each route become?" is the whole substance of
    Property 15 and is worth being able to assert on its own, without a
    browser or a filesystem.
    """
    targets: list[tuple[str, Path]] = []
    seen_routes: set[str] = set()
    used_slugs: set[str] = set()

    for route in routes:
        if route in seen_routes:
            continue
        seen_routes.add(route)

        slug = _unique_slug(route_slug(route), used_slugs)
        used_slugs.add(slug)
        targets.append(
            (_build_url(base_url, route), target_dir / f"{slug}{_SCREENSHOT_SUFFIX}")
        )

    return targets


def _unique_slug(slug: str, used: set[str]) -> str:
    """Returns `slug` if unused, else the first `<slug>-<n>` (n >= 2) that
    is. Guarantees one distinct file per distinct route even though
    `route_slug` is lossy.
    """
    if slug not in used:
        return slug

    suffix = 2
    while f"{slug}{_SLUG_SEPARATOR}{suffix}" in used:
        suffix += 1
    return f"{slug}{_SLUG_SEPARATOR}{suffix}"


@contextmanager
def _playwright_session() -> Iterator[CaptureFn]:
    """Launches one headless Chromium with a fixed viewport, yields a
    `capture(url, path)` bound to a single reused page, and shuts the
    browser down on exit — including when the caller raises, so a failed
    Run never leaks a browser process.

    `playwright` is imported here rather than at module scope, and both a
    missing package and a missing browser binary are reported as a
    `ScreenshotError` naming `playwright install chromium`; see the module
    docstring for why.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ScreenshotError(
            "Playwright is not installed, so no screenshots can be captured. "
            "Install it with `pip install playwright` followed by "
            "`playwright install chromium`."
        ) from exc

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment-dependent
            # Overwhelmingly the missing-browser-binary case: the pip
            # package installs no browser, and `playwright install
            # chromium` is the step that does.
            raise ScreenshotError(
                f"Could not launch a headless browser for screenshots: {exc}. "
                "Run `playwright install chromium` once on this machine."
            ) from exc

        try:
            page = browser.new_page(
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT}
            )
            page.set_default_navigation_timeout(_NAVIGATION_TIMEOUT_MS)
            yield lambda url, path: _capture_page(page, url, path)
        finally:
            browser.close()


def _capture_page(page: object, url: str, path: Path) -> None:
    """Navigates `page` to `url`, lets it settle, and writes a full-page
    PNG to `path`.

    `page` is typed loosely on purpose: the only Playwright type this
    module would otherwise name is imported lazily inside
    `_playwright_session`, and annotating it here would drag the import
    back to module scope.

    `wait_until="load"` plus a short fixed settle beats `networkidle` for a
    dev server holding an HMR websocket open; `full_page=True` captures
    below the fold, since a reviewer judging a UI change should not be
    limited to the viewport's first 800 pixels.
    """
    page.goto(url, wait_until="load")  # type: ignore[attr-defined]
    page.wait_for_timeout(_RENDER_SETTLE_MS)  # type: ignore[attr-defined]
    page.screenshot(path=str(path), full_page=True)  # type: ignore[attr-defined]


def _http_probe(url: str) -> int | None:
    """Issues one readiness GET against `url`, returning its status code, or
    `None` if the request could not complete (connection refused, read
    timeout, DNS failure — every `httpx.RequestError`).

    Redirects are deliberately NOT followed: only a direct 200 from the
    resolved `ready_path` counts as ready (see `READY_STATUS`), so
    following a redirect would report the status of some other URL.
    """
    try:
        response = httpx.get(
            url, timeout=_PROBE_REQUEST_TIMEOUT_S, follow_redirects=False
        )
    except httpx.RequestError:
        return None
    return response.status_code


def _build_url(base_url: str, path: str) -> str:
    """Joins an origin and a path into one URL, tolerating a trailing slash
    on the origin and a missing/extra leading slash on the path.

    `urllib.parse.urljoin` is deliberately not used: it treats a
    slash-less path as relative to the origin's last segment, which would
    silently mangle a `ready_path` written as `healthz` instead of
    `/healthz`. A `.vaultshield.yml` may hold either spelling.
    """
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _published_host_port(network: dict, port: int) -> int | None:
    """Extracts the host port Docker published for `<port>/tcp` from a
    container's `NetworkSettings`, or `None` if that port is not published.

    Docker reports the binding as a list (one entry per host interface, e.g.
    an IPv4 and an IPv6 one); all entries of one container port share the
    same host port, so the first entry carrying a `HostPort` wins.
    """
    bindings = network.get("Ports") or {}
    for binding in bindings.get(f"{port}/tcp") or ():
        host_port = (binding or {}).get("HostPort")
        if host_port:
            try:
                return int(host_port)
            except (TypeError, ValueError):
                return None
    return None


def _container_ip(network: dict) -> str | None:
    """Extracts the container's own IP address from its `NetworkSettings`.

    `NetworkSettings.IPAddress` is populated for the default bridge
    network; a container attached to a user-defined network leaves it empty
    and reports per-network addresses under `NetworkSettings.Networks`
    instead, so both are consulted.
    """
    address = network.get("IPAddress")
    if address:
        return str(address)

    for entry in (network.get("Networks") or {}).values():
        address = (entry or {}).get("IPAddress")
        if address:
            return str(address)
    return None
