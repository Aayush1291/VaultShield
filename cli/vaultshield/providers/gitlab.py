"""GitLab client: opening a merge request for a pushed branch.

The GitLab counterpart of `providers/github.py`, with the same one-function
surface and the same two credential shapes (see this package's `__init__`
docstring):

- an authenticated `glab` (`executable=`), used as
  `glab mr create --repo <path> --source-branch <branch> ... --yes`; the
  credential never leaves `glab`.
- a host token (`token=`), used as
  `POST /api/v4/projects/<path>/merge_requests` with a `PRIVATE-TOKEN`
  header.

Covers the GitLab half of Requirement 22.1. As with GitHub, the branch is
expected to already be on the remote — `submit.py` pushes it first — so
nothing here pushes, and `--source-branch`/`source_branch` name a ref the
host can already see.

### The target branch

GitLab requires a target branch for a merge request: the API rejects a
create without `target_branch`, and `glab` in a non-interactive invocation
needs `--target-branch` (its own no-local-clone example passes it
explicitly). `submit.py` resolves it locally from the remote's `HEAD`; when
that fails and `base` arrives as `None`, the project's `default_branch` is
read from the API (both paths, since a prompt-free `glab` cannot be relied
on to pick one).

### Self-managed GitLab

Unlike GitHub, GitLab's API always lives under `/api/v4` on the host
serving the repository, so gitlab.com and a self-managed instance differ
only by hostname. `glab` is given the project's full URL rather than its
bare path on a non-gitlab.com host, since a bare path would otherwise be
resolved against whatever host `glab` is configured for. That URL is rebuilt
from the parsed host and path rather than reusing the remote as given, so a
credential embedded in the remote URL never reaches a command line.

### Project paths are URL-encoded, not interpolated

GitLab addresses a project either by numeric id or by its URL-encoded full
path, so `group/subgroup/repo` becomes `group%2Fsubgroup%2Frepo`. Encoding
it (rather than pasting it into the path) is what keeps a nested group path
addressing the project it names instead of a deeper API route.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from vaultshield.providers import (
    ProviderError,
    RemoteRepository,
    last_url,
    parse_remote_url,
    request_json,
    required_url,
    run_host_cli,
)

# gitlab.com, for the one place a host needs distinguishing: what to pass
# `glab` as `--repo`.
PUBLIC_HOST = "gitlab.com"

# Every GitLab instance serves its REST API here.
API_PREFIX = "/api/v4"

# REST paths, relative to `https://<host>/api/v4`.
MERGE_REQUESTS_PATH_TEMPLATE = "/projects/{project}/merge_requests"
PROJECT_PATH_TEMPLATE = "/projects/{project}"

# Response keys read from those endpoints.
WEB_URL_KEY = "web_url"
DEFAULT_BRANCH_KEY = "default_branch"

# Request field names of the create-merge-request payload.
TITLE_FIELD = "title"
DESCRIPTION_FIELD = "description"
SOURCE_BRANCH_FIELD = "source_branch"
TARGET_BRANCH_FIELD = "target_branch"

# `glab` subcommand that opens a merge request.
CLI_CREATE_ARGS = ("mr", "create")

# Header GitLab reads a personal/project/group access token from.
_TOKEN_HEADER = "PRIVATE-TOKEN"


def open_merge_request(
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
    """Opens a merge request from `branch` on the project `remote_url`
    points at, and returns its URL.

    Signature-compatible with `github.open_pull_request` so `submit.py`
    dispatches to either through one call; `body` is GitLab's
    `description`.

    Exactly one of `executable` (an authenticated `glab`) and `token` (a
    host token) must be given. `cwd` is where `glab` is invoked and matters
    only for the CLI path.

    `api_origin` overrides the REST origin derived from the remote's host
    (`default_api_origin`), so a test can point the real request path at a
    local server instead of at gitlab.com. No production caller passes it.

    Raises:
        ProviderError: the remote URL is unparseable, `glab` failed or
            printed no URL, or the REST API could not be reached, refused
            the request, or answered without a merge request URL.
        ValueError: neither or both of `executable`/`token` were given — see
            `github.open_pull_request` for why this is not a
            `ProviderError`.
    """
    if bool(executable) == bool(token):
        raise ValueError(
            "open_merge_request needs exactly one of `executable` (an "
            "authenticated glab) or `token` (a host token)"
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


# Alias so a caller (and `submit.py`'s dispatch table) can use one name
# across providers. GitLab's own term is "merge request", which is why the
# canonical spelling above is `open_merge_request` rather than a GitHub word
# applied to a GitLab concept.
open_pull_request = open_merge_request


def default_api_origin(host: str) -> str:
    """The REST API origin for a GitLab `host` — the same shape for
    gitlab.com and for every self-managed instance.
    """
    return f"https://{host}{API_PREFIX}"


def project_id(path: str) -> str:
    """The URL-encoded project id GitLab addresses a project by: its full
    path with every separator percent-encoded.
    """
    return quote(path, safe="")


def _open_via_cli(
    executable: str,
    repository: RemoteRepository,
    branch: str,
    title: str,
    body: str,
    base: str | None,
    cwd: Path | None,
) -> str:
    """Opens the merge request through `glab mr create` and reads its URL
    out of the command's output.

    `--title`, `--description`, and `--yes` together are what make the
    command non-interactive: without a title `glab` refuses a
    non-interactive run outright, and without `--yes` it asks for
    confirmation before submitting. `--source-branch` names the
    already-pushed branch rather than relying on the checked-out one, and
    `--target-branch` is passed whenever it is known.
    """
    argv = [
        executable,
        *CLI_CREATE_ARGS,
        "--repo",
        _cli_repo_argument(repository),
        "--source-branch",
        branch,
        "--title",
        title,
        "--description",
        body,
        "--yes",
    ]
    if base:
        argv += ["--target-branch", base]

    step = f"opening a merge request for {branch} on {repository.path}"
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
    """Opens the merge request through GitLab's REST API and returns its
    `web_url`.
    """
    origin = api_origin or default_api_origin(repository.host)
    headers = _api_headers(token)
    project = project_id(repository.path)
    target = base or _default_branch(origin, headers, project, repository.path)

    step = f"opening a merge request for {branch} on {repository.path}"
    payload = {
        TITLE_FIELD: title,
        DESCRIPTION_FIELD: body,
        SOURCE_BRANCH_FIELD: branch,
        TARGET_BRANCH_FIELD: target,
    }
    response = request_json(
        "POST",
        f"{origin}{MERGE_REQUESTS_PATH_TEMPLATE.format(project=project)}",
        headers=headers,
        payload=payload,
        step=step,
    )
    return required_url(response, WEB_URL_KEY, step=step)


def _default_branch(
    origin: str, headers: dict[str, str], project: str, path: str
) -> str:
    """Reads the project's default branch, for the merge request's required
    `target_branch`.

    Raises `ProviderError` if GitLab answers without one, rather than
    guessing at `main`: see `github._default_branch` for why a guess is
    worse than a failure here.
    """
    step = f"looking up the default branch of {path}"
    response = request_json(
        "GET",
        f"{origin}{PROJECT_PATH_TEMPLATE.format(project=project)}",
        headers=headers,
        step=step,
    )
    branch = response.get(DEFAULT_BRANCH_KEY) if isinstance(response, dict) else None
    if not isinstance(branch, str) or not branch.strip():
        raise ProviderError(
            f"GitLab did not report a {DEFAULT_BRANCH_KEY!r} for {path}, so "
            "there is no target branch to open a merge request against."
        )
    return branch.strip()


def _cli_repo_argument(repository: RemoteRepository) -> str:
    """The `--repo` value for `glab`: the project's path on gitlab.com, and
    its full `https://` URL on a self-managed host.

    Rebuilt from the parsed host and path rather than passed through from
    the remote as given — see the module docstring.
    """
    if repository.host == PUBLIC_HOST or repository.host.endswith(
        f".{PUBLIC_HOST}"
    ):
        return repository.path
    return f"https://{repository.host}/{repository.path}"


def _api_headers(token: str) -> dict[str, str]:
    """The `PRIVATE-TOKEN` header GitLab authenticates a token with.

    The token travels in a header, never in the URL or on a command line —
    see this package's docstring.
    """
    return {_TOKEN_HEADER: token}
