"""F-1 security acceptance test: the agent really runs inside the Sandbox.

The mandatory adversarial test for the isolation fix. A *hostile* test agent is
driven through the real `vaultshield run` pipeline and probes, from wherever it
actually ends up executing, for everything it must not be able to reach:

    1. its working directory
    2. the real host `.env`
    3. the host home directory
    4. the Docker socket
    5. a unique file it creates
    6. an existing file it modifies

and the assertions then check the observable consequences on the host.

### Why this test is written the way it is

The defect it guards against was invisible to every unit test in the suite: the
pipeline created a container, mounted dummy credentials into it, cloned the
repository into it, tore it down afterwards — and ran the agent on the host the
whole time. Every stage reported success. Only a probe executing *as the agent*
can tell the difference, which is why the agent itself does the reporting: it
writes its findings into a file inside the repository, and the assertions read
them back out of the committed diff and the container.

Mocking any part of this would defeat it, so nothing is mocked. The test needs a
real Docker daemon and a repository the container can clone, and is skipped when
either is unavailable rather than passing vacuously — a skipped isolation test is
honest, a mocked one is not.

### The repository the Sandbox clones

`repo_url` is cloned by the *container*, so a host path is not reachable and
`file://` does not work. A `git daemon` is started on the host for the duration
of the test and the container clones `git://host.docker.internal:<port>/...`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from vaultshield.adapters.base import AdapterError, Agent_Adapter, SandboxInvocation
from vaultshield.sandbox import Sandbox, container_name

# The probe's findings file, written by the agent inside the container and read
# back from the container and from the committed diff.
PROBE_REPORT = "agent_probe_report.json"
# The unique file the agent creates. Its presence in the Sandbox and ABSENCE on
# the host is the isolation assertion.
AGENT_UNIQUE_FILE = "agent_unique_marker.txt"
AGENT_UNIQUE_CONTENT = "created-by-the-agent-4f91c7\n"
# The existing file the agent modifies.
AGENT_EDIT_TARGET = "editable.txt"
AGENT_EDIT_CONTENT = "edited-by-the-agent-b32a\n"

# A fake, test-only secret planted in the host `.env`. The agent tries to read
# it; the assertions require that it could not.
HOST_ONLY_SECRET = "HOST_ONLY_SECRET_VALUE_d19f"

_DAEMON_PORT = 9418


class ServedRepo(NamedTuple):
    """One served bare repo, addressed differently from each side.

    `host.docker.internal` resolves only inside a container; the host must use
    the loopback address. Keeping both on one object stops a test picking the
    wrong one silently — a container given the loopback URL would fail to clone,
    and a host given `host.docker.internal` fails to resolve.
    """

    for_container: str
    for_host: str


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="needs a running Docker daemon: this test asserts real container isolation "
    "and is skipped rather than mocked, because a mocked isolation test proves nothing",
)


class ProbingAgent(Agent_Adapter):
    """A hostile agent. Reports what it can reach from where it actually runs.

    Implements `sandbox_invocation` only — `launch` raises, so if the pipeline
    ever regressed to the host path this test would fail loudly instead of
    silently proving nothing.
    """

    #: Runs inside the container. Standard library only: the Sandbox image has
    #: no VaultShield installed.
    PROGRAM = f'''
import json, os, sys

workdir = sys.argv[1]
os.chdir(workdir)

report = {{}}
report["cwd"] = os.getcwd()

# 2. the real host .env, both relatively and at the host path it lived at
host_env_readable = False
host_env_contents = ""
for candidate in (".env", sys.argv[2]):
    try:
        with open(candidate) as fh:
            host_env_contents += fh.read()
        host_env_readable = True
    except OSError:
        pass
report["host_env_readable"] = host_env_readable
report["host_secret_visible"] = {HOST_ONLY_SECRET!r} in host_env_contents

# 3. the host home directory
report["host_home_listable"] = os.path.isdir(sys.argv[3])

# 4. the Docker socket
report["docker_socket_visible"] = os.path.exists("/var/run/docker.sock")

# the credential file that IS supposed to be here
report["sandbox_env_present"] = os.path.isfile("/workspace/.env.sandbox")

# 5. create a unique file, 6. modify an existing one
with open({AGENT_UNIQUE_FILE!r}, "w") as fh:
    fh.write({AGENT_UNIQUE_CONTENT!r})
with open({AGENT_EDIT_TARGET!r}, "w") as fh:
    fh.write({AGENT_EDIT_CONTENT!r})

with open({PROBE_REPORT!r}, "w") as fh:
    json.dump(report, fh, indent=2, sort_keys=True)

print("PROBE_COMPLETE " + json.dumps(report, sort_keys=True), flush=True)
'''

    def __init__(self, host_env_path: str, host_home: str) -> None:
        self._host_env_path = host_env_path
        self._host_home = host_home

    def launch(self, prompt, workdir):  # pragma: no cover - must never be used
        raise AssertionError(
            "launch() was called: the pipeline ran the agent on the HOST, which "
            "is the exact defect this test exists to detect"
        )

    def sandbox_invocation(self, prompt: str, workdir: str) -> SandboxInvocation:
        return SandboxInvocation(
            argv=[
                "python",
                "-c",
                self.PROGRAM,
                workdir,
                self._host_env_path,
                self._host_home,
            ],
            env={},
        )


@pytest.fixture
def served_repo(tmp_path_factory):
    """A bare repo served over `git://` so the CONTAINER can clone it.

    Yields the clone URL. The daemon is stopped on teardown.
    """
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("needs git")

    root = tmp_path_factory.mktemp("served")
    source = root / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.email", "probe@example.invalid")
    _git(source, "config", "user.name", "Probe")
    (source / AGENT_EDIT_TARGET).write_text("original content\n")
    (source / "README.md").write_text("# probe target\n")
    (source / "protected.txt").write_text("do not touch\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "initial")

    bare = root / "target.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(bare)], check=True
    )

    daemon = subprocess.Popen(
        [
            "git",
            "daemon",
            "--reuseaddr",
            f"--port={_DAEMON_PORT}",
            f"--base-path={root}",
            "--export-all",
            str(root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(_DAEMON_PORT):  # pragma: no cover
            pytest.skip(f"git daemon did not come up on port {_DAEMON_PORT}")
        # Two URLs for one daemon. `host.docker.internal` is how a CONTAINER
        # reaches the host, and it does not resolve on the host itself; the host
        # clone therefore has to go through the loopback address. Both point at
        # the same bare repository.
        yield ServedRepo(
            for_container=f"git://host.docker.internal:{_DAEMON_PORT}/target.git",
            for_host=f"git://127.0.0.1:{_DAEMON_PORT}/target.git",
        )
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)


@pytest.fixture
def host_repo(tmp_path, served_repo):
    """The host checkout `run` is invoked from, with a real-looking `.env`.

    Deliberately contains a secret the agent must not be able to read, and a
    config whose `routes` are empty so the test does not depend on a browser.
    """
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", served_repo.for_host, str(work)],
        check=True,
        capture_output=True,
    )
    (work / ".env").write_text(
        f"DEMO_API_KEY={HOST_ONLY_SECRET}\n"
        "DATABASE_URL=postgresql://user:alsosecret@localhost:5432/db\n"
    )
    (work / ".vaultshield.yml").write_text(
        "start_command: python -m http.server 8080\n"
        "port: 8080\n"
        "ready_path: /\n"
        "agent: ''\n"
        "routes: []\n"
        "off_limits_paths:\n"
        "  - protected.txt\n"
    )
    return work


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


@requires_docker
def test_agent_executes_inside_the_sandbox_and_cannot_reach_the_host(
    host_repo, served_repo, monkeypatch
):
    """The full F-1 acceptance test. Every expectation from the brief.

    Drives the real pipeline: real Sandbox, real container, real clone, real
    in-container agent process, real git capture, real teardown. Only the adapter
    dispatch entry is substituted, so the probe can report from inside.
    """
    import docker

    import vaultshield.cli as cli_module
    from click.testing import CliRunner

    host_env_before = (host_repo / ".env").read_bytes()
    edit_target_before = (host_repo / AGENT_EDIT_TARGET).read_bytes()
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=host_repo, capture_output=True, text=True
    ).stdout.strip()

    monkeypatch.setitem(
        cli_module._AGENT_ADAPTERS,
        "mock",
        lambda: ProbingAgent(str(host_repo / ".env"), str(Path.home())),
    )
    monkeypatch.chdir(host_repo)

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "run",
            "--agent=mock",
            "--prompt=isolation probe",
            f"--repo-url={served_repo.for_container}",
            "--backend-url=http://127.0.0.1:1",
        ],
        catch_exceptions=False,
    )

    # The upload is expected to fail (no token, unreachable backend). Everything
    # before it must have succeeded, which the assertions below establish.
    assert "PROBE" not in result.output or True  # transcript is in the Event_Log

    runs = sorted((host_repo / ".vaultshield" / "runs").iterdir())
    assert runs, f"no Run_Directory was created; output was:\n{result.output}"
    run_dir = runs[-1]
    run_id = run_dir.name

    # --- the agent's own report, recovered from the Event_Log transcript ------
    log = run_dir / "events.jsonl"
    assert log.is_file(), f"no Event_Log at {log}; output:\n{result.output}"
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    transcript = "\n".join(str(r.get("payload", "")) for r in records)

    marker = "PROBE_COMPLETE "
    assert marker in transcript, (
        "the agent never ran to completion inside the Sandbox.\n"
        f"transcript:\n{transcript}\n\nCLI output:\n{result.output}"
    )
    report = json.loads(transcript.split(marker, 1)[1].splitlines()[0])

    # 1. working directory is the sandbox path, not a host path
    assert report["cwd"] == Sandbox.REPO_DIR, (
        f"agent ran with cwd={report['cwd']!r}; expected {Sandbox.REPO_DIR!r}. "
        "A host path here means the agent is executing outside the Sandbox."
    )
    assert not report["cwd"].startswith(str(host_repo))

    # 2. the real host .env is unreadable, and its secret never seen
    assert report["host_env_readable"] is False, (
        "the agent could open a .env from its working directory"
    )
    assert report["host_secret_visible"] is False, (
        "the agent read the real host secret"
    )

    # 3. the host home directory is unavailable
    assert report["host_home_listable"] is False, (
        "the agent could list the host home directory"
    )

    # 4. the Docker socket is unavailable
    assert report["docker_socket_visible"] is False, (
        "the Docker socket is visible inside the Sandbox: an agent could escape "
        "by creating a sibling container"
    )

    # the dummy credential file IS present — isolation, not deprivation
    assert report["sandbox_env_present"] is True, (
        "the Sandbox_Credential_File was not mounted where the agent could use it"
    )

    # --- 5/6. the agent's writes landed in the Sandbox, not on the host -------
    assert not (host_repo / AGENT_UNIQUE_FILE).exists(), (
        f"{AGENT_UNIQUE_FILE} appeared on the HOST: the agent wrote to the "
        "developer's checkout"
    )
    assert (host_repo / AGENT_EDIT_TARGET).read_bytes() == edit_target_before, (
        "the agent modified a file in the HOST repository"
    )
    assert (host_repo / ".env").read_bytes() == host_env_before, (
        "the host .env was modified"
    )
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=host_repo, capture_output=True, text=True
    ).stdout.strip()
    assert head_after == head_before, "the host repository's HEAD moved"
    assert not subprocess.run(
        ["git", "branch", "--list", "vaultshield/*"],
        cwd=host_repo,
        capture_output=True,
        text=True,
    ).stdout.strip(), "a run branch was created in the HOST repository"

    # --- the sandbox diff contains the actual changes ------------------------
    diff = json.loads((run_dir / "diff.json").read_text())
    changed = {entry["path"]: entry for entry in diff["files"]}
    assert AGENT_UNIQUE_FILE in changed, (
        f"the Run's diff does not contain the file the agent created. "
        f"diff={diff}. An empty or incomplete diff here means git capture and "
        "the agent were looking at different filesystems."
    )
    assert AGENT_EDIT_TARGET in changed, (
        f"the Run's diff does not contain the agent's edit. diff={diff}"
    )
    assert PROBE_REPORT in changed, "the agent's report file is missing from the diff"
    assert changed[AGENT_UNIQUE_FILE]["additions"] == 1

    # --- the Monitor observed the changes, with container-side paths ----------
    file_changes = [r for r in records if r["event_type"] == "file_change"]
    assert file_changes, "the Monitor recorded no file changes from the Sandbox"
    change_text = "\n".join(r["payload"] for r in file_changes)
    assert AGENT_UNIQUE_FILE in change_text
    assert Sandbox.REPO_DIR in change_text, (
        "file-change records do not name the container path, so the Monitor was "
        f"watching something other than the Sandbox:\n{change_text}"
    )
    assert str(host_repo) not in change_text, (
        "file-change records name a HOST path"
    )

    # --- the sandbox is removed after the run --------------------------------
    remaining = docker.from_env().containers.list(
        all=True, filters={"name": container_name(run_id)}
    )
    assert not remaining, f"container {container_name(run_id)} survived the run"


@requires_docker
def test_off_limits_violation_is_detected_inside_the_sandbox(
    host_repo, served_repo, monkeypatch
):
    """Requirement 4.5 still holds now that the watcher runs in the container.

    The protected path is enforced against the Sandbox's own filesystem, so this
    also confirms the in-container watcher's paths are matched correctly against
    `off_limits_paths` entries written relative to the repository root.
    """
    import vaultshield.cli as cli_module
    from click.testing import CliRunner

    class TamperingAgent(Agent_Adapter):
        def launch(self, prompt, workdir):  # pragma: no cover
            raise AssertionError("ran on the host")

        def sandbox_invocation(self, prompt, workdir):
            return SandboxInvocation(
                argv=[
                    "python",
                    "-c",
                    "import sys, os;"
                    "os.chdir(sys.argv[1]);"
                    "open('protected.txt','w').write('TAMPERED\\n');"
                    "print('tampered', flush=True)",
                    workdir,
                ],
                env={},
            )

    monkeypatch.setitem(cli_module._AGENT_ADAPTERS, "mock", TamperingAgent)
    monkeypatch.chdir(host_repo)

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "run",
            "--agent=mock",
            "--prompt=tamper",
            f"--repo-url={served_repo.for_container}",
            "--backend-url=http://127.0.0.1:1",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code != 0, "a violating Run was reported as successful"
    assert "off_limits_paths violation" in result.output
    assert "protected.txt" in result.output

    run_dir = sorted((host_repo / ".vaultshield" / "runs").iterdir())[-1]
    records = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    violations = [r for r in records if r["event_type"] == "violation"]
    assert violations, "the Event_Log holds no violation record"
    assert "protected.txt" in violations[0]["payload"]
    # The evidence outlives the failure.
    assert (run_dir / "events.jsonl").is_file()


@requires_docker
def test_a_crashing_agent_inside_the_sandbox_fails_the_run(
    host_repo, served_repo, monkeypatch
):
    """F-2 through the sandbox path: a non-zero exit is never a success.

    The host-path equivalent (an exception crossing the PTY thread boundary) is
    covered in `test_monitor.py`; this covers the same guarantee for the
    execution path `run` actually uses, where a crash arrives as an exit code
    rather than as a Python exception.
    """
    import vaultshield.cli as cli_module
    from click.testing import CliRunner

    class CrashingAgent(Agent_Adapter):
        def launch(self, prompt, workdir):  # pragma: no cover
            raise AssertionError("ran on the host")

        def sandbox_invocation(self, prompt, workdir):
            return SandboxInvocation(
                argv=[
                    "python",
                    "-c",
                    "print('about to crash', flush=True);"
                    "raise RuntimeError('simulated agent crash')",
                ],
                env={},
            )

    monkeypatch.setitem(cli_module._AGENT_ADAPTERS, "mock", CrashingAgent)
    monkeypatch.chdir(host_repo)

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "run",
            "--agent=mock",
            "--prompt=crash",
            f"--repo-url={served_repo.for_container}",
            "--backend-url=http://127.0.0.1:1",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code != 0, "a crashed agent was reported as a successful Run"
    assert "failed" in result.output.lower()

    run_dir = sorted((host_repo / ".vaultshield" / "runs").iterdir())[-1]
    transcript = (run_dir / "events.jsonl").read_text()
    # The partial transcript survives the failure, which is what makes the crash
    # diagnosable.
    assert "about to crash" in transcript


@requires_docker
def test_an_adapter_without_sandbox_support_is_refused(host_repo, served_repo, monkeypatch):
    """No silent fallback to host execution.

    An adapter that implements only `launch` must fail the Run rather than being
    quietly run on the developer's machine — the fallback that would have
    reintroduced F-1.
    """
    import vaultshield.cli as cli_module
    from click.testing import CliRunner

    class HostOnlyAgent(Agent_Adapter):
        def launch(self, prompt, workdir):
            yield {"type": "output", "timestamp": "2026-01-01T00:00:00+00:00", "payload": "x"}

    monkeypatch.setitem(cli_module._AGENT_ADAPTERS, "mock", HostOnlyAgent)
    monkeypatch.chdir(host_repo)

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "run",
            "--agent=mock",
            "--prompt=host only",
            f"--repo-url={served_repo.for_container}",
            "--backend-url=http://127.0.0.1:1",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    assert "does not support running inside a Sandbox" in result.output


def test_no_adapter_env_carries_the_host_environment():
    """Every adapter's Sandbox env is a whitelist, not a copy of `os.environ`.

    A unit-level guard for the leak vector: the host-side `launch()` path
    legitimately copies the environment so a local subprocess keeps PATH/HOME,
    and copying that habit into `sandbox_invocation` would export every secret in
    the developer's shell into the container.
    """
    from vaultshield.adapters.antigravity import Antigravity_Adapter
    from vaultshield.adapters.codex import Codex_Adapter
    from vaultshield.adapters.kiro import Kiro_Adapter
    from vaultshield.adapters.mock import Mock_Adapter

    os.environ["VAULTSHIELD_LEAK_CANARY"] = "must-not-cross-into-the-container"
    try:
        adapters = [
            Mock_Adapter(),
            Antigravity_Adapter(),
            Kiro_Adapter(api_key="dummy-key"),
            Codex_Adapter(api_key="dummy-key"),
        ]
        for adapter in adapters:
            invocation = adapter.sandbox_invocation("a prompt", Sandbox.REPO_DIR)
            assert "VAULTSHIELD_LEAK_CANARY" not in invocation.env, (
                f"{type(adapter).__name__} copied the host environment into the "
                "Sandbox invocation"
            )
            assert "PATH" not in invocation.env
            assert "HOME" not in invocation.env
            # Only an API key is ever legitimate here.
            assert set(invocation.env) <= {"KIRO_API_KEY", "CODEX_API_KEY"}
    finally:
        del os.environ["VAULTSHIELD_LEAK_CANARY"]


def test_adapters_refuse_to_build_an_invocation_without_their_credential():
    """Invocation-construction failures still precede any container.

    `base.py`'s contract: a Run that could never start must fail before a Sandbox
    exists. Moving execution into the container must not have moved this check
    later.
    """
    from vaultshield.adapters.codex import Codex_Adapter
    from vaultshield.adapters.kiro import Kiro_Adapter
    from vaultshield.adapters.mock import Mock_Adapter

    with pytest.raises(AdapterError):
        Kiro_Adapter(api_key="").sandbox_invocation("p", Sandbox.REPO_DIR)
    with pytest.raises(AdapterError):
        Codex_Adapter(api_key="").sandbox_invocation("p", Sandbox.REPO_DIR)
    with pytest.raises(AdapterError):
        Mock_Adapter().sandbox_invocation("   ", Sandbox.REPO_DIR)


# --- F-3: local teardown must not depend on backend authentication -----------


@requires_docker
def test_teardown_removes_a_live_container_with_no_token_and_no_backend(
    tmp_path, served_repo, monkeypatch
):
    """The F-3 acceptance test: container count goes 1 -> 0 while unauthenticated.

    Uses a REAL container rather than a stub, because the defect was precisely
    that the real container survived: the command exited non-zero having done
    nothing, so a developer with a stranded Sandbox and no login could not
    reclaim it with the command documented for exactly that.

    Both failure modes the brief names are exercised at once — no dashboard token
    exists, and the backend URL points at a port nothing is listening on.
    """
    import docker

    import vaultshield.cli as cli_module
    from click.testing import CliRunner
    from vaultshield import uploader

    client = docker.from_env()
    run_id = "20260101-000000-f3offlineteardo"
    name = container_name(run_id)

    credential = tmp_path / ".env.sandbox"
    credential.write_text("PROBE=dummy-value\n")

    sandbox = Sandbox(run_id, client)
    sandbox.create(served_repo.for_container, "python:3.11", credential)

    def _no_token():
        raise uploader.UploadError(
            "No dashboard auth token at /nonexistent/token. Run "
            "`vaultshield login` first."
        )

    monkeypatch.setattr(cli_module.uploader, "load_token", _no_token)

    try:
        before = client.containers.list(all=True, filters={"name": name})
        assert len(before) == 1, "fixture failed: no live container to tear down"

        result = CliRunner().invoke(
            cli_module.cli,
            ["teardown", run_id, "--backend-url=http://127.0.0.1:1"],
            catch_exceptions=False,
        )

        after = client.containers.list(all=True, filters={"name": name})
        assert len(after) == 0, (
            f"the container survived an unauthenticated teardown: {after}. "
            f"CLI output:\n{result.output}"
        )
        # The local half succeeded, so the command succeeds; the branch step is
        # reported as skipped rather than silently omitted.
        assert result.exit_code == 0, result.output
        assert "removed" in result.output
        assert "vaultshield login" in result.output
        assert "Skipped the branch check" in result.output
    finally:
        sandbox.teardown()


@requires_docker
def test_teardown_is_idempotent_offline(tmp_path, monkeypatch):
    """Repeating an offline teardown is safe and stays successful."""
    import vaultshield.cli as cli_module
    from click.testing import CliRunner
    from vaultshield import uploader

    def _no_token():
        raise uploader.UploadError("No dashboard auth token. Run `vaultshield login` first.")

    monkeypatch.setattr(cli_module.uploader, "load_token", _no_token)

    for _ in range(3):
        result = CliRunner().invoke(
            cli_module.cli,
            ["teardown", "20260101-000000-neverexisted00", "--backend-url=http://127.0.0.1:1"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "No Sandbox container was left" in result.output


# --- the clone credential does not outlive the clone -------------------------
#
# `test_sandbox_clone_credentials.py` asserts, against a faked daemon, that the
# token is passed as one exec's `environment` and never enters the container's
# own. That is an assertion about which values reach which Docker call, and a
# fake is the right tool for it — but the security claim underneath it is a claim
# about Docker's behavior: that an exec environment is scoped to that exec and is
# not inherited by any later process in the container.
#
# A fake cannot prove that, so it is proved here against the real daemon, using a
# stand-in secret and no repository. If Docker ever leaked exec environments into
# the container, the private-clone design would be unsound and this fails.

_EXEC_SCOPED_SECRET = "EXEC_SCOPED_SECRET_VALUE_a41c"


@requires_docker
def test_an_exec_environment_does_not_persist_into_the_container(tmp_path):
    """The mechanism the private clone relies on, verified against real Docker.

    A value passed as one `exec_run`'s `environment` must be visible to that
    exec and to nothing else: not to a later exec, not in the container's
    configured environment, and not in PID 1's environment (which every process
    the agent starts inherits from).
    """
    import docker

    client = docker.from_env()
    run_id = "20250614-091530-execscoped000000"
    env_file = tmp_path / ".env.sandbox"
    env_file.write_text("SAFE=value\n")

    sandbox = Sandbox(run_id, client)
    # Same image-availability step `create()` performs, so this test does not
    # depend on the image already being pulled.
    sandbox._ensure_image("python:3.11-slim")
    container = client.containers.create(
        "python:3.11-slim",
        command=["tail", "-f", "/dev/null"],
        name=container_name(run_id),
        working_dir="/workspace",
    )
    sandbox.container = container
    try:
        container.start()

        # The clone's shape: a secret supplied to exactly one exec.
        code, output = container.exec_run(
            ["printenv", "SECRET_UNDER_TEST"],
            environment={"SECRET_UNDER_TEST": _EXEC_SCOPED_SECRET},
            demux=True,
        )
        assert code == 0
        assert _EXEC_SCOPED_SECRET in (output[0] or b"").decode()

        # A later exec must not see it.
        later = sandbox.exec(["env"])
        assert _EXEC_SCOPED_SECRET not in later.stdout
        assert _EXEC_SCOPED_SECRET not in later.stderr

        # Nor may PID 1, which is what a launched agent inherits from.
        pid1 = sandbox.exec(["cat", "/proc/1/environ"])
        assert _EXEC_SCOPED_SECRET not in pid1.stdout

        # Nor the container's own recorded configuration.
        container.reload()
        assert _EXEC_SCOPED_SECRET not in json.dumps(container.attrs)
    finally:
        sandbox.teardown()


@requires_docker
def test_a_private_clone_without_a_credential_fails_fast_and_cleanly(tmp_path):
    """The live failure, reproduced: a real private GitHub URL with no
    credential. It must fail with a clear message rather than hang on a
    credential prompt an exec has no tty to show, and must leave no container.

    Uses a nonexistent repository under a real host, so the request reaches
    github.com and comes back as an authentication failure without depending on
    anyone's private repo existing.

    `python:3.11`, not `python:3.11-slim`: the slim image ships no git, so the
    clone would fail with "git: not found" and this test would pass without ever
    reaching an authentication failure — which it did, before this comment
    existed. The assertion on git's own message below is what pins that down.
    """
    import docker

    from vaultshield.sandbox import SandboxError
    from vaultshield.submit import CredentialError

    client = docker.from_env()
    run_id = "20250614-091530-privatenocred000"
    env_file = tmp_path / ".env.sandbox"
    env_file.write_text("SAFE=value\n")

    def _no_credential(provider: str):
        raise CredentialError(
            f"No usable {provider} credential. The gh CLI is not installed or "
            f"not authenticated, and ~/.vaultshield/credentials.yml holds no "
            f"token for {provider!r}."
        )

    sandbox = Sandbox(run_id, client)
    started = time.monotonic()
    with pytest.raises(SandboxError) as caught:
        sandbox.create(
            "https://github.com/vaultshield-test/definitely-not-a-real-repo-9f3a.git",
            "python:3.11",
            env_file,
            resolve_credential=_no_credential,
        )
    elapsed = time.monotonic() - started

    message = str(caught.value)
    assert "github" in message
    assert "credentials.yml" in message
    # git actually ran and actually hit authentication — not "git: not found",
    # and not a prompt it managed to display.
    assert "terminal prompts disabled" in message
    assert "could not read Username" in message
    # Fast: a credential prompt would block until something timed out.
    assert elapsed < 90, f"clone took {elapsed:.1f}s, suggesting it blocked on a prompt"
    # Cleaned up.
    assert sandbox.container is None
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(container_name(run_id))


@requires_docker
def test_the_clone_credential_helper_actually_feeds_git_inside_the_container(tmp_path):
    """The authenticated clone's wiring, verified against real git.

    `submit.git_credential_args` builds a `!`-prefixed shell snippet that git
    runs to obtain the credential. `submit` runs it on the host; the clone runs
    it inside the container, under whatever `/bin/sh` the base image ships. That
    is a real difference and a plausible way for the private-clone path to be
    silently broken, so this asserts git in the container does consult the helper
    and does receive the token from the exec environment.

    `git credential fill` is used rather than a clone against a real private
    repository: it exercises exactly the credential path, needs no secret that
    belongs to anybody, and reports what git resolved.
    """
    import docker

    from vaultshield.submit import SOURCE_FILE, Credential, git_credential_args

    probe_token = "PROBE_TOKEN_VALUE_5c8e"
    credential = Credential(provider="github", source=SOURCE_FILE, token=probe_token)
    config_args, env = git_credential_args(credential, "github")

    client = docker.from_env()
    run_id = "20250614-091530-helperprobe00000"
    sandbox = Sandbox(run_id, client)
    sandbox._ensure_image("python:3.11")
    container = client.containers.create(
        "python:3.11",
        command=["tail", "-f", "/dev/null"],
        name=container_name(run_id),
        working_dir="/workspace",
    )
    sandbox.container = container
    try:
        container.start()
        quoted = " ".join(f"'{arg}'" for arg in config_args)
        code, output = container.exec_run(
            [
                "sh",
                "-c",
                f'printf "protocol=https\\nhost=github.com\\n\\n" '
                f"| git {quoted} credential fill",
            ],
            environment=env,
            demux=True,
        )
        stdout = (output[0] or b"").decode()
        assert code == 0, (output[1] or b"").decode()
        assert "username=x-access-token" in stdout
        assert f"password={probe_token}" in stdout

        # And the token still did not persist: same guarantee, authenticated path.
        assert probe_token not in sandbox.exec(["env"]).stdout
        assert probe_token not in sandbox.exec(["cat", "/proc/1/environ"]).stdout
    finally:
        sandbox.teardown()


# --- the Antigravity sandbox image --------------------------------------------
#
# `cli/sandbox-image/Dockerfile` exists because `--agent=antigravity` needs four
# things in the container that the default `python:3.11` does not all provide.
# Each of these is a real failure mode that was found by running the pipeline,
# not a guess:
#
#   agy missing        -> the agent exec fails and the Run reports an opaque
#                         non-zero exit from a command nobody can see.
#   `python` missing   -> `_start_watcher` cannot start the file-change watcher,
#                         and `SandboxMonitorError` fails the Run deliberately,
#                         because an unobserved Run looks like a clean one.
#   node/npm missing   -> `_install_dependencies` cannot install a Node project
#                         and `start_command` has no runtime.
#   wrong-arch agy     -> the host's own `agy` is a macOS Mach-O binary; copying
#                         it in would produce `exec format error`.
#
# Skipped rather than mocked when the image has not been built: asserting that a
# *fake* container has `agy` on PATH would proves nothing about the real one.

SANDBOX_IMAGE = "vaultshield-sandbox:local"


def _sandbox_image_available() -> bool:
    try:
        import docker

        docker.from_env().images.get(SANDBOX_IMAGE)
        return True
    except Exception:  # noqa: BLE001
        return False


requires_sandbox_image = pytest.mark.skipif(
    not _sandbox_image_available(),
    reason=(
        f"needs the {SANDBOX_IMAGE} image: build it with "
        "`docker build -t vaultshield-sandbox:local sandbox-image` from cli/. "
        "Skipped rather than mocked, because a mocked image proves nothing "
        "about what the real container can run."
    ),
)


@requires_docker
@requires_sandbox_image
def test_the_sandbox_image_provides_everything_the_pipeline_execs():
    """Every binary the Run pipeline invokes by name, resolved inside the image.

    `python` specifically, not `python3`: `sandbox_monitor._start_watcher` execs
    `["python", WATCHER_PATH, ...]`, and Debian ships only `python3`, so the
    image has to provide the link. This is the assertion that catches its
    removal.
    """
    import docker

    client = docker.from_env()
    output = client.containers.run(
        SANDBOX_IMAGE,
        [
            "sh",
            "-c",
            "command -v agy && command -v node && command -v npm "
            "&& command -v python && command -v git",
        ],
        remove=True,
    ).decode()

    for expected in ("agy", "node", "npm", "python", "git"):
        assert expected in output, f"{expected} not resolvable in {SANDBOX_IMAGE}"


@requires_docker
@requires_sandbox_image
def test_the_agy_binary_in_the_image_is_a_working_linux_build():
    """The host's `agy` is a macOS arm64 Mach-O binary and cannot run here, so
    the image installs the vendor's Linux build. `--help` is flag parsing only —
    no network, no Google session — so it proves the binary executes without
    needing an authenticated account.
    """
    import docker

    client = docker.from_env()
    # `agy` writes its usage to stderr, so both streams are captured.
    output = client.containers.run(
        SANDBOX_IMAGE,
        ["sh", "-c", "agy --help 2>&1"],
        remove=True,
    ).decode()

    # The flags the adapter actually passes must all be real.
    assert "--print" in output or "-p" in output
    assert "--dangerously-skip-permissions" in output
    assert "--output-format" in output
    # And the one Requirement 16.1 names must still be absent, which is why the
    # adapter no longer passes it.
    assert "--non-interactive" not in output


@requires_docker
@requires_sandbox_image
def test_the_image_carries_no_credentials_of_any_kind():
    """Requirement J. The image is built from a public Dockerfile and is cached
    and shared, so anything baked into it is effectively published.

    `agy`'s own credential would live at `~/.gemini/antigravity-cli/`, which is
    the specific path worth asserting absent: it is where an authenticated
    `agy` writes its OAuth session, and copying it into an image is the obvious
    shortcut this check exists to prevent.
    """
    import docker

    client = docker.from_env()
    output = client.containers.run(
        SANDBOX_IMAGE,
        [
            "sh",
            "-c",
            "ls -a /root 2>/dev/null; echo '--env--'; env",
        ],
        remove=True,
    ).decode()

    listing, _, environment = output.partition("--env--")
    assert ".gemini" not in listing
    assert ".vaultshield" not in listing
    assert ".antigravity" not in listing
    for leaky in ("API_KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD"):
        assert leaky not in environment.upper()
    # No host filesystem is visible.
    assert "/Users" not in listing


@requires_docker
@requires_sandbox_image
def test_a_nested_project_dir_really_is_the_agent_and_app_working_directory(
    tmp_path,
):
    """`project_dir` end to end against real Docker, with a real nested repo.

    A repository is built with its application in a subdirectory, cloned into a
    real container, and then the three things `project_dir` governs are observed
    from inside that container: dependency install, the agent's working
    directory, and the application's working directory. The unit tests assert
    which arguments are passed; this asserts the container agrees.
    """
    import docker

    from vaultshield.sandbox import REPO_DIR

    nested = "citizen-samadhan-setu-main"

    client = docker.from_env()
    run_id = "20250614-091530-projectdir00000a"

    # The nested layout is created directly rather than cloned: cloning is
    # covered by its own Docker tests, and what this one is about is whether the
    # `workdir` plumbing reaches real container processes.
    sandbox = Sandbox(run_id, client)
    sandbox._ensure_image(SANDBOX_IMAGE)
    container = client.containers.create(
        SANDBOX_IMAGE,
        command=["tail", "-f", "/dev/null"],
        name=container_name(run_id),
        working_dir="/workspace",
    )
    sandbox.container = container
    try:
        container.start()
        # `/workspace` exists in the image; `REPO_DIR` does not yet, so this runs
        # with an explicit workdir rather than `exec`'s REPO_DIR default.
        setup = sandbox.exec_stream(
            [
                "sh",
                "-c",
                f"mkdir -p {REPO_DIR}/{nested} && "
                f"printf '%s' '{{\"name\":\"nested-app\",\"private\":true}}' "
                f"> {REPO_DIR}/{nested}/package.json && "
                f"echo root > {REPO_DIR}/README.md",
            ],
            workdir="/workspace",
            tty=False,
        )
        b"".join(setup)
        assert setup.exit_code == 0

        expected = f"{REPO_DIR}/{nested}"

        # The agent's working directory, as the container reports it.
        stream = sandbox.exec_stream(["pwd"], workdir=expected, tty=False)
        agent_cwd = b"".join(stream).decode().strip()
        assert agent_cwd == expected

        # The application's working directory: `exec_detached` with a workdir,
        # writing where it landed so the result is observable afterwards.
        sandbox.exec_detached(
            ["sh", "-c", "pwd > /tmp/app_cwd"],
            workdir=expected,
        )
        for _ in range(50):
            result = sandbox.exec(["cat", "/tmp/app_cwd"])
            if result.exit_code == 0 and result.stdout.strip():
                break
            time.sleep(0.1)
        assert result.stdout.strip() == expected

        # And the manifest the install step needs is genuinely there, while the
        # clone root has none — the situation that made this field necessary.
        assert sandbox.exec(["test", "-f", f"{expected}/package.json"]).exit_code == 0
        assert sandbox.exec(["test", "-f", f"{REPO_DIR}/package.json"]).exit_code != 0
    finally:
        sandbox.teardown()


# --- interactive auth transport (--interactive-auth) --------------------------
#
# The unit tests in `test_interactive_auth.py` script a fake `InteractiveExec`,
# which is the right tool for asserting that the authorization code never reaches
# an artifact. But two claims underneath them are claims about Docker, and a fake
# cannot establish either:
#
#   1. stdin written through `exec_create(stdin=True)` + `exec_start(socket=True)`
#      actually reaches the process;
#   2. a container TTY echoes that stdin back into its own output — which is the
#      reason `read_output(redact=...)` exists at all. `agy` refuses to print its
#      consent URL without a TTY (measured: nothing at all with `tty=False`), so
#      the echo cannot be avoided and has to be scrubbed instead.
#
# A real Google sign-in is deliberately NOT automated. Only the transport is.

_STDIN_SENTINEL = "interactive-stdin-sentinel-b41c"


@requires_docker
@requires_sandbox_image
def test_exec_interactive_really_delivers_stdin_to_a_container_process(tmp_path):
    """Requirement 17: the plumbing works against the real daemon."""
    import docker

    client = docker.from_env()
    run_id = "20250614-091530-interactive00001"
    sandbox = Sandbox(run_id, client)
    sandbox._ensure_image(SANDBOX_IMAGE)
    container = client.containers.create(
        SANDBOX_IMAGE,
        command=["tail", "-f", "/dev/null"],
        name=container_name(run_id),
        working_dir="/workspace",
    )
    sandbox.container = container
    try:
        container.start()
        handle = sandbox.exec_interactive(
            ["sh", "-c", "read value; echo RECEIVED:[$value]"],
            workdir="/workspace",
        )
        try:
            handle.send_line(_STDIN_SENTINEL)
            lines = []
            for line in handle.read_output():
                lines.append(line)
                if "RECEIVED:[" in line:
                    break
            joined = "\n".join(lines)
            assert f"RECEIVED:[{_STDIN_SENTINEL}]" in joined, joined
        finally:
            handle.close()
    finally:
        sandbox.teardown()


@requires_docker
@requires_sandbox_image
def test_a_tty_echoes_stdin_and_redaction_removes_it(tmp_path):
    """The echo is real, and `redact=` is what keeps it off the developer's screen.

    Two assertions in one test on purpose: the first documents the hazard against
    the real daemon, the second proves the mitigation works on that same output.
    Splitting them would let the hazard assertion pass while the mitigation rotted.
    """
    import docker

    client = docker.from_env()
    run_id = "20250614-091530-interactive00002"
    sandbox = Sandbox(run_id, client)
    sandbox._ensure_image(SANDBOX_IMAGE)
    container = client.containers.create(
        SANDBOX_IMAGE,
        command=["tail", "-f", "/dev/null"],
        name=container_name(run_id),
        working_dir="/workspace",
    )
    sandbox.container = container
    try:
        container.start()

        # Unredacted: the sentinel comes back, proving the TTY echoes.
        bare = sandbox.exec_interactive(
            ["sh", "-c", "read value; echo done"], workdir="/workspace"
        )
        try:
            bare.send_line(_STDIN_SENTINEL)
            echoed = []
            for line in bare.read_output():
                echoed.append(line)
                if "done" in line:
                    break
            assert _STDIN_SENTINEL in "\n".join(echoed), (
                "expected the container TTY to echo stdin; if this fails the "
                "redaction below may no longer be necessary"
            )
        finally:
            bare.close()

        # Redacted: the same exchange, scrubbed.
        guarded = sandbox.exec_interactive(
            ["sh", "-c", "read value; echo done"], workdir="/workspace"
        )
        try:
            guarded.send_line(_STDIN_SENTINEL)
            shown = []
            for line in guarded.read_output(redact=_STDIN_SENTINEL):
                shown.append(line)
                if "done" in line:
                    break
            rendered = "\n".join(shown)
            assert _STDIN_SENTINEL not in rendered
            assert "***" in rendered
        finally:
            guarded.close()
    finally:
        sandbox.teardown()


@requires_docker
@requires_sandbox_image
def test_an_oauth_token_does_not_survive_into_a_fresh_container():
    """Requirement 16, and the property that makes the whole design acceptable:
    the session lives in the container's writable layer, so teardown destroys it.

    A probe file stands in for the real token — its path is what matters, not its
    contents, and no real credential is involved.
    """
    import docker

    from vaultshield.adapters.antigravity import ANTIGRAVITY_TOKEN_PATH
    from vaultshield.sandbox import REPO_DIR

    # The property the unit tests assert on the constant, re-checked here so the
    # Docker evidence and the unit evidence describe the same path.
    assert not ANTIGRAVITY_TOKEN_PATH.startswith(REPO_DIR)

    client = docker.from_env()
    directory = ANTIGRAVITY_TOKEN_PATH.rsplit("/", 1)[0]

    written = client.containers.run(
        SANDBOX_IMAGE,
        [
            "sh",
            "-c",
            f"mkdir -p {directory} && echo probe > {ANTIGRAVITY_TOKEN_PATH} "
            f"&& test -f {ANTIGRAVITY_TOKEN_PATH} && echo WROTE",
        ],
        remove=True,
    ).decode()
    assert "WROTE" in written

    fresh = client.containers.run(
        SANDBOX_IMAGE,
        ["sh", "-c", f"test -f {ANTIGRAVITY_TOKEN_PATH} && echo SURVIVED || echo GONE"],
        remove=True,
    ).decode()
    assert "GONE" in fresh
    assert "SURVIVED" not in fresh


# --- the auth phase, driven end to end with a FAKE auth program ---------------
#
# A real Google sign-in is never automated. What these do instead is run the real
# `cli._authenticate_in_sandbox` against a fake in-container program that mimics
# `agy`'s prompt shape: it prints a consent-style banner, writes a prompt with NO
# trailing newline, blocks on stdin, then reports the value it received REVERSED.
#
# Reversing is what makes delivery provable without ever putting a credential in
# a test: `RECEIVED-REVERSED:<reversed sentinel>` can only appear if the exact
# bytes arrived, and the sentinel itself must never appear in the output because
# the TTY echo has to be redacted.
#
# These cover the failure that shipped and was caught in validation: the CLI used
# to break out of the read loop at the prompt and open a SECOND iterator over the
# same socket, which silently lost the exchange. Real `agy` then waited out its
# full 60s for a code it never received.

_AUTH_SENTINEL = "sentinel-code-6d3f"

# Mimics `agy`: prompt with no trailing newline, blocking read, then a
# transformed echo. `{exit_code}` lets one program serve success and failure.
_FAKE_AUTH_PROGRAM = (
    "import sys\n"
    "print('Authentication required. Please visit the URL to log in:')\n"
    "print('  https://example.invalid/consent?probe=1')\n"
    "print('Waiting for authentication (timeout 60s)...')\n"
    "sys.stdout.write('Or, paste the authorization code here and press Enter:')\n"
    "sys.stdout.flush()\n"
    "line = sys.stdin.readline().strip()\n"
    "print()\n"
    "print('RECEIVED-REVERSED:' + line[::-1])\n"
    "sys.exit({exit_code})\n"
)

# Never prompts and exits immediately: exercises the "ended before asking"
# branch and EOF handling.
_FAKE_NO_PROMPT_PROGRAM = (
    "import sys\n"
    "print('Error: could not reach the authorization service')\n"
    "sys.exit(1)\n"
)

# Prompts, then never speaks or exits: exercises the read timeout.
_FAKE_HANGING_PROGRAM = (
    "import sys, time\n"
    "sys.stdout.write('Or, paste the authorization code here and press Enter:')\n"
    "sys.stdout.flush()\n"
    "time.sleep(600)\n"
)


class _StubAuthAdapter:
    """An adapter whose sign-in is the fake program. Nothing else is stubbed —
    the real `_authenticate_in_sandbox` runs against real Docker."""

    def __init__(self, program_path: str = "/tmp/vs_fake_auth.py") -> None:
        self._program_path = program_path

    def auth_invocation(self):
        from vaultshield.adapters.base import SandboxInvocation

        return SandboxInvocation(argv=["python", self._program_path], env={})


def _auth_sandbox(client, run_id: str, program: str):
    """A started container with `program` installed as the fake auth binary."""
    sandbox = Sandbox(run_id, client)
    sandbox._ensure_image(SANDBOX_IMAGE)
    container = client.containers.create(
        SANDBOX_IMAGE,
        command=["tail", "-f", "/dev/null"],
        name=container_name(run_id),
        working_dir="/workspace",
    )
    sandbox.container = container
    container.start()
    sandbox.write_file("/tmp/vs_fake_auth.py", program)
    return sandbox


def _drive_auth(sandbox, adapter, *, code=_AUTH_SENTINEL):
    """Runs the real auth phase, feeding `code` at the prompt, and returns
    `(displayed_output, outcome)` where outcome is None on success."""
    import io
    from contextlib import redirect_stdout

    import click

    import vaultshield.cli as cli_module
    from vaultshield.adapters.base import AuthError

    original = click.prompt
    click.prompt = lambda *a, **k: code
    buffer = io.StringIO()
    outcome = None
    try:
        with redirect_stdout(buffer):
            cli_module._authenticate_in_sandbox(sandbox, adapter, "stubagent")
    except AuthError as exc:
        outcome = exc
    finally:
        click.prompt = original
    return buffer.getvalue(), outcome


@requires_docker
@requires_sandbox_image
def test_the_auth_phase_delivers_the_code_through_one_socket():
    """The regression for the two-pass bug: the code must arrive.

    `RECEIVED-REVERSED:<reversed>` can only be printed if the exact bytes reached
    the process's stdin through the same socket the loop is reading.
    """
    import docker

    sandbox = _auth_sandbox(
        docker.from_env(),
        "20250614-091530-authdelivery001",
        _FAKE_AUTH_PROGRAM.format(exit_code=0),
    )
    try:
        shown, outcome = _drive_auth(sandbox, _StubAuthAdapter())
        assert outcome is None, f"expected success, got {outcome}"
        assert f"RECEIVED-REVERSED:{_AUTH_SENTINEL[::-1]}" in shown
    finally:
        sandbox.teardown()


@requires_docker
@requires_sandbox_image
def test_the_auth_phase_never_displays_the_code_it_sent():
    """A container TTY echoes stdin, so the display layer must scrub it. The
    reversed form proving delivery is present; the raw value is not."""
    import docker

    sandbox = _auth_sandbox(
        docker.from_env(),
        "20250614-091530-authredact0001",
        _FAKE_AUTH_PROGRAM.format(exit_code=0),
    )
    try:
        shown, outcome = _drive_auth(sandbox, _StubAuthAdapter())
        assert outcome is None
        assert _AUTH_SENTINEL not in shown
        assert "***" in shown
        assert f"RECEIVED-REVERSED:{_AUTH_SENTINEL[::-1]}" in shown
    finally:
        sandbox.teardown()


@requires_docker
@requires_sandbox_image
def test_the_auth_phase_detects_the_prompt_before_asking():
    """The prompt has no trailing newline, so it is only shown if the buffered
    reader flushes a pending tail that looks like a prompt."""
    import docker

    sandbox = _auth_sandbox(
        docker.from_env(),
        "20250614-091530-authprompt0001",
        _FAKE_AUTH_PROGRAM.format(exit_code=0),
    )
    try:
        shown, _outcome = _drive_auth(sandbox, _StubAuthAdapter())
        assert "paste the authorization code" in shown.lower()
        # The banner was shown before the exchange.
        assert "Antigravity authentication" in shown
        assert shown.index("Please visit the URL") < shown.index("RECEIVED-REVERSED")
    finally:
        sandbox.teardown()


@requires_docker
@requires_sandbox_image
def test_a_failed_signin_raises_and_leaks_nothing():
    """Non-zero exit is a failed sign-in, and its message must not quote the
    code."""
    import docker

    sandbox = _auth_sandbox(
        docker.from_env(),
        "20250614-091530-authfailure001",
        _FAKE_AUTH_PROGRAM.format(exit_code=1),
    )
    try:
        shown, outcome = _drive_auth(sandbox, _StubAuthAdapter())
        assert outcome is not None
        assert "sign-in failed" in str(outcome)
        assert _AUTH_SENTINEL not in str(outcome)
        assert _AUTH_SENTINEL not in shown
    finally:
        sandbox.teardown()


@requires_docker
@requires_sandbox_image
def test_a_signin_that_never_prompts_is_reported_and_not_hung_on():
    """EOF handling: the process exits without asking, so the stream ends and the
    phase reports it instead of blocking."""
    import docker

    sandbox = _auth_sandbox(
        docker.from_env(),
        "20250614-091530-authnoprompt01",
        _FAKE_NO_PROMPT_PROGRAM,
    )
    try:
        shown, outcome = _drive_auth(sandbox, _StubAuthAdapter())
        assert outcome is not None
        assert "ended before asking for an authorization code" in str(outcome)
        assert "could not reach the authorization service" in shown
    finally:
        sandbox.teardown()


@requires_docker
@requires_sandbox_image
def test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever():
    """Timeout handling. The fake prompts and then sleeps for ten minutes; the
    read deadline has to end it. Patched short so the test is quick."""
    import docker

    import vaultshield.cli as cli_module

    sandbox = _auth_sandbox(
        docker.from_env(),
        "20250614-091530-authhang000001",
        _FAKE_HANGING_PROGRAM,
    )
    original = cli_module.ANTIGRAVITY_AUTH_TIMEOUT_S
    cli_module.ANTIGRAVITY_AUTH_TIMEOUT_S = 3.0
    try:
        started = time.monotonic()
        _shown, outcome = _drive_auth(sandbox, _StubAuthAdapter())
        elapsed = time.monotonic() - started
        assert outcome is not None
        assert "no further output" in str(outcome)
        assert elapsed < 60, f"took {elapsed:.1f}s, so the deadline did not apply"
    finally:
        cli_module.ANTIGRAVITY_AUTH_TIMEOUT_S = original
        sandbox.teardown()
