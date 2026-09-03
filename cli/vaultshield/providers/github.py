"""GitHub client: opening a pull request for a pushed branch.

One function, `open_pull_request`, in the two shapes `resolve_git_credential`
can produce (see this package's `__init__` docstring):

- an authenticated `gh` (`executable=`), used as
  `gh pr create --repo <owner/repo> --head <branch> ...`; the credential
  never leaves `gh`.
- a host token (`token=`), used as
  `POST /repos/<owner>/<repo>/pulls` against GitHub's REST API with an
  `Authorization: Bearer` header.

Covers the GitHub half of Requirement 22.1's "SHALL open a pull request or
merge request using a local git or host credential". The branch is expected
to already exist on the remote — `submit.py` pushes it first — so nothing
here pushes anything, and `--head` names a ref the host can already see.

### The base branch

`base` is the branch the pull request merges into. `submit.py` resolves it
locally from the remote's `HEAD` and passes `None` when it could not, which
the two paths answer differently:

- `gh` is left to its own default (the flag is omitted), which is the
  repository's default branch as GitHub reports it.
- the API path has no such default — `base` is a required field of the
  create-pull-request payload — so the repository is read first
  (`GET /repos/<owner>/<repo>` -> `default_branch`) and that value is used.

### GitHub Enterprise

`api.github.com` serves github.com, while a GitHub Enterprise Server host
serves the same API under `https://<host>/api/v3`. Both are derived from
the remote's own host, so an Enterprise remote works without configuration;
`gh` is additionally given `<host>/<owner>/<repo>` as `--repo`, the form it
uses to address a non-github.com host.
"""

from __future__ import annotations

from pathlib import Path

from vaultshield.providers import (
    ProviderError,
    RemoteRepository,
    last_url,
    parse_remote_url,
    request_json,
    required_url,
    run_host_cli,
)

# github.com's REST API lives on its own host; every other (Enterprise)
# host serves the same API under this path prefix.
PUBLIC_HOST = "github.com"
PUBLIC_API_ORIGIN = "https://api.github.com"
ENTERPRISE_API_PREFIX = "/api/v3"

# REST paths, relative to the API origin resolved above.
PULLS_PATH_TEMPLATE = "/repos/{path}/pulls"
REPOSITORY_PATH_TEMPLATE = "/repos/{path}"

# Response keys read from those endpoints.
HTML_URL_KEY = "html_url"
DEFAULT_BRANCH_KEY = "default_branch"

# Request field names of the create-pull-request payload.
TITLE_FIELD = "title"
BODY_FIELD = "body"
HEAD_FIELD = "head"
BASE_FIELD = "base"

# `gh` subcommand that opens a pull request.
CLI_CREATE_ARGS = ("pr", "create")

# Headers GitHub's REST API expects: a bearer token, the versioned JSON
# media type, and the API version pin GitHub asks clients to send so a
# future default version change cannot alter the response shape read below.
_ACCEPT_HEADER = "application/vnd.github+json"
_API_VERSION_HEADER = "X-GitHub-Api-Version"
_API_VERSION = "2022-11-28"


def open_pull_request(
    remote_url: str,
    branch: str,
    *,
    title: str,
    body: str,
    base: str | None = None,
    executable: str | None = None,
    token: str | None = None,
    cwd: Path | None = None,
    api_origin: str | None = None,
) -> str:
    """Opens a pull request from `branch` on the repository `remote_url`
    points at, and returns its URL.

    Exactly one of `executable` (an authenticated `gh`) and `token` (a host
    token) must be given — they are the two shapes of a resolved
    `Credential`, and which one is present selects the CLI or the REST path.

    `cwd` is where `gh` is invoked, and matters only for the CLI path: `gh`
    reads the local repository's configuration for host and authentication
    context. The repository itself is always named explicitly via `--repo`,
    so the pull request cannot land on a different repository than the
    remote this was called with.

    `api_origin` overrides the REST origin derived from the remote's host
    (`default_api_origin`), so a test can point the real request path at a
    local server instead of at github.com. No production caller passes it.

    Raises:
        ProviderError: the remote URL is unparseable, `gh` failed or printed
            no URL, or the REST API could not be reached, refused the
            request, or answered without a pull request URL.
        ValueError: neither or both of `executable`/`token` were given — a
            bug in the caller, not a host failure (`Credential` makes the
            two shapes mutually exclusive), so it is deliberately not a
            `ProviderError` that would be reported to a developer as
            "GitHub refused this".
    """
    if bool(executable) == bool(token):
        raise ValueError(
            "open_pull_request needs exactly one of `executable` (an "
            "authenticated gh) or `token` (a host token)"
        )

    repository = parse_remote_url(remote_url)
    if executable:
        return _open_via_cli(
            executable, repository, branch, title, body, base, cwd
        )
    assert token is not None  # narrowed by the exactly-one check above
    return _open_via_api(
        token, repository, branch, title, body, base, api_origin
    )


def default_api_origin(host: str) -> str:
    """The REST API origin for a GitHub `host`.

    `api.github.com` for github.com (including its `www.` spelling), and
    `https://<host>/api/v3` for a GitHub Enterprise Server host.
    """
    if host == PUBLIC_HOST or host.endswith(f".{PUBLIC_HOST}"):
        return PUBLIC_API_ORIGIN
    return f"https://{host}{ENTERPRISE_API_PREFIX}"


def _open_via_cli(
    executable: str,
    repository: RemoteRepository,
    branch: str,
    title: str,
    body: str,
    base: str | None,
    cwd: Path | None,
) -> str:
    """Opens the pull request through `gh pr create` and reads its URL out
    of the command's output.

    `--title` and `--body` are both supplied, which is what makes `gh pr
    create` non-interactive; `--head` names the already-pushed branch so
    `gh` neither infers it from the checked-out branch nor offers to push
    one. `--base` is omitted when unknown, leaving `gh` to use the
    repository's default branch.
    """
    argv = [
        executable,
        *CLI_CREATE_ARGS,
        "--repo",
        _cli_repo_argument(repository),
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    if base:
        argv += ["--base", base]

    step = f"opening a pull request for {branch} on {repository.path}"
    return last_url(run_host_cli(argv, step=step, cwd=cwd), step=step)


def _open_via_api(
    token: str,
    repository: RemoteRepository,
    branch: str,
    title: str,
    body: str,
    base: str | None,
    api_origin: str | None,
) -> str:
    """Opens the pull request through GitHub's REST API and returns its
    `html_url`.

    `html_url` rather than `url`: the latter is the API resource, and what
    gets recorded on the Run (and read by a human) is the page a reviewer
    opens.
    """
    origin = api_origin or default_api_origin(repository.host)
    headers = _api_headers(token)
    target = base or _default_branch(origin, headers, repository)

    step = f"opening a pull request for {branch} on {repository.path}"
    payload = {
        TITLE_FIELD: title,
        BODY_FIELD: body,
        HEAD_FIELD: branch,
        BASE_FIELD: target,
    }
    response = request_json(
        "POST",
        f"{origin}{PULLS_PATH_TEMPLATE.format(path=repository.path)}",
        headers=headers,
        payload=payload,
        step=step,
    )
    return required_url(response, HTML_URL_KEY, step=step)


def _default_branch(
    origin: str, headers: dict[str, str], repository: RemoteRepository
) -> str:
    """Reads the repository's default branch, for the API path's required
    `base` field.

    Raises `ProviderError` if GitHub answers without one, rather than
    guessing at `main`: a wrong `base` opens a pull request against the
    wrong branch, which is worse than a failed submission that names what
    it could not determine.
    """
    step = f"looking up the default branch of {repository.path}"
    response = request_json(
        "GET",
        f"{origin}{REPOSITORY_PATH_TEMPLATE.format(path=repository.path)}",
        headers=headers,
        step=step,
    )
    branch = response.get(DEFAULT_BRANCH_KEY) if isinstance(response, dict) else None
    if not isinstance(branch, str) or not branch.strip():
        raise ProviderError(
            f"GitHub did not report a {DEFAULT_BRANCH_KEY!r} for "
            f"{repository.path}, so there is no base branch to open a pull "
            "request against."
        )
    return branch.strip()


def _cli_repo_argument(repository: RemoteRepository) -> str:
    """The `--repo` value for `gh`: `owner/repo` on github.com, and
    `host/owner/repo` on a GitHub Enterprise host, which is how `gh`
    addresses a repository outside github.com.
    """
    if repository.host == PUBLIC_HOST or repository.host.endswith(
        f".{PUBLIC_HOST}"
    ):
        return repository.path
    return f"{repository.host}/{repository.path}"


def _api_headers(token: str) -> dict[str, str]:
    """Authentication and content-negotiation headers for GitHub's REST
    API.

    The token travels in a header, never in the URL or on a command line —
    see this package's docstring.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": _ACCEPT_HEADER,
        _API_VERSION_HEADER: _API_VERSION,
    }
