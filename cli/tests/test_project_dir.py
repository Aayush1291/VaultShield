"""`project_dir`: a nested application root, and the boundary it must not cross.

A repository does not always put its application at the clone root. The one that
prompted this has its `package.json` at
`/workspace/repo/citizen-samadhan-setu-main`, so dependency install found no
manifest, the agent started in a directory with nothing in it, and
`start_command` ran where there was no `package.json` to run.

`project_dir` names that subdirectory. Two things make it worth its own test
module:

STRUCTURE   it has to reach every consumer that needs it — dependency install,
            the agent's working directory, the application's working directory —
            and deliberately NOT reach the two that must stay at the clone root
            (`git clone` and the file-change watcher).

BOUNDARY    it becomes a container working directory, so it is attacker-shaped
            input. `/workspace` is one level above the clone and holds the
            read-only `.env.sandbox` mount; the container filesystem is above
            that. It is validated twice, in two modules, and both layers are
            asserted here independently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from vaultshield.adapters.antigravity import Antigravity_Adapter
from vaultshield.adapters.base import SandboxInvocation
from vaultshield.config import ConfigError, ProjectConfig, load_config
from vaultshield.sandbox import (
    REPO_DIR,
    Sandbox,
    SandboxError,
    container_path_under_repo,
)

RUN_ID = "20250614-091530-deadbeefdeadbeef"

# The real repository's nested application root.
NESTED = "citizen-samadhan-setu-main"


def _write_config(directory: Path, **overrides: Any) -> Path:
    """A minimal valid `.vaultshield.yml`, plus whatever a test overrides."""
    document: dict[str, Any] = {
        "start_command": "npm run dev",
        "port": 3000,
        "ready_path": "/",
        "agent": "antigravity",
        "routes": ["/"],
        "off_limits_paths": [],
    }
    document.update(overrides)
    path = directory / ".vaultshield.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


# --- layer 1: config validation ----------------------------------------------


class TestProjectDirValidation:
    """`load_config` is `run`'s step 1 — before the Run_Directory exists, before
    credentials are built, before any container. A rejected `project_dir` costs
    nothing and leaves nothing behind, which is the whole reason it is validated
    there."""

    def test_absent_means_the_repository_root(self, tmp_path: Path):
        """Backwards compatibility: every config written before this field
        existed said "the app is at the clone root" implicitly."""
        _write_config(tmp_path)
        assert load_config(tmp_path).project_dir == ""

    @pytest.mark.parametrize(
        "written,expected",
        [
            (None, ""),  # `project_dir:` with no value
            ("", ""),
            ("   ", ""),  # present but blank means the root
            (".", ""),  # the root, the long way round
            ("./app", "app"),  # normalised
            ("app", "app"),
            ("services/api", "services/api"),
            ("a/./b", "a/b"),
            (NESTED, NESTED),
        ],
    )
    def test_accepted_values_normalise(self, tmp_path: Path, written, expected):
        _write_config(tmp_path, project_dir=written)
        assert load_config(tmp_path).project_dir == expected

    @pytest.mark.parametrize(
        "written",
        [
            123,
            12.5,
            True,
            ["app"],
            {"dir": "app"},
        ],
    )
    def test_a_wrong_type_is_rejected(self, tmp_path: Path, written):
        """Same treatment as every other typed field: YAML types a value for
        you, so a present, non-empty value can still be the wrong thing."""
        _write_config(tmp_path, project_dir=written)
        with pytest.raises(ConfigError) as caught:
            load_config(tmp_path)
        assert "project_dir" in str(caught.value)

    def test_the_error_names_the_field_and_the_file(self, tmp_path: Path):
        path = _write_config(tmp_path, project_dir="../escape")
        with pytest.raises(ConfigError) as caught:
            load_config(tmp_path)
        message = str(caught.value)
        assert "project_dir" in message
        assert str(path) in message


class TestProjectDirTraversalIsRejected:
    """The attack this field invites. Every case is refused at load time, so no
    container is ever created for a Run carrying one."""

    @pytest.mark.parametrize(
        "written",
        [
            "..",
            "../etc",
            "../../etc/passwd",
            "app/../..",
            "app/../../etc",
            "./../etc",
            "a/b/../../../c",
            # `/workspace/.env.sandbox` is exactly one level up from the clone:
            # the read-only credential mount is the nearest thing worth reaching.
            "../.env.sandbox",
        ],
    )
    def test_a_traversal_segment_is_refused(self, tmp_path: Path, written):
        _write_config(tmp_path, project_dir=written)
        with pytest.raises(ConfigError) as caught:
            load_config(tmp_path)
        assert "project_dir" in str(caught.value)

    @pytest.mark.parametrize(
        "written",
        ["/etc", "/workspace/repo", "/", "/srv/app"],
    )
    def test_an_absolute_path_is_refused(self, tmp_path: Path, written):
        """Even one that happens to name the right directory: `project_dir` is
        defined as relative, and accepting an absolute form would mean the
        containment check had something to re-derive rather than to enforce."""
        _write_config(tmp_path, project_dir=written)
        with pytest.raises(ConfigError):
            load_config(tmp_path)

    @pytest.mark.parametrize("written", ["a\\b", "..\\..\\etc", "C:\\app", "C:/app"])
    def test_a_windows_path_is_refused(self, tmp_path: Path, written):
        """Backslashes are rejected before the POSIX checks run, because
        `..\\..\\etc` has no `/` for a segment split to find."""
        _write_config(tmp_path, project_dir=written)
        with pytest.raises(ConfigError):
            load_config(tmp_path)

    def test_a_name_that_merely_contains_dots_is_still_accepted(
        self, tmp_path: Path
    ):
        """`..` is rejected by segment, not by substring, so a legitimate
        directory name containing dots is not caught by accident."""
        _write_config(tmp_path, project_dir="my..app/..hidden")
        assert load_config(tmp_path).project_dir == "my..app/..hidden"


class TestContainerPathResolutionIsIndependentlySafe:
    """`sandbox.container_path_under_repo` re-checks containment itself.

    Duplication on purpose: the config validator owns the good error message,
    this owns the guarantee. A future caller that resolves a path from somewhere
    other than a validated config still cannot escape the clone."""

    @pytest.mark.parametrize(
        "relative,expected",
        [
            ("", REPO_DIR),
            (".", REPO_DIR),
            ("   ", REPO_DIR),
            ("app", f"{REPO_DIR}/app"),
            ("./app", f"{REPO_DIR}/app"),
            ("services/api", f"{REPO_DIR}/services/api"),
            (NESTED, f"{REPO_DIR}/{NESTED}"),
        ],
    )
    def test_resolution(self, relative, expected):
        assert container_path_under_repo(relative) == expected

    @pytest.mark.parametrize(
        "relative",
        [
            "../etc",
            "..",
            "app/../../etc",
            "../../../../etc/passwd",
            "/etc/passwd",
            "/",
            "../.env.sandbox",
        ],
    )
    def test_an_escape_raises_even_without_config_validation(self, relative):
        with pytest.raises(SandboxError) as caught:
            container_path_under_repo(relative)
        message = str(caught.value)
        assert "project_dir" in message or "absolute" in message

    def test_the_resolved_path_is_always_inside_the_repository(self):
        """The property the two checks exist to guarantee."""
        for relative in ["", ".", "app", "./app", "a/b/c", NESTED, "my..app"]:
            resolved = container_path_under_repo(relative)
            assert resolved == REPO_DIR or resolved.startswith(f"{REPO_DIR}/")


# --- layer 2: every consumer, and the two non-consumers ----------------------


class _RecordingContainer:
    """Records the workdir of every exec, so a test can assert which directory
    each pipeline step ran in."""

    def __init__(self) -> None:
        self.execs: list[tuple[list[str], str | None]] = []
        self.removals: list[dict[str, Any]] = []
        self.attrs: dict[str, Any] = {"Config": {"Env": []}}

    def start(self) -> None:
        pass

    def exec_run(self, cmd, **kwargs):
        self.execs.append((cmd, kwargs.get("workdir")))
        return 0, (b"", b"")

    def remove(self, **kwargs: Any) -> None:
        self.removals.append(kwargs)

    def workdir_of(self, needle: str) -> str | None:
        """The workdir of the one exec whose command contains `needle`."""
        matches = [wd for cmd, wd in self.execs if needle in " ".join(cmd)]
        assert len(matches) == 1, f"expected one {needle!r} exec, got {matches!r}"
        return matches[0]


class _RecordingClient:
    def __init__(self, container: _RecordingContainer) -> None:
        self._container = container
        self.created: list[dict[str, Any]] = []
        outer = self

        class _Images:
            def get(self, name):
                return object()

            def pull(self, name):  # pragma: no cover - image always "present"
                raise AssertionError("should not pull")

        class _Containers:
            def create(self, image, **kwargs):
                outer.created.append({"image": image, **kwargs})
                return outer._container

        self.images = _Images()
        self.containers = _Containers()


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env.sandbox"
    path.write_text("DATABASE_URL=postgresql://dummy_user:dummy_password@localhost/db\n")
    return path


class TestProjectDirMovesDependencyInstall:
    """Requirement E: dependencies come from the project's own manifests, inside
    the container."""

    def test_install_runs_in_the_project_dir(self, env_file: Path):
        container = _RecordingContainer()
        client = _RecordingClient(container)
        Sandbox(RUN_ID, client).create(  # type: ignore[arg-type]
            "https://example.com/acme/widgets.git",
            "vaultshield-sandbox:local",
            env_file,
            project_dir=NESTED,
        )
        assert container.workdir_of("npm install") == f"{REPO_DIR}/{NESTED}"

    def test_install_runs_at_the_repo_root_when_unset(self, env_file: Path):
        container = _RecordingContainer()
        client = _RecordingClient(container)
        Sandbox(RUN_ID, client).create(  # type: ignore[arg-type]
            "https://example.com/acme/widgets.git",
            "vaultshield-sandbox:local",
            env_file,
        )
        assert container.workdir_of("npm install") == REPO_DIR

    def test_the_clone_still_lands_at_the_repository_root(self, env_file: Path):
        """Requirement D: `project_dir` must not change cloning. `.git` has to
        stay where `git_capture` commits and diffs."""
        container = _RecordingContainer()
        client = _RecordingClient(container)
        Sandbox(RUN_ID, client).create(  # type: ignore[arg-type]
            "https://example.com/acme/widgets.git",
            "vaultshield-sandbox:local",
            env_file,
            project_dir=NESTED,
        )
        clone = [cmd for cmd, _ in container.execs if "clone" in cmd]
        assert len(clone) == 1
        # The clone target is the repo root, not the project dir.
        assert clone[0][-1] == REPO_DIR
        assert NESTED not in " ".join(clone[0])


class TestProjectDirMovesTheApplication:
    """Requirement E: `npm run dev` has to run where `package.json` is."""

    def _config(self, **overrides: Any) -> ProjectConfig:
        fields: dict[str, Any] = {
            "start_command": "npm run dev",
            "port": 3000,
            "ready_path": "/",
            "agent": "antigravity",
            "routes": [],
            "off_limits_paths": [],
        }
        fields.update(overrides)
        return ProjectConfig(**fields)

    def test_the_app_starts_in_the_project_dir(self):
        from vaultshield.browser_capture import _start_app

        class _Sandbox:
            def __init__(self):
                self.calls: list[tuple[list[str], str | None]] = []

            def exec_detached(self, cmd, *, env=None, workdir=None):
                self.calls.append((cmd, workdir))

        sandbox = _Sandbox()
        _start_app(sandbox, self._config(project_dir=NESTED))  # type: ignore[arg-type]
        assert sandbox.calls[0][1] == f"{REPO_DIR}/{NESTED}"

    def test_the_app_starts_at_the_repo_root_when_unset(self):
        from vaultshield.browser_capture import _start_app

        class _Sandbox:
            def __init__(self):
                self.calls: list[tuple[list[str], str | None]] = []

            def exec_detached(self, cmd, *, env=None, workdir=None):
                self.calls.append((cmd, workdir))

        sandbox = _Sandbox()
        _start_app(sandbox, self._config())  # type: ignore[arg-type]
        assert sandbox.calls[0][1] == REPO_DIR

    def test_the_app_still_binds_all_interfaces(self):
        """Requirement E: the host has to be able to reach it for screenshots,
        so `project_dir` must not disturb the bind address."""
        from vaultshield.browser_capture import _start_app

        class _Sandbox:
            def __init__(self):
                self.envs: list[dict[str, str] | None] = []

            def exec_detached(self, cmd, *, env=None, workdir=None):
                self.envs.append(env)

        sandbox = _Sandbox()
        _start_app(sandbox, self._config(project_dir=NESTED))  # type: ignore[arg-type]
        env = sandbox.envs[0] or {}
        assert env["PORT"] == "3000"
        assert env["HOST"] == "0.0.0.0"
        assert env["HOSTNAME"] == "0.0.0.0"


class TestProjectDirMovesTheAgentButNotTheWatcher:
    """The distinction the design turns on. The agent's cwd narrows to the
    application root; the file-change watch stays as wide as the repository."""

    def _supervise(self, project_dir: str, tmp_path: Path):
        """Runs `supervise_in_sandbox` against a recording Sandbox, returning
        (agent_workdir, watcher_root)."""
        from vaultshield import sandbox_monitor

        seen: dict[str, Any] = {}

        class _Stream:
            exit_code = 0

            def __iter__(self):
                return iter([b"agent output\n"])

        class _Sandbox:
            run_id = RUN_ID
            WORKDIR = "/workspace"
            REPO_DIR = REPO_DIR

            def exec(self, cmd):
                from vaultshield.sandbox import ExecResult

                if cmd[:2] == ["test", "-f"]:
                    return ExecResult(exit_code=0, stdout="", stderr="")
                if cmd[0] == "pgrep":
                    return ExecResult(exit_code=1, stdout="", stderr="")
                if cmd[0] == "cat":
                    return ExecResult(exit_code=0, stdout="", stderr="")
                return ExecResult(exit_code=0, stdout="", stderr="")

            def write_file(self, path, content):
                pass

            def exec_detached(self, cmd, *, env=None, workdir=None):
                # The watcher is started detached; its walk root is argv[2].
                if "vaultshield_watch.py" in " ".join(cmd):
                    seen["watcher_root"] = cmd[2]

            def exec_stream(self, cmd, *, env=None, workdir=None, tty=True):
                seen["agent_workdir"] = workdir
                return _Stream()

        class _Adapter:
            def sandbox_invocation(self, prompt, workdir):
                seen["adapter_told"] = workdir
                return SandboxInvocation(argv=["agent"], env={})

        sandbox_monitor.supervise_in_sandbox(
            _Sandbox(),  # type: ignore[arg-type]
            _Adapter(),  # type: ignore[arg-type]
            "do the thing",
            tmp_path,
            agent_workdir=container_path_under_repo(project_dir),
        )
        return seen

    def test_the_agent_runs_in_the_project_dir(self, tmp_path: Path):
        seen = self._supervise(NESTED, tmp_path)
        assert seen["agent_workdir"] == f"{REPO_DIR}/{NESTED}"

    def test_the_adapter_is_told_the_same_directory(self, tmp_path: Path):
        """An adapter that names its workdir in its own arguments has to be told
        the one the process will actually run in."""
        seen = self._supervise(NESTED, tmp_path)
        assert seen["adapter_told"] == f"{REPO_DIR}/{NESTED}"
        assert seen["adapter_told"] == seen["agent_workdir"]

    def test_the_watcher_still_walks_the_whole_repository(self, tmp_path: Path):
        """Requirement G, and the reason `agent_workdir` is a separate parameter.

        If the watch root moved with the agent, changes outside the application
        directory would go unrecorded, and every `off_limits_paths` entry would
        silently start being matched against a different base.
        """
        seen = self._supervise(NESTED, tmp_path)
        assert seen["watcher_root"] == REPO_DIR
        assert seen["watcher_root"] != seen["agent_workdir"]

    def test_both_are_the_repo_root_when_unset(self, tmp_path: Path):
        seen = self._supervise("", tmp_path)
        assert seen["agent_workdir"] == REPO_DIR
        assert seen["watcher_root"] == REPO_DIR


# --- the security envelope ---------------------------------------------------


class TestNoHostExposure:
    """Requirement J, asserted against what `Sandbox.create` actually asks
    Docker for. These are cheap to keep and would catch a future convenience
    mount immediately."""

    def _created(self, env_file: Path, **kwargs: Any) -> dict[str, Any]:
        container = _RecordingContainer()
        client = _RecordingClient(container)
        Sandbox(RUN_ID, client).create(  # type: ignore[arg-type]
            "https://example.com/acme/widgets.git",
            "vaultshield-sandbox:local",
            env_file,
            **kwargs,
        )
        assert len(client.created) == 1
        return client.created[0]

    def test_the_only_mount_is_the_readonly_credential_file(self, env_file: Path):
        created = self._created(env_file, project_dir=NESTED)
        volumes = created.get("volumes") or {}
        assert len(volumes) == 1
        (host_path, spec), = volumes.items()
        assert host_path == str(env_file.resolve())
        assert spec == {"bind": "/workspace/.env.sandbox", "mode": "ro"}

    def test_no_host_repository_is_mounted(self, env_file: Path):
        """The agent works on the container's own clone, never the developer's
        checkout."""
        created = self._created(env_file, project_dir=NESTED)
        mounted = list((created.get("volumes") or {}).keys())
        assert mounted == [str(env_file.resolve())]
        assert not any("APSIT" in p or "Codes" in p for p in mounted)

    def test_no_docker_socket_is_mounted(self, env_file: Path):
        """A mounted socket would let the agent escape to the host daemon."""
        created = self._created(env_file)
        assert "/var/run/docker.sock" not in str(created)

    def test_no_host_home_is_mounted(self, env_file: Path):
        created = self._created(env_file)
        rendered = str(created)
        assert str(Path.home()) not in rendered

    def test_the_container_gets_no_environment_of_its_own(self, env_file: Path):
        """Requirement C: no arbitrary host environment inheritance. `create`
        passes no `environment`, so the container has only what the image
        defines."""
        created = self._created(env_file)
        assert not created.get("environment")

    def test_no_privileged_or_host_network_flags(self, env_file: Path):
        created = self._created(env_file)
        assert not created.get("privileged")
        assert created.get("network_mode") in (None, "")


class TestTheAntigravityInvocationLeaksNoHostEnvironment:
    """Requirement B/J at the adapter seam."""

    def test_the_env_whitelist_is_empty(self):
        """`agy` reads no API-key variable, so there is nothing to whitelist —
        and in particular the host environment is not copied in."""
        invocation = Antigravity_Adapter().sandbox_invocation(
            "do the thing", f"{REPO_DIR}/{NESTED}"
        )
        assert invocation.env == {}

    def test_no_host_variable_reaches_the_invocation(self, monkeypatch):
        """A secret in the developer's shell must not cross the boundary, even
        one whose name looks agent-related."""
        monkeypatch.setenv("GEMINI_API_KEY", "host-only-secret-value-4f1a")
        monkeypatch.setenv("GOOGLE_API_KEY", "host-only-secret-value-9c2b")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-only-secret-value-7e3d")

        invocation = Antigravity_Adapter().sandbox_invocation("do the thing", REPO_DIR)

        rendered = f"{invocation.argv!r} {invocation.env!r}"
        assert "host-only-secret-value" not in rendered
        assert invocation.env == {}

    def test_the_prompt_is_the_only_caller_supplied_argv_element(self):
        """Nothing from the environment is interpolated into the command line."""
        invocation = Antigravity_Adapter().sandbox_invocation("add a health check", REPO_DIR)
        assert invocation.argv.count("add a health check") == 1
        assert all(
            "/Users/" not in arg and "/home/" not in arg for arg in invocation.argv
        )


# --- routes: [] no longer requires the application to come up ----------------


class TestNoRoutesSkipsTheApplicationEntirely:
    """Requirement F. The observable no-routes result is unchanged — an empty
    list and no `screenshots/` directory — but it no longer depends on the
    application starting and answering a readiness probe.

    Before this, `start_and_capture` started the app, polled `ready_path` to a
    200 or a `ReadinessTimeout`, and only then reached the empty-routes check in
    `capture_route_screenshots`. So a project with `routes: []` and a
    placeholder `start_command` failed a Run whose agent work had already
    succeeded, in order to capture zero screenshots.
    """

    def _config(self, **overrides: Any) -> ProjectConfig:
        fields: dict[str, Any] = {
            "start_command": "npm run dev",
            "port": 3000,
            "ready_path": "/",
            "agent": "antigravity",
            "routes": [],
            "off_limits_paths": [],
        }
        fields.update(overrides)
        return ProjectConfig(**fields)

    class _Sandbox:
        run_id = RUN_ID
        container = None

        def __init__(self) -> None:
            self.started: list[Any] = []

        def exec_detached(self, cmd, *, env=None, workdir=None):
            self.started.append(cmd)

    def test_no_routes_returns_no_screenshots(self, tmp_path: Path):
        from vaultshield.browser_capture import start_and_capture

        assert start_and_capture(
            self._Sandbox(), self._config(), tmp_path, 5  # type: ignore[arg-type]
        ) == []

    def test_no_routes_never_starts_the_application(self, tmp_path: Path):
        from vaultshield.browser_capture import start_and_capture

        sandbox = self._Sandbox()
        start_and_capture(sandbox, self._config(), tmp_path, 5)  # type: ignore[arg-type]
        assert sandbox.started == []

    def test_no_routes_creates_no_screenshots_directory(self, tmp_path: Path):
        """Unchanged from before: a Run that captured nothing leaves no empty
        directory behind."""
        from vaultshield.browser_capture import start_and_capture
        from vaultshield.run_directory import screenshots_dir

        start_and_capture(self._Sandbox(), self._config(), tmp_path, 5)  # type: ignore[arg-type]
        assert not screenshots_dir(tmp_path).exists()

    def test_no_routes_cannot_time_out(self, tmp_path: Path):
        """The behavioural fix. `_Sandbox.container` is `None`, so the old order
        would have raised `SandboxError` from `resolve_base_url` before it could
        even reach readiness — proof that nothing before the short-circuit runs.
        """
        from vaultshield.browser_capture import ReadinessTimeout, start_and_capture

        try:
            result = start_and_capture(
                self._Sandbox(), self._config(), tmp_path, 0  # type: ignore[arg-type]
            )
        except (ReadinessTimeout, SandboxError) as exc:  # pragma: no cover
            pytest.fail(f"routes:[] should not require the app to be reachable: {exc}")
        assert result == []

    def test_a_configured_route_still_starts_the_application(self, tmp_path: Path):
        """The other half: the short-circuit must not disable capture when a
        route IS configured. Fails at `resolve_base_url` here because this fake
        has no container, which proves the app path was entered."""
        from vaultshield.browser_capture import start_and_capture

        with pytest.raises(SandboxError):
            start_and_capture(
                self._Sandbox(), self._config(routes=["/"]), tmp_path, 1  # type: ignore[arg-type]
            )


# --- teardown on every failure path ------------------------------------------


class TestTeardownAfterAFailureWithAProjectDir:
    """Requirement C/I: teardown is guaranteed, and `project_dir` does not
    introduce a path that leaks a container."""

    def test_a_failed_dependency_install_removes_the_container(
        self, env_file: Path
    ):
        class _FailingInstall(_RecordingContainer):
            def exec_run(self, cmd, **kwargs):
                self.execs.append((cmd, kwargs.get("workdir")))
                if "npm install" in " ".join(cmd):
                    return 1, (b"", b"npm ERR! missing package.json")
                return 0, (b"", b"")

        container = _FailingInstall()
        client = _RecordingClient(container)
        sandbox = Sandbox(RUN_ID, client)  # type: ignore[arg-type]
        with pytest.raises(SandboxError) as caught:
            sandbox.create(
                "https://example.com/acme/widgets.git",
                "vaultshield-sandbox:local",
                env_file,
                project_dir=NESTED,
            )
        assert "dependency install" in str(caught.value)
        assert container.removals == [{"v": True, "force": True}]
        assert sandbox.container is None

    def test_an_invalid_project_dir_fails_before_any_container_exists(
        self, env_file: Path
    ):
        """`container_path_under_repo` raises inside `_install_dependencies`, but
        the point is the config layer refuses first — so in the real pipeline no
        Docker call is ever made. Asserted here at the Sandbox layer as the
        backstop: even reached directly, it fails and cleans up."""
        container = _RecordingContainer()
        client = _RecordingClient(container)
        sandbox = Sandbox(RUN_ID, client)  # type: ignore[arg-type]
        with pytest.raises(SandboxError):
            sandbox.create(
                "https://example.com/acme/widgets.git",
                "vaultshield-sandbox:local",
                env_file,
                project_dir="../escape",
            )
        assert sandbox.container is None
        assert container.removals == [{"v": True, "force": True}]

    def test_teardown_is_idempotent_afterwards(self, env_file: Path):
        container = _RecordingContainer()
        client = _RecordingClient(container)
        sandbox = Sandbox(RUN_ID, client)  # type: ignore[arg-type]
        with pytest.raises(SandboxError):
            sandbox.create(
                "https://example.com/acme/widgets.git",
                "vaultshield-sandbox:local",
                env_file,
                project_dir="../escape",
            )
        sandbox.teardown()  # must not raise, must not remove twice
        assert len(container.removals) == 1
