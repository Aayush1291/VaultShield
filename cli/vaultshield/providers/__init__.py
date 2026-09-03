"""Repository_Host_Provider clients: the shared layer.

design.md's package layout puts one module per host under `providers/`
(`github.py`, `gitlab.py`) and annotates `submit.py` as "Submit_Command +
provider clients". This package is the provider half of that split: each
module knows how to open a pull request (GitHub) or merge request (GitLab)
for an already-pushed branch, and knows nothing about Runs, the
VaultShield_Backend, or how a credential was resolved. `submit.py` owns
the Run, the status gate, the push, and the `pr_url` PATCH, and dispatches
here for the one host-specific step (Requirement 22.1's "SHALL open a pull
request or merge request").

This module holds what both providers need and neither should own: the
remote-URL parser, the host-CLI runner, the JSON-over-HTTP helper, and
`ProviderError`.

### Why providers take `executable`/`token` rather than a `Credential`

`resolve_git_credential` (Task 29.1) returns a `Credential` in one of two
shapes — an authenticated `gh`/`glab` path, or a host token — and each
provider has to act differently for each: pass the work through to the CLI
that already holds the credential, or call the host's REST API directly.

The provider functions therefore take `executable=` and `token=` (exactly
one of them set), not the `Credential` itself. That keeps the dependency
arrow pointing one way — `submit` imports `providers`, never the reverse —
which is what makes both testable in isolation and avoids a circular
import between a `Credential` defined in `submit.py` and the provider
modules `submit.py` dispatches to.

### Two paths per provider, and why both exist

- **CLI passthrough** (`executable=`): `gh pr create` / `glab mr create`.
  The credential stays where the developer already keeps it, VaultShield
  never handles a token, and the host CLI resolves the repository, the
  default branch, and any host-specific detail itself. This is the path
  Requirement 22.2 asks for first.
- **REST API** (`token=`): the fallback for a machine with no host CLI (a
  CI runner, a container), using the token from
  `~/.vaultshield/credentials.yml`. Without it the credentials-file half
  of Requirement 22.2 could resolve a credential that nothing could use.

### Tokens stay out of messages

`ProviderError` messages quote command output and HTTP error bodies, so
two rules hold throughout: a token is never placed on a command line or in
a URL (it travels in a request header, or stays inside the host CLI), and
no message ever interpolates one. A host that echoes a token back in an
error body is the one leak this cannot prevent; none of the endpoints used
here does.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

# Suffix git remotes conventionally carry and that no API or CLI wants as
# part of a repository path.
_GIT_SUFFIX = ".git"

# Matches an scp-like remote (`git@github.com:org/repo.git`), which is
# the default form for SSH remotes and is NOT a URL — `urlsplit` reads the
# whole thing as a path. Recognised by "a colon before any slash, and no
# `scheme://`".
_SCP_LIKE_PATTERN = re.compile(r"\A(?:(?P<user>[^/@]+)@)?(?P<host>[^/:]+):(?P<path>.+)\Z")

# A URL in captured CLI output. Both `gh pr create` and `glab mr create`
# print the created pull/merge request's URL; this is how it is read back
# (see `last_url`).
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")

# Trailing characters a URL in prose picks up but does not own.
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}\u2019\u201d"

# Bound on one host-CLI invocation. Generous because `gh pr create` /
# `glab mr create` make several round trips to the host (resolve the
# repository, look up the default branch, create the request), bounded so a
# wedged network fails with a message instead of hanging the terminal.
_CLI_TIMEOUT_S = 120.0

# Bound on one REST call. Matches `uploader`'s login timeout: a single
# developer-initiated request against a real host.
_HTTP_TIMEOUT_S = 30.0

# Cap on how much of an error response body is quoted back, as in
# `uploader._error_detail`: a host's JSON error message is short and
# actionable, an HTML error page from a proxy is neither.
_MAX_ERROR_BODY_CHARS = 300


class ProviderError(Exception):
    """Raised when a Repository_Host_Provider could not open the pull or
    merge request it was asked for.

    Covers the remote URL being unparseable, the host CLI failing or
    printing no URL, and the REST API being unreachable, refusing the
    request, or answering without a usable URL.

    `submit()` wraps this in its own `SubmitError` so a caller has one
    exception type per command boundary (the same way `uploader.upload_run`
    wraps `RunDirectoryError`), which is why this type is deliberately
    provider-shaped rather than submit-shaped: nothing here knows it is
    being called for a Run.
    """


@dataclass(frozen=True)
class RemoteRepository:
    """A git remote resolved into the two things a host API needs: the host
    it lives on, and the repository's path on that host.

    `path` is the full namespace path with no `.git` suffix and no
    surrounding slashes — `org/repo` on GitHub, and on GitLab possibly
    nested (`group/subgroup/repo`), which is why it is kept as one string
    rather than split into owner and name. `owner`/`name` are available for
    the callers that genuinely need the last segment alone.

    Frozen: it describes a remote as configured at one moment, and nothing
    downstream should edit one.
    """

    host: str
    path: str

    @property
    def name(self) -> str:
        """The repository's own name: the last segment of `path`."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def owner(self) -> str:
        """Everything before the repository name — a user, an organisation,
        or a nested GitLab group path.
        """
        head, separator, _name = self.path.rpartition("/")
        return head if separator else ""


def parse_remote_url(remote_url: str) -> RemoteRepository:
    """Resolves a git remote URL into a `RemoteRepository`.

    Accepts every remote form git itself accepts for a hosted repository:

        git@github.com:org/repo.git            (scp-like, the SSH default)
        ssh://git@github.com:22/org/repo.git
        https://github.com/group/sub/repo.git
        https://token@gitlab.example.com/group/repo

    Userinfo (`git@`, or an embedded token) and any port are dropped, since
    neither is part of the repository's identity on the host, and dropping
    an embedded credential here means it cannot reach a message or an API
    call. The `.git` suffix and surrounding slashes are stripped.

    Raises `ProviderError` if the remote carries no host, or a path with
    fewer than two segments: every GitHub and GitLab repository is at least
    `<namespace>/<name>`, so anything shorter is a local path or a typo,
    and guessing at one would produce an API call against a URL nobody
    meant.
    """
    remote = remote_url.strip()
    if not remote:
        raise ProviderError(
            "Cannot open a pull request: the git remote URL is empty."
        )

    host, path = _split_remote(remote)
    host = _strip_userinfo_and_port(host)
    path = path.strip("/")
    if path.endswith(_GIT_SUFFIX):
        path = path[: -len(_GIT_SUFFIX)]
    path = path.strip("/")

    if not host or path.count("/") < 1 or not all(path.split("/")):
        raise ProviderError(
            f"Cannot read a repository host and path out of the git remote "
            f"{remote_url!r}. Expected something like "
            "'git@github.com:org/repo.git' or "
            "'https://gitlab.com/group/repo.git'."
        )
    return RemoteRepository(host=host, path=path)


def run_host_cli(
    argv: list[str], *, step: str, cwd: Path | None = None
) -> str:
    """Runs a host CLI (`gh`/`glab`) command and returns its stdout.

    `stdin` is `/dev/null` so a CLI that would otherwise prompt fails
    immediately instead of blocking until the timeout, and both streams are
    captured rather than inherited — the same reasoning as
    `submit.cli_is_authenticated`, plus the fact that the created request's
    URL is read out of stdout here.

    Arguments are passed as a list, never through a shell, so a title or
    body containing quotes, backticks, or newlines is passed through as
    data.

    Raises `ProviderError` naming `step`, the exit code, and the CLI's own
    output if the command fails, cannot be executed, or times out. The
    CLI's message is the useful part of that error ("a pull request for
    branch ... already exists"), so it is quoted rather than summarised.
    """
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(
            f"Timed out after {_CLI_TIMEOUT_S:.0f}s while {step} with "
            f"{argv[0]!r}."
        ) from exc
    except OSError as exc:
        raise ProviderError(f"Cannot run {argv[0]!r} while {step}: {exc}") from exc

    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "no output captured"
        )
        raise ProviderError(
            f"{argv[0]} failed while {step} (exit code "
            f"{completed.returncode}): {detail}"
        )
    return completed.stdout


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    step: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    """Sends one JSON REST request and returns the parsed response body.

    The single place both providers' API paths go through, so every
    transport failure, non-2xx status, and unparseable body is reported the
    same way and named by `step`. `headers` carries the token; it is never
    echoed into a message.

    Raises `ProviderError` if the host cannot be reached, answers a non-2xx
    status (quoting a truncated single-line form of its error body, which
    for both hosts is the actionable part), or answers with something that
    is not JSON.
    """
    try:
        response = httpx.request(
            method, url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT_S
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise ProviderError(
            f"Cannot reach the repository host at {url} while {step}: {exc}"
        ) from exc

    if not response.is_success:
        raise ProviderError(
            f"The repository host refused the request while {step}: {url} "
            f"answered HTTP {response.status_code}: {_error_detail(response)}"
        )

    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderError(
            f"The repository host answered HTTP {response.status_code} while "
            f"{step} with a body that is not JSON: {exc}"
        ) from exc


def required_url(payload: Any, key: str, *, step: str) -> str:
    """Returns `payload[key]` as a non-empty URL string.

    Both hosts answer a created pull/merge request with its web URL under
    one key (`html_url` on GitHub, `web_url` on GitLab), and that URL is
    the whole point of the call — it is what gets recorded on the Run — so
    a response without it is an error rather than an empty string written
    back to the backend.
    """
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(
            f"The repository host answered without a {key!r} while {step}, so "
            "there is no pull request URL to record."
        )
    return value.strip()


def last_url(output: str, *, step: str) -> str:
    """Returns the last URL printed in `output`.

    How the CLI passthrough path learns the URL it created: both `gh pr
    create` and `glab mr create` print the new request's URL, `gh` as the
    final line and `glab` at the end of a short summary, so the last URL in
    the output is the created request in both cases. Reading the output is
    the only option available — neither CLI is asked for JSON here, because
    the flags that produce it differ between the two and between versions,
    while "prints the URL" has been stable in both.

    Raises `ProviderError` quoting the output if it contains no URL: a
    zero-exit CLI that printed no URL did something this code does not
    understand, and inventing a `pr_url` for it would put a wrong URL on
    the Run.
    """
    matches = _URL_PATTERN.findall(output)
    if not matches:
        detail = " ".join(output.split()) or "no output captured"
        raise ProviderError(
            f"No pull request URL was printed while {step}: {detail}"
        )
    return matches[-1].rstrip(_URL_TRAILING_PUNCTUATION)


def _split_remote(remote: str) -> tuple[str, str]:
    """Splits a remote into its `(host, path)` halves, handling both the
    scp-like form and every `scheme://` form.

    The scp-like check comes first and is guarded on there being no `://`,
    because `ssh://git@host:22/org/repo` also contains a colon and IS a
    URL.
    """
    if "://" not in remote:
        match = _SCP_LIKE_PATTERN.match(remote)
        if match is None:
            return "", remote
        return match.group("host"), match.group("path")

    parts = urlsplit(remote)
    return parts.netloc, parts.path


def _strip_userinfo_and_port(netloc: str) -> str:
    """Reduces a URL authority to its hostname: drops `user[:password]@`
    and any `:port`.

    Dropping the userinfo is what keeps a token embedded in a remote URL
    (`https://<token>@github.com/org/repo.git`, which some CI setups
    configure) out of every API URL built from this host and out of every
    message quoting one.
    """
    _userinfo, separator, host = netloc.rpartition("@")
    host = host if separator else netloc
    # An IPv6 literal is bracketed (`[::1]:22`); the port is whatever
    # follows the closing bracket, so the split point is the last colon
    # outside the brackets.
    if host.startswith("["):
        closing = host.find("]")
        return host[: closing + 1] if closing != -1 else host
    return host.split(":", 1)[0]


def _error_detail(response: httpx.Response) -> str:
    """A short single-line summary of an error response body, for a
    `ProviderError` message.

    Same shape and same reasoning as `uploader._error_detail`, kept local
    because `uploader`'s is private to that module and this package
    deliberately imports nothing from it.
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
