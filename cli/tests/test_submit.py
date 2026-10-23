"""Unit tests for `submit.resolve_git_credential` (Task 29.1) and
`submit.submit` (Task 29.3).

Covers Requirement 22.2: the credential is resolved via `gh`/`glab`
passthrough first, then `~/.vaultshield/credentials.yml` at `chmod 600`,
and `CredentialError` when neither yields one. And Requirements 22.1, 22.3,
and 22.4 in the `submit()` section at the bottom of this file: the approved
-status gate (including that a rejected submission has no side effect), the
push, and the `pr_url` PATCH.

The credentials file is a REAL file under `tmp_path`, written with real
permission bits and reached through `resolve_git_credential`'s `home`
argument — so the mode check is exercised against the filesystem rather
than against a patched `stat` call. The host-CLI half is exercised both
ways: the precedence tests inject `which`/`check_auth` (the seams Task
29.2's property test needs), and `cli_is_authenticated` gets its own test
against a real executable stub, so the `auth status` subprocess itself is
covered and not only mocked around.

The `submit()` tests run against a REAL local HTTP server standing in for
`RunViewSet`'s detail route (the same pattern as `test_uploader.py`) and a
REAL local git repository with a REAL bare remote, so the status read, the
push, and the `pr_url` PATCH are all exercised end to end: a pushed branch
is asserted by looking in the remote, and a *not*-pushed branch by its
absence there. Only the host API call is substituted, through `submit`'s
`open_pull_request` seam — `providers/` has its own tests in
`test_providers.py`.

Task 29.2's property test for resolution precedence (Property 30), Task
29.4's for the submit gate (Property 31), and Task 29.5's for the backend
schema (Property 32) are separate, optional tasks and are deliberately not
duplicated here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from vaultshield.git_capture import run_branch_name
from vaultshield.run_directory import run_dir_path
from vaultshield.submit import (
    APPROVED_STATUS,
    CREDENTIALS_FILE_MODE,
    SOURCE_CLI,
    SOURCE_FILE,
    Credential,
    CredentialError,
    SubmitError,
    cli_is_authenticated,
    credentials_path,
    detect_provider,
    resolve_git_credential,
    submit,
)

_GITHUB_TOKEN = "ghp_file_sourced_token_value"
_GITLAB_TOKEN = "glpat_file_sourced_token_value"
_GH_PATH = "/usr/local/bin/gh"
_GLAB_PATH = "/usr/local/bin/glab"


def _write_credentials_file(
    home: Path, document: object, mode: int = CREDENTIALS_FILE_MODE
) -> Path:
    """Writes `document` as `<home>/.vaultshield/credentials.yml` at `mode`
    and returns the path.
    """
    path = credentials_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    path.chmod(mode)
    return path


def _found(executable: str):
    """A `which` stub that always resolves to `executable`."""
    return lambda _name: executable


def _missing(_name: str) -> None:
    """A `which` stub for a CLI that is not installed."""
    return None


def _authenticated(_executable: str) -> bool:
    return True


def _unauthenticated(_executable: str) -> bool:
    return False


def test_prefers_cli_passthrough_over_the_credentials_file(tmp_path: Path) -> None:
    # Both sources available: Requirement 22.2 puts the CLI first, and the
    # returned credential carries no token at all — the CLI holds it.
    _write_credentials_file(tmp_path, {"github": {"token": _GITHUB_TOKEN}})

    credential = resolve_git_credential(
        "github",
        home=tmp_path,
        which=_found(_GH_PATH),
        check_auth=_authenticated,
    )

    assert credential == Credential(
        provider="github", source=SOURCE_CLI, executable=_GH_PATH
    )
    assert credential.token is None


def test_falls_back_to_the_credentials_file_when_the_cli_is_missing(
    tmp_path: Path,
) -> None:
    _write_credentials_file(tmp_path, {"github": {"token": _GITHUB_TOKEN}})

    credential = resolve_git_credential(
        "github", home=tmp_path, which=_missing, check_auth=_authenticated
    )

    assert credential == Credential(
        provider="github", source=SOURCE_FILE, token=_GITHUB_TOKEN
    )


def test_falls_back_when_the_cli_is_installed_but_not_authenticated(
    tmp_path: Path,
) -> None:
    # An installed-but-logged-out CLI is a miss, not a failure.
    _write_credentials_file(tmp_path, {"gitlab": _GITLAB_TOKEN})

    credential = resolve_git_credential(
        "gitlab",
        home=tmp_path,
        which=_found(_GLAB_PATH),
        check_auth=_unauthenticated,
    )

    # Also covers the scalar shorthand (`gitlab: <token>`) and the
    # gitlab -> glab mapping.
    assert credential == Credential(
        provider="gitlab", source=SOURCE_FILE, token=_GITLAB_TOKEN
    )


def test_raises_when_neither_source_yields_a_credential(tmp_path: Path) -> None:
    # No CLI, and no credentials file at all.
    with pytest.raises(CredentialError) as excinfo:
        resolve_git_credential("github", home=tmp_path, which=_missing)

    message = str(excinfo.value)
    assert "gh" in message
    assert str(credentials_path(tmp_path)) in message


def test_raises_when_the_file_exists_without_an_entry_for_the_provider(
    tmp_path: Path,
) -> None:
    _write_credentials_file(tmp_path, {"gitlab": {"token": _GITLAB_TOKEN}})

    with pytest.raises(CredentialError):
        resolve_git_credential("github", home=tmp_path, which=_missing)


def test_refuses_a_credentials_file_with_looser_than_600_permissions(
    tmp_path: Path,
) -> None:
    path = _write_credentials_file(
        tmp_path, {"github": {"token": _GITHUB_TOKEN}}, mode=0o644
    )

    with pytest.raises(CredentialError) as excinfo:
        resolve_git_credential("github", home=tmp_path, which=_missing)

    message = str(excinfo.value)
    assert "0644" in message
    assert "chmod 600" in message
    # The refusal must not leak the token it declined to use.
    assert _GITHUB_TOKEN not in message
    assert path.exists()


def test_reads_a_credentials_file_at_exactly_600(tmp_path: Path) -> None:
    _write_credentials_file(
        tmp_path, {"github": {"token": _GITHUB_TOKEN}}, mode=0o600
    )

    credential = resolve_git_credential("github", home=tmp_path, which=_missing)

    assert credential.token == _GITHUB_TOKEN


def test_malformed_yaml_is_reported_without_quoting_the_file(tmp_path: Path) -> None:
    path = credentials_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'github: "{_GITHUB_TOKEN}\n', encoding="utf-8")
    path.chmod(CREDENTIALS_FILE_MODE)

    with pytest.raises(CredentialError) as excinfo:
        resolve_git_credential("github", home=tmp_path, which=_missing)

    message = str(excinfo.value)
    assert str(path) in message
    assert _GITHUB_TOKEN not in message


def test_raises_for_an_unsupported_provider(tmp_path: Path) -> None:
    with pytest.raises(CredentialError) as excinfo:
        resolve_git_credential("bitbucket", home=tmp_path, which=_missing)

    assert "github" in str(excinfo.value)
    assert "gitlab" in str(excinfo.value)


def test_repr_redacts_a_file_sourced_token() -> None:
    credential = Credential(
        provider="github", source=SOURCE_FILE, token=_GITHUB_TOKEN
    )

    assert _GITHUB_TOKEN not in repr(credential)
    assert "redacted" in repr(credential)


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, True), (1, False)],
)
def test_cli_is_authenticated_reads_the_auth_status_exit_code(
    tmp_path: Path, exit_code: int, expected: bool
) -> None:
    # A real executable, really spawned: `auth status` exiting 0 means
    # authenticated, non-zero means not.
    stub = tmp_path / "gh-stub"
    stub.write_text(
        f'#!/bin/sh\necho "auth status: $*" >&2\nexit {exit_code}\n', encoding="utf-8"
    )
    stub.chmod(0o755)

    assert cli_is_authenticated(str(stub)) is expected


def test_cli_is_authenticated_is_false_for_an_unusable_executable(
    tmp_path: Path,
) -> None:
    # Vanished between `which` and the check: a miss, not a crash.
    assert cli_is_authenticated(str(tmp_path / "does-not-exist")) is False


# --- `submit()` (Task 29.3) -------------------------------------------------

_RUN_ID = "20240101120000-abcd1234"
_BRANCH = run_branch_name(_RUN_ID)
_DASHBOARD_TOKEN = "0123456789abcdef0123456789abcdef01234567"
_PR_URL = "https://github.com/org/repo/pull/7"
_AGENT = "codex"


class _RunHandler(BaseHTTPRequestHandler):
    """Stands in for `RunViewSet`'s detail route on a real socket.

    `GET /api/runs/<run_id>/` answers the Run the server was configured
    with, and `PATCH` answers that Run merged with the submitted body — the
    shape `RunUpdateSerializer` returns. Either verb can be forced to an
    error status, which is how the "no such Run" and "the PATCH failed"
    paths are exercised. Every request is recorded so a test can assert what
    was actually sent, including the auth header.
    """

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        self.server.requests.append(  # type: ignore[attr-defined]
            (self.command, self.path, dict(self.headers), payload)
        )
        return payload

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self._record()
        status = self.server.get_status  # type: ignore[attr-defined]
        if status != 200:
            self._respond(status, json.dumps({"detail": "Not found."}).encode())
            return
        self._respond(200, json.dumps(self.server.run).encode())  # type: ignore[attr-defined]

    def do_PATCH(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        payload = self._record()
        status = self.server.patch_status  # type: ignore[attr-defined]
        if status != 200:
            self._respond(
                status, json.dumps({"detail": "Server error."}).encode()
            )
            return
        updated = {**self.server.run, **(payload or {})}  # type: ignore[attr-defined]
        self._respond(200, json.dumps(updated).encode())

    def log_message(self, *args: object) -> None:
        """Silences the handler's default stderr access log."""


@contextmanager
def _run_server(
    *,
    status: str = APPROVED_STATUS,
    get_status: int = 200,
    patch_status: int = 200,
) -> Iterator[ThreadingHTTPServer]:
    """Runs `_RunHandler` on an ephemeral port and yields the server.

    The Run it serves carries `status` (the field Requirement 22.4 gates on)
    and the fields `RunSerializer` renders. The yielded server carries
    `requests` (every `(method, path, headers, json_body)`, in order) and
    `origin`.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RunHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.run = {  # type: ignore[attr-defined]
        "id": 1,
        "project": 1,
        "run_id": _RUN_ID,
        "agent": _AGENT,
        "status": status,
        "pr_url": "",
        "stuck": False,
    }
    server.get_status = get_status  # type: ignore[attr-defined]
    server.patch_status = patch_status  # type: ignore[attr-defined]
    server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _RecordingOpener:
    """Stands in for a provider's `open_pull_request`/`open_merge_request`.

    Records every call and returns a fixed URL, so the tests can assert what
    the provider would have been asked to open without reaching a real host.
    Signature-compatible with `submit.PullRequestOpener`.
    """

    def __init__(self, url: str = _PR_URL) -> None:
        self.url = url
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "remote_url": remote_url,
                "branch": branch,
                "title": title,
                "body": body,
                "base": base,
                "executable": executable,
                "token": token,
                "cwd": cwd,
            }
        )
        return self.url


def _git(repo: Path, *args: str) -> str:
    """Runs a git command in `repo`, failing the test if git does.

    Identity and hook/signing overrides are passed per command (as
    `git_capture` does) so the tests do not depend on — or write to — the
    developer's own git configuration.
    """
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=VaultShield Test",
            "-c",
            "user.email=test@vaultshield.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _local_repo(tmp_path: Path, *, with_run_branch: bool = True) -> Path:
    """Builds a real repository with a real bare remote and returns the
    working checkout.

    The remote is a bare repository on disk, so `submit`'s push is a real
    push (over git's file transport, which needs no credential) whose effect
    a test can read back out of the remote. `refs/remotes/origin/HEAD` is
    recorded, which is what `submit` resolves the pull request's base branch
    from.

    `with_run_branch=False` leaves out the `vaultshield/run-<id>` branch, for
    the case the module docstring's "named gap" describes: a checkout that
    does not carry the Run's branch.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(remote)],
        capture_output=True,
        check=True,
    )

    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    # `symbolic-ref` rather than `--initial-branch`, which needs git 2.28+.
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--no-verify", "--quiet", "-m", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--quiet", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")

    if with_run_branch:
        _git(repo, "checkout", "--quiet", "-b", _BRANCH)
        (repo / "feature.py").write_text("print('agent output')\n", encoding="utf-8")
        _git(repo, "add", "feature.py")
        _git(repo, "commit", "--no-verify", "--quiet", "-m", f"VaultShield run {_RUN_ID}")
        _git(repo, "checkout", "--quiet", "main")

    return repo


def _remote_branches(repo: Path) -> set[str]:
    """The branches that exist on `repo`'s `origin`, read from the remote
    itself rather than from a local tracking ref.
    """
    output = _git(repo, "ls-remote", "--heads", "origin")
    return {
        line.split("refs/heads/", 1)[1]
        for line in output.splitlines()
        if "refs/heads/" in line
    }


def _file_credential() -> Credential:
    """A `SOURCE_FILE` credential, so the tests never consult the machine's
    own `gh`/`glab` or `~/.vaultshield/credentials.yml`.
    """
    return Credential(provider="github", source=SOURCE_FILE, token=_GITHUB_TOKEN)


def test_submits_an_approved_run_and_returns_the_pull_request_url(
    tmp_path: Path,
) -> None:
    repo = _local_repo(tmp_path)
    opener = _RecordingOpener()

    with _run_server() as server:
        pr_url = submit(
            _RUN_ID,
            server.origin,  # type: ignore[attr-defined]
            _DASHBOARD_TOKEN,
            repo_root=repo,
            provider="github",
            credential=_file_credential(),
            open_pull_request=opener,
        )
        requests = list(server.requests)  # type: ignore[attr-defined]

    # Requirement 22.1's push half: the Run's branch is really on the remote.
    assert _BRANCH in _remote_branches(repo)

    # The provider was asked to open a request for that branch, against the
    # base branch resolved from the remote's HEAD.
    assert len(opener.calls) == 1
    call = opener.calls[0]
    assert call["branch"] == _BRANCH
    assert call["base"] == "main"
    assert call["remote_url"] == str(tmp_path / "remote.git")
    assert _RUN_ID in call["title"]
    assert _AGENT in call["body"]
    # The credential reaches the provider as its two acted-on fields.
    assert call["token"] == _GITHUB_TOKEN
    assert call["executable"] is None

    # Requirement 22.3: the PR URL is recorded on the Run, and it is the URL
    # the provider returned.
    assert pr_url == _PR_URL
    methods = [method for method, _path, _headers, _body in requests]
    assert methods == ["GET", "PATCH"]
    patch_method, patch_path, patch_headers, patch_body = requests[1]
    assert patch_path == f"/api/runs/{_RUN_ID}/"
    # Requirement 22.5's client half: `pr_url` is the only field written.
    assert patch_body == {"pr_url": _PR_URL}
    assert patch_headers["Authorization"] == f"Token {_DASHBOARD_TOKEN}"
    # And the token never leaves the machine as anything else.
    assert _GITHUB_TOKEN not in json.dumps(patch_body)


@pytest.mark.parametrize("status", ["running", "awaiting_review", "rejected"])
def test_rejects_a_run_that_is_not_approved_and_reports_its_status(
    tmp_path: Path, status: str
) -> None:
    repo = _local_repo(tmp_path)
    opener = _RecordingOpener()

    with _run_server(status=status) as server:
        with pytest.raises(SubmitError) as excinfo:
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )
        requests = list(server.requests)  # type: ignore[attr-defined]

    # Requirement 22.4: the actual status is reported.
    message = str(excinfo.value)
    assert status in message
    assert APPROVED_STATUS in message

    # ...and nothing happened: no push, no request opened, and no PATCH —
    # only the read that established the status.
    assert _BRANCH not in _remote_branches(repo)
    assert opener.calls == []
    assert [method for method, _path, _headers, _body in requests] == ["GET"]


def test_raises_without_side_effects_when_the_backend_has_no_such_run(
    tmp_path: Path,
) -> None:
    repo = _local_repo(tmp_path)
    opener = _RecordingOpener()

    with _run_server(get_status=404) as server:
        with pytest.raises(SubmitError) as excinfo:
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )

    assert _RUN_ID in str(excinfo.value)
    assert _BRANCH not in _remote_branches(repo)
    assert opener.calls == []


def test_reports_the_pull_request_url_when_recording_it_fails(
    tmp_path: Path,
) -> None:
    repo = _local_repo(tmp_path)
    opener = _RecordingOpener()

    with _run_server(patch_status=500) as server:
        with pytest.raises(SubmitError) as excinfo:
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )

    # The pull request exists and only its record is missing, so the URL is
    # in the message — it is the one value the developer cannot recover from
    # the backend.
    message = str(excinfo.value)
    assert _PR_URL in message
    assert "500" in message
    assert _BRANCH in _remote_branches(repo)


def test_reports_a_failed_push_and_opens_no_request(tmp_path: Path) -> None:
    # A checkout that does not carry the Run's branch — the gap `submit.py`'s
    # docstring names. git refuses the push, and nothing further happens.
    repo = _local_repo(tmp_path, with_run_branch=False)
    opener = _RecordingOpener()

    with _run_server() as server:
        with pytest.raises(SubmitError) as excinfo:
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )
        requests = list(server.requests)  # type: ignore[attr-defined]

    assert _BRANCH in str(excinfo.value)
    assert opener.calls == []
    assert [method for method, _path, _headers, _body in requests] == ["GET"]


def test_rejects_an_invalid_run_id_before_reaching_the_backend() -> None:
    # `run_id` is interpolated into a git ref and a URL path, so it is
    # validated first, before any request is made.
    with pytest.raises(SubmitError):
        submit("../etc/passwd", "http://127.0.0.1:1", _DASHBOARD_TOKEN)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:org/repo.git", "github"),
        ("https://gitlab.com/group/repo.git", "gitlab"),
        ("https://gitlab.internal.example.com/group/repo.git", "gitlab"),
    ],
)
def test_detect_provider_reads_the_host_out_of_the_remote(
    remote: str, expected: str
) -> None:
    assert detect_provider(remote) == expected


def test_detect_provider_refuses_to_guess_an_unrecognised_host() -> None:
    with pytest.raises(SubmitError) as excinfo:
        detect_provider("git@git.example.com:org/repo.git")

    message = str(excinfo.value)
    assert "git.example.com" in message
    assert "github" in message and "gitlab" in message


def test_a_file_sourced_token_reaches_git_through_the_environment() -> None:
    """The one-shot credential helper `submit` gives `git push` answers with
    the token from the environment, and the token appears nowhere in the
    command.

    Reaches for the module's private helper constants deliberately: this is
    the mechanism that makes a `SOURCE_FILE` credential able to push at all,
    and the tests above cannot cover it because a local (file transport)
    remote never asks for a credential. `git credential fill` is git's own
    way of asking, so this exercises the real helper through real git.
    """
    from vaultshield.submit import (
        _GIT_CREDENTIAL_HELPER,
        _GIT_TOKEN_ENVVAR,
        _GIT_USERNAME_ENVVAR,
    )

    completed = subprocess.run(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={_GIT_CREDENTIAL_HELPER}",
            "credential",
            "fill",
        ],
        input="protocol=https\nhost=example.com\n\n",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            _GIT_USERNAME_ENVVAR: "x-access-token",
            _GIT_TOKEN_ENVVAR: _GITHUB_TOKEN,
        },
        check=True,
    )

    assert "username=x-access-token" in completed.stdout
    assert f"password={_GITHUB_TOKEN}" in completed.stdout
    # The token was passed in the environment; the command carries only the
    # names of the variables to read it from.
    assert _GITHUB_TOKEN not in _GIT_CREDENTIAL_HELPER


# --- importing the Run's branch from its bundle (Day-4 submit/bundle gap) ----
#
# These reuse `_local_repo` (real bare remote), `_run_server` (real backend
# socket), and `_RecordingOpener` (the provider seam). `branch.bundle` and
# `branch.json` are written into the Run_Directory under the checkout exactly
# where step 8 would put them, then the local branch is deleted to stand in
# for the destroyed capture helper, so `submit` has to import it.


def _origin_url(repo: Path) -> str:
    return _git(repo, "remote", "get-url", "origin").strip()


def _local_branches(repo: Path) -> set[str]:
    output = _git(repo, "branch", "--format=%(refname:short)")
    return {line.strip() for line in output.splitlines() if line.strip()}


def _make_run_bundle(
    repo: Path,
    run_dir: Path,
    *,
    repo_url: str,
    base_oid: str | None = None,
    sha_override: str | None = None,
) -> dict[str, Any]:
    """Writes `<run_dir>/branch.bundle` and `branch.json` the way step 8 does,
    from the `_BRANCH` the `_local_repo` fixture already created. `base_oid`
    defaults to the branch's parent (what `resolve_diff_base` records)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    parent = _git(repo, "rev-parse", f"{_BRANCH}~1").strip()
    tip = _git(repo, "rev-parse", _BRANCH).strip()
    bundle = run_dir / "branch.bundle"
    _git(repo, "bundle", "create", str(bundle), f"{parent}..{_BRANCH}")
    data = bundle.read_bytes()
    meta = {
        "run_id": _RUN_ID,
        "branch": _BRANCH,
        "base_oid": base_oid if base_oid is not None else parent,
        "tip_oid": tip,
        "repo_url": repo_url,
        "bundle_sha256": sha_override or hashlib.sha256(data).hexdigest(),
        "bundle_bytes": len(data),
    }
    (run_dir / "branch.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def test_submit_imports_the_branch_from_its_bundle_then_pushes(
    tmp_path: Path,
) -> None:
    repo = _local_repo(tmp_path, with_run_branch=True)
    run_dir = run_dir_path(repo, _RUN_ID)
    _make_run_bundle(repo, run_dir, repo_url=_origin_url(repo))
    # The capture helper is gone, so the checkout no longer carries the branch.
    _git(repo, "branch", "-D", _BRANCH)
    assert _BRANCH not in _local_branches(repo)
    opener = _RecordingOpener()

    with _run_server() as server:
        pr_url = submit(
            _RUN_ID,
            server.origin,  # type: ignore[attr-defined]
            _DASHBOARD_TOKEN,
            repo_root=repo,
            provider="github",
            credential=_file_credential(),
            open_pull_request=opener,
        )

    # The branch was imported into the checkout and then pushed to the remote.
    assert _BRANCH in _local_branches(repo)
    assert _BRANCH in _remote_branches(repo)
    assert pr_url == _PR_URL
    assert len(opener.calls) == 1
    assert opener.calls[0]["branch"] == _BRANCH


def test_submit_import_leaves_the_working_tree_untouched(tmp_path: Path) -> None:
    repo = _local_repo(tmp_path, with_run_branch=True)
    run_dir = run_dir_path(repo, _RUN_ID)
    _make_run_bundle(repo, run_dir, repo_url=_origin_url(repo))
    _git(repo, "branch", "-D", _BRANCH)

    head_before = _git(repo, "rev-parse", "HEAD").strip()
    status_before = _git(repo, "status", "--porcelain")
    readme_before = (repo / "README.md").read_text(encoding="utf-8")
    opener = _RecordingOpener()

    with _run_server() as server:
        submit(
            _RUN_ID,
            server.origin,  # type: ignore[attr-defined]
            _DASHBOARD_TOKEN,
            repo_root=repo,
            provider="github",
            credential=_file_credential(),
            open_pull_request=opener,
        )

    # The branch ref is created, but HEAD, the checked-out branch, the index,
    # and the working tree are all exactly as they were — the import is a
    # `git fetch` of one ref, never a checkout.
    assert _BRANCH in _local_branches(repo)
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert _git(repo, "status", "--porcelain") == status_before
    assert (repo / "README.md").read_text(encoding="utf-8") == readme_before
    # The file the Run added lives only on the branch, not in the working tree.
    assert not (repo / "feature.py").exists()


def test_submit_refuses_a_checkout_of_a_different_repository(tmp_path: Path) -> None:
    repo = _local_repo(tmp_path, with_run_branch=True)
    run_dir = run_dir_path(repo, _RUN_ID)
    _make_run_bundle(repo, run_dir, repo_url="https://github.com/someone/else.git")
    opener = _RecordingOpener()

    with _run_server() as server:
        with pytest.raises(SubmitError) as excinfo:
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )
        requests = list(server.requests)  # type: ignore[attr-defined]

    message = str(excinfo.value)
    assert "someone/else" in message
    assert "checkout of" in message
    # The mismatch stops the submission with no side effect beyond the read.
    assert [method for method, _p, _h, _b in requests] == ["GET"]
    assert len(opener.calls) == 0
    assert _BRANCH not in _remote_branches(repo)


def test_submit_refuses_a_bundle_whose_sha256_does_not_match(tmp_path: Path) -> None:
    repo = _local_repo(tmp_path, with_run_branch=True)
    run_dir = run_dir_path(repo, _RUN_ID)
    _make_run_bundle(repo, run_dir, repo_url=_origin_url(repo), sha_override="0" * 64)
    _git(repo, "branch", "-D", _BRANCH)  # force the import path
    opener = _RecordingOpener()

    with _run_server() as server:
        with pytest.raises(SubmitError, match="SHA-256"):
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )

    assert _BRANCH not in _local_branches(repo)
    assert _BRANCH not in _remote_branches(repo)
    assert len(opener.calls) == 0


def test_submit_stops_when_the_base_commit_is_missing(tmp_path: Path) -> None:
    repo = _local_repo(tmp_path, with_run_branch=False)
    run_dir = run_dir_path(repo, _RUN_ID)
    run_dir.mkdir(parents=True)

    # Build the bundle in a DIFFERENT repo, so its base commit is one this
    # checkout does not have.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "--quiet")
    _git(other, "symbolic-ref", "HEAD", "refs/heads/main")
    (other / "x.txt").write_text("x\n", encoding="utf-8")
    _git(other, "add", "x.txt")
    _git(other, "commit", "--no-verify", "--quiet", "-m", "other base")
    _git(other, "checkout", "--quiet", "-b", _BRANCH)
    (other / "y.txt").write_text("y\n", encoding="utf-8")
    _git(other, "add", "y.txt")
    _git(other, "commit", "--no-verify", "--quiet", "-m", "run")
    base_oid = _git(other, "rev-parse", f"{_BRANCH}~1").strip()
    bundle = run_dir / "branch.bundle"
    _git(other, "bundle", "create", str(bundle), f"{base_oid}..{_BRANCH}")
    data = bundle.read_bytes()
    (run_dir / "branch.json").write_text(
        json.dumps(
            {
                "run_id": _RUN_ID,
                "branch": _BRANCH,
                "base_oid": base_oid,
                "tip_oid": _git(other, "rev-parse", _BRANCH).strip(),
                "repo_url": _origin_url(repo),
                "bundle_sha256": hashlib.sha256(data).hexdigest(),
                "bundle_bytes": len(data),
            }
        ),
        encoding="utf-8",
    )
    opener = _RecordingOpener()

    with _run_server() as server:
        with pytest.raises(SubmitError) as excinfo:
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )

    message = str(excinfo.value)
    assert base_oid in message
    assert "git fetch" in message
    assert _BRANCH not in _remote_branches(repo)
    assert len(opener.calls) == 0


def test_submit_without_a_bundle_or_the_branch_is_a_clear_error(tmp_path: Path) -> None:
    repo = _local_repo(tmp_path, with_run_branch=False)
    opener = _RecordingOpener()

    with _run_server() as server:
        with pytest.raises(SubmitError) as excinfo:
            submit(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                repo_root=repo,
                provider="github",
                credential=_file_credential(),
                open_pull_request=opener,
            )

    message = str(excinfo.value)
    assert _BRANCH in message
    assert "bundle" in message
    assert len(opener.calls) == 0
