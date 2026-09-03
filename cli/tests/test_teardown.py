"""Tests for the Teardown_Command (`teardown.py`, Task 30.1).

Requirement 23.1 (a Run's active Sandbox is force-stopped and removed) and
Requirement 23.2 (a `rejected` Run's pushed remote branch is deleted using the
local git credential).

Both halves are exercised against real things wherever a real thing is
available: the branch deletion runs against a real bare git remote on disk
(over git's file transport, which needs no credential), and the Run_Status read
runs against a real HTTP server on a real socket — the same approach
`test_submit.py` takes, so a passing test means git and httpx really did what
the module asked of them.

Docker is the exception. A container cannot be created in a unit test without a
running daemon and a pulled image, so `_FakeDockerClient` stands in for the
daemon and records what was asked of it. What matters here is the contract
`teardown.py` depends on — the container is looked up by the name
`Sandbox.create` gives it, and removed with `v=True, force=True` (the
force-stop Requirement 23.1 asks for) — and that is exactly what the fake
records. `Sandbox.teardown`'s own behaviour against a real daemon is covered by
`sandbox.py`'s tests.
"""

from __future__ import annotations

import json
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import docker
import pytest

from vaultshield.git_capture import run_branch_name
from vaultshield.sandbox import container_name
from vaultshield.submit import SOURCE_FILE, Credential
from vaultshield.teardown import (
    REJECTED_STATUS,
    TeardownError,
    delete_run_branch,
    teardown,
    teardown_sandbox,
)

_RUN_ID = "20240101120000-abcd1234"
_OTHER_RUN_ID = "20240101120000-beef5678"
_BRANCH = run_branch_name(_RUN_ID)
_OTHER_BRANCH = run_branch_name(_OTHER_RUN_ID)
_DASHBOARD_TOKEN = "0123456789abcdef0123456789abcdef01234567"
_GITHUB_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


# --- the backend's Run detail route -----------------------------------------


class _RunHandler(BaseHTTPRequestHandler):
    """Stands in for `RunViewSet`'s detail route on a real socket.

    Answers `GET /api/runs/<run_id>/` with the Run it was configured with, or
    with a forced error status. Every request is recorded so a test can assert
    what was sent, including the auth header — and that nothing else was.
    """

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self.server.requests.append((self.command, self.path, dict(self.headers)))  # type: ignore[attr-defined]
        status = self.server.get_status  # type: ignore[attr-defined]
        body = (
            json.dumps(self.server.run)  # type: ignore[attr-defined]
            if status == 200
            else json.dumps({"detail": "Not found."})
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silences the handler's default stderr access log."""


@contextmanager
def _run_server(
    *, status: str = REJECTED_STATUS, get_status: int = 200
) -> Iterator[ThreadingHTTPServer]:
    """Runs `_RunHandler` on an ephemeral port and yields the server.

    The Run it serves carries `status` (the field Requirement 23.2 gates on)
    and the fields `RunSerializer` renders. The yielded server carries
    `requests` and `origin`.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RunHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.run = {  # type: ignore[attr-defined]
        "id": 1,
        "project": 1,
        "run_id": _RUN_ID,
        "agent": "codex",
        "status": status,
        "pr_url": "",
        "stuck": False,
    }
    server.get_status = get_status  # type: ignore[attr-defined]
    server.origin = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- the Docker daemon ------------------------------------------------------


class _FakeContainer:
    """Records the removal `Sandbox.teardown` performs on it."""

    def __init__(self) -> None:
        self.removals: list[dict[str, Any]] = []

    def remove(self, **kwargs: Any) -> None:
        self.removals.append(kwargs)


class _FakeContainerCollection:
    def __init__(self, containers: dict[str, _FakeContainer]) -> None:
        self._containers = containers
        self.lookups: list[str] = []

    def get(self, name: str) -> _FakeContainer:
        self.lookups.append(name)
        try:
            return self._containers[name]
        except KeyError:
            raise docker.errors.NotFound(f"no such container: {name}") from None


class _FakeDockerClient:
    """A stand-in for `DockerClient` holding a fixed set of containers."""

    def __init__(self, *run_ids: str) -> None:
        self._containers = {
            container_name(run_id): _FakeContainer() for run_id in run_ids
        }
        self.containers = _FakeContainerCollection(self._containers)

    def container_for(self, run_id: str) -> _FakeContainer:
        return self._containers[container_name(run_id)]


# --- a real repository with a real remote ------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Runs a git command in `repo`, failing the test if git does.

    Identity and hook/signing overrides are passed per command (as
    `git_capture` and `test_submit` do) so the tests do not depend on — or
    write to — the developer's own git configuration.
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


def _local_repo(tmp_path: Path, *pushed_branches: str) -> Path:
    """Builds a real repository with a real bare remote, pushes
    `pushed_branches` to it, and returns the working checkout.

    The remote is a bare repository on disk, so the deletion under test is a
    real ref deletion whose effect a test reads back out of the remote itself.
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

    for branch in pushed_branches:
        _git(repo, "checkout", "--quiet", "-b", branch)
        (repo / f"{branch.rsplit('-', 1)[-1]}.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "--all")
        _git(repo, "commit", "--no-verify", "--quiet", "-m", branch)
        _git(repo, "push", "--quiet", "origin", branch)
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
    """A `SOURCE_FILE` credential, so no test consults the machine's own
    `gh`/`glab` or `~/.vaultshield/credentials.yml`.
    """
    return Credential(provider="github", source=SOURCE_FILE, token=_GITHUB_TOKEN)


# --- Requirements 23.1 + 23.2 together --------------------------------------


def test_removes_the_sandbox_and_deletes_a_rejected_runs_branch(
    tmp_path: Path,
) -> None:
    repo = _local_repo(tmp_path, _BRANCH, _OTHER_BRANCH)
    client = _FakeDockerClient(_RUN_ID)

    with _run_server(status=REJECTED_STATUS) as server:
        teardown(
            _RUN_ID,
            server.origin,  # type: ignore[attr-defined]
            _DASHBOARD_TOKEN,
            client=client,
            repo_root=repo,
            credential=_file_credential(),
        )
        requests = list(server.requests)  # type: ignore[attr-defined]

    # Requirement 23.1: the Run's container was looked up by the name
    # `Sandbox.create` gives it, and force-removed with its volumes.
    assert client.containers.lookups == [container_name(_RUN_ID)]
    assert client.container_for(_RUN_ID).removals == [{"v": True, "force": True}]

    # Requirement 23.2: this Run's branch is gone from the remote, and the
    # other Run's branch (and the base branch) are untouched.
    assert _remote_branches(repo) == {"main", _OTHER_BRANCH}

    # One backend call, a read, carrying the Dashboard_Auth_Token.
    assert [(method, path) for method, path, _headers in requests] == [
        ("GET", f"/api/runs/{_RUN_ID}/")
    ]
    assert requests[0][2]["Authorization"] == f"Token {_DASHBOARD_TOKEN}"


@pytest.mark.parametrize(
    "status", ["running", "awaiting_review", "approved", "submitted"]
)
def test_leaves_the_branch_alone_for_a_run_that_is_not_rejected(
    tmp_path: Path, status: str
) -> None:
    """Requirement 23.2 names exactly one status. An approved or submitted
    Run's branch is the change being merged, so teardown must not delete it —
    while the Sandbox cleanup of Requirement 23.1 still happens.
    """
    repo = _local_repo(tmp_path, _BRANCH)
    client = _FakeDockerClient(_RUN_ID)

    with _run_server(status=status) as server:
        teardown(
            _RUN_ID,
            server.origin,  # type: ignore[attr-defined]
            _DASHBOARD_TOKEN,
            client=client,
            repo_root=repo,
            credential=_file_credential(),
        )

    assert client.container_for(_RUN_ID).removals == [{"v": True, "force": True}]
    assert _BRANCH in _remote_branches(repo)


def test_a_run_without_a_container_still_has_its_branch_deleted(
    tmp_path: Path,
) -> None:
    """The ordinary case: `vaultshield run` already tore the Sandbox down, so
    Requirement 23.1 has nothing to do and that is not a failure.
    """
    repo = _local_repo(tmp_path, _BRANCH)
    client = _FakeDockerClient()  # no containers at all

    with _run_server(status=REJECTED_STATUS) as server:
        teardown(
            _RUN_ID,
            server.origin,  # type: ignore[attr-defined]
            _DASHBOARD_TOKEN,
            client=client,
            repo_root=repo,
            credential=_file_credential(),
        )

    assert teardown_sandbox(_RUN_ID, client=client) is False
    assert _BRANCH not in _remote_branches(repo)


def test_reports_a_run_the_backend_does_not_know_and_what_was_cleaned_up(
    tmp_path: Path,
) -> None:
    """A mistyped run id is surfaced rather than silently doing nothing — and
    the message says the Sandbox step already ran, since it runs first.
    """
    repo = _local_repo(tmp_path, _BRANCH)
    client = _FakeDockerClient(_RUN_ID)

    with _run_server(get_status=404) as server:
        with pytest.raises(TeardownError) as excinfo:
            teardown(
                _RUN_ID,
                server.origin,  # type: ignore[attr-defined]
                _DASHBOARD_TOKEN,
                client=client,
                repo_root=repo,
                credential=_file_credential(),
            )

    message = str(excinfo.value)
    assert _RUN_ID in message
    assert "404" in message
    assert "Sandbox" in message
    # The container was still removed, and the remote was never touched.
    assert client.container_for(_RUN_ID).removals == [{"v": True, "force": True}]
    assert _BRANCH in _remote_branches(repo)


# --- Requirement 23.2 on its own --------------------------------------------


def test_deleting_a_branch_the_remote_does_not_carry_is_a_no_op(
    tmp_path: Path,
) -> None:
    """"...and it has a pushed branch": a rejected Run was never submitted, so
    usually there is nothing on the remote. Deleting twice therefore succeeds
    twice.
    """
    repo = _local_repo(tmp_path, _BRANCH)

    assert (
        delete_run_branch(
            _OTHER_RUN_ID, repo_root=repo, credential=_file_credential()
        )
        is False
    )
    assert delete_run_branch(
        _RUN_ID, repo_root=repo, credential=_file_credential()
    ) is True
    assert (
        delete_run_branch(_RUN_ID, repo_root=repo, credential=_file_credential())
        is False
    )
    assert _remote_branches(repo) == {"main"}


def test_reports_a_remote_that_does_not_exist(tmp_path: Path) -> None:
    repo = _local_repo(tmp_path, _BRANCH)

    with pytest.raises(TeardownError) as excinfo:
        delete_run_branch(
            _RUN_ID,
            repo_root=repo,
            remote="upstream",
            credential=_file_credential(),
        )

    assert "upstream" in str(excinfo.value)
    assert _BRANCH in _remote_branches(repo)


def test_rejects_an_invalid_run_id_before_touching_docker() -> None:
    """`run_id` reaches a container name, a URL path, and a git ref, so it is
    validated first.
    """
    client = _FakeDockerClient()

    with pytest.raises(TeardownError) as excinfo:
        teardown("../../etc", "http://127.0.0.1:1", _DASHBOARD_TOKEN, client=client)

    assert "../../etc" in str(excinfo.value)
    assert client.containers.lookups == []
