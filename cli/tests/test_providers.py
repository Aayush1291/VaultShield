"""Unit tests for the Repository_Host_Provider clients (`providers/`).

These are the host-specific half of Task 29.3 — Requirement 22.1's "SHALL
open a pull request or merge request using a local git or host credential" —
covering both credential shapes `resolve_git_credential` can produce:

- CLI passthrough is exercised against a REAL executable stub that records
  the argv it was given and prints a URL like `gh`/`glab` do, so the
  subprocess call, the flag set, and the URL-out-of-output parsing are all
  under test rather than mocked around.
- The REST path is exercised against a REAL local HTTP server (the pattern
  `test_uploader.py` and `test_browser_capture.py` already use), reached
  through the providers' `api_origin` seam. What each test asserts is what
  the host would have received: the endpoint, the auth header, and the
  request body.

`parse_remote_url` gets its own table, because every API URL and every
`--repo` argument is derived from it and git accepts several remote forms
for the same repository.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from vaultshield.providers import (
    ProviderError,
    RemoteRepository,
    last_url,
    parse_remote_url,
)
from vaultshield.providers import github, gitlab

_BRANCH = "vaultshield/run-abc123"
_TITLE = "VaultShield run abc123"
_BODY = "Opened by `vaultshield submit`."
_TOKEN = "ghp_a_host_token_value"
_PR_URL = "https://github.com/org/repo/pull/7"
_MR_URL = "https://gitlab.com/group/sub/repo/-/merge_requests/9"


# --- remote URL parsing -----------------------------------------------------


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        # scp-like, the default form for an SSH remote.
        ("git@github.com:org/repo.git", RemoteRepository("github.com", "org/repo")),
        # ssh:// with a port, which is a URL rather than an scp-like remote.
        (
            "ssh://git@github.com:22/org/repo.git",
            RemoteRepository("github.com", "org/repo"),
        ),
        # https, with and without the `.git` suffix.
        ("https://github.com/org/repo.git", RemoteRepository("github.com", "org/repo")),
        ("https://github.com/org/repo", RemoteRepository("github.com", "org/repo")),
        # A GitLab project inside nested groups: the whole path is the id.
        (
            "https://gitlab.com/group/sub/repo.git",
            RemoteRepository("gitlab.com", "group/sub/repo"),
        ),
        # A credential embedded in the remote is dropped, not carried into
        # any API URL built from it.
        (
            "https://token@gitlab.example.com/group/repo.git",
            RemoteRepository("gitlab.example.com", "group/repo"),
        ),
        # Surrounding whitespace, as `git remote get-url` output arrives.
        (
            "  git@github.com:org/repo.git\n",
            RemoteRepository("github.com", "org/repo"),
        ),
    ],
)
def test_parse_remote_url_reads_the_host_and_path(
    remote: str, expected: RemoteRepository
) -> None:
    assert parse_remote_url(remote) == expected


@pytest.mark.parametrize(
    "remote",
    [
        "",
        # A local path is a valid git remote but not a hosted repository.
        "/srv/git/repo.git",
        # One path segment cannot be `<namespace>/<name>`.
        "https://github.com/repo.git",
    ],
)
def test_parse_remote_url_rejects_a_remote_with_no_hosted_repository(
    remote: str,
) -> None:
    with pytest.raises(ProviderError):
        parse_remote_url(remote)


def test_parse_remote_url_exposes_the_owner_and_name_halves() -> None:
    repository = parse_remote_url("https://gitlab.com/group/sub/repo.git")

    assert repository.owner == "group/sub"
    assert repository.name == "repo"


def test_last_url_reads_the_final_url_the_cli_printed() -> None:
    output = (
        "Creating pull request for vaultshield/run-abc123 into main\n"
        f"{_PR_URL}\n"
    )

    assert last_url(output, step="opening a pull request") == _PR_URL


def test_last_url_raises_when_the_cli_printed_no_url() -> None:
    with pytest.raises(ProviderError):
        last_url("done\n", step="opening a pull request")


# --- CLI passthrough --------------------------------------------------------


def _cli_stub(tmp_path: Path, url: str) -> tuple[str, Path]:
    """Writes an executable standing in for `gh`/`glab`.

    It records the arguments it was given, one per line, and prints `url` the
    way both CLIs print the request they created. Returns the executable's
    path and the file it records into.
    """
    args_file = tmp_path / "argv.txt"
    stub = tmp_path / "host-cli"
    stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\necho "{url}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return str(stub), args_file


def _recorded_argv(args_file: Path) -> list[str]:
    return args_file.read_text(encoding="utf-8").splitlines()


def test_github_cli_passthrough_creates_the_pull_request_and_returns_its_url(
    tmp_path: Path,
) -> None:
    executable, args_file = _cli_stub(tmp_path, _PR_URL)

    url = github.open_pull_request(
        "git@github.com:org/repo.git",
        _BRANCH,
        title=_TITLE,
        body=_BODY,
        base="main",
        executable=executable,
    )

    assert url == _PR_URL
    argv = _recorded_argv(args_file)
    assert argv[:2] == list(github.CLI_CREATE_ARGS)
    # The repository, the head branch, and the base are all named explicitly,
    # so nothing is inferred from the checked-out branch.
    assert argv[argv.index("--repo") + 1] == "org/repo"
    assert argv[argv.index("--head") + 1] == _BRANCH
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--title") + 1] == _TITLE
    assert argv[argv.index("--body") + 1] == _BODY
    # The token never reaches a command line.
    assert _TOKEN not in " ".join(argv)


def test_github_cli_passthrough_omits_the_base_when_it_is_unknown(
    tmp_path: Path,
) -> None:
    executable, args_file = _cli_stub(tmp_path, _PR_URL)

    github.open_pull_request(
        "https://github.com/org/repo.git",
        _BRANCH,
        title=_TITLE,
        body=_BODY,
        executable=executable,
    )

    # `gh` applies the repository's default branch itself.
    assert "--base" not in _recorded_argv(args_file)


def test_github_cli_passthrough_names_an_enterprise_host_in_the_repo_argument(
    tmp_path: Path,
) -> None:
    executable, args_file = _cli_stub(tmp_path, _PR_URL)

    github.open_pull_request(
        "git@github.internal.example.com:org/repo.git",
        _BRANCH,
        title=_TITLE,
        body=_BODY,
        executable=executable,
    )

    argv = _recorded_argv(args_file)
    assert argv[argv.index("--repo") + 1] == "github.internal.example.com/org/repo"


def test_gitlab_cli_passthrough_creates_the_merge_request_and_returns_its_url(
    tmp_path: Path,
) -> None:
    executable, args_file = _cli_stub(tmp_path, _MR_URL)

    url = gitlab.open_merge_request(
        "git@gitlab.com:group/sub/repo.git",
        _BRANCH,
        title=_TITLE,
        body=_BODY,
        base="main",
        executable=executable,
    )

    assert url == _MR_URL
    argv = _recorded_argv(args_file)
    assert argv[:2] == list(gitlab.CLI_CREATE_ARGS)
    assert argv[argv.index("--repo") + 1] == "group/sub/repo"
    assert argv[argv.index("--source-branch") + 1] == _BRANCH
    assert argv[argv.index("--target-branch") + 1] == "main"
    assert argv[argv.index("--description") + 1] == _BODY
    # Without `--yes`, `glab` asks for confirmation and the command hangs.
    assert "--yes" in argv


def test_gitlab_cli_passthrough_names_a_self_managed_host_by_url(
    tmp_path: Path,
) -> None:
    executable, args_file = _cli_stub(tmp_path, _MR_URL)

    gitlab.open_merge_request(
        "https://token@gitlab.example.com/group/repo.git",
        _BRANCH,
        title=_TITLE,
        body=_BODY,
        executable=executable,
    )

    argv = _recorded_argv(args_file)
    repo_argument = argv[argv.index("--repo") + 1]
    # The URL is rebuilt from the parsed host and path, so the credential
    # embedded in the remote is not passed on a command line.
    assert repo_argument == "https://gitlab.example.com/group/repo"
    assert "token@" not in repo_argument


def test_a_failing_host_cli_is_reported_with_its_own_output(
    tmp_path: Path,
) -> None:
    stub = tmp_path / "gh-failing"
    stub.write_text(
        '#!/bin/sh\necho "a pull request already exists" >&2\nexit 1\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    with pytest.raises(ProviderError) as excinfo:
        github.open_pull_request(
            "git@github.com:org/repo.git",
            _BRANCH,
            title=_TITLE,
            body=_BODY,
            executable=str(stub),
        )

    assert "a pull request already exists" in str(excinfo.value)


def test_a_provider_needs_exactly_one_credential_shape() -> None:
    # Neither, and both: a caller bug rather than a host failure, so it is a
    # ValueError and not a ProviderError (see `Credential.__post_init__`).
    with pytest.raises(ValueError):
        github.open_pull_request(
            "git@github.com:org/repo.git", _BRANCH, title=_TITLE, body=_BODY
        )
    with pytest.raises(ValueError):
        github.open_pull_request(
            "git@github.com:org/repo.git",
            _BRANCH,
            title=_TITLE,
            body=_BODY,
            executable="/usr/local/bin/gh",
            token=_TOKEN,
        )


# --- REST API ---------------------------------------------------------------


class _HostHandler(BaseHTTPRequestHandler):
    """Stands in for a repository host's REST API on a real socket.

    Answers whatever `(status, body)` the server was configured with for the
    request's method, and records every `(method, path, headers, json_body)`
    so a test can assert what was actually sent.
    """

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        self.server.requests.append(  # type: ignore[attr-defined]
            (self.command, self.path, dict(self.headers), payload)
        )

        status, body = self.server.responses.get(  # type: ignore[attr-defined]
            self.command, (200, b"{}")
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle  # noqa: N815 - names fixed by BaseHTTPRequestHandler
    do_POST = _handle  # noqa: N815

    def log_message(self, *args: object) -> None:
        """Silences the handler's default stderr access log."""


@contextmanager
def _host_server(
    responses: dict[str, tuple[int, bytes]],
) -> Iterator[ThreadingHTTPServer]:
    """Runs `_HostHandler` on an ephemeral port and yields the server.

    `responses` maps an HTTP method to the `(status, body)` it answers with.
    The yielded server carries `requests` and `origin`.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HostHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.responses = responses  # type: ignore[attr-defined]
    server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_github_api_path_posts_the_pull_request_and_returns_its_html_url() -> None:
    created = json.dumps({"html_url": _PR_URL, "url": "https://api/x"}).encode()

    with _host_server({"POST": (201, created)}) as server:
        url = github.open_pull_request(
            "git@github.com:org/repo.git",
            _BRANCH,
            title=_TITLE,
            body=_BODY,
            base="main",
            token=_TOKEN,
            api_origin=server.origin,  # type: ignore[attr-defined]
        )

        assert url == _PR_URL
        method, path, headers, payload = server.requests[0]  # type: ignore[attr-defined]

    assert (method, path) == ("POST", "/repos/org/repo/pulls")
    # The token travels in a header, and the payload is exactly the four
    # fields GitHub's create-pull-request endpoint takes.
    assert headers["Authorization"] == f"Bearer {_TOKEN}"
    assert payload == {
        "title": _TITLE,
        "body": _BODY,
        "head": _BRANCH,
        "base": "main",
    }


def test_github_api_path_reads_the_default_branch_when_no_base_is_known() -> None:
    created = json.dumps({"html_url": _PR_URL}).encode()
    repository = json.dumps({"default_branch": "trunk"}).encode()

    with _host_server({"POST": (201, created), "GET": (200, repository)}) as server:
        github.open_pull_request(
            "git@github.com:org/repo.git",
            _BRANCH,
            title=_TITLE,
            body=_BODY,
            token=_TOKEN,
            api_origin=server.origin,  # type: ignore[attr-defined]
        )

        requests = list(server.requests)  # type: ignore[attr-defined]

    # The repository is read first, and its default branch becomes the base.
    assert requests[0][:2] == ("GET", "/repos/org/repo")
    assert requests[1][3]["base"] == "trunk"


def test_gitlab_api_path_posts_the_merge_request_and_returns_its_web_url() -> None:
    created = json.dumps({"web_url": _MR_URL}).encode()

    with _host_server({"POST": (201, created)}) as server:
        url = gitlab.open_merge_request(
            "git@gitlab.com:group/sub/repo.git",
            _BRANCH,
            title=_TITLE,
            body=_BODY,
            base="main",
            token=_TOKEN,
            api_origin=server.origin,  # type: ignore[attr-defined]
        )

        assert url == _MR_URL
        method, path, headers, payload = server.requests[0]  # type: ignore[attr-defined]

    # The nested group path is URL-encoded into a single project id.
    assert (method, path) == (
        "POST",
        "/projects/group%2Fsub%2Frepo/merge_requests",
    )
    assert headers["PRIVATE-TOKEN"] == _TOKEN
    assert payload == {
        "title": _TITLE,
        "description": _BODY,
        "source_branch": _BRANCH,
        "target_branch": "main",
    }


def test_gitlab_api_path_reads_the_default_branch_when_no_base_is_known() -> None:
    created = json.dumps({"web_url": _MR_URL}).encode()
    project = json.dumps({"default_branch": "trunk"}).encode()

    with _host_server({"POST": (201, created), "GET": (200, project)}) as server:
        gitlab.open_merge_request(
            "git@gitlab.com:group/repo.git",
            _BRANCH,
            title=_TITLE,
            body=_BODY,
            token=_TOKEN,
            api_origin=server.origin,  # type: ignore[attr-defined]
        )

        requests = list(server.requests)  # type: ignore[attr-defined]

    assert requests[0][:2] == ("GET", "/projects/group%2Frepo")
    assert requests[1][3]["target_branch"] == "trunk"


def test_a_refused_api_request_is_reported_without_the_token() -> None:
    refused = json.dumps({"message": "Validation Failed"}).encode()

    with _host_server({"POST": (422, refused)}) as server:
        with pytest.raises(ProviderError) as excinfo:
            github.open_pull_request(
                "git@github.com:org/repo.git",
                _BRANCH,
                title=_TITLE,
                body=_BODY,
                base="main",
                token=_TOKEN,
                api_origin=server.origin,  # type: ignore[attr-defined]
            )

    message = str(excinfo.value)
    assert "422" in message
    assert "Validation Failed" in message
    assert _TOKEN not in message


def test_a_created_request_without_a_url_is_an_error() -> None:
    # A 2xx with no URL in it leaves nothing to record on the Run, so it is
    # reported rather than turned into an empty `pr_url`.
    with _host_server({"POST": (201, b"{}")}) as server:
        with pytest.raises(ProviderError):
            github.open_pull_request(
                "git@github.com:org/repo.git",
                _BRANCH,
                title=_TITLE,
                body=_BODY,
                base="main",
                token=_TOKEN,
                api_origin=server.origin,  # type: ignore[attr-defined]
            )


def test_default_api_origins_follow_the_host() -> None:
    assert github.default_api_origin("github.com") == "https://api.github.com"
    assert (
        github.default_api_origin("github.internal.example.com")
        == "https://github.internal.example.com/api/v3"
    )
    assert gitlab.default_api_origin("gitlab.com") == "https://gitlab.com/api/v4"
    assert (
        gitlab.default_api_origin("gitlab.example.com")
        == "https://gitlab.example.com/api/v4"
    )
