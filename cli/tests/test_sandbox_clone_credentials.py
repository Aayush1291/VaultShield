"""Private-repository cloning inside the Sandbox, and the token's blast radius.

`Sandbox._clone_repo` used to run `git clone <repo_url>` with no credential
support at all, so a private repository failed with git's exit code 128. The fix
has to authenticate the clone WITHOUT the token becoming something the AI agent
can read, which is the part worth testing: an authenticated clone is easy, and
every easy way to do it leaks.

Four leak channels are asserted against explicitly, because each is a different
mistake with the same consequence:

    ARGV        the token must not appear in the exec's command line, which
                `docker inspect` and any argv-logging layer can read;
    URL         the token must not be spliced into `repo_url`, which would also
                persist it in the clone's own `.git/config` inside the container;
    CONTAINER   the token must not be part of the container's environment, which
                every later exec — the agent's included — inherits;
    MESSAGES    the token must not reach a `SandboxError`, and through it the
                Event_Log and the Run_Directory.

The Docker daemon is faked. These are assertions about which values are handed to
which Docker call, and a fake is the only way to inspect that precisely; the
real-daemon proof that an exec environment does not persist is in
`test_sandbox_execution.py`, gated on Docker being available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import docker
import pytest

from vaultshield.sandbox import Sandbox, SandboxError, container_name
from vaultshield.submit import (
    SOURCE_CLI,
    SOURCE_FILE,
    Credential,
    CredentialError,
    file_credential,
    resolve_git_credential,
)

RUN_ID = "20250614-091530-deadbeefdeadbeef"
BASE_IMAGE = "python:3.11-slim"

# The repository from the live failure that prompted this.
PRIVATE_REPO = "https://github.com/Aayush1291/APSIT-INTERNAL"
PUBLIC_REPO = "https://github.com/psf/requests.git"
# A URL with no host credential to offer: the git-daemon form the Docker-backed
# tests serve fixtures over.
DAEMON_REPO = "git://host.docker.internal:9418/target.git"

# Obviously fake, and distinctive enough that a substring search for it cannot
# match anything incidental.
FAKE_TOKEN = "ghp_FAKE_TOKEN_VALUE_f7a91c3e5b"


# --- the Docker daemon -------------------------------------------------------


class _Exec:
    """One recorded `exec_run` call."""

    def __init__(self, cmd: list[str], kwargs: dict[str, Any]) -> None:
        self.cmd = cmd
        self.workdir = kwargs.get("workdir")
        self.environment = kwargs.get("environment") or {}

    @property
    def is_clone(self) -> bool:
        return "clone" in self.cmd

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Exec(cmd={self.cmd!r}, environment={self.environment!r})"


class _FakeContainer:
    """Records every exec and removal, and fails whichever step a test says.

    `fail_on` is matched against the joined command, so a test can fail the
    clone while letting dependency installation succeed.
    """

    def __init__(self, *, fail_on: str | None = None, stderr: bytes = b"") -> None:
        self._fail_on = fail_on
        self._stderr = stderr
        self.execs: list[_Exec] = []
        self.removals: list[dict[str, Any]] = []
        self.attrs: dict[str, Any] = {"Config": {"Env": []}}
        self.started = False

    def start(self) -> None:
        self.started = True

    def exec_run(self, cmd: list[str], **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        self.execs.append(_Exec(cmd, kwargs))
        if self._fail_on is not None and self._fail_on in " ".join(cmd):
            return 128, (b"", self._stderr)
        return 0, (b"ok", b"")

    def remove(self, **kwargs: Any) -> None:
        self.removals.append(kwargs)

    # --- assertions the tests share ---

    @property
    def clone(self) -> _Exec:
        clones = [e for e in self.execs if e.is_clone]
        assert len(clones) == 1, f"expected exactly one clone exec, got {clones!r}"
        return clones[0]

    def execs_after_clone(self) -> list[_Exec]:
        index = self.execs.index(self.clone)
        return self.execs[index + 1 :]


class _FakeImages:
    def __init__(self) -> None:
        self.pulled: list[str] = []

    def get(self, name: str) -> object:
        return object()

    def pull(self, name: str) -> None:  # pragma: no cover - image is always present
        self.pulled.append(name)


class _FakeContainerCollection:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container
        self.created: list[dict[str, Any]] = []

    def create(self, image: str, **kwargs: Any) -> _FakeContainer:
        self.created.append({"image": image, **kwargs})
        return self._container


class _FakeDockerClient:
    def __init__(self, container: _FakeContainer) -> None:
        self.images = _FakeImages()
        self.containers = _FakeContainerCollection(container)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """A `.env.sandbox` for the bind mount. `create()` requires it to exist."""
    path = tmp_path / ".env.sandbox"
    path.write_text("DATABASE_URL=postgresql://dummy_user:dummy_password@localhost/db\n")
    return path


def _file_credential(provider: str = "github") -> Credential:
    return Credential(provider=provider, source=SOURCE_FILE, token=FAKE_TOKEN)


def _cli_credential(provider: str = "github") -> Credential:
    return Credential(provider=provider, source=SOURCE_CLI, executable="/usr/bin/gh")


def _missing_credential(provider: str) -> Credential:
    raise CredentialError(
        f"No usable {provider} credential. The gh CLI is not installed or not "
        f"authenticated, and /home/dev/.vaultshield/credentials.yml holds no "
        f"token for {provider!r}."
    )


def _create(
    container: _FakeContainer,
    env_file: Path,
    repo_url: str,
    *,
    resolve_credential: Any = None,
) -> tuple[Sandbox, _FakeDockerClient]:
    client = _FakeDockerClient(container)
    sandbox = Sandbox(RUN_ID, client)  # type: ignore[arg-type]
    sandbox.create(
        repo_url,
        BASE_IMAGE,
        env_file,
        resolve_credential=resolve_credential,
    )
    return sandbox, client


def _assert_token_absent(*blobs: Any) -> None:
    """The token appears nowhere in any of `blobs`, rendered as text."""
    for blob in blobs:
        assert FAKE_TOKEN not in str(blob), f"token leaked into {blob!r}"


# --- the public path, unchanged ----------------------------------------------


class TestPublicRepositoryNeedsNoCredential:
    """The pre-existing path. It has to keep working untouched, including for
    developers with no credential configured at all."""

    def test_a_public_clone_succeeds_with_no_credential_resolved(self, env_file: Path):
        """A resolver that always fails stands in for a machine with no `gh` and
        no credentials file — this one, in fact. The clone still runs."""
        container = _FakeContainer()
        _create(container, env_file, PUBLIC_REPO, resolve_credential=_missing_credential)

        assert container.clone.cmd[:2] == ["git", "clone"]
        assert container.clone.cmd[2] == PUBLIC_REPO
        assert container.started

    def test_a_public_clone_passes_no_credential_config_or_token(self, env_file: Path):
        """No `-c credential.helper`, and nothing token-shaped in the exec."""
        container = _FakeContainer()
        _create(container, env_file, PUBLIC_REPO, resolve_credential=_missing_credential)

        assert "credential.helper=" not in " ".join(container.clone.cmd)
        assert "-c" not in container.clone.cmd
        assert "VAULTSHIELD_GIT_TOKEN" not in container.clone.environment

    def test_the_url_is_never_rewritten(self, env_file: Path):
        """`repo_url` reaches git exactly as given, with a credential available.
        This is the URL-embedding leak channel: even when a token exists, it must
        not be spliced into the URL."""
        container = _FakeContainer()
        _create(container, env_file, PUBLIC_REPO, resolve_credential=lambda p: _file_credential(p))

        assert PUBLIC_REPO in container.clone.cmd
        _assert_token_absent(container.clone.cmd)
        assert not any("@github.com" in arg for arg in container.clone.cmd)

    def test_a_git_daemon_url_is_never_given_a_credential(self, env_file: Path):
        """`git://` cannot use a token, and the Docker-backed tests clone this
        form. A resolver that would raise proves it is not even consulted."""
        container = _FakeContainer()

        def _explode(provider: str) -> Credential:
            raise AssertionError(f"resolver must not be called for {DAEMON_REPO}")

        _create(container, env_file, DAEMON_REPO, resolve_credential=_explode)
        assert container.clone.cmd == [
            "git",
            "clone",
            DAEMON_REPO,
            "/workspace/repo",
        ]


# --- the private path --------------------------------------------------------


class TestPrivateCloneWithAFileCredential:
    """The case that failed live: a private GitHub repo, authenticated from
    `~/.vaultshield/credentials.yml` because `gh` is not installed."""

    def test_the_clone_is_authenticated_through_a_credential_helper(self, env_file: Path):
        container = _FakeContainer()
        _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p))

        cmd = container.clone.cmd
        assert cmd[0] == "git"
        # The helper list is reset first, so no globally-configured helper is
        # consulted for this command.
        assert "credential.helper=" in cmd
        assert any(arg.startswith("credential.helper=!f() {") for arg in cmd), cmd
        assert cmd[-2:] == [PRIVATE_REPO, "/workspace/repo"]

    def test_the_token_reaches_git_only_through_the_exec_environment(self, env_file: Path):
        container = _FakeContainer()
        _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p))

        env = container.clone.environment
        assert env["VAULTSHIELD_GIT_TOKEN"] == FAKE_TOKEN
        assert env["VAULTSHIELD_GIT_USERNAME"] == "x-access-token"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_the_token_is_never_in_the_command_argv(self, env_file: Path):
        """The ARGV channel. The helper snippet names the variables; only the
        environment holds their values."""
        container = _FakeContainer()
        _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p))

        _assert_token_absent(container.clone.cmd, " ".join(container.clone.cmd))
        # The snippet must reference the variable, not interpolate it.
        helper = next(
            arg for arg in container.clone.cmd if arg.startswith("credential.helper=!f() {")
        )
        assert "$VAULTSHIELD_GIT_TOKEN" in helper

    def test_a_gitlab_url_resolves_a_gitlab_credential(self, env_file: Path):
        """The provider comes from the URL, so a GitLab remote does not get asked
        for a GitHub token."""
        container = _FakeContainer()
        asked: list[str] = []

        def _resolver(provider: str) -> Credential:
            asked.append(provider)
            return _file_credential(provider)

        _create(container, env_file, "https://gitlab.com/group/repo.git", resolve_credential=_resolver)
        assert asked == ["gitlab"]
        assert container.clone.environment["VAULTSHIELD_GIT_USERNAME"] == "oauth2"


class TestTheTokenDoesNotOutliveTheClone:
    """The CONTAINER channel: whatever the clone got, the agent must not."""

    def test_the_container_is_created_with_no_environment_holding_the_token(
        self, env_file: Path
    ):
        container = _FakeContainer()
        _, client = _create(
            container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p)
        )

        created = client.containers.created
        assert len(created) == 1
        # Nothing token-bearing in ANY creation argument, not just `environment`.
        _assert_token_absent(created[0])
        assert not created[0].get("environment")

    def test_no_exec_after_the_clone_carries_the_token(self, env_file: Path):
        """Dependency installation runs after the clone and must be clean."""
        container = _FakeContainer()
        _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p))

        later = container.execs_after_clone()
        assert later, "expected the dependency-install exec after the clone"
        for exec_call in later:
            _assert_token_absent(exec_call.cmd, exec_call.environment)

    def test_an_agent_exec_after_create_carries_the_token_nowhere(self, env_file: Path):
        """The agent's own exec, taken through the public `exec` seam."""
        container = _FakeContainer()
        sandbox, _ = _create(
            container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p)
        )

        sandbox.exec(["printenv"])
        agent_exec = container.execs[-1]
        assert agent_exec.cmd == ["printenv"]
        _assert_token_absent(agent_exec.cmd, agent_exec.environment)

    def test_the_token_is_confined_to_exactly_one_exec(self, env_file: Path):
        container = _FakeContainer()
        sandbox, _ = _create(
            container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p)
        )
        sandbox.exec(["printenv"])

        carrying = [e for e in container.execs if FAKE_TOKEN in str(e.environment)]
        assert len(carrying) == 1
        assert carrying[0].is_clone


# --- failure paths -----------------------------------------------------------


class TestMissingCredential:
    """A private repo with nothing to authenticate it. Requirement: fail clearly
    and cleanly, without inventing a token or hanging on a prompt."""

    def test_the_failure_names_the_provider_and_the_fix(self, env_file: Path):
        container = _FakeContainer(
            fail_on="clone", stderr=b"fatal: could not read Username for 'https://github.com'"
        )
        with pytest.raises(SandboxError) as caught:
            _create(container, env_file, PRIVATE_REPO, resolve_credential=_missing_credential)

        message = str(caught.value)
        assert "github" in message
        assert PRIVATE_REPO in message
        # The resolver's own remediation text is carried through.
        assert "credentials.yml" in message
        # And git's actual reason is not thrown away.
        assert "could not read Username" in message

    def test_the_prompt_is_disabled_so_the_failure_is_immediate(self, env_file: Path):
        """Without this, git tries to ask for a username on an exec with no tty,
        which is the opaque exit-128 hang the live failure showed."""
        container = _FakeContainer(fail_on="clone")
        with pytest.raises(SandboxError):
            _create(container, env_file, PRIVATE_REPO, resolve_credential=_missing_credential)

        assert container.clone.environment["GIT_TERMINAL_PROMPT"] == "0"

    def test_a_cli_only_credential_is_reported_as_unusable_in_the_sandbox(
        self, env_file: Path
    ):
        """`gh` holds the token on the host; the container has neither it nor the
        developer's SSH key. The message has to say that rather than leave the
        developer staring at git's generic auth error."""
        container = _FakeContainer(fail_on="clone")
        with pytest.raises(SandboxError) as caught:
            _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _cli_credential(p))

        message = str(caught.value)
        assert "cannot be used from inside the Sandbox" in message
        assert "credentials.yml" in message

    def test_a_cli_only_credential_sends_no_credential_config(self, env_file: Path):
        """It carries no token, so it must not produce a half-authenticated
        clone either."""
        container = _FakeContainer()
        _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _cli_credential(p))

        assert "credential.helper=" not in " ".join(container.clone.cmd)

    def test_a_public_repo_still_clones_when_resolution_fails(self, env_file: Path):
        """Credential resolution failing must not be fatal on its own — the
        repository may simply be public."""
        container = _FakeContainer()  # clone succeeds
        _create(container, env_file, PUBLIC_REPO, resolve_credential=_missing_credential)
        assert container.clone.cmd[:2] == ["git", "clone"]


class TestFailureMessagesNeverCarryTheToken:
    """The MESSAGES channel. A `SandboxError` becomes an Event_Log record and a
    Run_Directory artifact, so it is a disk-writing path."""

    def test_git_output_quoting_the_token_is_redacted(self, env_file: Path):
        """git does not normally echo a helper-supplied credential, but the
        failure path is exactly where a transport error might quote what it was
        given."""
        container = _FakeContainer(
            fail_on="clone",
            stderr=f"fatal: Authentication failed for 'https://x-access-token:{FAKE_TOKEN}@github.com/o/r'".encode(),
        )
        with pytest.raises(SandboxError) as caught:
            _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p))

        message = str(caught.value)
        _assert_token_absent(message, repr(caught.value))
        assert "***" in message
        # Redacted, not discarded: the developer still learns what failed.
        assert "Authentication failed" in message

    def test_the_credential_repr_never_prints_the_token(self):
        """A `Credential` can land in a traceback frame, so its own `repr` is
        part of this guarantee."""
        _assert_token_absent(repr(_file_credential()), str(_file_credential()))


class TestTeardownAfterCloneFailure:
    """A failed clone must not leave a container behind (Requirement 4.4), and
    teardown must stay safe afterwards."""

    def test_the_container_is_removed_when_the_clone_fails(self, env_file: Path):
        container = _FakeContainer(fail_on="clone")
        with pytest.raises(SandboxError):
            _create(container, env_file, PRIVATE_REPO, resolve_credential=_missing_credential)

        assert container.removals == [{"v": True, "force": True}]

    def test_the_sandbox_forgets_the_container_so_teardown_is_a_noop(self, env_file: Path):
        container = _FakeContainer(fail_on="clone")
        client = _FakeDockerClient(container)
        sandbox = Sandbox(RUN_ID, client)  # type: ignore[arg-type]
        with pytest.raises(SandboxError):
            sandbox.create(PRIVATE_REPO, BASE_IMAGE, env_file, resolve_credential=_missing_credential)

        assert sandbox.container is None
        sandbox.teardown()  # must not raise, must not remove twice
        assert len(container.removals) == 1

    def test_cleanup_survives_a_removal_error(self, env_file: Path):
        """The original clone failure is what the developer needs to see, not a
        secondary cleanup error."""

        class _StubbornContainer(_FakeContainer):
            def remove(self, **kwargs: Any) -> None:
                super().remove(**kwargs)
                raise docker.errors.APIError("daemon refused removal")

        container = _StubbornContainer(fail_on="clone", stderr=b"fatal: repository not found")
        with pytest.raises(SandboxError) as caught:
            _create(container, env_file, PRIVATE_REPO, resolve_credential=_missing_credential)

        assert "repository not found" in str(caught.value)
        assert container.removals == [{"v": True, "force": True}]

    def test_a_failed_clone_never_puts_the_token_in_the_message(self, env_file: Path):
        container = _FakeContainer(fail_on="clone", stderr=b"fatal: repository not found")
        with pytest.raises(SandboxError) as caught:
            _create(container, env_file, PRIVATE_REPO, resolve_credential=lambda p: _file_credential(p))

        _assert_token_absent(str(caught.value))


# --- which hosts may be sent a token ----------------------------------------


class TestOnlyPositivelyIdentifiedHostsGetACredential:
    """`submit.detect_provider` matches the product name anywhere in a hostname,
    which is right for choosing an API but not for choosing who to trust with a
    token. Before this change a lookalike host was harmless, because the clone
    sent no credential at all; adding one must not turn it into an exfiltration
    route."""

    @pytest.mark.parametrize(
        "repo_url",
        [
            "https://github.com.evil.example/org/repo.git",
            "https://evilgithub.com/org/repo.git",
            "https://gitlab.com.attacker.test/group/repo.git",
            "https://notgithub.example/org/repo.git",
        ],
    )
    def test_a_lookalike_host_is_cloned_without_a_credential(
        self, env_file: Path, repo_url: str
    ):
        container = _FakeContainer()

        def _explode(provider: str) -> Credential:
            raise AssertionError(f"a token must not be resolved for {repo_url}")

        _create(container, env_file, repo_url, resolve_credential=_explode)
        assert container.clone.cmd == ["git", "clone", repo_url, "/workspace/repo"]

    @pytest.mark.parametrize(
        "repo_url,expected",
        [
            ("https://github.com/org/repo.git", "github"),
            ("https://www.github.com/org/repo.git", "github"),
            ("http://github.com/org/repo", "github"),
            ("https://gitlab.com/group/repo.git", "gitlab"),
        ],
    )
    def test_a_genuine_host_resolves_its_own_provider(
        self, env_file: Path, repo_url: str, expected: str
    ):
        container = _FakeContainer()
        asked: list[str] = []

        def _resolver(provider: str) -> Credential:
            asked.append(provider)
            return _file_credential(provider)

        _create(container, env_file, repo_url, resolve_credential=_resolver)
        assert asked == [expected]

    @pytest.mark.parametrize(
        "repo_url",
        [
            "git@github.com:org/repo.git",
            "ssh://git@github.com/org/repo.git",
            DAEMON_REPO,
            "/srv/local/repo",
            "file:///srv/local/repo",
        ],
    )
    def test_a_url_that_cannot_use_a_token_is_never_given_one(
        self, env_file: Path, repo_url: str
    ):
        """A host token authenticates HTTP basic auth and nothing else. SSH uses
        keys, which the container does not have and must not be given."""
        container = _FakeContainer()

        def _explode(provider: str) -> Credential:
            raise AssertionError(f"a token must not be resolved for {repo_url}")

        _create(container, env_file, repo_url, resolve_credential=_explode)
        assert "credential.helper=" not in " ".join(container.clone.cmd)


# --- the existing credential store, unchanged --------------------------------


class TestExistingCredentialSourceBehaviorIsUnchanged:
    """The clone reuses `resolve_git_credential`/`file_credential` rather than
    adding a second store. These pin the reused behavior so a future change to
    the clone cannot quietly alter what the CLI resolves."""

    def _write_credentials(self, home: Path, body: str, mode: int = 0o600) -> Path:
        directory = home / ".vaultshield"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "credentials.yml"
        path.write_text(body)
        path.chmod(mode)
        return path

    def test_a_mode_600_file_still_yields_a_source_file_credential(self, tmp_path: Path):
        """The path this machine actually uses: no `gh`, token from the file."""
        self._write_credentials(tmp_path, f"github:\n  token: {FAKE_TOKEN}\n")
        credential = resolve_git_credential(
            "github", home=tmp_path, which=lambda name: None
        )
        assert credential.source == SOURCE_FILE
        assert credential.token == FAKE_TOKEN
        assert credential.provider == "github"

    def test_a_loose_mode_file_is_still_refused(self, tmp_path: Path):
        self._write_credentials(tmp_path, f"github:\n  token: {FAKE_TOKEN}\n", mode=0o644)
        with pytest.raises(CredentialError):
            file_credential("github", home=tmp_path)

    def test_an_authenticated_cli_still_wins_over_the_file(self, tmp_path: Path):
        """Resolution order is unchanged: CLI passthrough first."""
        self._write_credentials(tmp_path, f"github:\n  token: {FAKE_TOKEN}\n")
        credential = resolve_git_credential(
            "github",
            home=tmp_path,
            which=lambda name: f"/usr/bin/{name}",
            check_auth=lambda executable: True,
        )
        assert credential.source == SOURCE_CLI
        assert credential.token is None

    def test_an_unauthenticated_cli_still_falls_through_to_the_file(self, tmp_path: Path):
        self._write_credentials(tmp_path, f"github:\n  token: {FAKE_TOKEN}\n")
        credential = resolve_git_credential(
            "github",
            home=tmp_path,
            which=lambda name: f"/usr/bin/{name}",
            check_auth=lambda executable: False,
        )
        assert credential.source == SOURCE_FILE

    def test_no_cli_and_no_file_still_raises_with_remediation(self, tmp_path: Path):
        with pytest.raises(CredentialError) as caught:
            resolve_git_credential("github", home=tmp_path, which=lambda name: None)
        message = str(caught.value)
        assert "gh auth login" in message
        assert "credentials.yml" in message

    def test_the_clone_uses_that_resolver_by_default(self, env_file: Path, tmp_path: Path):
        """End to end through the real resolver, with `home` pointed at a
        fixture: no test-only credential path exists in the clone."""
        self._write_credentials(tmp_path, f"github:\n  token: {FAKE_TOKEN}\n")
        container = _FakeContainer()
        _create(
            container,
            env_file,
            PRIVATE_REPO,
            resolve_credential=lambda provider: resolve_git_credential(
                provider, home=tmp_path, which=lambda name: None
            ),
        )
        assert container.clone.environment["VAULTSHIELD_GIT_TOKEN"] == FAKE_TOKEN
        _assert_token_absent(container.clone.cmd)


def test_the_container_name_is_unchanged_by_any_of_this():
    """Guards the correlation `teardown` depends on."""
    assert container_name(RUN_ID) == f"vaultshield-run-{RUN_ID}"
