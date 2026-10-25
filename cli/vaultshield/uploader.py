"""Uploader: `vaultshield login` and the Run-artifact upload.

THIS FILE implements Tasks 18.1 and 18.2 — `login()` with the
`~/.vaultshield/token` path/read/write helpers it needs, and
design.md's `upload_run()`, which posts a finished Run's Event_Log, diff,
and screenshots to the backend. Wiring `upload_run` into the `run`
pipeline (Task 18.5) is a separate task; nothing here reaches into
`cli.py`.

Covers Requirement 14.1: "WHEN a developer runs `vaultshield login`, THE
VaultShield_CLI SHALL obtain and store a dashboard authentication token
separate from any git or repository-host credential."
And Requirement 14.2: "WHEN a local Run completes, THE Uploader SHALL
package the Run's Event_Log, diff, and screenshots and SHALL submit them
to the VaultShield_Backend using the stored dashboard authentication
token."
And Requirement 14.3: "WHEN the Uploader finishes submitting a Run's
artifacts, THE VaultShield_Backend SHALL make the complete Run retrievable
through `GET /api/runs/:id/`" — the client half of which is submitting
every artifact to the endpoint that stores it, since
`RunDetailSerializer` nests each stored row automatically.
And Requirement 14.4: "IF the Uploader cannot reach the
VaultShield_Backend, THEN THE VaultShield_CLI SHALL report the upload
failure and SHALL preserve the Run_Directory contents on local disk."
And the client half of Requirement 27.1: "THE VaultShield_Backend SHALL
expose a `POST /api/auth/login/` endpoint that accepts a User's credential
and, on success, returns a Dashboard_Auth_Token" — this module is what
posts that credential and keeps the token that comes back.

`httpx` is the HTTP client, per design.md's tech stack ("`httpx` for the
Uploader/Submit HTTP calls"), the same client `browser_capture.py` already
polls readiness with.

### The credential, and where it comes from

design.md fixes the signature as `login(backend_url: str) -> None`, so the
username and password are not parameters of the flow — they are collected
from the developer at the terminal (`click.prompt`, with the password
hidden and never echoed). `username`/`password` are accepted as
keyword-only arguments so a caller that already has them (the `login`
command's `--username` option, a test) can pass them and skip the
corresponding prompt; omitting both keeps design.md's one-argument call
exactly as written.

A password is deliberately NOT accepted as a CLI option anywhere: an
option value lands in shell history and in the process list of every other
user on the machine. Prompting is the only way in.

`POST <backend_url>/api/auth/login/` is sent as JSON
(`{"username": ..., "password": ...}`) and the token is read from the
response's `token` key — the exact request and response shape
`accounts.views.LoginView` (Task 15.1) inherits from DRF's
`ObtainAuthToken`, and the path `vaultshield_backend/urls.py` routes.

### Separation from git/host credentials (Requirement 14.1)

The token is stored in a file of its own, `~/.vaultshield/token`, holding
nothing but the token. design.md's `resolve_git_credential` (Task 29.1)
reads a Repository_Host_Provider token from a *different* file in the same
directory, `~/.vaultshield/credentials.yml`. Nothing in this module reads,
writes, parses, or even names that file, so the two credentials cannot be
confused for one another or leak into each other's storage: "separate
from any git or repository-host credential" is true by construction, not
by a check. Task 18.4's test asserts exactly that.

The two files share a parent directory because that directory is simply
where VaultShield keeps per-developer state; a shared *directory* is not
shared *storage*, and both files are owner-only.

### Permissions, and why the file is opened rather than written

Requirement 14.1 asks for storage; the task asks for `chmod 600`; the
token is a bearer credential for the whole dashboard API, so the mode is
load-bearing rather than cosmetic. `store_token` therefore does NOT write
the file and then chmod it: between those two steps the token would exist
on disk readable by every user on the machine. Instead it `os.open`s the
path with mode `0o600` and `os.fchmod`s the descriptor before writing a
byte — the descriptor form so the mode lands on the file this call
created, and unconditionally because `os.open`'s mode argument applies
only when the file did not already exist (a token file left at `0644` by
an earlier tool, or by a permissive umask, is tightened rather than
trusted). The containing directory is created `0o700` for the same reason.

`os.fchmod` is POSIX-only, which costs this module nothing: `monitor.py`
imports `pty` at module scope, so the CLI as a whole already runs only
where POSIX permissions exist. A `mode`-less fallback is deliberately not
added — a token stored without `0600` would satisfy no requirement here.

Writing happens ONLY after a token has been successfully obtained, so a
failed login never truncates a working token and never leaves an empty or
half-written file behind. Every failure — an unreachable backend, a
rejected credential (Requirement 27.2), a response with no token in it, a
filesystem error — raises `UploadError` with the offending endpoint or
path named, and no message ever includes the password or the token itself.

### Uploading a Run (`upload_run`, Requirements 14.2-14.4)

Three artifacts, three endpoints, in design.md's own order:

    <run_dir>/events.jsonl        -> POST /api/runs/<run_id>/events/
    <run_dir>/diff.json           -> POST /api/runs/<run_id>/diff/
    <run_dir>/screenshots/*.png   -> POST /api/runs/<run_id>/screenshots/

Every request carries `Authorization: Token <token>` — the header DRF's
`TokenAuthentication` reads, which `settings.REST_FRAMEWORK` makes the
only way to authenticate against `/api/` (Requirement 27.5). All three
requests go through one `httpx.Client`, so a Run's whole upload reuses a
single connection and the header is set once instead of per call.

Each artifact is posted in the shape the component that wrote it already
produced, with no translation in between:

- **Events.** `events.jsonl` is one JSON object per line
  (`event_type`/`timestamp`/`payload`, written by `monitor.py`), and the
  events endpoint accepts a bare JSON array of exactly those objects, so
  a batch is literally a list of parsed lines — minus any whose payload is
  blank, which the backend would reject (`_without_blank_payloads`).
- **Diff.** `diff.json` is `{"files": [...]}` (written by
  `git_capture.py`), which is verbatim what the diff endpoint accepts —
  `RunDiff.files` is the same JSON value.
- **Screenshots.** One multipart POST per PNG (`route` + `image`), which
  is what the endpoint accepts: one Screenshot per request.

### Event order (Requirement 13.1's client half)

Order is carried by *position*, never by a field the CLI sends:
`RunEvent.sequence` is read-only on the ingest serializer and is assigned
by the backend from each event's index in the submitted array, continuing
past the highest sequence already stored for that Run. So preserving the
Event_Log's order is exactly two things here, and nothing else:

1. Each batch lists its events in `events.jsonl` line order.
2. Batches are posted one at a time, in order, each awaiting its
   response — so batch N is stored (and numbered) before batch N+1 is
   sent.

Sending a `sequence` field would be silently ignored, so none is
constructed; the parsed lines go out as they were read. Concurrency is
what would actually break the ordering, which is why the batches are not
posted in parallel even though that would be faster.

`_EVENT_BATCH_SIZE` bounds one request rather than the whole log: a long
Run's Event_Log is thousands of terminal-output chunks, and a single POST
of all of them would exceed Django's `DATA_UPLOAD_MAX_MEMORY_SIZE`
(2.5 MB by default, not raised in `settings.py`) and be rejected outright.
See the constant for how the number is derived.

### The Run_Directory is read-only here (Requirement 14.4 / Property 24)

Nothing in the upload path opens an artifact for anything but reading, and
nothing creates, moves, truncates, or deletes a path under `run_dir`.
That is the whole implementation of Requirement 14.4's "SHALL preserve the
Run_Directory contents on local disk" and of design.md's error-handling
rule that "`UploadError` never deletes or mutates `Run_Directory` — a
failed upload is always retryable from the same local artifacts": there is
no cleanup step to skip on failure, because there is no cleanup step at
all. Reporting the failure is `cli.py`'s half (it maps `UploadError` to a
`click.ClickException`, as `login` already does).

A missing artifact is not a failure. A Run whose git capture failed has no
`diff.json`, a Run with no configured routes has no `screenshots/`, and a
Run that produced no output has an empty (or absent) `events.jsonl`; in
each case the stage is skipped and the rest of the Run still uploads,
because a partially-captured Run is still worth reviewing. An artifact
that exists but cannot be read or parsed IS a failure — a truncated
`diff.json` would otherwise be silently dropped from the review.

What a *retry* looks like, stated plainly because a failed upload is
expected to be retried from these same artifacts: diff ingest is an
upsert, so re-posting is a no-op; event and screenshot ingest are not
idempotent, so a batch whose response was lost after it committed will
appear twice. That is the backend's documented trade (events have no
natural key), not something this module can fix by holding state — and it
is why `upload_run` keeps no local record of progress: a partial-progress
file inside `run_dir` would be a mutation of the artifacts Requirement
14.4 exists to protect.

### Registering and closing a Run (`create_run`, `complete_run`, `mark_run_failed`)

The artifact endpoints are nested under a Run the backend must already
know, so `run` registers it first (`create_run`, `POST /api/runs/`, before
any Sandbox exists) and closes it last: `complete_run` PATCHes
`awaiting_review` after `upload_run` returns, and on any failure after
registration `mark_run_failed` PATCHes `failed`. The backend accepts both
PATCHes only from `running` (409 otherwise). `create_run` and
`complete_run` raise `UploadError` like everything else here;
`mark_run_failed` never raises and returns a warning instead, because it
runs while another error is already on its way to the developer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import click
import httpx

from vaultshield.run_directory import (
    RunDirectoryError,
    diff_path,
    event_log_path,
    screenshots_dir,
    validate_run_id,
)

# The login endpoint, relative to `backend_url`. Trailing slash included:
# `vaultshield_backend/urls.py` routes `api/auth/login/` and Django's
# `APPEND_SLASH` would otherwise answer the slash-less spelling with a
# redirect that a POST cannot follow without losing its body.
LOGIN_PATH = "/api/auth/login/"

# Response key carrying the Dashboard_Auth_Token, per Requirement 27.1's
# `{"token": "<Dashboard_Auth_Token>"}` and DRF's `ObtainAuthToken`.
TOKEN_RESPONSE_KEY = "token"

# Request field names for the credential, matching DRF's
# `AuthTokenSerializer`, which is what `LoginView` validates with.
USERNAME_FIELD = "username"
PASSWORD_FIELD = "password"

# Per-developer VaultShield state directory, under the developer's home:
# `~/.vaultshield/`. Deliberately NOT imported from `run_directory.py`,
# which defines the same string for a different place — the *repository*
# local `<repo_root>/.vaultshield/runs/` artifact tree. One is per-machine
# credential state that outlives every Run; the other is per-repository
# artifacts. Sharing a constant would tie two paths together that are only
# coincidentally spelled alike.
VAULTSHIELD_HOME_DIRNAME = ".vaultshield"

# The token file itself: `~/.vaultshield/token`, per design.md. Its whole
# content is the token, so there is no format to parse and nothing else
# stored alongside it (see the module docstring on separation).
TOKEN_FILENAME = "token"

# Owner-only permissions for the token file and its parent directory.
# `0o600`: the task's `chmod 600`, and the only sane mode for a bearer
# credential. `0o700` on the directory so another user cannot list, add
# to, or replace files in it either.
TOKEN_FILE_MODE = 0o600
TOKEN_DIR_MODE = 0o700

# Timeout for the single login request. Generous compared with
# `browser_capture.py`'s 5-second readiness probe because this is one
# interactive, developer-initiated call against a real backend (which may
# be hashing a password behind a cold connection), not one of dozens of
# poll attempts — but still bounded, so a hung socket fails with a message
# instead of hanging the terminal indefinitely.
_LOGIN_REQUEST_TIMEOUT_S = 30.0

# Status codes that mean "the backend understood the request and refused
# the credential" (Requirement 27.2 — DRF's `AuthTokenSerializer` fails
# validation with a 400; a 401 is included because a proxy or a future
# authenticator may answer that way instead). Reported as a credential
# problem rather than as a generic HTTP failure, since that is the one
# failure the developer can actually fix by retyping.
_CREDENTIAL_REJECTED_STATUSES = (400, 401)

# Cap on how much of an error response body is quoted back in an
# `UploadError` message. DRF's error detail is short and genuinely
# actionable ("Unable to log in with provided credentials."), but an HTML
# error page from a misconfigured proxy is neither, and pasting one into a
# terminal buries the message.
_MAX_ERROR_BODY_CHARS = 300

# The three artifact-ingest endpoints, relative to `backend_url`, with the
# `run_id` left to `str.format`. Exactly design.md's REST API surface
# (`POST /api/runs/:id/events/`, `.../diff/`, `.../screenshots/`), and
# `:id` is the CLI's own `run_id` because `RunViewSet.lookup_field` is
# `run_id`, not the database pk — so nothing here has to learn a second
# identifier for a Run it already named. Trailing slashes for the same
# reason as `LOGIN_PATH`: a POST cannot follow `APPEND_SLASH`'s redirect
# without losing its body.
EVENTS_PATH_TEMPLATE = "/api/runs/{run_id}/events/"
DIFF_PATH_TEMPLATE = "/api/runs/{run_id}/diff/"
SCREENSHOTS_PATH_TEMPLATE = "/api/runs/{run_id}/screenshots/"

# The Run collection and detail routes `create_run`, `complete_run`, and
# `mark_run_failed` address. Trailing slashes for the same reason as above.
RUNS_PATH = "/api/runs/"
# Same path as submit.RUN_DETAIL_PATH_TEMPLATE (submit imports this module, so it cannot be shared the other way).
RUN_DETAIL_PATH_TEMPLATE = "/api/runs/{run_id}/"

# Timeout for registering and completing a Run: one small JSON request each,
# so far shorter than an artifact upload, but long enough for a cold backend.
_RUN_CONTROL_TIMEOUT_S = 15.0

# Timeout for the best-effort failed mark. It runs on the way out of a Run that
# has already failed, so it must not hold that exit up for long. httpx applies
# a timeout per phase (connect, write, read, pool), not in total: this bounds
# the connect at 2 s and every other phase at 5 s, and the short connect is
# what keeps the common "backend is gone" case quick.
_MARK_FAILED_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

# The two Run_Status values the CLI writes itself, through PATCH. The backend
# accepts either only from `running` and answers 409 otherwise.
FAILED_STATUS = "failed"
AWAITING_REVIEW_STATUS = "awaiting_review"

# Statuses that mean the dashboard auth token itself was refused, so the fix
# is to log in again rather than anything about the Run.
_TOKEN_REJECTED_STATUSES = (401, 403)

# The `Authorization` header carrying the Dashboard_Auth_Token, in the form
# DRF's `TokenAuthentication` parses: the scheme keyword `Token`, a space,
# then the token. Not `Bearer` — DRF's own keyword is `Token`, and a
# `Bearer` prefix is silently treated as no credential at all.
AUTH_HEADER_NAME = "Authorization"
AUTH_SCHEME = "Token"

# Multipart field names for a screenshot upload, matching
# `ScreenshotIngestSerializer`'s writable fields.
SCREENSHOT_ROUTE_FIELD = "route"
SCREENSHOT_IMAGE_FIELD = "image"

# Which files in `<run_dir>/screenshots/` are screenshots. PNG because
# design.md fixes the artifact as `<run_dir>/screenshots/<route-slug>.png`
# and `browser_capture.py` writes nothing else there. Spelled locally
# rather than imported from `browser_capture`: that module imports
# `sandbox` (and therefore `docker`) at module scope, which is a heavy
# import to take on every upload for the sake of a four-character glob.
_SCREENSHOT_GLOB = "*.png"
_SCREENSHOT_CONTENT_TYPE = "image/png"

# Events per `POST .../events/` request. Bounds one request, not the log:
# see the module docstring's "Event order" section for why the log is
# split at all and why the batches are posted sequentially.
#
# 100 is derived from what an event actually is. `monitor.py` reads pty
# output in 4096-byte chunks and writes one event per chunk, so a full
# batch is ~400 KB of payload before JSON escaping — comfortably inside
# Django's 2.5 MB `DATA_UPLOAD_MAX_MEMORY_SIZE`, with enough headroom that
# output made largely of escaped control characters (which terminal output
# genuinely is: every ANSI escape and newline expands) still fits rather
# than being rejected as too large. Larger batches would mean fewer round
# trips, but a rejected batch costs the whole upload, and a Run's upload is
# not on anyone's critical path.
_EVENT_BATCH_SIZE = 100

# Per-request timeout for the artifact uploads. Longer than the login
# request's because these requests carry real payloads (a screenshot is
# often a megabyte or two of PNG) and because the events endpoint writes
# its whole batch inside a locked transaction before answering. Still
# bounded, so an upload against a wedged backend fails with a message
# naming the stage instead of hanging a CI job forever.
_UPLOAD_REQUEST_TIMEOUT_S = 60.0


class UploadError(Exception):
    """Raised by this module when a backend call could not be completed, or
    its result could not be stored.

    design.md gives the Uploader this single exception type, shared by
    `login()` and `upload_run()`, so `cli.py` maps one name to
    a `click.ClickException` per command — matching `config.ConfigError`,
    `sandbox.SandboxError`, and `run_directory.RunDirectoryError`, each of
    which is its own module's single failure type.

    Every message names the endpoint or the path that failed. No message
    ever contains the password that was sent or the token that came back:
    an exception is the single most likely thing in this flow to be pasted
    into a bug report or captured by a CI log.
    """


def token_path(home: Path | None = None) -> Path:
    """Returns the Dashboard_Auth_Token's path: `~/.vaultshield/token`.

    Pure — computes a path and creates nothing (`store_token` does that).

    `home` defaults to `Path.home()` and exists so a caller can point the
    whole flow at a different home directory; `cli.py` never passes it.
    Taking it as an argument, rather than reading `Path.home()` deep inside
    `store_token`, is what lets the tests assert real permissions on a real
    file under `tmp_path` without mutating the environment of the process
    they run in — the same reason `run_directory.py`'s helpers take
    `repo_root` instead of calling `Path.cwd()` themselves.
    """
    root = home if home is not None else Path.home()
    return root / VAULTSHIELD_HOME_DIRNAME / TOKEN_FILENAME


def login(
    backend_url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    home: Path | None = None,
) -> None:
    """Obtains a Dashboard_Auth_Token from `backend_url` and stores it at
    `~/.vaultshield/token` with mode `0o600` (Requirements 14.1, 27.1).

    Posts the developer's username/password credential to
    `<backend_url>/api/auth/login/` and keeps the `token` from the
    response. `username` and `password` are prompted for at the terminal
    when not supplied — see the module docstring for why a password is
    never accepted as a CLI option.

    Nothing is written to disk unless a token was obtained, so a failed
    login leaves any previously stored token exactly as it was.

    Returns nothing, per design.md: where the token went is
    `token_path(home)`, which a caller that wants to report it can ask for
    directly instead of being handed a path it already knows how to derive.

    Raises `UploadError` if the backend cannot be reached, rejects the
    credential (Requirement 27.2), or answers without a usable token, and
    if the token could not be written to disk.
    """
    username, password = _resolve_credentials(username, password)
    token = _request_token(backend_url, username, password)
    store_token(token, home)


def store_token(token: str, home: Path | None = None) -> Path:
    """Writes `token` to `~/.vaultshield/token` with mode `0o600` (inside a
    `0o700` `~/.vaultshield/`) and returns that path.

    The file's entire content is the token plus a trailing newline, so it
    stays readable with `cat` and diffable by eye; `read_text().strip()` is
    all it takes to read back.

    Owner-only permissions are established before any content is written,
    and are re-applied even when the file already existed — see the module
    docstring's "Permissions" section for why both of those matter. An
    existing token is replaced.

    Raises `UploadError`, naming the path, if the directory or the file
    could not be created or written.
    """
    path = token_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=TOKEN_DIR_MODE)
        # `mode=` above only applies to directories this call creates, so an
        # existing `~/.vaultshield/` keeps whatever mode it has. That is
        # deliberate: `resolve_git_credential` (Task 29.1) shares this
        # directory, and silently re-permissioning a directory another
        # component owns files in is not this function's call to make. The
        # token file's own `0o600` does not depend on it.
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE
        )
        try:
            # Unconditional, so a pre-existing loose mode (or a umask that
            # widened the `os.open` mode) is corrected before the token is
            # on disk rather than after.
            os.fchmod(descriptor, TOKEN_FILE_MODE)
            token_file = os.fdopen(descriptor, "w")
        except BaseException:
            # Nothing owns the descriptor yet — neither `fchmod` nor a
            # failed `fdopen` closes it — so this is the one place it has to
            # be closed by hand. Past this point the file object owns it.
            os.close(descriptor)
            raise
        with token_file:
            token_file.write(f"{token}\n")
    except OSError as exc:
        raise UploadError(
            f"Cannot store the dashboard auth token at {path}: {exc}"
        ) from exc
    return path


def load_token(home: Path | None = None) -> str:
    """Returns the Dashboard_Auth_Token stored by `login` at
    `~/.vaultshield/token`.

    The inverse of `store_token`, and the counterpart every caller of
    `upload_run` needs — `upload_run` takes the token as a parameter (per
    design.md's signature) rather than reading it itself, so that the "load
    a credential" and "use a credential" steps stay separable and testable
    apart.

    Whitespace is stripped, so the trailing newline `store_token` writes is
    not sent as part of the header value.

    Raises `UploadError` naming the path, and the command that creates it,
    if the file does not exist, cannot be read, or is empty: "you have not
    logged in yet" is the single most likely reason an upload cannot start,
    and it is fixable in one step.
    """
    path = token_path(home)
    try:
        token = path.read_text().strip()
    except FileNotFoundError as exc:
        raise UploadError(
            f"No dashboard auth token at {path}. Run `vaultshield login` "
            "first."
        ) from exc
    except OSError as exc:
        raise UploadError(
            f"Cannot read the dashboard auth token at {path}: {exc}"
        ) from exc

    if not token:
        raise UploadError(
            f"The dashboard auth token at {path} is empty. Run "
            "`vaultshield login` again."
        )
    return token


def upload_run(
    run_dir: Path,
    run_id: str,
    backend_url: str,
    token: str,
    *,
    batch_size: int = _EVENT_BATCH_SIZE,
    scrub: Callable[[str], str] = lambda text: text,
) -> None:
    """Submits a completed Run's artifacts to `backend_url`, authenticated
    with `token` (Requirements 14.2, 14.3).

    POSTs the Event_Log to `/api/runs/<run_id>/events/` in batches, in
    `events.jsonl` line order; the structured diff to
    `/api/runs/<run_id>/diff/`; and each `<run_dir>/screenshots/*.png` to
    `/api/runs/<run_id>/screenshots/` as its own multipart request. Once
    all three stages complete, every artifact is readable through
    `GET /api/runs/<run_id>/`, which `RunDetailSerializer` nests
    automatically — that is Requirement 14.3, and it needs nothing from
    this function beyond having posted each artifact to the endpoint that
    stores it.

    An artifact that is absent is skipped rather than treated as an error;
    see the module docstring's "Run_Directory is read-only here" section
    for which absences are legitimate and why.

    `batch_size` is a keyword-only addition to design.md's reference
    signature, which stays valid for every positional caller. It exists so
    a test can drive the multi-batch path (and the failure of a *later*
    batch) with a handful of events instead of hundreds.

    `scrub` (keyword-only, Batch 3 FR5) is applied to every event payload
    before it is sent; `run` passes `SecretSet.scrub`, which replaces the
    agent's credential needles with `***`. The file on disk is not changed.

    `run_dir` is read and never written: no file under it is created,
    moved, truncated, or removed, whether this call succeeds or fails, so a
    failed upload is retryable from the same artifacts (Requirement 14.4,
    Property 24).

    Raises `UploadError` — naming the stage and the endpoint — if the
    backend cannot be reached, answers a non-2xx status, or if an artifact
    that exists cannot be read or parsed. Reporting that failure to the
    developer is the caller's half of Requirement 14.4.
    """
    try:
        validate_run_id(run_id)
    except RunDirectoryError as exc:
        # Re-raised as this module's own failure type so a caller catching
        # `UploadError` around an upload does not also have to know
        # `run_directory`'s. The check matters here because `run_id` is
        # interpolated into a URL path: `submit`/`teardown` take it straight
        # from the command line, and an unvalidated value could address a
        # different endpoint entirely.
        raise UploadError(f"Cannot upload run {run_id!r}: {exc}") from exc

    events_url = _build_url(backend_url, EVENTS_PATH_TEMPLATE.format(run_id=run_id))
    diff_url = _build_url(backend_url, DIFF_PATH_TEMPLATE.format(run_id=run_id))
    screenshots_url = _build_url(
        backend_url, SCREENSHOTS_PATH_TEMPLATE.format(run_id=run_id)
    )

    # One client for the whole Run: the auth header is set once, and the
    # connection is reused across every batch and every screenshot rather
    # than reopened per request.
    with httpx.Client(
        headers=_auth_headers(token), timeout=_UPLOAD_REQUEST_TIMEOUT_S
    ) as client:
        _upload_events(client, events_url, run_dir, batch_size, scrub)
        _upload_diff(client, diff_url, run_dir)
        _upload_screenshots(client, screenshots_url, run_dir)


def create_run(
    backend_url: str, token: str, *, project: str, run_id: str, agent: str
) -> None:
    """Registers a new Run with the backend before anything else about it
    exists (Requirement 12.2's `POST /api/runs/`, client half).

    POSTs `{project, run_id, agent}` to `/api/runs/`. `status` is not sent:
    the backend's default is `running`, which is the only state a Run that
    has not started yet can honestly be in. Registering first is what lets
    `upload_run` address `/api/runs/<run_id>/...` at the end of the Run, and
    what lets a failure in between be recorded against the Run as `failed`.

    One-shot `httpx.post`, the same style as `_request_token`: this is a
    single request, so a `Client` would buy nothing.

    Raises `UploadError` if `run_id` is not a safe path component (checked
    before any request), if the backend cannot be reached, if it rejects the
    dashboard auth token (401/403, with the `vaultshield login` fix named),
    or if it refuses the Run for any other reason (an unknown project or a
    duplicate `run_id` is a 400 whose detail is quoted). No message ever
    contains the token.
    """
    _guard_run_id(run_id)
    url = _build_url(backend_url, RUNS_PATH)
    response = _send_run_control(
        httpx.post,
        url,
        token,
        {"project": project, "run_id": run_id, "agent": agent},
    )
    _raise_for_rejected_token(response, url, backend_url)
    if not response.is_success:
        raise UploadError(
            f"The backend at {url} refused to register run {run_id} "
            f"(HTTP {response.status_code}): {_error_detail(response)}"
        )


def complete_run(backend_url: str, token: str, run_id: str) -> None:
    """Moves a registered Run from `running` to `awaiting_review` once every
    artifact has been uploaded (Requirement 12.4's `PATCH /api/runs/:id/`,
    with a Requirement 11.2 status; client half).

    PATCHes `{"status": "awaiting_review"}` to `/api/runs/<run_id>/`. This is
    the completion signal: diff ingest cannot be it, because screenshots are
    posted after the diff and their number is unknown to the backend. The
    backend accepts it only from `running`.

    Raises `UploadError` on a bad `run_id`, an unreachable backend, or a
    rejected token (same messages as `create_run`), on a 409 (the Run is no
    longer running; the backend's `current_status` is named), and on any
    other non-2xx status with the backend's detail quoted.
    """
    _guard_run_id(run_id)
    url = _build_url(backend_url, RUN_DETAIL_PATH_TEMPLATE.format(run_id=run_id))
    response = _send_run_control(
        httpx.patch, url, token, {"status": AWAITING_REVIEW_STATUS}
    )
    _raise_for_rejected_token(response, url, backend_url)
    if response.status_code == 409:
        raise UploadError(
            f"The backend at {url} refused to mark run {run_id} ready for "
            f"review: it is no longer running (backend reports "
            f"`{_current_status(response)}`)"
        )
    if not response.is_success:
        raise UploadError(
            f"The backend at {url} refused to mark run {run_id} ready for "
            f"review (HTTP {response.status_code}): {_error_detail(response)}"
        )


def mark_run_failed(backend_url: str, token: str, run_id: str) -> str | None:
    """Best-effort: marks a registered Run `failed` after it errored
    (Requirement 12.4's PATCH with a Requirement 11.2 status), and returns
    `None` on success or a one-line warning.

    PATCHes `{"status": "failed"}` to `/api/runs/<run_id>/` with the short
    `_MARK_FAILED_TIMEOUT`, because it runs on the way out of a Run that has
    already failed and must not hold that exit up.

    NEVER raises. It catches `Exception` and `KeyboardInterrupt`, so neither a
    defect here nor a Ctrl-C during the request can replace the error the
    caller is in the middle of reporting. Every failure becomes the returned
    warning instead: a bad `run_id`, an unreachable backend, any non-2xx
    status, including a 409, which means the Run had already left `running`
    (for example a completion that landed but whose response was lost). It
    does not print and does not import click: reporting the warning is the
    caller's decision. No warning ever contains the token.
    """
    try:
        _guard_run_id(run_id)
        url = _build_url(
            backend_url, RUN_DETAIL_PATH_TEMPLATE.format(run_id=run_id)
        )
        try:
            response = httpx.patch(
                url,
                headers=_auth_headers(token),
                json={"status": FAILED_STATUS},
                timeout=_MARK_FAILED_TIMEOUT,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return f"cannot reach the VaultShield backend at {url}: {exc}"
        if response.is_success:
            return None
        if response.status_code == 409:
            return (
                f"{url} answered HTTP 409: the run is no longer running "
                f"(backend reports `{_current_status(response)}`)"
            )
        return (
            f"{url} answered HTTP {response.status_code}: "
            f"{_error_detail(response)}"
        )
    except UploadError as exc:
        return str(exc)
    except (Exception, KeyboardInterrupt) as exc:
        # No str(exc): keep it one line and token-free.
        return f"unexpected {type(exc).__name__}"


def _guard_run_id(run_id: str) -> None:
    """Raises `UploadError` unless `run_id` is a safe single path component.

    The same check `upload_run` makes, for the same reason: `run_id` is
    interpolated into a URL path, and an unvalidated value could address a
    different endpoint entirely.
    """
    try:
        validate_run_id(run_id)
    except RunDirectoryError as exc:
        raise UploadError(f"Cannot register run {run_id!r}: {exc}") from exc


def _send_run_control(
    send: Callable[..., httpx.Response], url: str, token: str, payload: dict
) -> httpx.Response:
    """Sends one Run-control request (`httpx.post` or `httpx.patch`) and
    returns the response, or raises `UploadError` naming `url`.

    Catches `(httpx.HTTPError, httpx.InvalidURL)` exactly as `_post` does:
    `InvalidURL` is not an `HTTPError`, and a malformed `--backend-url` must
    read as "cannot reach the backend", not as a traceback.
    """
    try:
        return send(
            url,
            headers=_auth_headers(token),
            json=payload,
            timeout=_RUN_CONTROL_TIMEOUT_S,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise UploadError(
            f"Cannot reach the VaultShield backend at {url}: {exc}"
        ) from exc


def _raise_for_rejected_token(
    response: httpx.Response, url: str, backend_url: str
) -> None:
    """Raises `UploadError` naming the `vaultshield login` fix when the
    backend refused the dashboard auth token itself (401/403)."""
    if response.status_code in _TOKEN_REJECTED_STATUSES:
        raise UploadError(
            f"The backend at {url} rejected the dashboard auth token (HTTP "
            f"{response.status_code}). Run `vaultshield login --backend-url "
            f"{backend_url}` again."
        )


def _current_status(response: httpx.Response) -> str:
    """The `current_status` a 409 `StateTransitionError` body carries, or a
    placeholder when the body has none."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None
    status = payload.get("current_status") if isinstance(payload, dict) else None
    return status if isinstance(status, str) and status else "unknown"


def _resolve_credentials(
    username: str | None, password: str | None
) -> tuple[str, str]:
    """Returns the `(username, password)` to log in with, prompting at the
    terminal for whichever one was not supplied.

    The password prompt hides its input and is not confirmed: this is a
    login against an existing account, not the creation of one, so a typo
    costs a rejected request rather than an unusable credential — and a
    confirmation prompt on every login would be pure friction.
    """
    if username is None:
        username = click.prompt("Dashboard username")
    if password is None:
        password = click.prompt("Dashboard password", hide_input=True)
    return username, password


def _request_token(backend_url: str, username: str, password: str) -> str:
    """Posts the credential to `<backend_url>/api/auth/login/` and returns
    the Dashboard_Auth_Token from the response.

    Raises `UploadError` on a transport failure, on any non-2xx status
    (with a credential-specific message for the rejection statuses in
    `_CREDENTIAL_REJECTED_STATUSES`), and on a 2xx response that carries no
    usable token — a backend that answers `200` with something other than
    `{"token": ...}` is not one this CLI can talk to, and storing an empty
    file would only defer the failure to the first upload.
    """
    url = _build_url(backend_url, LOGIN_PATH)
    try:
        response = httpx.post(
            url,
            json={USERNAME_FIELD: username, PASSWORD_FIELD: password},
            timeout=_LOGIN_REQUEST_TIMEOUT_S,
        )
    except httpx.RequestError as exc:
        # Connection refused, DNS failure, TLS error, read timeout: the
        # request never got an answer, so no credential was verified and
        # nothing is stored.
        raise UploadError(
            f"Cannot reach the VaultShield backend at {url}: {exc}"
        ) from exc

    if response.status_code in _CREDENTIAL_REJECTED_STATUSES:
        raise UploadError(
            f"The backend at {url} rejected the credential for user "
            f"{username!r} (HTTP {response.status_code}): "
            f"{_error_detail(response)}"
        )
    if not response.is_success:
        raise UploadError(
            f"Login failed at {url} with HTTP {response.status_code}: "
            f"{_error_detail(response)}"
        )

    token = _extract_token(response, url)
    return token


def _extract_token(response: httpx.Response, url: str) -> str:
    """Pulls the token out of a successful login response.

    Raises `UploadError` if the body is not JSON, is not a JSON object, or
    has no non-empty `token` string in it. The body is never quoted in
    these messages: unlike an error response, a 2xx body from this endpoint
    is exactly where a token lives, and an exception is the last place a
    token should ever appear.
    """
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise UploadError(
            f"The backend at {url} answered HTTP {response.status_code} with a "
            f"body that is not JSON, so no dashboard auth token could be read "
            f"from it: {exc}"
        ) from exc

    token = payload.get(TOKEN_RESPONSE_KEY) if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise UploadError(
            f"The backend at {url} answered HTTP {response.status_code} without a "
            f"{TOKEN_RESPONSE_KEY!r} value, so there is no dashboard auth token "
            "to store."
        )
    return token.strip()


def _auth_headers(token: str) -> dict[str, str]:
    """The `Authorization: Token <token>` header every artifact request
    carries (Requirement 14.2's "using the stored dashboard authentication
    token").
    """
    return {AUTH_HEADER_NAME: f"{AUTH_SCHEME} {token}"}


def _upload_events(
    client: httpx.Client,
    url: str,
    run_dir: Path,
    batch_size: int,
    scrub: Callable[[str], str] = lambda text: text,
) -> None:
    """POSTs `<run_dir>/events.jsonl` to `url` in batches of `batch_size`,
    in file order, one request at a time.

    Each batch is a bare JSON array of the parsed lines, exactly as read —
    see the module docstring's "Event order" section for why order is
    carried by position and why no `sequence` field is constructed.

    A Run with no Event_Log, or an empty one, sends nothing at all rather
    than an empty array: there is no artifact to submit, and a request that
    stores zero events only adds a way for the upload to fail.

    Events with a blank payload are left out (see `_without_blank_payloads`),
    with one note saying how many; the file on disk is not changed.
    """
    events = _without_blank_payloads(
        _scrubbed_events(_read_event_log(event_log_path(run_dir)), scrub)
    )
    for batch in _batched(events, batch_size):
        # Sequential and awaited: batch N must be stored before batch N+1 is
        # sent, because the backend numbers each batch from the highest
        # sequence already stored for the Run.
        _post(client, url, "events", json=batch)


def _scrubbed_events(
    events: list[dict[str, Any]], scrub: Callable[[str], str]
) -> list[dict[str, Any]]:
    """`events` with `scrub` applied to every string payload (Batch 3 FR5)."""
    return [
        {**event, "payload": scrub(event["payload"])}
        if isinstance(event.get("payload"), str)
        else event
        for event in events
    ]


def _without_blank_payloads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`events` minus every event whose `payload` is a string that is empty
    or whitespace-only, in their original order, and one note naming how
    many were left out.

    Such an event carries nothing to show, and the backend rejects it: the
    ingest serializer's `payload` is a DRF `CharField` that strips before its
    blank check, so one `""` (a blank line in the agent's reply) fails the
    whole batch with HTTP 400. `str.strip()` is the same test DRF applies.
    Applied after scrubbing, to exactly what would be sent. A non-string or
    missing payload is not touched here.
    """
    kept = [
        event
        for event in events
        if not (isinstance(event.get("payload"), str) and not event["payload"].strip())
    ]
    skipped = len(events) - len(kept)
    if skipped:
        noun = "event" if skipped == 1 else "events"
        click.echo(f"Skipped {skipped} blank {noun} (nothing to show).")
    return kept


def upload_events_best_effort(
    backend_url: str,
    token: str,
    run_id: str,
    run_dir: Path,
    *,
    scrub: Callable[[str], str],
) -> str | None:
    """Best-effort: POSTs a FAILED Run's `events.jsonl`, scrubbed, so its
    violations reach the dashboard timeline (Batch 3 FR4, D8). Returns `None`
    on success (or when there is no Event_Log) and a one-line warning
    otherwise.

    NEVER raises, for the same reason as `mark_run_failed`: it runs on the way
    out of a Run that already failed and must not replace that error. Uses the
    same short timeout. No warning ever contains the token or a payload.
    """
    try:
        _guard_run_id(run_id)
        url = _build_url(backend_url, EVENTS_PATH_TEMPLATE.format(run_id=run_id))
        events = _without_blank_payloads(
            _scrubbed_events(_read_event_log(event_log_path(run_dir)), scrub)
        )
        if not events:
            return None
        with httpx.Client(
            headers=_auth_headers(token), timeout=_MARK_FAILED_TIMEOUT
        ) as client:
            for batch in _batched(events, _EVENT_BATCH_SIZE):
                _post(client, url, "events", json=batch)
        return None
    except UploadError as exc:
        return str(exc).splitlines()[0] if str(exc) else "upload failed"
    except (Exception, KeyboardInterrupt) as exc:
        return f"unexpected {type(exc).__name__}"


def _upload_diff(client: httpx.Client, url: str, run_dir: Path) -> None:
    """POSTs `<run_dir>/diff.json` to `url`, verbatim.

    The file's parsed contents are the request body unchanged: it is already
    `{"files": [...]}`, which is what the diff endpoint accepts. A Run with
    no `diff.json` — git capture failed, or never ran — skips this stage.
    """
    diff = _read_diff(diff_path(run_dir))
    if diff is None:
        return
    _post(client, url, "diff", json=diff)


def _upload_screenshots(client: httpx.Client, url: str, run_dir: Path) -> None:
    """POSTs each `<run_dir>/screenshots/*.png` to `url` as its own
    multipart request (`route` + `image`), in filename order.

    Filename order rather than directory order so an upload is
    reproducible: `Path.glob` yields whatever the filesystem hands back,
    which differs between machines. Screenshots carry no ordering
    requirement of their own — each is identified by its `route` — so any
    stable order will do, and a stable one makes a failed upload's partial
    progress predictable.

    A Run with no `screenshots/` directory (no configured routes, or a
    readiness timeout) skips this stage.
    """
    for path in _screenshot_files(run_dir):
        image = _read_screenshot(path)
        _post(
            client,
            url,
            f"screenshot {path.name}",
            data={SCREENSHOT_ROUTE_FIELD: _screenshot_route(path)},
            files={
                SCREENSHOT_IMAGE_FIELD: (
                    path.name,
                    image,
                    _SCREENSHOT_CONTENT_TYPE,
                )
            },
        )


def _read_event_log(path: Path) -> list[dict[str, Any]]:
    """Parses the JSONL Event_Log at `path` into a list of event objects, in
    file order.

    Returns an empty list if `path` does not exist: a Run that produced no
    events has nothing to upload, which is not a failure. Blank lines are
    skipped — a trailing newline at the end of the file is not an event.

    Read-only, and read whole rather than streamed: an Event_Log is at most
    a few megabytes of text, and holding it means the file handle is closed
    before the first request goes out, so nothing keeps the artifact open
    across a network call.

    Raises `UploadError` naming `path` if it cannot be read, if a line is
    not valid JSON, or if a line is not a JSON object — the ingest endpoint
    expects an array of objects, and a malformed Event_Log silently dropped
    from the upload would mean a Run reviewed against an incomplete record.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UploadError(f"Cannot read the event log at {path}: {exc}") from exc

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UploadError(
                f"Cannot upload the event log at {path}: line {line_number} is "
                f"not valid JSON: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise UploadError(
                f"Cannot upload the event log at {path}: line {line_number} is "
                f"a {type(event).__name__}, not a JSON object"
            )
        events.append(event)
    return events


def _read_diff(path: Path) -> Any | None:
    """Parses the structured diff at `path`, or returns `None` if there is
    no diff artifact for this Run.

    Read-only. Raises `UploadError` naming `path` if the file exists but
    cannot be read or is not valid JSON. The *shape* of the parsed value is
    deliberately not checked here: `git_capture.py` owns that shape and the
    diff endpoint validates it per-file, so a shape check in between would
    be a third copy of the same rule, free to drift from both.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UploadError(f"Cannot read the diff at {path}: {exc}") from exc

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise UploadError(
            f"Cannot upload the diff at {path}: it is not valid JSON: {exc}"
        ) from exc


def _screenshot_files(run_dir: Path) -> list[Path]:
    """The Run's screenshots, in filename order.

    Empty when `<run_dir>/screenshots/` does not exist — `Path.glob` on a
    missing directory yields nothing rather than raising, so no existence
    check is needed and none is added. Directories that happen to match the
    glob are excluded; only files are uploadable.
    """
    return sorted(
        path
        for path in screenshots_dir(run_dir).glob(_SCREENSHOT_GLOB)
        if path.is_file()
    )


def _read_screenshot(path: Path) -> bytes:
    """Returns the bytes of one screenshot. Read-only.

    Read whole rather than handed to `httpx` as an open file object, for the
    same reason `_read_event_log` reads whole: the file is closed before the
    request is made, so an upload never holds a handle on an artifact
    across a network call. A screenshot is a PNG of a single viewport, so
    the memory cost is a megabyte or two.

    Raises `UploadError` naming `path` if it cannot be read — including the
    `FileNotFoundError` of a screenshot removed between the glob and the
    read, which is a real (if unlikely) race and not something to upload an
    empty image for.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UploadError(f"Cannot read the screenshot at {path}: {exc}") from exc


def _screenshot_route(path: Path) -> str:
    """The `route` a screenshot is uploaded under: its filename stem.

    `browser_capture.route_slug` is what produced that stem, and it is
    lossy and irreversible by design — `/users/42?tab=activity` becomes
    `users-42-tab-activity`, and `/a/b` and `/a-b` both become `a-b` — so
    the original route cannot be recovered from the file. Nothing else in
    the Run_Directory records it either: `browser_capture` writes PNGs and
    no manifest, and design.md's `upload_run` signature takes no config, so
    the configured route list is not available here.

    The stem is therefore sent as-is rather than dressed up as a URL path.
    A reconstructed `/users-42-tab-activity` would *look* like a route the
    developer configured while being one that does not exist, which is
    worse in a review UI than an obviously-slugged label. `route_slug`
    guarantees the stem is non-empty (`root` for `/`), which is what the
    endpoint requires — `Screenshot.route` is non-blank and at most 255
    characters, and a slug is capped well under that.

    Recording the exact configured route would need `browser_capture` to
    write a route -> filename manifest into the Run_Directory; that is a
    change to Task 11.2's artifact contract, not something to invent here.
    """
    return path.stem


def _batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    """Yields consecutive slices of `items` of at most `size`, in order.

    `itertools.batched` would do this, but it is 3.12+ and this package
    supports 3.11 (`requires-python = ">=3.11"`).

    A `size` below 1 would loop forever on a non-empty sequence, so it is
    floored at 1 rather than trusted: `batch_size` reaches here from
    `upload_run`'s keyword argument.
    """
    step = max(1, size)
    for start in range(0, len(items), step):
        yield list(items[start : start + step])


def _post(client: httpx.Client, url: str, stage: str, **kwargs: Any) -> httpx.Response:
    """POSTs to `url` and returns the response, or raises `UploadError`.

    The single place every artifact request goes through, so that every
    network and HTTP failure in the upload is wrapped exactly once and
    every message names the stage that failed (`events`, `diff`, or the
    specific screenshot) alongside the endpoint. `stage` is in the message
    because "the upload failed" is not actionable when three different
    endpoints could have been the one to refuse.

    `httpx.HTTPError` covers both halves of a failed call: `RequestError`
    for a request that never got an answer (connection refused, DNS
    failure, TLS error, timeout — Requirement 14.4's "cannot reach the
    backend") and anything raised while a response is being read. A non-2xx
    status is checked explicitly rather than via `raise_for_status`, so the
    message can quote the backend's own error detail, which for a DRF
    validation error is the most useful sentence available.

    `httpx.InvalidURL` is caught alongside it — it does not derive from
    `HTTPError` — so a malformed `backend_url` (a missing scheme, a typo in
    a `--backend-url` flag) is reported as an upload failure naming the URL
    rather than as a traceback out of the middle of a Run's upload.

    Nothing here touches the filesystem, which is what makes Requirement
    14.4's preservation guarantee hold on every one of these paths.
    """
    try:
        response = client.post(url, **kwargs)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise UploadError(
            f"Upload failed at stage {stage!r}: cannot reach the VaultShield "
            f"backend at {url}: {exc}"
        ) from exc

    if not response.is_success:
        raise UploadError(
            f"Upload failed at stage {stage!r}: {url} answered HTTP "
            f"{response.status_code}: {_error_detail(response)}"
        )
    return response


def _error_detail(response: httpx.Response) -> str:
    """Returns a short, single-line summary of an error response's body, for
    an `UploadError` message.

    DRF answers a rejected credential with a JSON error detail worth
    showing verbatim; a proxy or a debug page answers with HTML that is
    not, so the text is collapsed to one line and truncated to
    `_MAX_ERROR_BODY_CHARS`. An unreadable or empty body yields a fixed
    placeholder rather than an empty string, so the message never trails
    off after its colon.
    """
    try:
        body = response.text
    except Exception:  # pragma: no cover - a decoding failure is not the story
        return "<unreadable response body>"

    collapsed = " ".join(body.split())
    if not collapsed:
        return "<empty response body>"
    if len(collapsed) > _MAX_ERROR_BODY_CHARS:
        return f"{collapsed[:_MAX_ERROR_BODY_CHARS]}..."
    return collapsed


def _build_url(backend_url: str, path: str) -> str:
    """Joins the backend origin and an API path into one URL, tolerating a
    trailing slash on the origin and a missing/extra leading slash on the
    path.

    Same rule (and the same reason for not using `urllib.parse.urljoin`,
    which would resolve a slash-less path against the origin's last
    segment) as `browser_capture._build_url`. Kept local rather than
    imported: importing it would pull `browser_capture`'s module-level
    Playwright/Sandbox-adjacent imports into every `login`, and a
    two-token f-string is not a dependency worth taking.
    """
    return f"{backend_url.rstrip('/')}/{path.lstrip('/')}"
