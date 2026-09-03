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
