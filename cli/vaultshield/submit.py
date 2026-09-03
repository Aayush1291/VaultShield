"""Submit_Command (`submit.py`): credential resolution and `submit()`.

THIS FILE implements Tasks 29.1 and 29.3 — design.md's
`resolve_git_credential` (with the `Credential` it returns and the
`CredentialError` it raises), and design.md's `submit()`, which gates on a
Run's approved status, pushes the Run's branch, opens a pull/merge request
through `providers/`, and records the resulting `pr_url` on the Run.
Wiring `vaultshield submit <run-id>` as a Click command (Task 29.6) is a
separate task: nothing here touches `cli.py`.

Covers Requirement 22.2: "THE Submit_Command SHALL resolve the local git or
host credential using `gh`/`glab` command passthrough first, then
`~/.vaultshield/credentials.yml` with `chmod 600` permissions as fallback."
And Requirement 22.1: "WHEN a developer runs `vaultshield submit <run-id>`
for a Run with Run_Status `approved`, THE Submit_Command SHALL push the
Run's branch to the configured Repository_Host_Provider and SHALL open a
pull request or merge request using a local git or host credential."
And Requirement 22.3: "WHEN the Submit_Command successfully opens a pull
request or merge request, THE Submit_Command SHALL send a `PATCH` request
to the VaultShield_Backend recording the resulting `pr_url` on the Run."
And Requirement 22.4: "IF a developer runs `vaultshield submit <run-id>`
for a Run whose Run_Status is not `approved`, THEN THE Submit_Command
SHALL reject the submission and SHALL report the Run's current
Run_Status."
And the client half of Requirement 22.5 ("THE VaultShield_Backend SHALL
NOT store a Repository_Host_Provider access token"): the only value this
module ever sends to the backend is `pr_url`.

### Resolution order, and why the order is the whole point

    1. `gh auth status` / `glab auth status`  (Repository_Host_Provider CLI)
    2. `~/.vaultshield/credentials.yml`        (host token, mode 600)
    3. otherwise: CredentialError

Step 1 comes first because it is the credential the developer already
maintains. `gh`/`glab` hold their token in the OS keychain (or in the
developer's own config, or in `GH_TOKEN`/`GITLAB_TOKEN`), refresh it,
scope it, and revoke it — so passing the work through the CLI means
VaultShield never copies a long-lived token into a file of its own just to
do what an already-authenticated tool can do. Step 2 exists for the
machines where that CLI is not installed (a CI runner, a container), and
its file is required to be owner-only precisely because it IS a token at
rest. design.md's Property 30 fixes this precedence as a testable
invariant, which is why steps 1 and 2 are separate, individually callable
functions (`cli_credential`, `file_credential`) rather than branches buried
inside one body.

A CLI that is installed but NOT authenticated is not a failure — it is a
miss, and resolution falls through to step 2. Requirement 22.2 calls the
file a "fallback", and a developer with `glab` installed but logged out
still has a working credential in the file; treating an unauthenticated CLI
as fatal would make installing a tool break a setup that worked before.

### Nothing here reaches the network or the git host

`gh auth status` / `glab auth status` do contact the host to validate the
stored token, so "step 1 succeeded" means the CLI itself reported a usable
credential. Beyond that one status call this module performs no git
operation and no HTTP request: it answers "which credential would a push
use", and the push is Task 29.3's job. So a `CredentialError` from here
provably precedes any push or PR attempt, which is design.md's error
handling rule for this component ("`CredentialError` is raised (and no
push/PR attempted) when neither `gh`/`glab` passthrough nor the credentials
file yields a usable token").

### The credentials file, and its permissions

    ~/.vaultshield/credentials.yml   (mode 600, owner read/write only)

    github:
      token: <repository-host token>
    gitlab:
      token: <repository-host token>

A bare scalar (`github: <token>`) is accepted as shorthand for the same
thing, since a one-provider file has nothing else to say.

The mode is checked BEFORE the file is read, and a file carrying any
permission bit outside `0o600` is refused rather than read — the "refuses
to read if permissions are looser" half of design.md's docstring. Refusing
after reading would be theatre: the token would already be in this
process's memory, and the point of the check is that VaultShield will not
consume a host token that other users on the machine can also read. The
check is deliberately literal about "looser than 600" (any bit beyond owner
read/write, including a stray owner-execute), because the fix is one
`chmod 600` the error message spells out, and a mode nobody intended is
worth reporting rather than guessing at.

`Path.stat` follows symlinks on purpose: if the path is a link, the mode
that matters is the mode of the file whose bytes would actually be read,
not the link's own (which POSIX fixes at `0o777` and ignores).

This file is NOT `~/.vaultshield/token`, which `uploader.py` writes and
which holds the Dashboard_Auth_Token. Two files, two credentials, one
directory — see design.md's security note: "The dashboard auth token
(`vaultshield login`) is stored separately from git/host credentials ...
so a leak of one does not expose the other". `VAULTSHIELD_HOME_DIRNAME` is
imported from `uploader` rather than respelled here because it is genuinely
the same directory (unlike `run_directory.py`'s same-named but unrelated
per-repository path, which `uploader` documents its reasons for not
sharing).

### Tokens stay out of messages, reprs, and tracebacks

No exception raised here ever contains a token, and `Credential.__repr__`
redacts the one it holds. That covers the realistic leak paths for a
credential this module handles: a traceback pasted into a bug report, a
`print`/log of a resolved credential, and a CI job's captured output. Two
specific consequences worth naming, because both look like missing detail:

- A `yaml.YAMLError` is reported as "malformed YAML" with the path and
  nothing else. PyYAML's message quotes the offending region of the
  document, which in this file is a line with a token on it.
- `auth status` output is captured and discarded, never quoted. It is also
  captured rather than inherited so the check cannot write into the pty
  `monitor.py` may have swapped onto this process's fd 1/2, and so a
  `gh`-generated hint never lands in a Run's Event_Log.

### Testability seams

`resolve_git_credential` takes design.md's single positional argument, plus
keyword-only `home`, `which`, and `check_auth` — so every part of the
environment it consults can be supplied by a caller instead of discovered:
the credentials file's location (`home`, exactly as `uploader.token_path`
takes it), whether the host CLI is installed (`which`), and whether that
CLI is authenticated (`check_auth`). Task 29.2's property test needs all
three to enumerate the availability combinations Property 30 quantifies
over; no production caller passes any of them.

### `submit()`: the gate comes first, and nothing precedes it

    1. GET  <backend_url>/api/runs/<run_id>/     (read the Run)
    2. status != 'approved'  ->  SubmitError, and stop
    3. git remote get-url <remote>              (which host, which repo)
    4. resolve_git_credential(provider)
    5. git push <remote> vaultshield/run-<run_id>
    6. providers.<host>.open_*_request(...)     ->  pr_url
    7. PATCH <backend_url>/api/runs/<run_id>/  {"pr_url": <pr_url>}

Step 2 is Requirement 22.4, and its position is the requirement: design.md
says a non-`approved` Run "raises SubmitError including the actual status
and performs no git/network side effects". Step 1 is a read, so a rejected
submission has run one `GET` and nothing else — no branch pushed, no
request opened, no field written. The actual status is in the message
because "not approved" is not actionable: `awaiting_review` means go review
it, `rejected` means it will never be submitted, and `running` means the
Run is not finished.

Each later step is also ordered by what it makes irreversible. The
credential is resolved BEFORE the push, so a machine with no usable
credential fails without having pushed anything (design.md: "`CredentialError`
is raised (and no push/PR attempted)"). The push happens BEFORE the
pull/merge request, because a request whose head branch the host cannot see
is refused. The `pr_url` PATCH happens LAST, because it records something
that already exists — a Run cannot carry a URL to a pull request that was
never opened.

The one failure that cannot be made clean is a successful pull request
followed by a failed PATCH: the request exists on the host and the Run does
not know its URL. That is why the PATCH failure message quotes the
`pr_url` — it is the only copy of a value the developer may need to record
by hand — rather than reporting the HTTP error alone.

An already-submitted Run (one whose `pr_url` is set) is deliberately NOT
short-circuited. Requirement 22.1's only precondition is `approved`, and a
second submission is refused by the host itself, with its own message about
the existing request. Returning the stored URL instead would be a rule the
spec does not state, and it would hide a genuine mistake.

### Nothing about a repository-host token reaches the backend

Two requests go to the VaultShield_Backend, and they carry the
Dashboard_Auth_Token (`Authorization: Token ...`) and nothing else from
this module's credential world. The PATCH body is exactly
`{"pr_url": <url>}` — one public URL. That is the client half of
Requirement 22.5 and of design.md's security note that `submit` "keeps host
credentials entirely on the host and only ever sends the resulting public
`pr_url` back to the backend": there is no code path here that could send
one, not a rule that some check enforces.

### Authenticating the push without putting a token anywhere it lingers

`git push` authenticates in one of two ways, depending on which shape
`resolve_git_credential` returned:

- **`SOURCE_CLI`.** Nothing is injected. The push uses the developer's own
  git transport credentials — an SSH key, or whatever credential helper
  `gh auth setup-git`/`glab auth git-credential` already installed — which
  is exactly Requirement 22.1's "using a local git or host credential", and
  is why VaultShield never has to hold a secret on this path.
- **`SOURCE_FILE`.** The host token is passed to git through a one-shot
  credential helper (`-c credential.helper=...`) that reads it from the
  environment. The token therefore never appears in `argv` (which every
  other user on the machine can read via the process list) and never in a
  remote URL (which git prints back in its own error messages, and which
  `git remote -v` would then show). The `-c credential.helper=` that
  precedes it resets the helper list, so the helper VaultShield supplies is
  the one that answers for this push rather than competing with a
  configured one.

`GIT_TERMINAL_PROMPT=0` is set for both. Output is captured, so a git
credential prompt would be invisible: with prompting off, an HTTPS remote
with no usable credential fails immediately with git's own message instead
of appearing to hang.

### The branch has to be in the repository `submit` runs in (a named gap)

`submit()` pushes from the repository at `repo_root` (the invocation
directory by default), which is the developer's own checkout. The Run's
branch, however, is committed by `git_capture.commit_run_branch` inside the
*Sandbox's* clone (see that module's docstring), and `cli.py`'s judgment
call 2 already records that the current adapters write on the host rather
than in the Sandbox. So on a machine where those two facts meet, the local
checkout has the agent's changes in its working tree but no
`vaultshield/run-<run_id>` branch, and this push fails with git's own
"src refspec ... does not match any".

That is stated rather than papered over, exactly as `cli.py` states it: no
task in the plan owns moving the capture commit onto the host checkout (or
fetching it out of the Sandbox before teardown), and the honest failure is
more useful than a fabricated branch. The push is not pre-checked with a
`rev-parse` for the same reason `git_capture._checked_ref` does not
pre-check refs: git's message is better than ours would be. Nothing is
mutated on the way to that failure — the push is the first side effect, and
`--set-upstream` is deliberately not passed, so even a successful submission
leaves the local repository's configuration untouched.

### `submit()`'s testability seams

design.md's `submit(run_id, backend_url, token)` stays valid for every
caller that passes only those three. The keyword-only additions describe
the parts of the environment the function would otherwise discover:
`repo_root`/`remote` (which checkout and which remote to push), `provider`
(for a self-managed host whose name contains neither "github" nor
"gitlab"), `credential` (an already-resolved credential), and
`open_pull_request` (the provider call itself). A test can therefore drive
the whole flow against a real local backend and a real local git remote,
with only the host API replaced; `cli.py` passes none of them.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import httpx
import yaml

from vaultshield.git_capture import run_branch_name
from vaultshield.providers import ProviderError, parse_remote_url
from vaultshield.providers import github as github_provider
from vaultshield.providers import gitlab as gitlab_provider
from vaultshield.run_directory import RunDirectoryError
from vaultshield.uploader import (
    AUTH_HEADER_NAME,
    AUTH_SCHEME,
    VAULTSHIELD_HOME_DIRNAME,
)

# Repository_Host_Provider -> the host CLI that speaks for it, per
# Requirement 22.2's "`gh`/`glab` command passthrough". Also the set of
# providers this module supports: a key that is not in here has neither a
# CLI to pass through to nor a meaningful entry in the credentials file.
PROVIDER_CLIS: dict[str, str] = {"github": "gh", "gitlab": "glab"}

# Subcommand asked of the host CLI to decide whether it holds a usable
# credential — `gh auth status` / `glab auth status`, exactly as design.md
# names them. Both exit 0 when authenticated and non-zero when not.
AUTH_STATUS_ARGS = ("auth", "status")

# The git/host credential file: `~/.vaultshield/credentials.yml`, per
# design.md. Distinct from `uploader.TOKEN_FILENAME` in the same directory —
# see the module docstring.
CREDENTIALS_FILENAME = "credentials.yml"

# Required mode for that file: owner read/write, nothing else
# (Requirement 22.2's `chmod 600`). Any bit outside this mask is "looser"
# and is refused before the file is read.
CREDENTIALS_FILE_MODE = 0o600

# Which source a resolved `Credential` came from. `SOURCE_CLI` means "use
# `gh`/`glab` for the push/PR calls"; `SOURCE_FILE` means "authenticate with
# this token". Task 29.3 branches on this, so the two values are named here
# rather than spelled as bare strings at each use.
SOURCE_CLI = "cli"
SOURCE_FILE = "file"

# Key holding the token inside a provider's mapping in the credentials file.
TOKEN_KEY = "token"

# Bound on the `auth status` call. Generous because both CLIs contact the
# repository host to validate the stored token (and may unlock an OS
# keychain on the way), but bounded so a wedged network leaves the
# developer with a fallback attempt and an error instead of a hung command.
_AUTH_STATUS_TIMEOUT_S = 20.0

# Placeholder printed instead of a token by `Credential.__repr__`.
_REDACTED = "<redacted>"

# --- `submit()` (Task 29.3) -------------------------------------------------

# The Run detail endpoint, relative to `backend_url`, used for both the
# status read (`GET`) and the `pr_url` write (`PATCH`) — design.md's
# `GET /api/runs/:id/` and `PATCH /api/runs/:id/`. `:id` is the CLI's own
# `run_id` because `RunViewSet.lookup_field` is `run_id`. Trailing slash for
# the same reason as `uploader.LOGIN_PATH`: a body-carrying request cannot
# follow Django's `APPEND_SLASH` redirect without losing its body.
RUN_DETAIL_PATH_TEMPLATE = "/api/runs/{run_id}/"

# The one Run_Status a Run may be submitted from (Requirement 22.1), and the
# fields of the Run representation this module reads and writes — matching
# `runs.serializers.RunSerializer`.
APPROVED_STATUS = "approved"
STATUS_FIELD = "status"
PR_URL_FIELD = "pr_url"
AGENT_FIELD = "agent"

# Git remote the Run's branch is pushed to when a caller names no other.
# `origin` is git's own default remote name, so this is the remote a clone
# already has.
DEFAULT_REMOTE = "origin"

# Repository_Host_Provider detection: a substring of the remote's hostname
# that identifies the host. Deliberately a substring rather than an exact
# match, so `github.com`, `gitlab.com`, and a self-managed
# `gitlab.internal.example.com` are all recognised. A host that matches
# neither (a self-managed instance at `git.example.com`) is not guessed at —
# `submit`'s `provider` argument names it instead.
PROVIDER_HOST_KEYWORDS: dict[str, str] = {"github": "github", "gitlab": "gitlab"}

# The provider call that opens the pull/merge request, per host. Same
# signature on both sides (see `providers/`), so the dispatch is a lookup
# rather than a branch.
PULL_REQUEST_OPENERS: dict[str, "PullRequestOpener"] = {
    "github": github_provider.open_pull_request,
    "gitlab": gitlab_provider.open_merge_request,
}

# Username sent alongside a `SOURCE_FILE` token when pushing over HTTPS.
# Both hosts authenticate on the token alone and document a fixed
# placeholder for the username half of basic auth: `x-access-token` for
# GitHub, `oauth2` for GitLab.
PROVIDER_PUSH_USERNAMES: dict[str, str] = {
    "github": "x-access-token",
    "gitlab": "oauth2",
}

# Environment variables the one-shot git credential helper reads the
# `SOURCE_FILE` credential from. The token is passed this way, never in
# `argv` or in a URL — see the module docstring.
_GIT_USERNAME_ENVVAR = "VAULTSHIELD_GIT_USERNAME"
_GIT_TOKEN_ENVVAR = "VAULTSHIELD_GIT_TOKEN"

# The helper itself: a `!`-prefixed value is a shell command, which git runs
# with the credential operation (`get`/`store`/`erase`) as its first
# argument. Only `get` is answered; `store`/`erase` are no-ops, because there
# is nothing to cache — the credential lives in
# `~/.vaultshield/credentials.yml` and is re-resolved per submission.
_GIT_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && printf "username=%s\\npassword=%s\\n" '
    '"$' + _GIT_USERNAME_ENVVAR + '" "$' + _GIT_TOKEN_ENVVAR + '"; }; f'
)

# Turns off git's interactive credential prompt. Output is captured, so a
# prompt would be invisible and would block until the timeout; with this
# set, a missing credential fails immediately with git's own message.
_GIT_TERMINAL_PROMPT_ENVVAR = "GIT_TERMINAL_PROMPT"

# Bound on one git invocation. Generous because a push transfers real
# objects over the network, bounded so a wedged remote fails with a message
# instead of hanging the terminal.
_GIT_TIMEOUT_S = 300.0

# Bound on one backend request (the Run read and the `pr_url` write). Same
# value, and the same reasoning, as `uploader._LOGIN_REQUEST_TIMEOUT_S`: one
# interactive, developer-initiated call.
_BACKEND_REQUEST_TIMEOUT_S = 30.0

# Cap on how much of an error response body is quoted back in a
# `SubmitError`, as in `uploader._error_detail`.
_MAX_ERROR_BODY_CHARS = 300


class CredentialError(Exception):
    """Raised when no usable git/host credential could be resolved, or when
    the credentials file cannot be consumed as one.

    Four causes, all local to this module:

    - Neither source yielded a credential: the host CLI is missing or not
      authenticated, and the credentials file has no usable token for the
      provider. This is design.md's headline case, and per its Error
      Handling section no push or PR is ever attempted when it is raised —
      guaranteed here by the fact that this module performs no git or HTTP
      operation at all.
    - `~/.vaultshield/credentials.yml` exists with permissions looser than
      `0o600`, so it was refused rather than read (Requirement 22.2).
    - That file exists but is unreadable, is not valid YAML, or does not
      parse to a mapping.
    - `provider` is not a Repository_Host_Provider this module supports.

    One exception type per module, matching `ConfigError`, `SandboxError`,
    `UploadError`, `RunDirectoryError`, and `GitCaptureError`: `cli.py` maps
    each to a `click.ClickException` at its command boundary, so a failed
    submission reaches the developer as one actionable line rather than a
    traceback.

    No message ever contains a token, or any part of the credentials file's
    content — see the module docstring's section on that.
    """


class SubmitError(Exception):
    """Raised when a Run could not be submitted as a pull/merge request.

    design.md names this type for exactly one of its causes — a Run whose
    Run_Status is not `approved`, "including the actual status" (Requirement
    22.4) — and it carries the rest of the command's failures too, so
    `cli.py` maps one name per module to a `click.ClickException`:

    - the Run's status is not `approved` (no side effect has happened);
    - the backend could not be reached, has no such Run, or answered
      something that is not a Run;
    - the repository, its remote, or its Repository_Host_Provider could not
      be determined;
    - `git push` failed;
    - the host refused to open the pull/merge request (a wrapped
      `providers.ProviderError`);
    - the pull/merge request was opened but the `pr_url` PATCH failed — the
      one case where the message carries a value the developer needs, so it
      quotes the URL.

    A `CredentialError` from `resolve_git_credential` is deliberately NOT
    wrapped in this: it names its own fix (`gh auth login`, or a
    `chmod 600`'d credentials file), it is raised before any side effect,
    and re-wrapping it would bury that message one level deeper.
    """


class PullRequestOpener(Protocol):
    """The call `submit()` makes to open the pull/merge request.

    Structural type of `providers.github.open_pull_request` and
    `providers.gitlab.open_merge_request`, which is also what a caller
    passing `submit(..., open_pull_request=...)` has to satisfy. Declared
    here rather than in `providers/` because it describes what the
    Submit_Command needs, not what a provider happens to offer.
    """

    def __call__(
        self,
        remote_url: str,
        branch: str,
        *,
        title: str,
        body: str,
        base: str | None = None,
        executable: str | None = None,
        token: str | None = None,
        cwd: Path | None = None,
    ) -> str:
        ...  # pragma: no cover - a typing declaration, never executed


@dataclass(frozen=True, repr=False)
class Credential:
    """A resolved git/host credential, as returned by
    `resolve_git_credential`.

    Exactly one of two shapes, discriminated by `source`:

    - `source == SOURCE_CLI`: `executable` is the resolved path to the
      authenticated `gh`/`glab` binary, and `token` is `None`. VaultShield
      holds no secret in this case — the CLI does, and Task 29.3 invokes it
      for the push/PR calls. This is the passthrough Requirement 22.2 asks
      for first.
    - `source == SOURCE_FILE`: `token` is the host token read from
      `~/.vaultshield/credentials.yml`, and `executable` is `None`.

    Frozen, because a credential is a fact resolved at one moment about the
    machine's state; a caller that wants a different one resolves again.

    `repr` is hand-written (hence `repr=False`) so a `Credential` can be
    logged, asserted on, or caught in a traceback frame without printing
    the token — the generated dataclass `repr` would print it in full.
    """

    provider: str
    source: str
    executable: str | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        """Enforces the one-of-two-shapes invariant above.

        Raises `ValueError`, NOT `CredentialError`: a `Credential` whose
        fields contradict its `source` is a bug in the code that built it,
        and `cli.py` turns `CredentialError` into a message telling the
        developer to log in — advice that would be wrong and confusing
        here. `resolve_git_credential` cannot produce either violation, so
        this only ever fires on a hand-constructed instance.
        """
        if self.source == SOURCE_CLI:
            if not self.executable or self.token is not None:
                raise ValueError(
                    f"A {SOURCE_CLI!r}-sourced Credential must carry an "
                    "executable and no token"
                )
        elif self.source == SOURCE_FILE:
            if not self.token or self.executable is not None:
                raise ValueError(
                    f"A {SOURCE_FILE!r}-sourced Credential must carry a token "
                    "and no executable"
                )
        else:
            raise ValueError(
                f"Unknown Credential source {self.source!r}: expected "
                f"{SOURCE_CLI!r} or {SOURCE_FILE!r}"
            )

    def __repr__(self) -> str:
        """Dataclass-style repr with the token redacted.

        The presence of a token is shown (it is what distinguishes the two
        shapes) but never its value.
        """
        token = _REDACTED if self.token is not None else None
        return (
            f"{type(self).__name__}(provider={self.provider!r}, "
            f"source={self.source!r}, executable={self.executable!r}, "
            f"token={token!r})"
        )


def credentials_path(home: Path | None = None) -> Path:
    """Returns the git/host credential file's path:
    `~/.vaultshield/credentials.yml`.

    Pure — computes a path and creates nothing. Unlike the Dashboard_Auth_
    Token's file, this one is never written by VaultShield: the developer
    creates it by hand (and `chmod 600`s it), so there is no `store_*`
    counterpart here.

    `home` defaults to `Path.home()` and exists for the same reason
    `uploader.token_path` takes it: a test can point the whole resolution
    at a real file under `tmp_path`, with real permission bits, without
    mutating the environment of the process it runs in.
    """
    root = home if home is not None else Path.home()
    return root / VAULTSHIELD_HOME_DIRNAME / CREDENTIALS_FILENAME


def resolve_git_credential(
    provider: Literal["github", "gitlab"],
    *,
    home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    check_auth: Callable[[str], bool] | None = None,
) -> Credential:
    """Resolves the local git/host credential for `provider`
    (Requirement 22.2).

    Resolution order, per design.md:

    1. `gh auth status` / `glab auth status` passthrough — if the matching
       CLI is installed and authenticated, it is used directly for the
       push/PR calls (`source == SOURCE_CLI`).
    2. Otherwise `~/.vaultshield/credentials.yml`, which must be mode
       `0o600`, for a host token (`source == SOURCE_FILE`).

    Performs no git operation and no network request beyond the CLI's own
    `auth status` check, so raising leaves nothing pushed and no PR opened.

    `home`, `which`, and `check_auth` are keyword-only additions to
    design.md's reference signature, which stays valid for every caller
    that passes only `provider`; see the module docstring's "Testability
    seams".

    Raises:
        CredentialError: neither source yielded a usable credential; or the
            credentials file was refused for looser-than-`0o600`
            permissions, was unreadable, or could not be parsed as a YAML
            mapping; or `provider` is not supported.
    """
    cli_name = _provider_cli(provider)

    credential = cli_credential(provider, which=which, check_auth=check_auth)
    if credential is not None:
        return credential

    credential = file_credential(provider, home=home)
    if credential is not None:
        return credential

    path = credentials_path(home)
    raise CredentialError(
        f"No usable {provider} credential. The {cli_name} CLI is not "
        f"installed or not authenticated, and {path} holds no token for "
        f"{provider!r}. Run `{cli_name} auth login`, or add a "
        f"`{provider}:` entry with a `{TOKEN_KEY}:` value to {path} and "
        f"`chmod 600` it."
    )


def cli_credential(
    provider: Literal["github", "gitlab"],
    *,
    which: Callable[[str], str | None] | None = None,
    check_auth: Callable[[str], bool] | None = None,
) -> Credential | None:
    """Step 1 of resolution: returns a `SOURCE_CLI` `Credential` if
    `provider`'s host CLI is installed AND reports itself authenticated,
    else `None`.

    `None` covers both misses — not on `PATH`, and on `PATH` but logged
    out — because both mean the same thing to the caller: try the fallback.
    Neither is an error (see the module docstring).

    `which` locates the executable (default `shutil.which`) and
    `check_auth` decides whether it is authenticated (default
    `cli_is_authenticated`); the resolved path from `which` is what gets
    handed to `check_auth` and stored on the `Credential`, so the binary
    that was verified is the same one Task 29.3 will invoke, even if `PATH`
    changes underneath.

    Raises `CredentialError` only for an unsupported `provider`.
    """
    locate = which if which is not None else shutil.which
    is_authenticated = check_auth if check_auth is not None else cli_is_authenticated

    executable = locate(_provider_cli(provider))
    if not executable:
        return None
    if not is_authenticated(executable):
        return None
    return Credential(provider=provider, source=SOURCE_CLI, executable=executable)


def cli_is_authenticated(executable: str) -> bool:
    """Runs `<executable> auth status` and returns whether it exited 0.

    Both `gh` and `glab` exit 0 when they hold a valid credential and
    non-zero when they do not, so the exit status is the entire answer and
    the output is never parsed — which keeps this working across CLI
    versions that reword their messages, and keeps a token out of anything
    this module could later report.

    `stdin` is `/dev/null` so a CLI that would otherwise prompt fails fast
    instead of blocking until the timeout, and both output streams are
    captured (then discarded) rather than inherited — see the module
    docstring for why inheriting them would be actively harmful.

    Every failure mode of the call itself — the binary vanished between
    `which` and here, it is not executable, it hung past the timeout, it
    died on a signal — returns `False`, because each means the same thing:
    this CLI did not produce a credential, so resolution should fall
    through to the file. None of them is a reason to abandon a submission
    that the fallback could still complete.
    """
    try:
        completed = subprocess.run(
            [executable, *AUTH_STATUS_ARGS],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_AUTH_STATUS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def file_credential(
    provider: Literal["github", "gitlab"], *, home: Path | None = None
) -> Credential | None:
    """Step 2 of resolution: returns a `SOURCE_FILE` `Credential` built from
    `~/.vaultshield/credentials.yml`, or `None` if that file does not exist
    or carries no usable token for `provider`.

    Requires the file to be mode `0o600` and refuses to read it otherwise
    (Requirement 22.2). That refusal is a `CredentialError`, not a `None`:
    a developer whose token file is world-readable has a credential that
    VaultShield deliberately will not use, and "no credential found" would
    hide both the fact and the one-command fix. It cannot block a developer
    whose CLI works, because step 1 already returned before this function
    is reached.

    Raises:
        CredentialError: the file's permissions are looser than `0o600`, it
            could not be read, it is not valid YAML, or its document root
            is not a mapping.
    """
    path = credentials_path(home)
    document = _read_credentials_document(path)
    if document is None:
        return None

    token = _provider_token(document, provider)
    if token is None:
        return None
    return Credential(provider=provider, source=SOURCE_FILE, token=token)


def _provider_cli(provider: str) -> str:
    """Returns the host CLI executable name for `provider` (`gh`/`glab`),
    raising `CredentialError` naming the supported providers otherwise.

    Validated up front, in one place, so an unsupported provider fails
    before anything is executed or read rather than as a `KeyError` from
    somewhere deeper.
    """
    cli_name = PROVIDER_CLIS.get(provider)
    if cli_name is None:
        supported = ", ".join(repr(name) for name in sorted(PROVIDER_CLIS))
        raise CredentialError(
            f"Unsupported repository host provider {provider!r}: expected one "
            f"of {supported}."
        )
    return cli_name


def _read_credentials_document(path: Path) -> dict[str, Any] | None:
    """Returns the parsed `credentials.yml` mapping, or `None` when the file
    does not exist or is empty.

    The permission check happens before the first byte is read; see the
    module docstring for why that ordering is the point of the check.

    Raises `CredentialError` — never quoting the file's content — if the
    mode is looser than `0o600`, the file cannot be read, it is not valid
    YAML, or its root is not a mapping.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError(
            f"Cannot check the permissions of the git credential file {path}: "
            f"{exc}"
        ) from exc

    if mode & ~CREDENTIALS_FILE_MODE:
        raise CredentialError(
            f"Refusing to read the git credential file {path}: its permissions "
            f"are {mode:04o}, which is looser than the required "
            f"{CREDENTIALS_FILE_MODE:04o}. Run `chmod 600 {path}`."
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(
            f"Cannot read the git credential file {path}: {exc}"
        ) from exc

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        # The parser's own message quotes the offending region of the
        # document, which here is a line carrying a token — so it is
        # deliberately dropped rather than chained into the message.
        raise CredentialError(
            f"Malformed YAML in the git credential file {path}."
        ) from exc

    if document is None:
        return None
    if not isinstance(document, dict):
        raise CredentialError(
            f"Malformed git credential file {path}: expected a mapping at the "
            "document root."
        )
    return document


def _provider_token(document: dict[str, Any], provider: str) -> str | None:
    """Extracts `provider`'s host token from a parsed `credentials.yml`,
    or `None` if there is no usable one.

    Two accepted shapes, per the module docstring:

        github:
          token: <token>       # canonical
        gitlab: <token>        # scalar shorthand

    Anything else for the provider's value — a missing key, a mapping with
    no `token`, a non-string or blank token, a list — yields `None` rather
    than its own error. They all mean "this file does not authenticate
    `provider`", and `resolve_git_credential`'s final message already
    spells out both the expected entry and the alternative, so a dedicated
    error per malformed shape would add noise without adding an action.
    Surrounding whitespace is stripped, so a trailing newline from an
    editor is not sent as part of a token.
    """
    entry = document.get(provider)
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        value = entry.get(TOKEN_KEY)
        if isinstance(value, str):
            return value.strip() or None
    return None


# --- `submit()` (Task 29.3) -------------------------------------------------


def submit(
    run_id: str,
    backend_url: str,
    token: str,
    *,
    repo_root: Path | None = None,
    remote: str = DEFAULT_REMOTE,
    provider: Literal["github", "gitlab"] | None = None,
    credential: Credential | None = None,
    open_pull_request: PullRequestOpener | None = None,
) -> str:
    """Submits an approved Run as a pull/merge request and returns its URL
    (Requirements 22.1, 22.3, 22.4).

    Reads the Run from `<backend_url>/api/runs/<run_id>/` with the
    Dashboard_Auth_Token `token`. If its Run_Status is not `approved`, raises
    `SubmitError` naming the actual status and does nothing else — no branch
    is pushed, no request is opened, nothing is written back. Otherwise
    pushes `vaultshield/run-<run_id>` to `remote`, opens a pull request
    (GitHub) or merge request (GitLab) with the credential
    `resolve_git_credential` resolves, and PATCHes `{"pr_url": <url>}` to the
    same Run endpoint. Returns that URL.

    The keyword-only arguments are additions to design.md's
    `submit(run_id, backend_url, token)`, which stays valid as written:

        repo_root: the checkout to push from (default: the invocation
            directory). Its `remote` is also what determines which host and
            which repository the request is opened on.
        remote: the git remote to push to (default `origin`).
        provider: the Repository_Host_Provider, when the remote's hostname
            does not identify it (a self-managed GitLab at
            `git.example.com`).
        credential: an already-resolved credential, instead of resolving one.
        open_pull_request: the provider call, instead of dispatching on
            `provider`.

    Raises:
        SubmitError: the Run is not `approved` (message includes its actual
            status); the backend could not be reached or has no such Run;
            the remote or its provider could not be determined; the push
            failed; the host refused the request; or the request was opened
            and the `pr_url` PATCH then failed (message includes the URL).
        CredentialError: no usable git/host credential — raised before the
            push, so nothing has happened yet (Requirement 22.2).
    """
    branch = _run_branch(run_id)
    run_url = _build_url(
        backend_url, RUN_DETAIL_PATH_TEMPLATE.format(run_id=run_id)
    )

    # One client for both backend calls, so the auth header is built once and
    # the connection is reused across the read and the write.
    with httpx.Client(
        headers=_auth_headers(token), timeout=_BACKEND_REQUEST_TIMEOUT_S
    ) as client:
        run = _fetch_run(client, run_url, run_id)
        # Requirement 22.4's gate. Everything below this line is a side
        # effect, and everything above it is a read.
        _require_approved(run, run_id)

        root = repo_root if repo_root is not None else Path.cwd()
        remote_url = _remote_url(root, remote)
        host_provider = provider or detect_provider(remote_url)
        resolved = (
            credential
            if credential is not None
            else resolve_git_credential(host_provider)
        )

        _push_branch(root, remote, branch, resolved, host_provider)

        opener = open_pull_request or _pull_request_opener(host_provider)
        pr_url = _open_request(
            opener,
            remote_url=remote_url,
            branch=branch,
            base=_remote_default_branch(root, remote),
            title=_request_title(run_id),
            body=_request_body(run_id, run, backend_url),
            credential=resolved,
            cwd=root,
        )

        _record_pr_url(client, run_url, pr_url, run_id)

    return pr_url


def detect_provider(remote_url: str) -> str:
    """Returns the Repository_Host_Provider `remote_url` points at
    (`"github"` or `"gitlab"`), from its hostname.

    Matches on a substring of the host (see `PROVIDER_HOST_KEYWORDS`), so
    self-managed instances that keep the product name in their hostname are
    recognised without configuration.

    Raises `SubmitError` naming the host, the supported providers, and the
    way to say so explicitly, if the hostname identifies neither. Guessing
    would mean opening a request against the wrong host's API with a token
    intended for another.
    """
    try:
        host = parse_remote_url(remote_url).host
    except ProviderError as exc:
        raise SubmitError(str(exc)) from exc

    lowered = host.lower()
    for name, keyword in PROVIDER_HOST_KEYWORDS.items():
        if keyword in lowered:
            return name

    supported = ", ".join(repr(name) for name in sorted(PROVIDER_HOST_KEYWORDS))
    raise SubmitError(
        f"Cannot tell which repository host {host!r} is (from remote "
        f"{remote_url!r}). Supported providers are {supported}; pass the "
        "provider explicitly for a self-managed instance."
    )


def _run_branch(run_id: str) -> str:
    """The Run's branch name, `vaultshield/run-<run_id>`.

    Delegates to `git_capture.run_branch_name` rather than rebuilding the
    string: that module owns the spelling for all four components that need
    it (capture, diff, submit, teardown), and it validates `run_id` on the
    way — which matters here because `run_id` arrives from a command line
    and is interpolated into both a git ref and a URL path.

    `RunDirectoryError` is re-raised as `SubmitError` so a caller catching
    this module's failure type does not also have to know
    `run_directory`'s — the same wrapping `uploader.upload_run` does for the
    same reason.
    """
    try:
        return run_branch_name(run_id)
    except RunDirectoryError as exc:
        raise SubmitError(f"Cannot submit run {run_id!r}: {exc}") from exc


def _fetch_run(
    client: httpx.Client, run_url: str, run_id: str
) -> dict[str, Any]:
    """Reads the Run from the backend, for the status gate.

    A read, with no side effect of its own, so every failure here leaves the
    submission exactly as unstarted as a rejected status does.

    Raises `SubmitError` if the backend cannot be reached, does not have
    this Run (a `404`, reported as its own case because it is the one a
    developer causes by mistyping a `run_id`), answers any other non-2xx
    status, or answers with something that is not a JSON object.
    """
    try:
        response = client.get(run_url)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise SubmitError(
            f"Cannot reach the VaultShield backend at {run_url}: {exc}"
        ) from exc

    if response.status_code == 404:
        raise SubmitError(
            f"The VaultShield backend has no run {run_id!r} ({run_url} "
            "answered HTTP 404). Check the run id, and that this Run's "
            "artifacts were uploaded."
        )
    if not response.is_success:
        raise SubmitError(
            f"Cannot read run {run_id!r}: {run_url} answered HTTP "
            f"{response.status_code}: {_error_detail(response)}"
        )

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise SubmitError(
            f"The backend at {run_url} answered with a body that is not JSON, "
            f"so run {run_id!r} could not be read: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise SubmitError(
            f"The backend at {run_url} answered with a "
            f"{type(payload).__name__} rather than a run object, so run "
            f"{run_id!r} could not be read."
        )
    return payload


def _require_approved(run: dict[str, Any], run_id: str) -> None:
    """Enforces Requirement 22.4: raises `SubmitError` unless the Run's
    Run_Status is `approved`, naming the status it actually has.

    The actual status is the whole point of the message — see the module
    docstring. A Run with no `status` at all is reported separately: that is
    a backend contract problem rather than a review-state problem, and
    reporting it as "not approved" would send the developer to the dashboard
    to fix something that is not there.
    """
    status = run.get(STATUS_FIELD)
    if not isinstance(status, str) or not status:
        raise SubmitError(
            f"Cannot submit run {run_id!r}: the backend returned it without a "
            f"{STATUS_FIELD!r}, so there is no review state to check."
        )
    if status != APPROVED_STATUS:
        raise SubmitError(
            f"Run {run_id} is {status!r}, not {APPROVED_STATUS!r}, so it "
            "cannot be submitted. Nothing was pushed and no pull request was "
            "opened."
        )


def _remote_url(repo_root: Path, remote: str) -> str:
    """Returns `remote`'s URL in the repository at `repo_root`.

    `git remote get-url` rather than reading `.git/config`, so an
    `insteadOf`/`pushInsteadOf` rewrite the developer configured is applied
    the same way git itself would apply it.

    Raises `SubmitError` if the directory is not a git repository or has no
    such remote — git's own message says which.
    """
    url = _git(
        repo_root,
        ["remote", "get-url", remote],
        step=f"reading the URL of git remote {remote!r}",
    ).strip()
    if not url:
        raise SubmitError(
            f"Git remote {remote!r} in {repo_root} has no URL, so there is no "
            "repository host to submit to."
        )
    return url


def _remote_default_branch(repo_root: Path, remote: str) -> str | None:
    """The remote's default branch (the base a pull request targets), or
    `None` if it cannot be resolved locally.

    Read from `refs/remotes/<remote>/HEAD`, which `git clone` sets and
    `git remote set-head` refreshes. A clone that has never recorded it
    yields `None` rather than an error: both provider paths can still
    proceed — the host CLI applies its own default, and the API path reads
    the repository's `default_branch` — so failing here would turn a
    resolvable submission into an error.
    """
    try:
        output = _git(
            repo_root,
            ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"],
            step=f"resolving the default branch of {remote!r}",
        )
    except SubmitError:
        return None

    reference = output.strip()
    prefix = f"{remote}/"
    if reference.startswith(prefix):
        reference = reference[len(prefix) :]
    return reference or None


def git_credential_args(
    credential: Credential, provider: str
) -> tuple[list[str], dict[str, str]]:
    """The `git -c` overrides and the environment additions that authenticate
    one git command with `credential`.

    Returns `(config_args, env)`, both ready to hand to a git invocation:
    `config_args` goes before the subcommand (`git <config_args> push ...`)
    and `env` is added to the process environment.

    - A `SOURCE_CLI` credential adds no config at all: git uses the
      developer's own transport credentials (an SSH key, or the credential
      helper `gh auth setup-git`/`glab auth git-credential` installed), which
      is Requirement 22.2's passthrough carried through to the git layer.
    - A `SOURCE_FILE` credential is supplied through a one-shot credential
      helper that reads it out of the environment, so the token never appears
      in `argv` or in a remote URL — see the module docstring.

    `GIT_TERMINAL_PROMPT=0` is set either way, so a git command with no usable
    credential fails with git's own message instead of blocking on a prompt
    nobody can see.

    Public, and separate from the push, because `teardown.py` authenticates
    its own git commands (`ls-remote` and the branch delete Requirement 23.2
    asks for) with a credential from the same `resolve_git_credential` — and
    a second copy of the token-handling above is exactly the kind of thing
    that drifts.
    """
    env = {_GIT_TERMINAL_PROMPT_ENVVAR: "0"}
    if not credential.token:
        return [], env

    env[_GIT_USERNAME_ENVVAR] = PROVIDER_PUSH_USERNAMES.get(provider, "vaultshield")
    env[_GIT_TOKEN_ENVVAR] = credential.token
    # An empty value resets git's helper list, so the helper that follows is
    # the only one consulted for this command rather than one candidate among
    # whatever is configured globally.
    config_args = [
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper={_GIT_CREDENTIAL_HELPER}",
    ]
    return config_args, env


def _push_branch(
    repo_root: Path,
    remote: str,
    branch: str,
    credential: Credential,
    provider: str,
) -> None:
    """Pushes `branch` to `remote` (Requirement 22.1's push half).

    The first side effect of a submission, and it is deliberately a plain
    `git push <remote> <branch>`: no `--force` (a Run's branch is written
    once, so a rejected non-fast-forward means something else moved and is
    worth stopping for), and no `--set-upstream` (which would write to the
    developer's `.git/config` for a submission that does not need it).
    Re-pushing an unchanged branch is a no-op, so a submission whose later
    steps failed can be retried from here.

    A `SOURCE_FILE` credential is supplied through a one-shot credential
    helper reading the token from the environment; a `SOURCE_CLI` credential
    adds nothing, leaving git to use the developer's own transport
    credentials. See the module docstring for why the token goes nowhere
    near `argv` or the remote URL.

    Raises `SubmitError` naming git's own output if the push fails — most
    usefully when the branch does not exist in this checkout (see the module
    docstring's named gap) or when the remote refuses the credential.
    """
    config_args, env = git_credential_args(credential, provider)

    _git(
        repo_root,
        [*config_args, "push", remote, branch],
        step=f"pushing {branch} to {remote}",
        env=env,
    )


def _open_request(
    opener: PullRequestOpener,
    *,
    remote_url: str,
    branch: str,
    base: str | None,
    title: str,
    body: str,
    credential: Credential,
    cwd: Path,
) -> str:
    """Opens the pull/merge request and returns its URL (Requirement 22.1's
    request half).

    Hands the credential to the provider as the two fields it acts on —
    `executable` for `gh`/`glab` passthrough, `token` for the REST path —
    rather than as the `Credential` itself, which is what keeps `providers/`
    from having to import this module (see that package's docstring).

    `ProviderError` is wrapped in `SubmitError` so a caller has one
    exception type for a failed submission. The push has already happened by
    this point, which the message says out loud: the branch is on the remote
    and re-running `submit` is the retry, so a developer who reads "opening
    the pull request failed" should not be left wondering whether their
    branch made it.
    """
    try:
        return opener(
            remote_url,
            branch,
            title=title,
            body=body,
            base=base,
            executable=credential.executable,
            token=credential.token,
            cwd=cwd,
        )
    except ProviderError as exc:
        raise SubmitError(
            f"{exc} The branch {branch} was pushed, so retrying `vaultshield "
            "submit` once the cause is fixed will not push it again."
        ) from exc


def _record_pr_url(
    client: httpx.Client, run_url: str, pr_url: str, run_id: str
) -> None:
    """PATCHes `{"pr_url": <pr_url>}` onto the Run (Requirement 22.3).

    Exactly that one field, which is also the only value this module ever
    writes to the backend (Requirement 22.5).

    Raises `SubmitError` quoting `pr_url` if the request could not be made
    or was refused: the pull/merge request exists at that URL and only the
    record of it is missing, so the URL is the actionable part of the
    message — see the module docstring.
    """
    try:
        response = client.patch(run_url, json={PR_URL_FIELD: pr_url})
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise SubmitError(
            f"The pull request for run {run_id} was opened at {pr_url}, but "
            f"the VaultShield backend at {run_url} could not be reached to "
            f"record it: {exc}"
        ) from exc

    if not response.is_success:
        raise SubmitError(
            f"The pull request for run {run_id} was opened at {pr_url}, but "
            f"{run_url} answered HTTP {response.status_code} when recording "
            f"it: {_error_detail(response)}"
        )


def _pull_request_opener(provider: str) -> PullRequestOpener:
    """The provider call that opens a request on `provider`.

    Raises `CredentialError`, not `SubmitError`, for an unsupported provider:
    `_provider_cli` has already rejected every value that could reach here
    through `resolve_git_credential`, so this only fires for a `provider=`
    argument that named a host this CLI has no client for, and reusing the
    same message keeps one answer to "which providers are supported".
    """
    opener = PULL_REQUEST_OPENERS.get(provider)
    if opener is None:
        supported = ", ".join(repr(name) for name in sorted(PULL_REQUEST_OPENERS))
        raise CredentialError(
            f"Unsupported repository host provider {provider!r}: expected one "
            f"of {supported}."
        )
    return opener


def _request_title(run_id: str) -> str:
    """The pull/merge request title.

    Leads with the product name and the `run_id`, matching
    `git_capture._commit_message`, so the request, the commit, the
    Run_Directory, and the dashboard record all read as the same Run.
    """
    return f"VaultShield run {run_id}"


def _request_body(run_id: str, run: dict[str, Any], backend_url: str) -> str:
    """The pull/merge request description.

    Says what a reviewer on the host cannot otherwise know: that the branch
    was produced by an agent inside a VaultShield Sandbox, which agent, and
    that it was approved in the dashboard before being submitted (a Run
    reaches this function only through the `approved` gate). The Run's id and
    the backend it was reviewed on are included so the full Event_Log, diff,
    and security findings are findable from the request.

    The agent is read from the Run the backend returned rather than passed
    in, and is omitted if absent, so the body degrades to the parts that are
    always true instead of asserting an agent that was never recorded.
    """
    agent = run.get(AGENT_FIELD)
    lines = [
        "Opened by `vaultshield submit`.",
        "",
        f"- Run: `{run_id}`",
    ]
    if isinstance(agent, str) and agent:
        lines.append(f"- Agent: `{agent}`")
    lines += [
        f"- Reviewed in VaultShield at {backend_url}",
        "",
        "This branch was produced by an AI coding agent in a VaultShield "
        "sandbox and was approved in the VaultShield dashboard before "
        "submission.",
    ]
    return "\n".join(lines)


def _git(
    repo_root: Path,
    args: list[str],
    *,
    step: str,
    env: dict[str, str] | None = None,
) -> str:
    """Runs one `git` command in `repo_root` and returns its stdout.

    The single place every git invocation in this module goes through, so
    each failure is wrapped exactly once and every message names the step.
    Arguments are a list, never a shell string, so a branch name or a remote
    URL is passed as data.

    `env` adds to (rather than replaces) the current environment: git needs
    `HOME`, `PATH`, and `SSH_AUTH_SOCK` from it to find the developer's
    configuration and keys. `stdin` is `/dev/null` so a command that would
    read from the terminal fails instead of blocking on captured input.

    Raises `SubmitError` naming `step` and git's own output on a non-zero
    exit, a missing `git`, or a timeout.
    """
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SubmitError(
            f"Timed out after {_GIT_TIMEOUT_S:.0f}s while {step}."
        ) from exc
    except OSError as exc:
        raise SubmitError(f"Cannot run git while {step}: {exc}") from exc

    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "no output captured"
        )
        raise SubmitError(
            f"Git failed while {step} (exit code {completed.returncode}): "
            f"{detail}"
        )
    return completed.stdout


def _auth_headers(token: str) -> dict[str, str]:
    """The `Authorization: Token <token>` header both backend requests
    carry.

    Built from `uploader`'s own public constants rather than respelling the
    scheme, so the Dashboard_Auth_Token is presented exactly as DRF's
    `TokenAuthentication` parses it in one place for the whole CLI.
    """
    return {AUTH_HEADER_NAME: f"{AUTH_SCHEME} {token}"}


def _build_url(backend_url: str, path: str) -> str:
    """Joins the backend origin and an API path, tolerating a trailing slash
    on the origin and a missing one on the path.

    Same one-liner (and the same reason for not using `urllib.parse.urljoin`,
    which would resolve a slash-less path against the origin's last segment)
    as `uploader._build_url` and `browser_capture._build_url`; kept local
    because both of those are private to their modules.
    """
    return f"{backend_url.rstrip('/')}/{path.lstrip('/')}"


def _error_detail(response: httpx.Response) -> str:
    """A short single-line summary of an error response body, for a
    `SubmitError` message.

    Same shape and reasoning as `uploader._error_detail`: DRF's error detail
    is worth quoting verbatim, an HTML page from a proxy is not, so the body
    is collapsed to one line and truncated.
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
