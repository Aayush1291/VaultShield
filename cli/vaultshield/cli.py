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
developer runs once — ``run``'s upload stage only ever *reads* that token
(``uploader.load_token``) and never prompts for a credential mid-Run.

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
from vaultshield.config import ConfigError, detect_framework, load_config, write_config
from vaultshield.credentials import build_sandbox_env
from vaultshield.git_capture import (
    GitCaptureError,
    build_structured_diff,
    commit_run_branch,
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
#   a one-line message, since Run status is a backend concern the CLI does
#   not reach until Task 18.5's Uploader).
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
# one. This pipeline passes `<branch>~1`: `commit_run_branch` always adds
# exactly one commit (`--allow-empty`, so even a no-change Run has one), so
# the capture commit's first parent IS the commit the Sandbox's clone was
# sitting on before the agent touched it — the pre-agent state, named
# without having to know whether the repository's default branch is `main`,
# `master`, or something else. The one shape this cannot describe is a clone
# whose `HEAD` is its own root commit (a repository with a single commit):
# that commit has no parent, so git reports an unknown revision and the Run
# fails here with its Event_Log and screenshots already on disk. Left as-is
# rather than papered over with a fallback, because the alternative —
# resolving `HEAD` with a `git rev-parse` of our own before committing —
# would put git commands in this module, and `git_capture.py` deliberately
# owns every one of them.
#
# What the diff actually contains is bounded by judgment call 2 below, the
# same way the screenshots are: the current adapters write on the host, so
# the Sandbox's clone has no agent changes to commit and the captured diff
# is empty. That is the unowned gap named in judgment call 2 surfacing in a
# third place, not a new problem introduced here.
#
# The Uploader is wired in below as step 10 (Task 18.5, Requirements 14.2
# and 14.3): once the local Run is complete, `uploader.load_token` reads the
# Dashboard_Auth_Token `vaultshield login` stored and `uploader.upload_run`
# POSTs the Run's Event_Log, diff, and screenshots to `--backend-url`.
# Three things about where it sits:
#
# - AFTER the `finally` that tears the Sandbox down, exactly where
#   design.md's flowchart puts it ("Sandbox_Module: teardown → Uploader:
#   POST artifacts to backend"). The upload reads only files under
#   `run_dir`, so it needs nothing from the container, and holding a
#   container open across a network upload would leak one for as long as the
#   backend is slow.
# - Outside the `try`, so an upload failure cannot be mistaken for one of
#   the Run's own failures and so `sandbox.teardown()` has already run and
#   already printed by the time the upload is attempted.
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
# — which is why the upload is the last thing `run` does and nothing follows
# it.
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
# One gap this step inherits, named rather than papered over: the artifact
# endpoints are nested under a Run the backend already knows
# (`/api/runs/<run_id>/...`, addressed by `RunViewSet.lookup_field =
# "run_id"`), and nothing creates that row. `Run.project` is a required
# foreign key, and neither `.vaultshield.yml` nor `ProjectConfig` carries a
# project — same shape as judgment call 1's `--repo-url`, except that a
# `POST /api/runs/` would need a project id the CLI has no way to know. So
# against a backend that has never seen this `run_id`, the upload fails with
# the backend's own `404`, reported like any other upload failure with the
# artifacts preserved. Creating (or resolving) the Run record is a real
# piece of wiring that no task in the plan owns; inventing a project id here
# would only move the failure.
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
# `GitCaptureError` from step 8, the `UploadError` step 10 raises for a
# missing token or an unreachable backend, and the `OSError` the Monitor
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
    required=True,
    help="Prompt passed to the selected Agent_Adapter's launch(prompt, workdir).",
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
@_backend_url_option("VaultShield backend this Run's artifacts are uploaded to")
def run(
    agent: str,
    prompt: str,
    repo_url: str,
    base_image: str,
    env_file: Path | None,
    ready_timeout: int,
    interactive_auth: bool,
    backend_url: str,
) -> None:
    """Execute one sandboxed, monitored Run and write its Event_Log.

    Runs the per-Run pipeline in design.md's order, writing the Run's
    artifacts into `.vaultshield/runs/<run_id>/`:

    \b
      1. Config_Module      load the existing `.vaultshield.yml`
      2. Run_Directory      generate a run_id, create its directory
      3. Credentials_Module build `.env.sandbox` (dummy values only)
      4. Agent_Adapter      resolve `--agent` (before any Sandbox exists)
      5. Sandbox_Module     create a Sandbox, mount the credential file
      6. Monitor            run the agent under PTY + file-change capture,
                            write the merged Event_Log
      7. Browser_Capture    start the app in the Sandbox, poll `ready_path`,
                            screenshot each configured route
      8. Git capture        commit the Sandbox's changes to
                            `vaultshield/run-<run_id>`, write `diff.json`
      9. Teardown           tear the Sandbox down, on success or failure
     10. Uploader           POST the Event_Log, diff, and screenshots to
                            `--backend-url` with the token from
                            `vaultshield login`

    Requires a running Docker daemon, and a `vaultshield login` for the
    upload. Every failure is reported as a one-line error; when the
    Event_Log has already been written, the message names the Run_Directory
    it was preserved in. A failed upload leaves the Run_Directory exactly as
    the Run wrote it, so it can be uploaded again from the same artifacts.
    """
    repo_root = Path.cwd()

    # 1. Config_Module. Before anything else, and before anything is
    #    created on disk — design.md: "both stop `run`/`submit`/`teardown`
    #    before any side effect".
    try:
        config = load_config(repo_root)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    # 2. Run_Directory (Task 8.1). `generate_run_id`'s output is always a
    #    valid single path component, so `create_run_dir` cannot raise
    #    `RunDirectoryError` here.
    run_id = generate_run_id()
    run_dir = create_run_dir(repo_root, run_id)
    click.echo(f"Run {run_id}: artifacts in {run_dir}")

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

    try:
        # `publish_port` is what makes step 7 reachable from the host: it
        # publishes `config.port` on an ephemeral host port that
        # `browser_capture.resolve_base_url` reads back off the container.
        sandbox.create(
            repo_url,
            base_image,
            sandbox_env,
            publish_port=config.port,
            # Only moves where dependencies are installed. The clone still
            # lands at Sandbox.REPO_DIR, so git capture is unaffected.
            project_dir=config.project_dir,
        )
        click.echo(f"Sandbox created for run {run_id} (cloned {repo_url}).")

        # 5b. Optional in-Sandbox sign-in (`--interactive-auth`). Between the
        #     Sandbox existing and the agent starting, because the agent cannot
        #     start unauthenticated and the session has to live in the container
        #     that is about to run it. Skipped entirely without the flag, so the
        #     default behaviour of every agent is unchanged.
        if interactive_auth:
            _authenticate_in_sandbox(sandbox, adapter, agent)

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

        # 7. Browser_Capture_Module (Requirement 9.1): agent execution has
        #    completed — `supervise` returned — so the application is
        #    started inside the still-running Sandbox from `start_command`
        #    and `port`, `ready_path` is polled, and each configured route
        #    is screenshotted into the Run_Directory.
        screenshots = start_and_capture(sandbox, config, run_dir, ready_timeout)
        _report_screenshots(screenshots, run_dir)

        # 8. Git branch + diff capture (Requirements 10.1, 10.2): agent
        #    execution has completed, so everything the agent left in the
        #    Sandbox's clone is committed to `vaultshield/run-<run_id>` and
        #    the per-file addition/deletion counts between that commit's
        #    parent and the branch are written to `<run_dir>/diff.json`.
        #    Both run while the container is still up — see the comment
        #    block above for why `<branch>~1` is the base ref.
        branch = commit_run_branch(sandbox, run_id)
        diff_file = build_structured_diff(
            sandbox, f"{branch}~1", branch, run_dir
        )
        click.echo(
            f"Committed run {run_id} to branch {branch} in the Sandbox; "
            f"structured diff written to {diff_file}"
        )
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
        # ref does not resolve (see the root-commit note above), or
        # `--numstat` output could not be parsed. The Event_Log and any
        # screenshots are already on disk, so the message names where the
        # Run's evidence survives even though its diff was never written.
        raise click.ClickException(
            f"Run {run_id} failed: {exc} "
            f"(Event_Log preserved at {event_log_path(run_dir)})"
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
        sandbox.teardown()
        click.echo(f"Sandbox torn down for run {run_id}.")

    click.echo(f"Run {run_id} complete. Event_Log written to {log_path}")

    # 10. Uploader (Requirements 14.2, 14.3): the local Run is complete and
    #     the Sandbox is gone, so the artifacts in the Run_Directory are
    #     final — package them and submit them to the backend, which is what
    #     makes this Run retrievable through `GET /api/runs/<run_id>/`.
    _upload_run_artifacts(run_dir, run_id, backend_url)


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


def _upload_run_artifacts(run_dir: Path, run_id: str, backend_url: str) -> None:
    """Submits this Run's artifacts to `backend_url` with the stored
    Dashboard_Auth_Token (Requirements 14.2, 14.3).

    Two `UploadError` sources, one handler: `load_token` raises it when
    `vaultshield login` has not been run (or its token file is unreadable or
    empty), and `upload_run` raises it for an unreachable backend, a non-2xx
    response, or an artifact that exists but cannot be read. Both mean the
    same thing to the developer — the Run is on disk and is not in the
    dashboard — and both are fixed the same way, by fixing the cause and
    uploading these same artifacts again, so one message covers both rather
    than splitting a distinction the developer cannot act on differently.
    `load_token`'s own message already names `vaultshield login` when that is
    the cause.

    Requirement 14.4's preservation half needs no code here, which is the
    point: nothing in this function (or in `upload_run`) creates, moves,
    truncates, or deletes anything under `run_dir`, so there is no cleanup
    step to skip on failure. The message names the directory so the artifacts
    that survived are findable.
    """
    try:
        token = uploader.load_token()
        uploader.upload_run(run_dir, run_id, backend_url, token)
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


if __name__ == "__main__":
    cli()
