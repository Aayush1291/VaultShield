"""Teardown_Command (`teardown.py`): cleaning up after a Run.

THIS FILE implements Task 30.1 — design.md's

    def teardown(run_id: str, backend_url: str, token: str) -> None:
        \"\"\"If the Run has an active Sandbox, force-stops and removes it
        (Sandbox.teardown, idempotent). If the Run's Run_Status is
        'rejected' and it has a pushed branch, deletes that remote branch
        via resolve_git_credential().\"\"\"

Wiring `vaultshield teardown <run-id>` as a Click command (Task 30.3) is a
separate task: nothing here touches `cli.py`.

Covers Requirement 23.1 ("WHEN a developer runs `vaultshield teardown
<run-id>` for a Run with an active Sandbox, THE Teardown_Command SHALL
force-stop and remove that Sandbox") and Requirement 23.2 ("WHEN a developer
runs `vaultshield teardown <run-id>` for a Run with Run_Status `rejected`,
THE Teardown_Command SHALL delete the Run's pushed remote branch from the
Repository_Host_Provider using the local git credential").

### Two independent cleanups, in this order

    1. remove the Run's Sandbox container          (local, unconditional)
    2. GET <backend_url>/api/runs/<run_id>/        (read the Run_Status)
    3. status == 'rejected'  ->  delete vaultshield/run-<run_id> on the remote

The Sandbox goes first because it is the part with no preconditions. Requirement
23.1 asks for it for *any* Run with an active Sandbox — no status gate — and it
is a purely local operation, so a backend that is down, a mistyped run id, or a
Run that was never uploaded cannot leave a container running on the developer's
machine. That is the opposite ordering from `submit.py`, deliberately: `submit`
reads first because its side effects are irreversible and unwanted for a Run
that is not `approved`, while here the local cleanup is wanted regardless of
what the backend says, and the whole point of the command is that stale
resources do not accumulate.

Step 3 is the only status-gated part, and it is gated on `rejected` alone.
A branch belonging to an `approved` or `submitted` Run is the change a
developer is about to merge (or has merged), so this command will not delete
it; an `awaiting_review` Run is still being looked at.

### Finding a Sandbox this process never created

`vaultshield teardown` runs in a different process from the `vaultshield run`
that created the Sandbox, so there is no live `Sandbox` object to call
`teardown()` on and no local record of the container. The container is found
by the name `sandbox.create()` gave it — `sandbox.container_name(run_id)` —
which is why that spelling lives in `sandbox.py` as a shared function rather
than as an f-string in each of the two places that needs it.

The found container is then attached to a fresh `Sandbox` (`sandbox.container
= container`) and removed through `Sandbox.teardown()`, exactly as design.md
specifies, rather than by calling `container.remove(...)` here. `Sandbox.
teardown` is what defines what "removed" means for a VaultShield Sandbox —
`remove(v=True, force=True)`: SIGKILL the container, take its anonymous
volumes with it, tolerate a container that is already gone — and a second
implementation of that in this module would be a second answer to
Requirement 23.1. `container` is a public attribute of `Sandbox` in design.md's
own reference snippet, so assigning it is using the documented shape of the
object, not reaching into it.

A Run with no container is not an error: `teardown_sandbox` returns `False`.
Requirement 23.1 is conditional ("for a Run with an active Sandbox"), and the
ordinary case is a Run whose `vaultshield run` already tore its Sandbox down
at the end of the pipeline. Being a no-op there is what makes this command
safe to run twice, or to run on any run id a developer has to hand.

A Docker daemon that cannot be reached at all IS an error, even though it
means nothing is currently running. Containers survive a daemon restart, so
"Docker is down" does not mean "there is no leftover container" — it means
this command cannot tell, and reporting success would be a claim about the
developer's machine that was never checked. `sandbox.py` draws the same line
for the same reason: idempotency is about tolerating "there is nothing left to
tear down", not about hiding a broken Docker environment. The cost is that a
stopped daemon also blocks the branch deletion below, which the error message
says out loud so the developer knows what still has to happen after they start
Docker.

### Deleting the branch with git, not with a host API

Requirement 23.2 says "using the local git credential", and design.md says
"via `resolve_git_credential()`" — so the deletion is `git push <remote>
--delete vaultshield/run-<run_id>`, authenticated exactly the way `submit`'s
push is (`submit.git_credential_args`, which is shared rather than copied so
there is one implementation of getting a token into git without putting it in
`argv` or a URL). No provider REST call and no `gh`/`glab` subcommand: every
host implements ref deletion over the git protocol, so the git path needs no
per-provider client, and a `SOURCE_CLI` credential works through the
developer's own transport credentials without VaultShield holding a secret at
all.

The Repository_Host_Provider is still identified, for one narrow purpose: the
username half of basic auth differs per host (`x-access-token` on GitHub,
`oauth2` on GitLab), and that is `submit.PROVIDER_PUSH_USERNAMES`. It is also
what `resolve_git_credential` resolves a credential *for*. When a caller
supplies a credential, its own `provider` is used and the remote is never
consulted — a resolved credential already names its host, so re-deriving it
from a remote URL could only disagree with it.

### "and it has a pushed branch"

Checked, not assumed: `git ls-remote --heads <remote> refs/heads/<branch>`
before the delete. A rejected Run usually has NO branch on the remote — the
branch is pushed by `vaultshield submit`, which only runs for an `approved`
Run — so "nothing to delete" is the common case, and `git push --delete` on a
missing ref fails with git's own "remote ref does not exist". Pre-checking
turns that into `False` (nothing to do) and keeps the whole command
idempotent: running `teardown` twice on the same rejected Run succeeds twice.
The check is exact rather than pattern-matched (the returned ref is compared
to `refs/heads/<branch>` in full), so a branch whose name merely ends with
this Run's branch name cannot be mistaken for it — Property 33's "without
affecting branches belonging to other runs".

`--force` is not passed, and it does not exist for a deletion. Only the one
ref named here is ever deleted, in the one remote named here.

### Nothing here can leak a credential, and nothing sends one anywhere

One request goes to the VaultShield_Backend — a `GET` of the Run, carrying the
Dashboard_Auth_Token — and it is a read: this module writes nothing to the
backend at all, not even the `pr_url` `submit` records. The repository-host
credential is used only by git, in this process, on this machine
(Requirement 22.5's client half). No message raised from here contains a
token: the git error path quotes git's own output, which cannot contain the
token because the token is passed through the environment, and the backend
error path quotes a truncated response body.

### Testability seams

design.md's `teardown(run_id, backend_url, token)` stays valid for every
caller that passes only those three, `cli.py` included. The keyword-only
additions describe the parts of the environment the function would otherwise
discover for itself: `client` (the Docker daemon to talk to), `repo_root` and
`remote` (which checkout and which remote hold the branch), `provider` (for a
self-managed host whose name contains neither "github" nor "gitlab"), and
`credential` (an already-resolved credential). A test can therefore drive the
whole flow against a fake Docker client, a real local backend, and a real
local git remote.

The two steps are also separately callable — `teardown_sandbox` and
`delete_run_branch`, one per requirement — each returning whether it actually
did anything. `teardown()` composes them and returns `None`, per design.md;
a caller that wants to report what happened (Task 30.3's Click command, which
prints) can call the two directly instead.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Literal

import docker
import httpx
from docker import DockerClient

from vaultshield.git_capture import run_branch_name
from vaultshield.run_directory import RunDirectoryError, validate_run_id
from vaultshield.sandbox import Sandbox, SandboxError, container_name
from vaultshield.submit import (
    DEFAULT_REMOTE,
    RUN_DETAIL_PATH_TEMPLATE,
    STATUS_FIELD,
    Credential,
    SubmitError,
    detect_provider,
    git_credential_args,
    resolve_git_credential,
)
from vaultshield.uploader import AUTH_HEADER_NAME, AUTH_SCHEME

# The one Run_Status whose branch this command deletes (Requirement 23.2).
# `submit.APPROVED_STATUS`'s counterpart, and the reason the two commands
# cannot both act on the same Run.
REJECTED_STATUS = "rejected"

# Ref namespace a branch lives in on the remote. Spelled in full when asking
# `ls-remote` about a branch and when reading its answer back, so the match is
# exact rather than a suffix match on the branch name.
_HEADS_REF_PREFIX = "refs/heads/"

# Turns off git's interactive credential prompt for every git command here.
# Output is captured, so a prompt would be invisible and would block until the
# timeout; with this set, a missing credential fails immediately with git's own
# message. Same reasoning (and same variable) as `submit`'s push.
_GIT_TERMINAL_PROMPT_ENVVAR = "GIT_TERMINAL_PROMPT"

# Bound on one git invocation. Lower than `submit`'s push timeout because
# neither command run here transfers repository objects — `ls-remote` reads the
# remote's refs and `push --delete` removes one — but still generous enough for
# a slow network handshake, and bounded so a wedged remote fails with a message
# instead of hanging the terminal.
_GIT_TIMEOUT_S = 120.0

# Bound on the one backend request. Same value, and the same reasoning, as
# `submit._BACKEND_REQUEST_TIMEOUT_S`: one interactive, developer-initiated
# call.
_BACKEND_REQUEST_TIMEOUT_S = 30.0

# Cap on how much of an error response body is quoted back in a
# `TeardownError`, as in `uploader._error_detail` and `submit._error_detail`.
_MAX_ERROR_BODY_CHARS = 300


class TeardownError(Exception):
    """Raised when a Run could not be torn down.

    Causes, in the order the command can hit them:

    - `run_id` is not a usable run id (a wrapped `RunDirectoryError`, so a
      caller catching this module's failure type does not also have to know
      `run_directory`'s);
    - the Docker daemon could not be reached, or refused to remove the Run's
      container;
    - the backend could not be reached, has no such Run, or answered something
      that is not a Run — the message names what had already been cleaned up
      by then, since the Sandbox step runs first;
    - the repository, its remote, or its Repository_Host_Provider could not be
      determined;
    - the remote's branches could not be listed, or the branch deletion was
      refused.

    One exception type per module, matching `ConfigError`, `SandboxError`,
    `UploadError`, `RunDirectoryError`, `GitCaptureError`, and `SubmitError`:
    `cli.py` maps each to a `click.ClickException` at its command boundary, so
    a failed teardown reaches the developer as one actionable line rather than
    a traceback.

    A `CredentialError` from `resolve_git_credential` is deliberately NOT
    wrapped, for the same reason `submit` does not wrap it: it names its own
    fix (`gh auth login`, or a `chmod 600`'d credentials file) and re-wrapping
    it would bury that message one level deeper.
    """


def teardown(
    run_id: str,
    backend_url: str,
    token: str,
    *,
    client: DockerClient | None = None,
    repo_root: Path | None = None,
    remote: str = DEFAULT_REMOTE,
    provider: Literal["github", "gitlab"] | None = None,
    credential: Credential | None = None,
) -> None:
    """Tears down Run `run_id`: removes its Sandbox, and deletes its pushed
    remote branch if the Run was rejected (Requirements 23.1, 23.2).

    Removes the Run's container if one still exists, then reads the Run from
    `<backend_url>/api/runs/<run_id>/` with the Dashboard_Auth_Token `token`.
    If its Run_Status is `rejected` and `vaultshield/run-<run_id>` is on the
    remote, deletes that branch using the local git credential. Any other
    status leaves the remote untouched.

    Safe to run more than once: both steps are no-ops when there is nothing
    left to do (no container, or no branch on the remote).

    The keyword-only arguments are additions to design.md's
    `teardown(run_id, backend_url, token)`, which stays valid as written:

        client: the Docker client to use (default: `docker.from_env()`).
        repo_root: the checkout whose `remote` holds the branch (default: the
            invocation directory).
        remote: the git remote to delete the branch from (default `origin`).
        provider: the Repository_Host_Provider, when the remote's hostname
            does not identify it (a self-managed GitLab at
            `git.example.com`).
        credential: an already-resolved credential, instead of resolving one.

    Raises:
        TeardownError: any of the causes listed on that type — a Docker daemon
            that could not be reached, a backend that could not be reached or
            has no such Run, a remote whose provider could not be determined,
            or a failed branch deletion.
        CredentialError: no usable git/host credential for the branch deletion
            (Requirement 22.2's resolution, reused here) — raised before the
            remote is touched.
    """
    sandbox_removed = teardown_sandbox(run_id, client=client)

    sync_rejected_branch(
        run_id,
        backend_url,
        token,
        sandbox_removed=sandbox_removed,
        repo_root=repo_root,
        remote=remote,
        provider=provider,
        credential=credential,
    )


def sync_rejected_branch(
    run_id: str,
    backend_url: str,
    token: str,
    *,
    sandbox_removed: bool = False,
    repo_root: Path | None = None,
    remote: str = DEFAULT_REMOTE,
    provider: Literal["github", "gitlab"] | None = None,
    credential: Credential | None = None,
) -> None:
    """The remote half of teardown: delete the branch if the Run was rejected.

    Reads the Run's status from `<backend_url>/api/runs/<run_id>/` and, when it
    is `rejected` and the branch is on the remote, deletes it. Any other status
    leaves the remote untouched — an approved or submitted Run's branch is a
    change that is about to be merged.

    Split out of `teardown_run` so the two halves can be driven independently.
    `teardown_run` still composes them in the same order with the same
    behaviour; the CLI calls them separately because the LOCAL half must not
    depend on this one. Removing a stranded container is exactly what a
    developer needs when there is no token or the backend is down, and gating it
    behind an authenticated read made the documented recovery path unusable in
    precisely that situation.

    Args:
        sandbox_removed: whether the local half already removed a container.
            Passed through so a backend failure's message can say what did
            happen before it, which is what tells a developer whether
            re-running is safe (it is).

    Raises:
        TeardownError: backend unreachable or no such Run, a remote whose
            provider could not be determined, or a failed branch deletion.
        CredentialError: no usable git/host credential — raised before the
            remote is touched.
    """
    status = _fetch_run_status(
        run_id, backend_url, token, sandbox_removed=sandbox_removed
    )
    if status != REJECTED_STATUS:
        return

    delete_run_branch(
        run_id,
        repo_root=repo_root,
        remote=remote,
        provider=provider,
        credential=credential,
    )


def teardown_sandbox(run_id: str, *, client: DockerClient | None = None) -> bool:
    """Force-stops and removes Run `run_id`'s Sandbox, returning whether there
    was one to remove (Requirement 23.1).

    Looks the container up by the name `Sandbox.create` gave it and removes it
    through `Sandbox.teardown` — see the module docstring for why the removal
    goes through `Sandbox` rather than through the container directly.

    `False` means the Run has no container: either its `vaultshield run` tore
    the Sandbox down already, or the Run never had one. That is not a failure
    (Requirement 23.1 is conditional on there being an active Sandbox), and it
    is what makes the whole command repeatable.

    Raises:
        TeardownError: `run_id` is not a usable run id; the Docker daemon
            could not be reached; or Docker refused to remove the container.
    """
    name = container_name(_validated_run_id(run_id))
    docker_client = client if client is not None else _docker_client(run_id)

    try:
        container = docker_client.containers.get(name)
    except docker.errors.NotFound:
        return False
    except docker.errors.DockerException as exc:
        raise TeardownError(
            f"Cannot look up the Sandbox container {name!r} for run {run_id}: "
            f"{exc}"
        ) from exc

    sandbox = Sandbox(run_id, docker_client)
    # `container` is `Sandbox`'s own public attribute (design.md names it),
    # and setting it is what lets `Sandbox.teardown` — the single definition
    # of what removing a VaultShield Sandbox means — act on a container this
    # process did not create.
    sandbox.container = container
    try:
        sandbox.teardown()
    except (SandboxError, docker.errors.DockerException) as exc:
        raise TeardownError(
            f"Cannot remove the Sandbox container {name!r} for run {run_id}: "
            f"{exc}"
        ) from exc
    return True


def delete_run_branch(
    run_id: str,
    *,
    repo_root: Path | None = None,
    remote: str = DEFAULT_REMOTE,
    provider: Literal["github", "gitlab"] | None = None,
    credential: Credential | None = None,
) -> bool:
    """Deletes Run `run_id`'s branch from `remote`, returning whether there was
    a branch there to delete (Requirement 23.2).

    Resolves the local git credential (`resolve_git_credential`, unless one is
    supplied), checks whether `vaultshield/run-<run_id>` exists on the remote,
    and deletes exactly that ref if it does. `False` means the remote does not
    carry the branch — the usual case for a rejected Run, which was never
    submitted — and nothing was sent.

    Applies no Run_Status gate of its own: `teardown()` owns the `rejected`
    condition Requirement 23.2 states, so this function is the mechanism, not
    the policy.

    Raises:
        TeardownError: `run_id` is not a usable run id; the repository, its
            remote, or its Repository_Host_Provider could not be determined;
            the remote's branches could not be listed; or the deletion was
            refused.
        CredentialError: no usable git/host credential — raised before the
            remote is contacted.
    """
    branch = _run_branch(run_id)
    root = repo_root if repo_root is not None else Path.cwd()

    if credential is not None:
        host_provider: str = provider or credential.provider
        resolved = credential
    else:
        host_provider = provider or _detect_provider(root, remote)
        resolved = resolve_git_credential(host_provider)

    config_args, env = git_credential_args(resolved, host_provider)

    if not _remote_has_branch(root, remote, branch, config_args, env):
        return False

    _git(
        root,
        [*config_args, "push", remote, "--delete", branch],
        step=f"deleting {branch} from {remote}",
        env=env,
    )
    return True


def _validated_run_id(run_id: str) -> str:
    """Returns `run_id` if it is a usable run id, else raises
    `TeardownError`.

    `run_id` arrives from a command line and is interpolated into a container
    name, a URL path, and a git ref, so it is validated before any of the
    three is built. `RunDirectoryError` is re-raised as this module's own type
    for the reason `TeardownError` documents.
    """
    try:
        return validate_run_id(run_id)
    except RunDirectoryError as exc:
        raise TeardownError(f"Cannot tear down run {run_id!r}: {exc}") from exc


def _run_branch(run_id: str) -> str:
    """The Run's branch name, `vaultshield/run-<run_id>`.

    Delegates to `git_capture.run_branch_name`, which owns the spelling for
    every component that needs it (capture, diff, submit, teardown) and
    validates `run_id` on the way — so the branch this command deletes is
    provably the branch `commit_run_branch` created and `submit` pushed.
    """
    try:
        return run_branch_name(run_id)
    except RunDirectoryError as exc:
        raise TeardownError(f"Cannot tear down run {run_id!r}: {exc}") from exc


def _docker_client(run_id: str) -> DockerClient:
    """Connects to the Docker daemon the way `cli.py` does.

    Raises `TeardownError` if the daemon cannot be reached, naming the branch
    deletion as the part that has not run either — see the module docstring
    for why an unreachable daemon is a failure rather than "nothing to do".
    """
    try:
        return docker.from_env()
    except docker.errors.DockerException as exc:
        raise TeardownError(
            f"Cannot reach Docker to tear down the Sandbox for run {run_id}: "
            f"{exc}. Start Docker and run `vaultshield teardown {run_id}` "
            "again — no branch has been deleted either."
        ) from exc


def _fetch_run_status(
    run_id: str, backend_url: str, token: str, *, sandbox_removed: bool
) -> str:
    """Reads the Run's Run_Status from the backend, for Requirement 23.2's
    gate.

    A read, and the only backend call this module makes.

    Every failure message ends with what the Sandbox step already did, because
    that step ran first: a developer whose backend is unreachable still needs
    to know whether the container is gone, and whether re-running the command
    is safe (it is — see `teardown_sandbox`).

    Raises `TeardownError` if the backend cannot be reached, does not have
    this Run (a `404`, reported separately because it is the one a developer
    causes by mistyping a run id), answers any other non-2xx status, answers
    with something that is not a JSON object, or answers without a status.
    """
    run_url = _build_url(backend_url, RUN_DETAIL_PATH_TEMPLATE.format(run_id=run_id))
    note = _sandbox_note(run_id, sandbox_removed)

    try:
        response = httpx.get(
            run_url,
            headers=_auth_headers(token),
            timeout=_BACKEND_REQUEST_TIMEOUT_S,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise TeardownError(
            f"Cannot reach the VaultShield backend at {run_url} to read run "
            f"{run_id}'s status: {exc}.{note}"
        ) from exc

    if response.status_code == 404:
        raise TeardownError(
            f"The VaultShield backend has no run {run_id!r} ({run_url} "
            f"answered HTTP 404), so there is no review status to act on. "
            f"Check the run id.{note}"
        )
    if not response.is_success:
        raise TeardownError(
            f"Cannot read run {run_id!r}: {run_url} answered HTTP "
            f"{response.status_code}: {_error_detail(response)}{note}"
        )

    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise TeardownError(
            f"The backend at {run_url} answered with a body that is not JSON, "
            f"so run {run_id!r}'s status could not be read: {exc}.{note}"
        ) from exc

    status = payload.get(STATUS_FIELD) if isinstance(payload, dict) else None
    if not isinstance(status, str) or not status:
        raise TeardownError(
            f"The backend at {run_url} returned run {run_id!r} without a "
            f"{STATUS_FIELD!r}, so there is no review state to check.{note}"
        )
    return status


def _sandbox_note(run_id: str, sandbox_removed: bool) -> str:
    """The trailing clause every post-Sandbox failure message carries, saying
    what the first step did.

    Empty when there was no container, so a message does not report a
    non-event.
    """
    if not sandbox_removed:
        return ""
    return f" The Sandbox for run {run_id} was removed."


def _detect_provider(repo_root: Path, remote: str) -> str:
    """The Repository_Host_Provider behind `remote`, read from its URL.

    Reuses `submit.detect_provider` (and, through it, `providers.
    parse_remote_url`) so the CLI has one answer to "which host is this
    remote", and re-raises its `SubmitError` as this module's own type — the
    failure is a teardown failure here, and a developer reading it is not
    submitting anything.
    """
    url = _git(
        repo_root,
        ["remote", "get-url", remote],
        step=f"reading the URL of git remote {remote!r}",
    ).strip()
    if not url:
        raise TeardownError(
            f"Git remote {remote!r} in {repo_root} has no URL, so there is no "
            "repository host to delete the branch from."
        )

    try:
        return detect_provider(url)
    except SubmitError as exc:
        raise TeardownError(str(exc)) from exc


def _remote_has_branch(
    repo_root: Path,
    remote: str,
    branch: str,
    config_args: list[str],
    env: dict[str, str],
) -> bool:
    """Whether `remote` carries `branch` — the "has a pushed branch" half of
    Requirement 23.2.

    Asks the remote itself (`git ls-remote`) rather than a local tracking ref,
    which can be stale in either direction: a `remotes/<remote>/<branch>` ref
    can outlive the branch, and a branch pushed from another checkout has no
    local ref at all.

    The ref returned is compared to `refs/heads/<branch>` in full, so this
    cannot match a different run's branch whose name happens to end with this
    one's — `ls-remote`'s pattern argument matches on the tail of a ref name,
    which is a suffix match, and Property 33 requires that other runs'
    branches are unaffected.
    """
    output = _git(
        repo_root,
        [*config_args, "ls-remote", "--heads", remote, f"{_HEADS_REF_PREFIX}{branch}"],
        step=f"checking whether {remote} carries {branch}",
        env=env,
    )
    wanted = f"{_HEADS_REF_PREFIX}{branch}"
    for line in output.splitlines():
        _object_id, _tab, ref = line.partition("\t")
        if ref.strip() == wanted:
            return True
    return False


def _git(
    repo_root: Path,
    args: list[str],
    *,
    step: str,
    env: dict[str, str] | None = None,
) -> str:
    """Runs one `git` command in `repo_root` and returns its stdout.

    The single place every git invocation in this module goes through, so each
    failure is wrapped exactly once and every message names the step.
    Arguments are a list, never a shell string, so a branch name or a remote
    name is passed as data.

    Same shape as `submit._git` (which is private to that module, as
    `uploader._build_url` is to its own) with this module's own timeout and
    error type. `env` adds to rather than replaces the current environment:
    git needs `HOME`, `PATH`, and `SSH_AUTH_SOCK` from it to find the
    developer's configuration and keys. `stdin` is `/dev/null` so a command
    that would read from the terminal fails instead of blocking on captured
    input.

    Raises `TeardownError` naming `step` and git's own output on a non-zero
    exit, a missing `git`, or a timeout.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env={**os.environ, _GIT_TERMINAL_PROMPT_ENVVAR: "0", **(env or {})},
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TeardownError(
            f"Timed out after {_GIT_TIMEOUT_S:.0f}s while {step}."
        ) from exc
    except OSError as exc:
        raise TeardownError(f"Cannot run git while {step}: {exc}") from exc

    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "no output captured"
        )
        raise TeardownError(
            f"Git failed while {step} (exit code {completed.returncode}): "
            f"{detail}"
        )
    return completed.stdout


def _auth_headers(token: str) -> dict[str, str]:
    """The `Authorization: Token <token>` header the backend read carries.

    Built from `uploader`'s public constants rather than respelling the
    scheme, so the Dashboard_Auth_Token is presented exactly as DRF's
    `TokenAuthentication` parses it in one place for the whole CLI.
    """
    return {AUTH_HEADER_NAME: f"{AUTH_SCHEME} {token}"}


def _build_url(backend_url: str, path: str) -> str:
    """Joins the backend origin and an API path, tolerating a trailing slash
    on the origin and a missing one on the path.

    Same one-liner as `uploader._build_url`, `submit._build_url`, and
    `browser_capture._build_url`; kept local because each of those is private
    to its own module.
    """
    return f"{backend_url.rstrip('/')}/{path.lstrip('/')}"


def _error_detail(response: httpx.Response) -> str:
    """A short single-line summary of an error response body, for a
    `TeardownError` message.

    Same shape and reasoning as `uploader._error_detail` and
    `submit._error_detail`: DRF's error detail is worth quoting verbatim, an
    HTML page from a proxy is not, so the body is collapsed to one line and
    truncated.
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
