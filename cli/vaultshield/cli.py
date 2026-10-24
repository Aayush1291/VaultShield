"""VaultShield CLI entry point.

Defines the top-level Click group (``cli``) that the ``vaultshield`` console
script maps to, plus the ``init``, ``login``, ``run``, ``submit``, and
``teardown`` commands.

``run`` is the full per-Run pipeline (Task 8.2), wired in design.md's own
order: Config_Module → Credentials_Module → Sandbox_Module → the
Agent_Adapter selected by ``--agent`` → Monitor → Browser_Capture_Module
(Task 11.5) → git branch/diff capture (Task 12.4) → Sandbox teardown →
Uploader (Task 18.5), with the Event_Log and the other artifacts in the
Run_Directory throughout. Every stage in design.md's flowchart is now
wired, and ``_AGENT_ADAPTERS`` is now complete: ``"mock": Mock_Adapter``
(Task 6.4), ``"codex": Codex_Adapter`` (Task 10.5), ``"kiro":
Kiro_Adapter`` (Task 19.5), and ``"antigravity": Antigravity_Adapter``
(Task 20.4) — Requirement 22.1's three MVP adapters plus ``mock``. Each of
those tasks added one entry and one import, without touching the
``--agent`` option's own definition or ``run`` itself — including the
upload, which is the last stage of ``run`` itself and so happens for every
agent in that table rather than for any particular one.

``login`` (Task 18.1) is `uploader.login` behind a Click command: it
exchanges a dashboard username/password for the Dashboard_Auth_Token and
stores it at ``~/.vaultshield/token``. It stays a separate command that a
developer runs once — ``run`` only ever *reads* that token
(``uploader.load_token``, when it registers the Run before any Sandbox
exists) and never prompts for a credential mid-Run.

``submit`` (Task 29.6) is `submit.submit` behind a Click command: it takes
the ``<run-id>`` the dashboard prints for an approved Run and opens the
pull/merge request for it, using the same stored Dashboard_Auth_Token
``run``'s upload stage reads. It is a separate command precisely because it
happens after a human review — ``run`` captures a Run, a Reviewer approves
it, and only then is there anything to submit.

``teardown`` (Task 30.3) is `teardown.teardown` behind a Click command: it
takes a ``<run-id>`` and cleans up what is left of that Run — removing its
Sandbox if one is still up, and deleting its pushed remote branch if the Run
was rejected — reading the Run's review status with the same stored
Dashboard_Auth_Token ``run``'s upload stage and ``submit`` read. It is a
separate command for the same reason ``submit`` is: what has to be cleaned up
is only knowable after a Run has finished, and for the branch, only after a
human review. It does not replace ``run``'s own step 9, which already tears
down the Sandbox it created — it is for what that step could not reach, a Run
whose ``run`` was killed or whose container outlived a crash. With it the
command set is complete: ``init``, ``login``, ``run``, ``submit``,
``teardown``.

Framework detection (``detect_framework``) is called ONLY from ``init``
below — ``run`` loads the existing ``.vaultshield.yml`` via ``load_config``
instead of re-detecting it, per Requirement 1.4.

Also defines ``_sandbox-smoke`` (Task 4.4): a TEMPORARY debug command, not a
permanent CLI feature (see its own docstring below). It exists solely so a
developer can manually verify Sandbox_Module (Task 4) against a real Docker
daemon and a real repo; it is deliberately excluded from the automated test
suite.
"""

import uuid
import sys
from pathlib import Path

import click
import docker

from vaultshield import __version__
# `uploader` is imported as a module, unlike every other import here,
# because the `login` command below and `uploader.login` share a name: a
# direct `from vaultshield.uploader import login` would be shadowed by the
# command function Click registers under that same name.
from vaultshield import uploader
from vaultshield.adapters.antigravity import Antigravity_Adapter
from vaultshield.adapters.base import AdapterError, Agent_Adapter, AuthError
from vaultshield.adapters.codex import Codex_Adapter
from vaultshield.adapters.kiro import Kiro_Adapter
from vaultshield.adapters.mock import Mock_Adapter
from vaultshield.browser_capture import (
    DEFAULT_READY_TIMEOUT_S,
    ReadinessTimeout,
    ScreenshotError,
    start_and_capture,
)
from vaultshield.config import (
    ConfigError,
    ProjectConfig,
    detect_framework,
    load_config,
    write_config,
)
from vaultshield.credentials import build_sandbox_env
from vaultshield.git_capture import (
    GitCaptureError,
    build_structured_diff,
    commit_run_branch,
    resolve_diff_base,
)
from vaultshield.monitor import Monitor, OffLimitsViolation
from vaultshield.sandbox_monitor import SandboxMonitorError, supervise_in_sandbox
from vaultshield.run_directory import (
    create_run_dir,
    event_log_path,
    generate_run_id,
    screenshots_dir,
)
from vaultshield.adapters.antigravity import (
    ANTIGRAVITY_AUTH_TIMEOUT_S,
    ANTIGRAVITY_OAUTH_SCOPES,
)
from vaultshield.sandbox import Sandbox, SandboxError, container_path_under_repo
from vaultshield import egress
from vaultshield.egress import EgressConfigError, EgressError, EgressGuard
from vaultshield.git_capture import UnsafeGitConfig
from vaultshield.monitor import _append_event_log
from vaultshield import token_hygiene
from vaultshield.token_hygiene import (
    SecretSet,
    TokenHygieneViolation,
    check_run_outputs,
    skipped_snapshot_event,
)
from vaultshield import trusted_capture
from vaultshield.trusted_capture import (
    TrustedCapture,
    TrustedCaptureCaps,
    archive_sandbox_repo,
    export_run_bundle,
    kill_sandbox,
    notices_to_events,
    record_trusted_base,
    sandbox_image_id,
)

# Substring of `agy`'s own prompt for the pasted code ("Or, paste the
# authorization code here and press Enter:"), matched case-insensitively so the
# auth phase knows when to stop displaying and start asking. Matched on a
# fragment rather than the whole sentence so a reworded prompt still works.
_AUTH_PROMPT_MARKER = "paste the authorization code"
# `submit` is imported under another name for the same reason `uploader` is
# imported as a module: the `submit` command below and `submit.submit` share
# a name, so a plain `from vaultshield.submit import submit` would be
# shadowed by the command Click registers under it. The two error types come
# in directly, like every other module's do.
from vaultshield.submit import CredentialError, SubmitError
from vaultshield.submit import submit as submit_run
# `teardown` is imported under another name for the same reason `submit` is:
# the `teardown` command below and `teardown.teardown` share a name. Only
# `TeardownError` comes in directly — the `CredentialError` this module's
# branch deletion can raise is the same class already imported from `submit`
# above, since `teardown.py` resolves its credential through
# `submit.resolve_git_credential`.
from vaultshield.teardown import TeardownError, sync_rejected_branch, teardown_sandbox
from vaultshield.teardown import teardown as teardown_run
# The orchestrator behind `vaultshield ask`, reused by `run --ask` to build the
# agent prompt from the project and the developer's answers. `run_orchestrator`
# runs the same flow as `ask` and returns the enriched prompt instead of
# printing it.
from vaultshield.orchestrate.commands import run_orchestrator

# Adapter dispatch table for the `run` command (Task 6.4): maps an
# `--agent` choice to its Agent_Adapter subclass. `run`'s `--agent` option
# derives its `click.Choice` from this dict's keys at import time (see
# below), so a task that adds a new adapter only ever needs to add one
# entry here plus an import, without touching the option's definition or
# `run` itself. `"codex"` (Task 10.5, Requirement 8.1) is exactly that: one
# entry, so `--agent=codex` selects `Codex_Adapter` and `codex exec
# "<prompt>" --full-auto --ephemeral` is what the Run actually invokes.
#
# `"kiro"` (Task 19.5, Requirement 15.1) is the same one-entry change, so
# `--agent=kiro` selects `Kiro_Adapter` and `kiro-cli chat
# --no-interactive "<prompt>"` — with `KIRO_API_KEY` injected — is what the
# Run invokes. Everything else about that Run is unchanged: the Sandbox,
# the Monitor's PTY + file-change capture, browser capture, git capture,
# teardown, and the upload are all `run`'s own stages and are shared by
# every entry in this table, so registering an adapter is the whole of the
# wiring.
#
# `"antigravity"` (Task 20.4, Requirement 16.1) is the last one, and the
# same one-entry change again: `--agent=antigravity` selects
# `Antigravity_Adapter`, so `agy -p "<prompt>" --non-interactive` is what
# the Run invokes, with the agent's `isatty()` check satisfied by a
# terminal on every stdio fd (Requirement 16.2 — `adapters/antigravity.py`
# owns how) and a clean-but-silent exit failing the Run rather than passing
# for success (Requirement 16.3). That completes Requirement 22.1's adapter
# set — Codex, Kiro, Antigravity, with `mock` being the Monitor's own
# integration target rather than a fourth agent — so `--agent`'s choices
# are final and no further entry is due here.
#
# Every value must be constructible with no arguments, since `run`
# instantiates the selected class as `_AGENT_ADAPTERS[agent]()`. Both
# `Codex_Adapter`'s and `Kiro_Adapter`'s optional `api_key` parameters are
# compatible with that: omitting it means "read `CODEX_API_KEY` /
# `KIRO_API_KEY` from the environment", which is what Requirements 8.1 and
# 15.1 ask for. It also means a missing credential is reported by the
# adapter at `launch()` — as an `AdapterError`, i.e. a failed Run naming
# the unset variable — rather than by this table, which knows nothing
# about credentials. `Antigravity_Adapter` takes no constructor arguments
# at all: Requirement 16.1 names no credential for `agy`, so there is
# nothing for this table to pass or to omit.
_AGENT_ADAPTERS: dict[str, type[Agent_Adapter]] = {
    "antigravity": Antigravity_Adapter,
    "codex": Codex_Adapter,
    "kiro": Kiro_Adapter,
    "mock": Mock_Adapter,
}

# Source file `run` reads real credential values from when `--env-file`
# isn't given: `<repo_root>/.env`, the conventional name a developer
# already keeps their local values in. Nothing in it is ever copied into
# the Sandbox — `build_sandbox_env` only mirrors its key names and
# structural shapes (Requirements 3.1, 3.3).
_DEFAULT_SOURCE_ENV_FILENAME = ".env"

# Filename of the generated Sandbox_Credential_File, written inside the
# Run_Directory (so `<run_dir>/.env.sandbox`) and bind-mounted into the
# Sandbox by `Sandbox.create`. Deliberately NOT added to
# `run_directory.py`'s artifact constants: that module owns the three
# artifacts a Run *produces* for upload (`events.jsonl`, `diff.json`,
# `screenshots/`), whereas this is a per-Run *input* this pipeline
# generates and nothing else reads. Per-Run rather than
# `<repo_root>/.env.sandbox` so two concurrent Runs can't overwrite each
# other's credential file, and so a developer's own `.env.sandbox` (if
# they keep one) is never clobbered.
_SANDBOX_ENV_FILENAME = ".env.sandbox"

# Default Docker image the Sandbox builds/reuses for a Run. `python:3.11`
# rather than `python:3.11-slim` for the reason spelled out in
# `_sandbox-smoke`'s docstring below: `Sandbox.create` always runs
# `git clone` first, and the slim variant ships no `git`, so every Run
# would fail at the clone step. Overridable per Run with `--base-image`.
_DEFAULT_BASE_IMAGE = "python:3.11"

# Where the three backend-facing commands — `run`'s upload stage, `submit`,
# and `teardown` — address the VaultShield backend when `--backend-url` /
# `$VAULTSHIELD_BACKEND_URL` says nothing else. `_backend_url_option` below
# is the one option definition all three carry, so this default and the env
# var beside it are stated once rather than restated per command.
#
# `http://localhost:8000` because of Requirement 24.2: the CLI is not a
# compose service — `docker-compose.yml` deliberately defines seven services
# and no CLI (Requirement 24.1) — so it runs on the host machine, *outside*
# the compose network, and reaches the backend through the port the `web`
# service publishes, never over the compose-internal hostname `web:8000`,
# which does not resolve from the host.
#
# Both halves of that URL agree with the stack as written (Task 32.2's whole
# job), so a defaulted CLI reaches a default `docker compose up` with nothing
# configured on either side:
#
#   - `8000` is the host side of `web`'s port mapping,
#     `"${WEB_PUBLISHED_PORT:-8000}:8000"`. It is also Django's own default
#     port and what `config.py`'s Django detection assumes, so the same
#     default reaches a host-run `manage.py runserver` when the stack is down.
#   - `localhost` is in `settings.py`'s default `VAULTSHIELD_ALLOWED_HOSTS`
#     (`localhost,127.0.0.1,web`), so Django accepts the `Host` header these
#     requests arrive with. `localhost` rather than `127.0.0.1` because that
#     is the address Requirement 24.2 names; both are allowed hosts, and
#     compose publishes on all interfaces, so either reaches `web`.
#
# A default is safe here in a way it deliberately is not for `login` (see
# that command's own comment): loopback is the developer's own machine, so a
# defaulted upload cannot send the Dashboard_Auth_Token to a host the
# developer did not name — while a defaulted `login` would send a *password*
# to whatever answers there.
_DEFAULT_BACKEND_URL = "http://localhost:8000"

# Environment variable that overrides `_DEFAULT_BACKEND_URL` for all three
# commands, so a developer (or a CI job) running against a backend that is
# not on `localhost:8000` sets it once for the shell instead of passing
# `--backend-url` to every command. It is also the override for a compose
# stack whose published port was moved: nothing here reads compose's own
# variables, so a stack brought up with `WEB_PUBLISHED_PORT=8080` is reached
# with `VAULTSHIELD_BACKEND_URL=http://localhost:8080`. The explicit option
# still wins over it, per Click's own precedence.
_BACKEND_URL_ENVVAR = "VAULTSHIELD_BACKEND_URL"


def _backend_url_option(help_text: str):
    """The `--backend-url` option shared by `run`, `submit`, and `teardown`.

    A decorator factory rather than the same `click.option` call written out
    three times, so the default and the env var behind Requirement 24.2 have
    exactly one definition. The three commands are steps of one workflow
    against one backend — a Run is uploaded, reviewed, then submitted or
    torn down — and a command that disagreed about which backend that is
    would upload a Run somewhere it could never be submitted from. Only the
    help text differs, since what each command does with that backend does.

    `login` deliberately does NOT use this: its `--backend-url` is required
    rather than defaulted (see that command's own comment).
    """
    return click.option(
        "--backend-url",
        default=_DEFAULT_BACKEND_URL,
        show_default=True,
        envvar=_BACKEND_URL_ENVVAR,
        help=f"{help_text}  [env var: {_BACKEND_URL_ENVVAR}]",
    )


@click.group()
@click.version_option(version=__version__, prog_name="vaultshield")
def cli() -> None:
    """VaultShield: sandboxed, monitored AI coding agent runs."""


@cli.command()
def init() -> None:
    """Detect the project's framework and write a `.vaultshield.yml` config.

    Inspects the current directory (the invocation directory) for
    recognized framework signatures. On a match, writes the detected
    `start_command`/`port`/`ready_path` plus placeholder `agent`/`routes`/
    `off_limits_paths` fields and confirms the written path. On no match,
    writes an empty-fields config and instructs the developer to fill them
    in manually (Requirement 1.3).
    """
    repo_root = Path.cwd()
    detected = detect_framework(repo_root)
    config_path = write_config(repo_root, detected)

    if detected.get("start_command"):
        click.echo(f"Detected project configuration. Wrote {config_path}")
    else:
        click.echo(
            f"Wrote {config_path}, but no recognized framework signature was "
            "found. Please fill in `start_command`, `port`, and `ready_path` "
            "manually."
        )


# `login` (Task 18.1, Requirements 14.1 and 27.1): the whole command is
# `uploader.login` plus error mapping, so the reasoning that isn't about
# Click lives in `uploader.py`'s module docstring rather than here.
#
# `--username` is an option; the password is not, and never will be — an
# option value lands in shell history and in the process list, so
# `uploader.login` prompts for it (see its `_resolve_credentials`). Passing
# neither is the ordinary interactive case: both are prompted for.
#
# `--backend-url` is required rather than defaulted, matching `run`'s
# `--repo-url`: nothing in `.vaultshield.yml` carries a backend URL, and
# guessing `http://localhost:8000` would silently log a developer into the
# wrong dashboard whenever the real one is elsewhere.
@cli.command()
@click.option(
    "--backend-url",
    required=True,
    help="VaultShield backend origin, e.g. https://vaultshield.example.com",
)
@click.option(
    "--username",
    default=None,
    help="Dashboard username  [default: prompt]",
)
def login(backend_url: str, username: str | None) -> None:
    """Obtain a dashboard auth token and store it at `~/.vaultshield/token`.

    Prompts for the dashboard username (unless `--username` is given) and
    password, exchanges them for a Dashboard_Auth_Token at
    `POST <backend-url>/api/auth/login/`, and stores that token in a file of
    its own, readable only by you (mode 600). The token is kept entirely
    separately from any git or repository-host credential.

    Nothing is written unless the login succeeds, so a failed attempt leaves
    an existing token untouched.
    """
    try:
        uploader.login(backend_url, username=username)
    except uploader.UploadError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Dashboard auth token stored at {uploader.token_path()}")


# `run`: the full per-Run pipeline (Task 8.2, Requirements 7.1, 7.2, 7.3).
# `run`'s own docstring is deliberately short — Click prints it verbatim as
# `vaultshield run --help` — so the reasoning behind the wiring lives here
# instead.
#
# Stage order follows design.md's CLI-pipeline flowchart exactly:
# Config_Module → Credentials_Module → Sandbox_Module → Agent_Adapter →
# Monitor → Browser_Capture_Module → teardown. Two orderings inside that
# are load-bearing:
#
# - The Run_Directory is created between Config_Module and
#   Credentials_Module, because the generated Sandbox_Credential_File is
#   written INSIDE it. A `ConfigError` therefore stops the Run before
#   anything at all exists on disk, matching design.md's "both stop
#   `run`/`submit`/`teardown` before any side effect".
# - The Agent_Adapter is instantiated BEFORE `Sandbox.create`, per
#   `adapters/base.py`'s contract that an invocation-construction
#   `AdapterError` is raised before any Sandbox exists, so no container is
#   ever created for a Run that could never start.
#
# Browser_Capture_Module is wired in below as step 7 (Task 11.5,
# Requirement 9.1): `start_and_capture` runs immediately after
# `Monitor.supervise` returns — i.e. after agent execution has completed,
# which is the "starts after agent execution" of Requirement 9.1 — and
# before teardown, because the application it starts lives inside the
# Sandbox. Two consequences of putting it inside the same `try` as the
# Monitor:
#
# - `Sandbox.create` is now called with `publish_port=config.port`, which is
#   what lets `browser_capture.resolve_base_url` find a host-side origin to
#   poll and to screenshot from. Without it that module falls back to the
#   container's IP, which is unreachable through Docker Desktop's VM on
#   macOS/Windows.
# - A `ReadinessTimeout` or a `ScreenshotError` fails the Run through the
#   same `finally` as every other failure, so the container is torn down
#   either way (design.md's Error Handling section: on timeout "zero
#   screenshots are written and the Run is marked failed rather than
#   partially screenshotted" — marked failed here means a non-zero exit and
#   a one-line message, plus the best-effort `failed` mark on the backend
#   that the outer handler makes; see "Registering the Run" below).
#
# What the screenshots actually show is limited by judgment call 2 below:
# the application starts from the Sandbox's own clone, so with an adapter
# that writes on the host (every adapter in the dispatch table:
# `Mock_Adapter`, `Codex_Adapter`, `Kiro_Adapter`, `Antigravity_Adapter`)
# the captured UI is the *pre-agent* state of the repository. That
# is the same unowned gap judgment call 2 names, surfacing in a second
# place rather than a new problem introduced here.
#
# Git branch + diff capture is wired in below as step 8 (Task 12.4,
# Requirements 10.1 and 10.2): `commit_run_branch` runs after browser
# capture and before teardown, because both of its git commands run inside
# the Sandbox's clone — once the container is gone there is nothing left to
# commit or diff. Browser capture comes first for the same reason it comes
# after the Monitor: the screenshots are of the application as the agent
# left it, and the capture commit does not change what is on disk.
#
# `build_structured_diff` needs a base ref, and design.md does not say which
# one. This pipeline asks `git_capture.resolve_diff_base` for it, which
# returns `<branch>~1` — the capture commit's first parent. `commit_run_branch`
# always adds exactly one commit (`--allow-empty`, so even a no-change Run
# has one), so that parent IS the commit the Sandbox's clone was sitting on
# before the agent touched it — the pre-agent state, named without having to
# know whether the repository's default branch is `main`, `master`, or
# something else. The one shape `~1` cannot describe is a capture commit that
# is itself a root commit, which happens when the Sandbox's clone has no
# commits at all (an empty repository: `checkout -b` works on an unborn
# branch, and the capture commit becomes the root). A one-commit clone is
# fine — its Run branch has two commits. For the root case
# `resolve_diff_base` returns git's empty tree instead, so every file the
# agent wrote is reported as added (Requirement 10.2) rather than the Run
# failing on an unknown revision. The `rev-parse`/`hash-object` calls live in
# `git_capture.py`, which deliberately owns every git command.
#
# What the diff actually contains is bounded by judgment call 2 below, the
# same way the screenshots are: the current adapters write on the host, so
# the Sandbox's clone has no agent changes to commit and the captured diff
# is empty. That is the unowned gap named in judgment call 2 surfacing in a
# third place, not a new problem introduced here.
#
# The Uploader is wired in below as step 10 (Task 18.5, Requirements 14.2
# and 14.3): once the local Run is complete, `uploader.upload_run` POSTs the
# Run's Event_Log, diff, and screenshots to `--backend-url` with the
# Dashboard_Auth_Token step 2b already read (`uploader.load_token`, from what
# `vaultshield login` stored). Three things about where it sits:
#
# - AFTER the `finally` that tears the Sandbox down, exactly where
#   design.md's flowchart puts it ("Sandbox_Module: teardown → Uploader:
#   POST artifacts to backend"). The upload reads only files under
#   `run_dir`, so it needs nothing from the container, and holding a
#   container open across a network upload would leak one for as long as the
#   backend is slow.
# - Outside the inner `try` (it is inside the outer one that marks the Run
#   failed, see "Registering the Run" below), so an upload failure cannot be
#   mistaken for one of the Run's own failures and so `sandbox.teardown()`
#   has already run and already printed by the time the upload is attempted.
# - In `run` itself rather than in any adapter path, so it happens for every
#   entry in `_AGENT_ADAPTERS` — mock, codex, kiro, and antigravity —
#   without any of those adapters' tasks having to remember to wire an
#   upload.
#
# Requirement 14.4 is what shapes its failure handling, in
# `_upload_run_artifacts` below: a Run whose artifacts were captured and
# whose upload then failed is reported as an upload failure and its
# Run_Directory is left exactly as the Run wrote it, so the same artifacts
# can be re-uploaded. `upload_run` guarantees the "not mutated" half by
# never writing under `run_dir` at all (uploader.py's own docstring); this
# module's half is to report it and to not clean anything up on the way out
# — which is why nothing follows the upload except step 11's completion
# PATCH, which touches the backend's Run row and never the Run_Directory.
#
# Four judgment calls, none of them settled by design.md:
#
# 1. `--repo-url` is a required option. `Sandbox.create(repo_url, ...)`
#    needs a repository to clone, and neither `.vaultshield.yml` nor
#    `ProjectConfig` carries one — design.md puts `repo_url` on the
#    backend's `Project` model (Task 14.2), which does not exist yet and
#    which the CLI would not consult at Run time anyway. A required option
#    fails fast through Click's own validation, rather than guessing at a
#    remote: resolving the repository's `origin` would put a git dependency
#    in this task, and git is Task 12's concern.
#
# 2. The Monitor watches the host repository root — the same directory
#    `.vaultshield.yml` was loaded from — and that path is also what the
#    adapter receives as its `workdir`. `Mock_Adapter` runs in-process on
#    the host and spawns nothing (see `adapters/mock.py`), so the host
#    repository root IS its working directory. `Codex_Adapter` (wired in
#    at Task 10.5) is the case that comment anticipated: it spawns a real
#    `codex exec` subprocess, and with `workdir` set to the host
#    repository root that process runs on the HOST, not inside the
#    Sandbox this pipeline created. `Kiro_Adapter` (wired in at Task 19.5)
#    is the same shape — a real `kiro-cli chat --no-interactive`
#    subprocess, spawned with this `workdir`, so on the host — and
#    `Antigravity_Adapter` (wired in at Task 20.4) is a third instance of
#    it, a real `agy -p "<prompt>" --non-interactive` subprocess spawned
#    the same way, which makes this gap a property of every real-CLI
#    adapter rather than of Codex alone. Routing an adapter's process
#    through `Sandbox.exec` (and making its writes visible to this watcher
#    via a bind mount, or watching from inside the container) is a real
#    gap, stated here rather than papered over: no task in the plan owns
#    it, and Tasks 10.5, 19.5 and 20.4 are dispatch-table wiring only.
#
# 3. A missing default source env file is not an error. With no
#    `--env-file` given and no `<repo_root>/.env` present, an empty
#    Sandbox_Credential_File is written and mounted — Requirement 7.1 asks
#    for the file to be injected, and a repository with no local
#    credentials has no keys to mirror into it. An explicitly passed
#    `--env-file` that does not exist IS an error, since that can only be a
#    mistake (see `_build_run_credential_file`).
#
# 4. A failed upload exits non-zero. Requirement 14.4 asks for the failure
#    to be *reported*, not for the Run to be called a success with a
#    warning: the artifacts only exist to be reviewed in the dashboard, so
#    "captured but nowhere near a reviewer" is not a finished Run, and a CI
#    job that shell-exits on `vaultshield run` should notice. It is reported
#    the same way every other failure in this module is — one line, no
#    traceback — and the message names the Run_Directory the artifacts are
#    still sitting in, so the retry is obvious. Everything the Run captured
#    has already been echoed by then, so a non-zero exit costs no
#    information about what was produced.
#
# Registering the Run (step 2b) and closing it (step 11). The artifact
# endpoints are nested under a Run the backend already knows
# (`/api/runs/<run_id>/...`, addressed by `RunViewSet.lookup_field =
# "run_id"`). This used to be a named gap: nothing created that row, so
# against a backend that had never seen the `run_id` the upload failed with a
# `404`. It is closed in three places:
#
# - Step 1 resolves the project `Run.project` needs (`_resolve_project`:
#   `--project` > `.vaultshield.yml`'s `project:` > a one-line error), so "no
#   project" stops the Run before the Run_Directory exists, like a
#   `ConfigError`.
# - Step 2b, right after the Run_Directory exists and before any Docker call,
#   loads the token and POSTs the Run (`uploader.create_run`). A missing
#   token, an unreachable backend, a 401, or a refused project or `run_id`
#   aborts with one line ending "(no Sandbox was created)". The empty
#   Run_Directory step 2 made is left behind.
# - Everything after 2b runs inside one outer `try`/`except BaseException`.
#   On success, step 11 PATCHes `awaiting_review` after the last artifact
#   (`_complete_run`). On any failure (a stage's ClickException, a failed
#   upload or completion, `KeyboardInterrupt`, Click's `Abort` at the
#   sign-in code prompt) the inner `finally` has already torn the Sandbox
#   down; the handler then makes exactly one best-effort `failed` PATCH
#   (`_mark_run_failed_best_effort`) and re-raises the original exception
#   unchanged. The failed mark never raises and adds at most one `Warning:`
#   line on stderr; the artifacts stay on disk exactly as before. Because
#   registration comes before step 3, a local preflight failure after it
#   (`--env-file not found`, Docker not running) also leaves a `failed` Run
#   in the dashboard. `failed` is terminal for the CLI: the backend accepts
#   `awaiting_review` only from `running`.
#
# Residual cases that still leave a Run `running`, from where the handler
# sits rather than by oversight: a CLI that is SIGKILLed or loses power runs
# no Python at all; and a `create_run` whose response was lost after the
# backend committed (a timeout), or a Ctrl-C during step 2b, happens before
# the outer handler is entered, so nothing marks that Run failed. A
# server-side reaper for stale `running` Runs is a follow-up.
#
# One observed wrinkle of watching the repository root (judgment call 2):
# the Run_Directory sits inside the watched tree, so VaultShield's own
# writes there are in principle visible to the file-change watcher. In
# practice only the credential file's atomic-write temp file can be — every
# other artifact is written after the watcher has stopped — and only on
# macOS, where FSEvents can deliver a notification for an operation that
# completed just before the watch was established; Linux inotify, which
# production Sandboxes run on, never delivers pre-watch events. The result
# is at most two extra `file_change` records naming a `..env.sandbox.*` temp
# path on a dev machine: log noise, not a wrong verdict, and filtering the
# watched tree is `monitor.py`'s concern rather than this pipeline's.
#
# Failure reporting: `ConfigError`, `SandboxError`, an adapter's own
# `AdapterError`, an `OffLimitsViolation` (Requirement 4.5's failed Run),
# a `ReadinessTimeout`/`ScreenshotError` from browser capture, a
# `GitCaptureError` from step 8, the `UploadError` steps 2b, 10 and 11 raise
# for a missing token or an unreachable backend, and the `OSError` the Monitor
# raises when a file-change watch could not be established are each
# surfaced as a `click.ClickException` — a one-line message rather than a
# traceback.
# Everything after `supervise` returns (both browser-capture failures and
# every git-capture failure) happens with the Event_Log already on disk, so
# those messages name the Run_Directory the evidence survives in.
# `ReadinessTimeout` and `ScreenshotError` are handled separately rather
# than as one clause because design.md requires them to stay
# distinguishable: the first says the agent's application never came up, the
# second says it came up and answered a 200 and only the visual capture
# failed — so the Run's Event_Log and diff are still worth reviewing.
#
# Two of step 8's failure modes deliberately have no clause of their own.
# A Docker-level failure during git capture reaches the existing
# `SandboxError` clause, because `git_capture.py` propagates `SandboxError`
# unchanged rather than rewrapping it — "Docker is broken" reads the same
# here as it does at `create`. And a failure writing `diff.json` is an
# `OSError`, which the existing clause already covers with a message that
# stays neutral about which filesystem operation failed.
@cli.command()
@click.option(
    "--agent",
    type=click.Choice(sorted(_AGENT_ADAPTERS)),
    required=True,
    help="Agent_Adapter to launch, selected from the adapter dispatch table.",
)
@click.option(
    "--prompt",
    default=None,
    help=(
        "Prompt passed to the selected Agent_Adapter's launch(prompt, workdir). "
        "Give exactly one of --prompt or --ask."
    ),
)
@click.option(
    "--ask",
    default=None,
    help=(
        "Instead of --prompt: build the agent prompt with the VaultShield "
        "orchestrator from this task description. It scans the current directory "
        "as the project (run it from a checkout of the repo, as with `submit`), "
        "asks a few clarifying questions, and uses the enriched result as the "
        "prompt. Give exactly one of --prompt or --ask."
    ),
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="With --ask: skip the clarifying questions (for scripts).",
)
@click.option(
    "--answers",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "With --ask: read answers from a JSON file, {\"question id\": \"answer\"}, "
        "instead of asking."
    ),
)
@click.option(
    "--repo-url",
    required=True,
    help="Repository the Sandbox clones for this Run.",
)
@click.option(
    "--base-image",
    default=_DEFAULT_BASE_IMAGE,
    show_default=True,
    help="Docker image the Sandbox builds/reuses for this Run.",
)
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Source environment file whose keys are mirrored into the "
        "Sandbox_Credential_File with dummy values  [default: "
        f"{_DEFAULT_SOURCE_ENV_FILENAME}]"
    ),
)
@click.option(
    "--ready-timeout",
    type=click.IntRange(min=0),
    default=DEFAULT_READY_TIMEOUT_S,
    show_default=True,
    help=(
        "Seconds to wait for the application's `ready_path` to answer 200 "
        "before failing the Run with no screenshots."
    ),
)
@click.option(
    "--interactive-auth",
    is_flag=True,
    default=False,
    help=(
        "Sign the agent in to its own service inside the Sandbox before the Run, "
        "by completing an OAuth exchange in your terminal. Only Antigravity needs "
        "this. The session is created in the container and destroyed with it; your "
        "host login is never read."
    ),
)
@click.option(
    "--project",
    default=None,
    help=(
        "Backend Project id this Run belongs to  [default: `project` in "
        ".vaultshield.yml]"
    ),
)
@click.option(
    "--egress",
    "egress_mode",
    type=click.Choice(["off", "log-only", "allowlist"]),
    default=None,
    help=(
        "Egress policy for the Sandbox after clone and install: `off` (no "
        "filtering), `log-only` (record unlisted hosts), `allowlist` (only the "
        "agent's allowlisted hosts)  [default: `egress.mode` in .vaultshield.yml, "
        "else off]"
    ),
)
@_backend_url_option("VaultShield backend this Run's artifacts are uploaded to")
def run(
    agent: str,
    prompt: str | None,
    ask: str | None,
    repo_url: str,
    base_image: str,
    env_file: Path | None,
    ready_timeout: int,
    interactive_auth: bool,
    project: str | None,
    egress_mode: str | None,
    yes: bool,
    answers: Path | None,
    backend_url: str,
) -> None:
    """Execute one sandboxed, monitored Run and write its Event_Log.

    Runs the per-Run pipeline in design.md's order, writing the Run's
    artifacts into `.vaultshield/runs/<run_id>/`:

    \b
      1. Config_Module      load the existing `.vaultshield.yml`, resolve
                            the project (`--project` or `project:`)
      2. Run_Directory      generate a run_id, create its directory
     2b. Backend            register the Run (POST /api/runs/); abort if
                            unreachable, before any Sandbox exists
      3. Credentials_Module build `.env.sandbox` (dummy values only)
      4. Agent_Adapter      resolve `--agent` (before any Sandbox exists)
     5a. Egress             with `--egress`/`egress.mode` not off: per-Run
                            networks and the filtering proxy
      5. Sandbox_Module     create a Sandbox, mount the credential file;
                            then (egress on) switch it to the internal
                            network
 5-base. Trusted base       record the pre-agent commit and objects
                            (Docker API only)
     5c. Token hygiene      snapshot the agent's credential (after sign-in)
      6. Monitor            run the agent under PTY + file-change capture,
                            write the merged Event_Log
      7. Browser_Capture    start the app in the Sandbox, poll `ready_path`,
                            screenshot each configured route
      8. Git capture        stop the Sandbox, rebuild its repo in a trusted
                            capture helper, commit to
                            `vaultshield/run-<run_id>`, write `diff.json`,
                            scan outputs for the agent's credential
      9. Teardown           tear the Sandbox down, on success or failure
     10. Uploader           POST the Event_Log, diff, and screenshots to
                            `--backend-url` with the token from
                            `vaultshield login`
     11. Backend            mark the Run awaiting_review; on any failure
                            after 2b the Run is marked failed

    The agent prompt comes from exactly one of `--prompt` (a literal prompt,
    as before) or `--ask "<task>"`. With `--ask`, the VaultShield orchestrator
    runs first — at the very start, before anything is created on disk — and
    builds an enriched prompt from the current directory's project context and
    your answers (reuses `vaultshield ask`; `--yes` and `--answers` behave as
    they do there). The enriched prompt is saved under `.vaultshield/context/`
    and copied into the Run_Directory as `prompt.md`.

    Requires a running Docker daemon, a reachable backend, and a
    `vaultshield login` before anything runs. Every failure is reported as a
    one-line error; when the Event_Log has already been written, the message
    names the Run_Directory it was preserved in. A failed upload leaves the
    Run_Directory exactly as the Run wrote it, so the artifacts stay on disk.
    """
    # The agent prompt comes from exactly one of `--prompt` or `--ask`. This is
    # an argument-only check, so it stops before any config read or side effect.
    if (prompt is None) == (ask is None):
        raise click.UsageError("Give exactly one of --prompt or --ask.")

    repo_root = Path.cwd()

    # 1. Config_Module. Before anything else, and before anything is
    #    created on disk — design.md: "both stop `run`/`submit`/`teardown`
    #    before any side effect".
    try:
        config = load_config(repo_root)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    # Still step 1, so "no project" also stops the Run before anything exists
    # on disk.
    project_id = _resolve_project(project, config)
    # Still step 1 (Batch 3 FR3): an invalid egress policy stops the Run here.
    policy = _resolve_egress(agent, config, egress_mode)

    # 1b. Orchestrator (--ask): build the enriched agent prompt from the
    #     project (the current directory) and the developer's answers, at the
    #     very start — before the run id, the Run_Directory, the backend
    #     registration, or any Docker call exist. A missing model key, a bad
    #     config, an unreadable project, or a leaked secret stops here with
    #     nothing created; a Ctrl-C during the questions likewise leaves nothing
    #     behind. With `--prompt`, this is skipped and the literal prompt is used
    #     exactly as before.
    built = None
    if ask is not None:
        built = run_orchestrator(ask, skip_questions=yes, answers_file=answers)
        prompt = built.text

    # 2. Run_Directory (Task 8.1). `generate_run_id`'s output is always a
    #    valid single path component, so `create_run_dir` cannot raise
    #    `RunDirectoryError` here.
    run_id = generate_run_id()
    run_dir = create_run_dir(repo_root, run_id)
    click.echo(f"Run {run_id}: artifacts in {run_dir}")

    # 2b. Backend: register the Run before any Sandbox exists, so the upload
    #     at the end has a Run to address and a failure in between can be
    #     recorded against it. Unreachable, unauthenticated, or refused: abort
    #     here, with no container created (see "Registering the Run" above).
    try:
        token = uploader.load_token()
        uploader.create_run(
            backend_url, token, project=project_id, run_id=run_id, agent=agent
        )
    except uploader.UploadError as exc:
        # Parenthesised suffix: several UploadError texts end in an httpx message
        # with no full stop ("…: All connection attempts failed").
        raise click.ClickException(
            f"Cannot start run {run_id}: {exc} (no Sandbox was created)"
        ) from exc
    click.echo(f"Run {run_id} registered with {backend_url}.")
    # Everything from here on runs with a registered Run, so any failure,
    # `KeyboardInterrupt` included, marks it `failed` exactly once, after the
    # inner `finally` has torn the Sandbox down, and is then re-raised
    # unchanged. Nothing between `create_run` returning and this `try` can
    # raise except `click.echo`.
    #
    # The Run's credential needles (Batch 3 FR5), in host memory only.
    secrets = SecretSet()
    try:
        # 3. Credentials_Module: dummy values only, never a real credential.
        sandbox_env = _build_run_credential_file(repo_root, run_dir, env_file)

        # 4. Agent_Adapter, instantiated before any Sandbox exists.
        adapter = _AGENT_ADAPTERS[agent]()

        # 5. Sandbox_Module.
        try:
            client = docker.from_env()
        except docker.errors.DockerException as exc:
            raise click.ClickException(
                f"Cannot reach the Docker daemon: {exc}. `vaultshield run` needs "
                "Docker running locally to create a Sandbox."
            ) from exc

        sandbox = Sandbox(run_id, client)
        # Bound before the inner `try` and makes no Docker call, so its
        # teardown in the `finally` is always safe (Batch 3 FR6).
        guard = EgressGuard(run_id, client, policy, app_port=config.port)
        base = None
        caps = TrustedCaptureCaps()

        try:
            # 5a. Egress (Batch 3 FR2): networks + proxy, before the Sandbox.
            #     A no-op with egress off.
            guard.start()

            # `publish_port` is what makes step 7 reachable from the host: it
            # publishes `config.port` on an ephemeral host port that
            # `browser_capture.resolve_base_url` reads back off the container.
            # With egress on, the proxy publishes it instead (D4).
            sandbox.create(
                repo_url,
                base_image,
                sandbox_env,
                publish_port=None if guard.active else config.port,
                # Only moves where dependencies are installed. The clone still
                # lands at Sandbox.REPO_DIR, so git capture is unaffected.
                project_dir=config.project_dir,
                **guard.sandbox_create_kwargs(),
            )
            click.echo(f"Sandbox created for run {run_id} (cloned {repo_url}).")
            # Still step 5: clone and install had network; from here on the
            # Sandbox reaches only the proxy (D3). A no-op with egress off.
            guard.isolate(sandbox)
            if guard.active:
                click.echo(
                    f"Sandbox for run {run_id} isolated: egress {policy.mode} for "
                    f"agent {agent}."
                )

            # 5-base (Batch 3 FR5, trusted capture): the pre-agent base commit
            #     and objects, read through the Docker API before any agent
            #     code — the sign-in included — runs.
            base = record_trusted_base(sandbox, caps=caps)

            # 5b. Optional in-Sandbox sign-in (`--interactive-auth`). Between the
            #     Sandbox existing and the agent starting, because the agent cannot
            #     start unauthenticated and the session has to live in the container
            #     that is about to run it. Skipped entirely without the flag, so the
            #     default behaviour of every agent is unchanged.
            if interactive_auth:
                _authenticate_in_sandbox(sandbox, adapter, agent)

            # 5c. Credential snapshot #1 (Batch 3 FR5): after sign-in, before
            #     the agent; it fixes the credential file set.
            snap1 = _take_snapshot(sandbox, adapter, secrets, run_dir, None)

            # 6. Monitor, INSIDE the Sandbox. The agent runs as a real process in
            #    the container, with the container's clone as its working
            #    directory; both event producers observe that container (its pty
            #    for terminal output, an in-container watcher for file changes)
            #    and the merged Event_Log is written to the host Run_Directory.
            #
            #    Deliberately NOT `Monitor(repo_root, ...).supervise(...)`. That
            #    ran the agent in this process against `repo_root` — the
            #    developer's own checkout — which meant the agent could read the
            #    real `.env` next to it, its writes landed on the host, and step 8
            #    below diffed a container clone nothing had touched, producing an
            #    empty `diff.json`. `repo_root` is now used only to locate the
            #    config and the Run_Directory; it is never the agent's workdir.
            log_path = supervise_in_sandbox(
                sandbox,
                adapter,
                prompt,
                run_dir,
                off_limits_paths=config.off_limits_paths,
                # The agent starts in the application root when the repository
                # nests it. The file-change watcher stays rooted at the repository
                # either way, so `off_limits_paths` keeps its repository-relative
                # meaning and changes outside the app directory are still captured
                # — see `supervise_in_sandbox`'s `agent_workdir`.
                agent_workdir=container_path_under_repo(config.project_dir),
            )
            # Credential snapshot #2a: a token the agent refreshed.
            _take_snapshot(sandbox, adapter, secrets, run_dir, snap1.credential_files)

            # 7. Browser_Capture_Module (Requirement 9.1): agent execution has
            #    completed — `supervise` returned — so the application is
            #    started inside the still-running Sandbox from `start_command`
            #    and `port`, `ready_path` is polled, and each configured route
            #    is screenshotted into the Run_Directory.
            # With egress on, the proxy publishes the app port (D4); with it
            # off the call is exactly as before.
            capture_kwargs = (
                {"published_by": guard.port_publisher}
                if guard.port_publisher is not None
                else {}
            )
            screenshots = start_and_capture(
                sandbox, config, run_dir, ready_timeout, **capture_kwargs
            )
            _report_screenshots(screenshots, run_dir)

            # 8. Git branch + diff capture (Requirements 10.1, 10.2): agent
            #    execution has completed, so everything the agent left in the
            #    Sandbox's clone is committed to `vaultshield/run-<run_id>` and
            #    the per-file addition/deletion counts between that commit's
            #    parent (or the empty tree, for a root capture commit) and the
            #    branch are written to `<run_dir>/diff.json`.
            #
            #    Batch 3 FR5 (trusted capture): none of it runs in the Sandbox.
            #    The Sandbox is killed first (Docker API), the authoritative
            #    credential snapshot #2 is taken, and the branch is built by git
            #    in a fresh capture helper from the 5-base objects plus a
            #    filtered archive of the work tree. Then the outputs are scanned
            #    for the agent's credential before anything is uploaded.
            kill_sandbox(sandbox)
            _take_snapshot(sandbox, adapter, secrets, run_dir, snap1.credential_files)
            capture = TrustedCapture(run_id, client, image_id=sandbox_image_id(sandbox))
            split = None
            try:
                capture.start()
                split = archive_sandbox_repo(sandbox, caps=caps)
                if split.notices:
                    _append_event_log(
                        event_log_path(run_dir), notices_to_events(split.notices)
                    )
                capture.load(split, base)
                branch = commit_run_branch(capture, run_id)
                base_ref = resolve_diff_base(capture, branch)
                diff_file = build_structured_diff(capture, base_ref, branch, run_dir)
                check_run_outputs(capture, secrets, run_dir, branch, base_ref)
                # Only now that token hygiene has passed (and the diff was not
                # quarantined) export the branch as a `git bundle`, so `submit`
                # can push it from a checkout that never saw this helper. Done
                # before `capture.remove()` — the helper holds the only copy of
                # the Run's commit objects.
                bundle = export_run_bundle(
                    capture, run_id, base_ref, branch, run_dir, repo_url, caps=caps
                )
            finally:
                capture.remove()
                if split is not None:
                    split.close()
            click.echo(
                f"Committed run {run_id} to branch {branch} in the capture helper; "
                f"structured diff written to {diff_file}"
            )
            if bundle is None:
                click.echo(
                    f"Run {run_id}: branch {branch} adds no commit beyond its base, "
                    "so no branch bundle was written."
                )
            else:
                click.echo(
                    f"Run {run_id}: branch bundle written to {bundle.path} "
                    f"({bundle.bundle_bytes} bytes)."
                )
        except EgressError as exc:
            # Batch 3 FR2: the topology, the proxy or the isolation check
            # failed. Includes `EgressConfigError`, which step 1 already turns
            # into its own message, so this is the Docker-side half.
            raise click.ClickException(f"Run {run_id} failed: {exc}") from exc
        except (TokenHygieneViolation, UnsafeGitConfig) as exc:
            # Batch 3 FR5: credential material would have left the machine, or
            # the repository's git setup could subvert capture. Before the
            # `GitCaptureError` clause, because `UnsafeGitConfig` is one. The
            # message never carries the matched bytes.
            if isinstance(exc, UnsafeGitConfig):
                _record_unsafe_git_config_best_effort(run_dir)
            raise click.ClickException(
                f"Run {run_id} failed: {exc} {_event_log_suffix(run_dir)}"
            ) from exc
        except AuthError as exc:
            # The Sandbox exists and the repo is cloned, but the agent was never
            # started, so unlike the `AdapterError` clause below there is no
            # Event_Log to name. The `finally` still tears the Sandbox down, which
            # destroys any partial session with it.
            raise click.ClickException(f"Run {run_id} failed: {exc}") from exc
        except SandboxError as exc:
            raise click.ClickException(str(exc)) from exc
        except SandboxMonitorError as exc:
            # The Sandbox and the agent were fine, but the Monitor could not
            # establish or read its own in-container file-change watch. Reported
            # separately from `SandboxError` because the fix is different: this
            # usually means the Sandbox image has no `python` on PATH, so nothing
            # could observe the agent's file changes. Failing is correct — an
            # unobserved Run looks like a clean Run that changed nothing.
            raise click.ClickException(f"Run {run_id} failed: {exc}") from exc
        except (AdapterError, OffLimitsViolation) as exc:
            # `supervise` raises both of these only after the Event_Log has
            # been written, so the Run failed but its evidence survives.
            raise click.ClickException(
                f"Run {run_id} failed: {exc} "
                f"(Event_Log preserved at {event_log_path(run_dir)})"
            ) from exc
        except ReadinessTimeout as exc:
            # Requirement 9.4: a startup timeout is reported as a failed Run
            # with zero screenshots — `start_and_capture` raises before it
            # touches `run_dir`, so there is no partial set to mention.
            raise click.ClickException(
                f"Run {run_id} failed: {exc}. No screenshots were captured "
                f"(Event_Log preserved at {event_log_path(run_dir)})"
            ) from exc
        except ScreenshotError as exc:
            # The application did come up (a 200 was observed); only the visual
            # capture failed. Whatever was written before the failure is left in
            # place and named, since partial visual evidence still helps a
            # reviewer.
            raise click.ClickException(
                f"Run {run_id} failed: {exc} "
                f"({len(exc.saved)} screenshot(s) kept, Event_Log preserved at "
                f"{event_log_path(run_dir)})"
            ) from exc
        except GitCaptureError as exc:
            # A git command that ran and failed: the branch already exists, the
            # clone is not a git repository, git refused the commit, the base
            # ref does not resolve (see the root-commit note above),
            # `--numstat` output could not be parsed, or the diff's metadata
            # alone is over the upload cap. The Event_Log and any
            # screenshots are already on disk, so the message names where the
            # Run's evidence survives even though its diff was never written.
            # A trusted-capture failure at 5-base or 5c happens before the
            # agent ran, so the suffix only names an Event_Log that exists.
            raise click.ClickException(
                f"Run {run_id} failed: {exc} {_event_log_suffix(run_dir)}"
            ) from exc
        except OSError as exc:
            # Most likely the Monitor could not establish its file-change
            # watch, in which case the adapter was never launched at all
            # (Requirement 6.2); any other filesystem-level failure inside
            # `supervise` (e.g. writing the Event_Log) or in step 8's
            # `diff.json` write lands here too, so the message stays neutral
            # about which one it was.
            raise click.ClickException(f"Run {run_id} failed: {exc}") from exc
        finally:
            # 9. Requirement 7.2: the Sandbox is torn down when `run`
            #    completes — on the success path and on every failure path
            #    alike, including a browser-capture or git-capture failure, so
            #    neither a timed-out application nor a refused commit ever
            #    leaves a container (or the dev server detached inside it)
            #    behind.
            try:
                sandbox.teardown()
                click.echo(f"Sandbox torn down for run {run_id}.")
            finally:
                # Batch 3 D10, each step best-effort and never raising: the
                # capture helper backstop (a no-op if step 8 removed it), the
                # 5-base object spool, then the egress proxy logs → violation
                # events, the proxy and both networks.
                _remove_capture_helper_best_effort(run_id, client)
                if base is not None:
                    base.close()
                guard.teardown(run_dir)

        click.echo(f"Run {run_id} complete. Event_Log written to {log_path}")

        # 10. Uploader (Requirements 14.2, 14.3): the local Run is complete and
        #     the Sandbox is gone, so the artifacts in the Run_Directory are
        #     final — package them and submit them to the backend, which is what
        #     makes this Run retrievable through `GET /api/runs/<run_id>/`.
        _upload_run_artifacts(run_dir, run_id, backend_url, token, secrets)

        # 11. Backend: every artifact is in, so the Run is ready for review.
        #     After the screenshots, not after the diff: the diff is not the
        #     last artifact, and a Run must not be approvable while its
        #     screenshots are still arriving.
        _complete_run(run_id, backend_url, token)
    except BaseException:
        # Batch 3 FR4 (D8): the failed Run's timeline — its off-limits, egress
        # and token-hygiene violations included — is uploaded scrubbed and
        # best-effort, after teardown and before the failed mark.
        _upload_events_best_effort(run_id, backend_url, token, run_dir, secrets)
        _mark_run_failed_best_effort(run_id, backend_url, token)
        raise


def _resolve_egress(agent: str, config: ProjectConfig, egress_mode: str | None):
    """Step 1's egress half (Batch 3 FR3): the effective policy, or a
    `ClickException` before anything exists on disk."""
    try:
        return egress.resolve_policy(
            agent, config.egress, egress_mode, app_port=config.port
        )
    except EgressConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _take_snapshot(sandbox, adapter, secrets: SecretSet, run_dir: Path, credential_files):
    """One credential snapshot (Batch 3 FR5, §5.5): needles merged into
    `secrets`, and one non-failing event when files were too large to scan.
    Errors propagate as `GitCaptureError`."""
    snap = token_hygiene.snapshot(
        sandbox, adapter.credential_paths(), credential_files=credential_files
    )
    secrets.add(snap.secrets)
    if snap.skipped > 0:
        _append_event_log(event_log_path(run_dir), [skipped_snapshot_event(snap.skipped)])
    return snap


def _event_log_suffix(run_dir: Path) -> str:
    """`(Event_Log preserved at …)` only when the Event_Log exists, else
    `(the agent never started)` (Batch 3, review round 4 finding 7)."""
    path = event_log_path(run_dir)
    if path.exists():
        return f"(Event_Log preserved at {path})"
    return "(the agent never started)"


def _record_unsafe_git_config_best_effort(run_dir: Path) -> None:
    """The `unsafe_git_config` violation event; never raises."""
    try:
        token_hygiene.record_unsafe_git_config(run_dir)
    except Exception:  # noqa: BLE001 - must not replace the error in flight
        pass


def _remove_capture_helper_best_effort(run_id: str, client) -> None:
    """The inner `finally`'s capture-helper backstop (Batch 3 D10). Never
    raises; at most one warning, exception type only."""
    try:
        trusted_capture.remove_capture_helper(run_id, client)
    except BaseException as exc:  # noqa: BLE001
        click.echo(
            f"Warning: could not remove the capture helper for run {run_id}: "
            f"{type(exc).__name__}",
            err=True,
        )


def _upload_events_best_effort(
    run_id: str, backend_url: str, token: str, run_dir: Path, secrets: SecretSet
) -> None:
    """Uploads a failed Run's Event_Log, scrubbed (Batch 3 FR4, D8). Skipped
    when there is no Event_Log; never raises; at most one warning line."""
    try:
        if not event_log_path(run_dir).exists():
            return
        warning = uploader.upload_events_best_effort(
            backend_url, token, run_id, run_dir, scrub=secrets.scrub
        )
    except BaseException as exc:  # noqa: BLE001
        warning = f"unexpected {type(exc).__name__}"
    if warning:
        try:
            click.echo(
                f"Warning: could not upload the timeline of run {run_id}: {warning}",
                err=True,
            )
        except BaseException:  # noqa: BLE001
            pass


def _authenticate_in_sandbox(sandbox: Sandbox, adapter, agent: str) -> None:
    """Runs the agent's in-Sandbox sign-in, reading the authorization code from
    the developer's terminal.

    Only reached for `--interactive-auth`. An adapter that needs no sign-in
    returns `None` from `auth_invocation()` and this is a no-op with a note, so
    passing the flag to `--agent=mock` is harmless rather than an error.

    ### What must not happen here, and how each is prevented

    The authorization code is a bearer of the developer's Google identity for as
    long as it is unredeemed, and the session it mints is readable by the agent.
    Five channels are closed deliberately:

    * **argv** — the code is written to the exec's stdin socket. It is never an
      argument, so it cannot appear in `docker inspect` or in a process list.
    * **environment** — `Sandbox.exec_interactive` takes no `env` parameter at
      all, so there is nowhere to put it even by mistake.
    * **the Event_Log** — this runs through `exec_interactive`, which no part of
      `sandbox_monitor` touches. The monitored path (`exec_stream`) has no stdin
      to record. The separation is structural, not a filter.
    * **the terminal** — read with `getpass`, so the host terminal does not echo
      it; and because a container TTY echoes stdin back into its own output, every
      line displayed from the container is scrubbed of the code before printing.
      `agy` needs the TTY (without one it prints no consent URL at all), so
      suppressing the echo at the display layer is the only available answer.
    * **exceptions** — `AuthError` messages below are fixed strings plus `agy`'s
      own output; the code is never interpolated into one.

    The consent banner prints BEFORE the exec starts, because `agy`'s wait for the
    code is 60 seconds and the developer should not be spending it reading.

    Raises:
        AuthError: the sign-in failed, timed out, or was refused. The caller's
            `finally` tears the Sandbox down either way.
    """
    invocation = adapter.auth_invocation()
    if invocation is None:
        click.echo(
            f"--interactive-auth was passed, but the {agent} adapter needs no "
            "in-Sandbox sign-in. Continuing without one."
        )
        return

    scopes = getattr(adapter, "OAUTH_SCOPES", None) or ANTIGRAVITY_OAUTH_SCOPES
    click.echo("")
    click.echo("──────────────── Antigravity authentication ────────────────")
    click.echo(
        "This authentication is created inside the disposable VaultShield\n"
        "sandbox. Your existing host Antigravity login is not read or modified."
    )
    click.echo("")
    click.echo("The sandboxed agent will have the following Google scopes:")
    for scope in scopes:
        click.echo(f"  {scope}")
    click.echo("")
    click.echo(
        "These scopes are broad, not narrow: `cloud-platform` is general Google\n"
        "Cloud authority and `userinfo.*` identifies your account. The agent can\n"
        "read its own authentication session while it runs, and VaultShield cannot\n"
        "prevent it from using that session. The session is destroyed when the\n"
        "Sandbox is torn down."
    )
    click.echo("")
    click.echo(
        "Open the authorization URL below in your browser, approve access, then\n"
        "paste the authorization code at the prompt."
    )
    click.echo("─────────────────────────────────────────────────────────────")

    # A neutral working directory: the sign-in has no business running in the
    # clone, so even a probe that misbehaved could not touch the repository.
    exec_handle = sandbox.exec_interactive(
        invocation.argv,
        workdir=Sandbox.WORKDIR,
        timeout_s=ANTIGRAVITY_AUTH_TIMEOUT_S,
    )

    asked = False
    try:
        # ONE pass over the output, with the code supplied from inside it. Reading
        # in two passes opens a second iterator over the same socket and loses the
        # exchange — measured against real `agy`, which then waited out its full
        # 60s for a code it never received. See `InteractiveExec.read_output`.
        for line in exec_handle.read_output():
            click.echo(line)
            if asked or _AUTH_PROMPT_MARKER not in line.lower():
                continue

            asked = True
            # Flushed before prompting so the consent URL is on screen first: a
            # non-TTY stdout is block-buffered while the prompt flushes
            # immediately, which would ask for a code before showing the URL to
            # get it from.
            sys.stdout.flush()

            code = click.prompt(
                "Authorization code", hide_input=True, prompt_suffix=": "
            ).strip()
            if not code:
                raise AuthError(
                    f"No authorization code was entered, so the {agent} agent "
                    "could not be signed in."
                )

            # Written through the SAME socket the loop above is reading, with no
            # settle delay. An earlier version slept here; that was compensating
            # for the two-pass read, not for anything real. Measured after the
            # single-pass fix: 4/4 deliveries with zero delay.
            #
            # Redaction arms itself inside `send_line`, so the TTY echo that
            # arrives on the next iteration is already covered.
            exec_handle.send_line(code)

        tail = exec_handle.flush_remaining()
        if tail.strip():
            click.echo(tail)

        if exec_handle.timed_out:
            raise AuthError(
                f"The {agent} sign-in produced no further output for "
                f"{ANTIGRAVITY_AUTH_TIMEOUT_S:.0f}s and was abandoned. Nothing "
                "was started."
            )

        if not asked:
            raise AuthError(
                f"The {agent} sign-in ended before asking for an authorization "
                "code. Its output is above."
            )

        exit_code = exec_handle.wait()
        if exit_code not in (0, None):
            raise AuthError(
                f"The {agent} sign-in failed inside the Sandbox (exit code "
                f"{exit_code}). Its output is above; the authorization code may "
                "have expired or been mistyped. Nothing was started."
            )
    except KeyboardInterrupt as exc:
        # Ctrl-C during a sign-in is a deliberate abort, and the `finally` in
        # `run` still tears the Sandbox down.
        raise AuthError(
            f"The {agent} sign-in was cancelled. No agent was started."
        ) from exc
    finally:
        exec_handle.close()

    click.echo("Authenticated inside the Sandbox. Starting monitored agent run...")
    click.echo("")


def _resolve_project(project: str | None, config: ProjectConfig) -> str:
    """The backend Project id this Run is registered under (Requirement
    12.2's "associated with an existing Project", client half).

    `--project` wins over `.vaultshield.yml`'s `project:`; each is stripped,
    and empty means absent. The id is not format-checked here: the backend
    answers an unknown or malformed one with a 400 that step 2b surfaces.
    Called in step 1, so a missing project stops the Run before the
    Run_Directory exists. Name-based lookup is deliberately not offered,
    because `Project.name` is not unique.

    Raises:
        click.ClickException: neither source names a project.
    """
    resolved = (project or "").strip() or config.project.strip()
    if not resolved:
        raise click.ClickException(
            "No project for this Run: pass --project <project-id> or set "
            "`project: <project-id>` in .vaultshield.yml (ids are listed by "
            "the dashboard / GET /api/projects/)."
        )
    return resolved


def _complete_run(run_id: str, backend_url: str, token: str) -> None:
    """Step 11: moves the Run from `running` to `awaiting_review` once every
    artifact is uploaded (Requirement 12.4's PATCH, client half).

    Raises:
        click.ClickException: `uploader.complete_run` raised `UploadError`.
            `run`'s outer handler then marks the Run `failed`, which the
            backend refuses with a 409 if the completion did land.
    """
    try:
        uploader.complete_run(backend_url, token, run_id)
    except uploader.UploadError as exc:
        raise click.ClickException(
            f"Run {run_id} was uploaded but could not be marked ready for "
            f"review: {exc}"
        ) from exc


def _mark_run_failed_best_effort(run_id: str, backend_url: str, token: str) -> None:
    """Marks the registered Run `failed` on the backend (Requirement 12.4's
    PATCH with a Requirement 11.2 status, client half); never raises and
    never replaces the error in flight.

    Called only from `run`'s outer `except BaseException`, after the inner
    `finally` has torn the Sandbox down, and immediately followed by a bare
    `raise`. `uploader.mark_run_failed` is already specified never to raise;
    this guard is a second layer, so a defect there (or a test stand-in that
    raises) still cannot replace the original error. Swallowing a
    `KeyboardInterrupt` that lands during the PATCH is deliberate: the
    caller's bare `raise` then re-raises the original exception, which is the
    one the developer needs to see. A failure is reported as one `Warning:`
    line on stderr and nothing else.
    """
    try:
        warning = uploader.mark_run_failed(backend_url, token, run_id)
    except BaseException as exc:  # incl. KeyboardInterrupt during the PATCH
        # No str(exc): keep it one line and token-free.
        warning = f"unexpected {type(exc).__name__}"
    if warning:
        try:
            click.echo(
                f"Warning: could not mark run {run_id} as failed on the backend: {warning}",
                err=True,
            )
        except Exception:
            pass


def _upload_run_artifacts(
    run_dir: Path, run_id: str, backend_url: str, token: str, secrets: SecretSet
) -> None:
    """Submits this Run's artifacts to `backend_url` with the
    Dashboard_Auth_Token step 2b loaded (Requirements 14.2, 14.3).

    `upload_run` raises `UploadError` for an unreachable backend, a non-2xx
    response, or an artifact that exists but cannot be read. Each means the
    same thing to the developer — the Run is on disk and is not in the
    dashboard — so one message covers them. The token is no longer loaded
    here: step 2b needs it to register the Run, so a missing `vaultshield
    login` stops the Run before any Sandbox exists instead of at the end.

    Requirement 14.4's preservation half needs no code here, which is the
    point: nothing in this function (or in `upload_run`) creates, moves,
    truncates, or deletes anything under `run_dir`, so there is no cleanup
    step to skip on failure. The message names the directory so the artifacts
    that survived are findable.
    """
    try:
        # Event payloads are scrubbed of credential needles (Batch 3 FR5).
        uploader.upload_run(run_dir, run_id, backend_url, token, scrub=secrets.scrub)
    except uploader.UploadError as exc:
        raise click.ClickException(
            f"Run {run_id} was captured but could not be uploaded: {exc} "
            f"(artifacts preserved at {run_dir}; upload them again once the "
            "cause is fixed)"
        ) from exc

    click.echo(f"Run {run_id} uploaded to {backend_url}")


def _report_screenshots(screenshots: list[Path], run_dir: Path) -> None:
    """Reports what step 7 captured: how many screenshots and where.

    An empty list is not a failure and is reported as its own line rather
    than silently: `config.routes` defaults to `[]` (which is what `init`
    writes as a placeholder), so a Run with no configured routes captures
    nothing and creates no `screenshots/` directory — worth saying out loud,
    since "no screenshots" otherwise looks like something went wrong.
    """
    if not screenshots:
        click.echo(
            "No routes configured in .vaultshield.yml, so no screenshots were "
            "captured. Add `routes` to capture the running application."
        )
        return

    click.echo(
        f"Captured {len(screenshots)} screenshot(s) into "
        f"{screenshots_dir(run_dir)}"
    )


def _build_run_credential_file(
    repo_root: Path, run_dir: Path, env_file: Path | None
) -> Path:
    """Builds this Run's Sandbox_Credential_File at
    `<run_dir>/.env.sandbox` and returns its path.

    `env_file` is `--env-file` as given (resolved against `repo_root` when
    relative) or `None` for the `<repo_root>/.env` default. A missing
    default source file yields an empty credential file, while a missing
    explicitly-passed one is an error — see judgment call 3 in the comment
    block above `run` for why.
    """
    dest = run_dir / _SANDBOX_ENV_FILENAME

    if env_file is None:
        source = repo_root / _DEFAULT_SOURCE_ENV_FILENAME
        if not source.is_file():
            dest.write_text("")
            click.echo(
                f"No {_DEFAULT_SOURCE_ENV_FILENAME} in {repo_root}; mounting an "
                f"empty Sandbox_Credential_File at {dest}."
            )
            return dest
    else:
        source = env_file if env_file.is_absolute() else repo_root / env_file
        if not source.is_file():
            raise click.ClickException(f"--env-file not found: {source}")

    try:
        build_sandbox_env(source, dest)
    except OSError as exc:
        raise click.ClickException(
            f"Cannot build the Sandbox_Credential_File from {source}: {exc}"
        ) from exc

    click.echo(f"Sandbox_Credential_File built from {source} at {dest}.")
    return dest


# `submit` (Task 29.6, Requirements 22.1 and 22.4): the whole command is
# `submit.submit` plus token loading and error mapping, so the reasoning that
# isn't about Click lives in `submit.py`'s module docstring rather than here.
# Four things about the wiring:
#
# - `RUN_ID` is a positional argument rather than an option, because
#   Requirement 22.1 spells the command `vaultshield submit <run-id>` and the
#   dashboard prints that exact string for an approved Run (Requirement
#   21.4) — what a developer copies out of the browser has to be what this
#   command accepts. It is deliberately not validated here: `submit`
#   validates it (via `git_capture.run_branch_name`) before it reaches either
#   the backend URL or a git ref, and its message names the rule that was
#   broken in a way a Click callback's would not.
#
# - `--backend-url` is defaulted and env-var-backed exactly as `run`'s is,
#   rather than required as `login`'s is. A submission has to address the
#   same backend the Run was uploaded to, so sharing the default and
#   `VAULTSHIELD_BACKEND_URL` with `run` is what keeps the two agreeing
#   without the developer restating it per command. And what a defaulted URL
#   receives here is the Dashboard_Auth_Token on loopback — the developer's
#   own machine — not the *password* a defaulted `login` would hand to
#   whatever answers there.
#
# - The token is read from `~/.vaultshield/token` and never prompted for,
#   the same way `run`'s upload stage reads it: `submit` makes two backend
#   calls and neither of them is a login.
#
# - None of `submit`'s keyword-only arguments are passed. They exist as
#   testability seams (`repo_root`, `remote`, `provider`, `credential`,
#   `open_pull_request`), so this command submits from the invocation
#   directory through `origin`, with the host resolved from that remote and
#   the credential resolved per Requirement 22.2 — all of which `submit.py`
#   owns.
#
# Failure reporting matches every other command in this module: one line, no
# traceback. The three types are mapped together because the developer acts
# on the message, not on the type, and each message is already the actionable
# one:
#
#   - `SubmitError` carries Requirement 22.4's rejection — the message names
#     the Run's actual Run_Status, and by then nothing has been pushed and no
#     request opened — plus an unreachable backend, an unknown run id, a
#     failed push, a host that refused the request, and the opened-but-
#     unrecorded case whose message quotes the `pr_url`.
#   - `CredentialError` means neither `gh`/`glab` passthrough nor
#     `~/.vaultshield/credentials.yml` yielded a token, and it is raised
#     before the push, so its own message (a `gh auth login`, or a
#     `chmod 600`) is the whole fix.
#   - `UploadError` can only come from `load_token` here, i.e. from a missing
#     `vaultshield login`, which its message already names.
@cli.command()
@click.argument("run_id")
@_backend_url_option("VaultShield backend holding this Run")
def submit(run_id: str, backend_url: str) -> None:
    """Open a pull or merge request for the approved Run RUN_ID.

    Reads the Run from the backend with the token `vaultshield login` stored.
    If its status is `approved`, pushes `vaultshield/run-<run-id>` to
    `origin`, opens a pull request (GitHub) or merge request (GitLab) using
    your local git/host credential, records the resulting URL on the Run, and
    prints it.

    A Run that is not `approved` is refused with its current status: nothing
    is pushed and no request is opened. Your repository-host credential never
    leaves this machine — only the resulting public URL is sent to the
    backend.
    """
    try:
        token = uploader.load_token()
        pr_url = submit_run(run_id, backend_url, token)
    except (uploader.UploadError, SubmitError, CredentialError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Run {run_id} submitted: {pr_url}")


# `teardown` (Task 30.3, Requirements 23.1 and 23.2): the whole command is
# `teardown.teardown` plus token loading and error mapping, so the reasoning
# that isn't about Click — why the Sandbox is removed before the backend is
# read, why the branch deletion is gated on `rejected` alone, and why both
# steps are no-ops when there is nothing left to do — lives in `teardown.py`'s
# module docstring rather than here. The wiring is deliberately the same shape
# as `submit`'s, since the two commands take the same argument and address the
# same backend:
#
# - `RUN_ID` is a positional argument, because Requirements 23.1 and 23.2 both
#   spell the command `vaultshield teardown <run-id>`. Not validated here:
#   `teardown` validates it (via `run_directory.validate_run_id`) before it is
#   interpolated into a container name, a URL path, or a git ref, and its
#   message names the rule that was broken.
#
# - `--backend-url` is defaulted and env-var-backed exactly as `run`'s and
#   `submit`'s are, so all three address the same backend without the
#   developer restating it per command.
#
# - The token is read from `~/.vaultshield/token` and never prompted for. The
#   backend is only read here (one `GET` of the Run, for its Run_Status);
#   nothing about this command writes to it.
#
# - None of `teardown`'s keyword-only arguments are passed. They are
#   testability seams (`client`, `repo_root`, `remote`, `provider`,
#   `credential`), so this command tears down through `docker.from_env()` and
#   deletes from `origin` in the invocation directory, with the host and the
#   credential resolved by `teardown.py`.
#
# `teardown()` returns `None` per design.md, so the success line states the
# post-condition it guarantees rather than claiming a particular action: the
# Run has no container, and no branch on the remote if it was rejected. Which
# of those two actually had anything to do is not reported, because the
# composed function does not say — and a developer running this command wants
# the end state, not an inventory. (`teardown_sandbox` and `delete_run_branch`
# do each return whether they acted, but only `teardown()` owns Requirement
# 23.2's `rejected` gate, so calling them separately from here would mean
# re-implementing that policy in this module.)
#
# One consequence of loading the token first, named rather than papered over:
# a developer who has never run `vaultshield login` cannot use this command at
# all, even though Requirement 23.1's Sandbox removal is purely local and
# needs no backend. That is the ordering `teardown.py` argues against for an
# unreachable backend, and it is accepted here only because there is no
# sensible way to ask "was this Run rejected?" without a token — reporting the
# missing login before touching anything, with a message that says nothing was
# torn down, is more honest than a half-run command whose output the developer
# then has to interpret.
#
# Failure reporting matches every other command in this module: one line, no
# traceback.
#
#   - `TeardownError` covers an unusable run id, a Docker daemon that could
#     not be reached or refused the removal, a backend that could not be
#     reached or has no such Run, a remote whose host could not be determined,
#     and a refused branch deletion. Its messages already name what the
#     Sandbox step did before the failure, which is the part a developer needs
#     in order to know whether re-running is safe (it is).
#   - `CredentialError` means neither `gh`/`glab` passthrough nor
#     `~/.vaultshield/credentials.yml` yielded a token for the branch
#     deletion. It is raised before the remote is contacted, and its own
#     message is the whole fix.
@cli.command()
@click.argument("run_id")
@_backend_url_option("VaultShield backend holding this Run")
def teardown(run_id: str, backend_url: str) -> None:
    """Clean up after the Run RUN_ID: its Sandbox, and its branch if rejected.

    Force-stops and removes the Run's Sandbox container if one is still
    around, then reads the Run from the backend with the token `vaultshield
    login` stored. If its status is `rejected`, deletes
    `vaultshield/run-<run-id>` from `origin` using your local git credential.

    Any other status leaves the remote untouched — an approved or submitted
    Run's branch is a change you are about to merge. Safe to run more than
    once, and safe to run on a Run that has no container and no pushed branch:
    both steps do nothing when there is nothing left to do.

    The local Sandbox removal needs no login and no reachable backend. If the
    token is missing or the backend cannot be read, the container is still
    removed and only the branch step is skipped — reclaiming a stranded
    container is exactly what this command is for when the backend is down.
    """
    # 1. LOCAL cleanup, first and unconditionally. No token, no network.
    #
    #    This used to sit behind `uploader.load_token()`, which meant a
    #    developer with a stranded container and no login (or an unreachable
    #    backend) could not remove it with the very command documented for that
    #    purpose — the container survived and the command exited non-zero
    #    having done nothing.
    try:
        sandbox_removed = teardown_sandbox(run_id)
    except TeardownError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Sandbox for run {run_id} removed."
        if sandbox_removed
        else f"No Sandbox container was left for run {run_id}."
    )

    # 2. REMOTE sync, best effort. A missing token or an unreachable backend
    #    downgrades this to a warning rather than failing the command: the
    #    local half already succeeded, and reporting failure would misdescribe
    #    what happened. Exit status stays 0 so scripted cleanup loops work
    #    offline.
    try:
        token = uploader.load_token()
    except uploader.UploadError as exc:
        click.echo(
            f"Skipped the branch check for run {run_id}: {exc} "
            "The Sandbox was still removed; re-run this command once logged in "
            "if the Run was rejected and its pushed branch needs deleting."
        )
        return

    try:
        sync_rejected_branch(
            run_id, backend_url, token, sandbox_removed=sandbox_removed
        )
    except (TeardownError, CredentialError) as exc:
        # A genuine failure of the remote half — an unknown Run, an unreachable
        # backend, an unresolvable credential — is still a command failure, and
        # is reported exactly as before. Only the *not logged in* case above is
        # downgraded, because that is a precondition the developer has not met
        # rather than something that went wrong. Both messages already name what
        # the local half did first, so it is never ambiguous whether the
        # container was reclaimed.
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Run {run_id} torn down: no Sandbox container is left for it, and if "
        "the Run was rejected its pushed branch is no longer on the remote."
    )


@cli.command("_sandbox-smoke")
@click.argument("repo_url")
@click.option(
    "--base-image",
    default="python:3.11",
    show_default=True,
    help="Docker image to build/reuse for the Sandbox.",
)
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=Path(".env.sandbox"),
    show_default=True,
    help="Host path of the credential file to mount into the Sandbox.",
)
def sandbox_smoke(repo_url: str, base_image: str, env_file: Path) -> None:
    """Create and tear down a real Sandbox against REPO_URL. TEMPORARY debug scaffolding.

    This command is NOT a permanent, tested CLI feature — it exists solely
    to let a developer manually verify Sandbox_Module (Task 4:
    `Sandbox.create`/`Sandbox.exec`/`Sandbox.teardown`) against a real
    Docker daemon and a real cloned repository, since Task 4's automated
    property tests (4.2, 4.3) exercise `Sandbox` without a real repo clone.
    It requires Docker to be running locally and reachable via the
    environment Docker normally uses (`docker.from_env()`), and network
    access to clone `REPO_URL`.

    `--base-image` defaults to `python:3.11` rather than the smaller
    `python:3.11-slim` tag: `Sandbox.create` always runs `git clone`
    first, unconditionally, before the dependency-install step — there is
    no way to reach dependency-install without a working clone. The
    `slim` variant is built directly from `debian:bookworm-slim` and has
    no `git` preinstalled, which would make the clone step fail
    immediately on every invocation and defeat the point of this smoke
    test. The full `python:3.11` tag is built on top of
    `buildpack-deps:bookworm-scm`, which does carry `git` (plus `pip`),
    so both the clone step and a `requirements.txt`-based dependency
    install have something real to do. Pick a different `--base-image`
    (e.g. a Node image, for a repo with a `package.json`) to smoke-test
    the `npm install` path instead.

    This is throwaway debug scaffolding (Task 8.1's `run_id`/Run_Directory
    helpers don't exist yet), so `run_id` here is just a short local
    unique id, not a real Run_Directory-backed run.
    """
    run_id = f"smoke-{uuid.uuid4().hex[:8]}"
    client = docker.from_env()
    sandbox = Sandbox(run_id, client)

    # `Sandbox.create` requires `env_file` to already exist on disk and
    # raises `SandboxError` (naming the missing path) otherwise. Rather
    # than pre-creating a placeholder file to paper over a missing
    # `--env-file`, we let that `SandboxError` surface naturally through
    # the same handling below — simpler, and it still gives the developer
    # a clear, actionable message for this debug-only tool.
    try:
        sandbox.create(repo_url, base_image, env_file)
        click.echo(f"Sandbox created for run '{run_id}' (container vaultshield-run-{run_id}).")
    except SandboxError as exc:
        raise click.ClickException(str(exc))
    finally:
        sandbox.teardown()
        click.echo(f"Sandbox torn down for run '{run_id}'.")


# VaultShield Protect: its commands live in vaultshield/protect/ and attach here.
from vaultshield.protect.commands import register_protect_commands  # noqa: E402

register_protect_commands(cli)

# VaultShield Orchestrate: its commands live in vaultshield/orchestrate/ and attach here.
from vaultshield.orchestrate.commands import register_orchestrate_commands  # noqa: E402

register_orchestrate_commands(cli)


if __name__ == "__main__":
    cli()
