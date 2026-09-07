"""`--interactive-auth`: signing the agent in inside the Sandbox.

Antigravity cannot be authenticated any other way. It reads no API-key variable
(`GOOGLE_APPLICATION_CREDENTIALS` is never even opened — 0 of 730 traced `openat`
calls; `GEMINI_API_KEY`/`GOOGLE_API_KEY` are ignored), has no `login` subcommand,
and persists its session as a file. So the only route is to complete a Google
OAuth exchange, and the safest place to complete it is inside the disposable
container that is about to run the agent.

That makes this the most security-sensitive code path in the CLI, and these tests
are organised by the thing that must not happen rather than by function:

    DEFAULT     the flag is opt-in; without it nothing about any agent changes.
    ISOLATION   no host credential store is read — not `~/.gemini`, not the
                macOS Keychain, not the host's existing login.
    THE CODE    the authorization code reaches the container's stdin and nowhere
                else: not argv, not the environment, not the Event_Log, not an
                exception, not the terminal.
    THE TOKEN   the session lives at a path outside the repository, so it cannot
                enter `diff.json` or a file_change event, and it dies with the
                container.
    TEARDOWN    the Sandbox is destroyed whether the sign-in succeeds, fails, or
                is cancelled.

A real Google sign-in is deliberately NOT automated. The transport underneath it
is verified against real Docker at the bottom of this file; the human part stays
manual.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import vaultshield.cli as cli_module
from vaultshield.adapters.antigravity import (
    ANTIGRAVITY_OAUTH_SCOPES,
    ANTIGRAVITY_TOKEN_PATH,
    Antigravity_Adapter,
)
from vaultshield.adapters.base import AuthError
from vaultshield.adapters.codex import Codex_Adapter
from vaultshield.adapters.kiro import Kiro_Adapter
from vaultshield.adapters.mock import Mock_Adapter
from vaultshield.cli import cli
from vaultshield.run_directory import event_log_path
from vaultshield.sandbox import REPO_DIR

# A stand-in authorization code. Distinctive enough that a substring search for it
# cannot match anything incidental, and obviously not a real Google code.
FAKE_CODE = "4/0AfakeAUTHcode_SPIKE_VALUE_9q2z"

# What `agy` prints while waiting. The auth phase watches for the last line.
_CONSENT_OUTPUT = [
    "Authentication required. Please visit the URL to log in:",
    "  https://accounts.google.com/o/oauth2/auth?client_id=1071006060591-x&scope=y",
    "Waiting for authentication (timeout 60s)...",
    "Or, paste the authorization code here and press Enter:",
]

_CONFIG_YAML = """\
start_command: npm run dev
port: 3000
ready_path: /
agent: antigravity
project_dir: citizen-samadhan-setu-main
routes: []
off_limits_paths: []
"""

_RUN_ARGS = [
    "run",
    "--agent=antigravity",
    "--prompt=do the thing",
    "--repo-url=https://example.com/acme/widgets.git",
]


# --- fakes -------------------------------------------------------------------


class _FakeInteractiveExec:
    """Scripted stand-in for `InteractiveExec`.

    Reproduces the one behaviour that makes this path dangerous: a container TTY
    echoes stdin back into its own output. `read_output` therefore replays the
    code it was sent, so a test can prove the display layer scrubs it.
    """

    def __init__(self, *, prompt_lines=None, exit_code=0, after_send=None) -> None:
        self.prompt_lines = list(_CONSENT_OUTPUT if prompt_lines is None else prompt_lines)
        self.exit_code = exit_code
        self.after_send = ["hello!", "Signed in."] if after_send is None else list(after_send)
        self.sent: list[str] = []
        self.closed = False
        self._phase = 0

    def read_output(self, *, redact: str | None = None):
        if self._phase == 0:
            self._phase = 1
            for line in self.prompt_lines:
                yield _scrub(line, redact)
        else:
            # The TTY echo: whatever was sent comes straight back.
            for sent in self.sent:
                yield _scrub(sent, redact)
            for line in self.after_send:
                yield _scrub(line, redact)

    def send_line(self, text: str) -> None:
        self.sent.append(text)

    def flush_remaining(self, *, redact: str | None = None) -> str:
        return ""

    def wait(self) -> int | None:
        return self.exit_code

    def close(self) -> None:
        self.closed = True


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def _wire(
    monkeypatch,
    *,
    interactive=None,
    auth_raises=None,
    supervise_raises=None,
):
    """Stands in for every Docker- and network-dependent stage of `run`.

    Returns `(calls, seen)`: `calls` is the stage log in order, `seen` records
    what each stage was handed.
    """
    calls: list[str] = []
    seen: dict[str, Any] = {}
    handle = interactive if interactive is not None else _FakeInteractiveExec()
    seen["handle"] = handle

    class _Sandbox:
        WORKDIR = "/workspace"
        REPO_DIR = REPO_DIR

        def __init__(self, run_id, client):
            self.run_id = run_id
            self.container = object()
            seen["sandbox"] = self

        def create(self, repo_url, base_image, env_file, *, publish_port=None, project_dir=""):
            calls.append("sandbox.create")
            seen["create_kwargs"] = {
                "repo_url": repo_url,
                "base_image": base_image,
                "env_file": str(env_file),
                "publish_port": publish_port,
                "project_dir": project_dir,
            }

        def exec_interactive(self, cmd, *, workdir=None):
            calls.append("sandbox.exec_interactive")
            seen["auth_argv"] = cmd
            seen["auth_workdir"] = workdir
            if auth_raises is not None:
                raise auth_raises
            return handle

        def teardown(self):
            calls.append("sandbox.teardown")

    def _supervise(sandbox, adapter, prompt, run_dir, *, off_limits_paths=None, **kwargs):
        calls.append("monitor.supervise_in_sandbox")
        if supervise_raises is not None:
            raise supervise_raises
        log = event_log_path(run_dir)
        # A realistic Event_Log: the monitored agent's output only.
        log.write_text(
            json.dumps(
                {"event_type": "output", "timestamp": "2026-01-01T00:00:00.000000Z",
                 "payload": "[agent] working"}
            )
            + "\n"
        )
        seen["event_log"] = log
        return log

    def _capture(sandbox, config, run_dir, ready_timeout_s=None):
        calls.append("browser_capture")
        return []

    def _commit(sandbox, run_id):
        calls.append("git.commit")
        return f"vaultshield/run-{run_id}"

    def _diff(sandbox, base_ref, branch, run_dir):
        calls.append("git.diff")
        written = run_dir / "diff.json"
        written.write_text('{"files": []}\n')
        seen["diff"] = written
        return written

    monkeypatch.setattr(cli_module, "Sandbox", _Sandbox)
    monkeypatch.setattr(cli_module, "supervise_in_sandbox", _supervise)
    monkeypatch.setattr(cli_module, "start_and_capture", _capture)
    monkeypatch.setattr(cli_module, "commit_run_branch", _commit)
    monkeypatch.setattr(cli_module, "build_structured_diff", _diff)
    monkeypatch.setattr(cli_module.uploader, "load_token", lambda: "test-token")
    monkeypatch.setattr(
        cli_module.uploader, "upload_run", lambda *a, **k: calls.append("upload")
    )
    monkeypatch.setattr(cli_module.docker, "from_env", lambda: object())
    return calls, seen


def _invoke(args, *, stdin=None):
    return CliRunner().invoke(cli, args, input=stdin, catch_exceptions=False)


# --- DEFAULT: the flag is opt-in ---------------------------------------------


class TestTheFlagIsOptIn:
    """Requirement: default behaviour MUST be unchanged when the flag is absent."""

    def test_no_auth_phase_without_the_flag(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire(monkeypatch)
            result = runner.invoke(cli, _RUN_ARGS, catch_exceptions=False)
            assert result.exit_code == 0, result.output
            assert "sandbox.exec_interactive" not in calls

    def test_the_flag_defaults_to_false(self):
        flag = next(
            p for p in cli.commands["run"].params if p.name == "interactive_auth"
        )
        assert flag.is_flag is True
        assert flag.default is False

    def test_the_pipeline_order_is_unchanged_without_the_flag(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire(monkeypatch)
            runner.invoke(cli, _RUN_ARGS, catch_exceptions=False)
            assert calls == [
                "sandbox.create",
                "monitor.supervise_in_sandbox",
                "browser_capture",
                "git.commit",
                "git.diff",
                "sandbox.teardown",
                "upload",
            ]

    def test_the_auth_phase_runs_immediately_after_create(self, monkeypatch):
        """Between the Sandbox existing and the agent starting — the agent cannot
        start unauthenticated, and the session must live in the container that
        will run it."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire(monkeypatch)
            runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert calls.index("sandbox.create") < calls.index("sandbox.exec_interactive")
            assert calls.index("sandbox.exec_interactive") < calls.index(
                "monitor.supervise_in_sandbox"
            )


class TestOtherAdaptersAreUnaffected:
    """Only Antigravity needs a sign-in. Every other adapter returns `None`, so
    the flag is a no-op for them rather than an error."""

    @pytest.mark.parametrize(
        "adapter", [Codex_Adapter, Kiro_Adapter, Mock_Adapter]
    )
    def test_no_auth_invocation(self, adapter):
        assert adapter().auth_invocation() is None

    def test_the_flag_is_harmless_for_an_agent_that_needs_no_signin(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire(monkeypatch)
            result = runner.invoke(
                cli,
                [a if a != "--agent=antigravity" else "--agent=mock" for a in _RUN_ARGS]
                + ["--interactive-auth"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            assert "needs no in-Sandbox sign-in" in result.output
            assert "sandbox.exec_interactive" not in calls


# --- ISOLATION: no host credential store is touched --------------------------


class TestNoHostCredentialStoreIsRead:
    """Requirements 1-3: never the Keychain, never host `~/.gemini`, never the
    developer's existing login."""

    def test_the_auth_invocation_carries_no_host_path(self):
        argv = Antigravity_Adapter().auth_invocation().argv
        rendered = " ".join(argv)
        assert str(Path.home()) not in rendered
        assert ".gemini" not in rendered
        assert "Keychain" not in rendered
        assert "/Users/" not in rendered

    def test_the_auth_invocation_carries_no_credential(self):
        invocation = Antigravity_Adapter().auth_invocation()
        assert invocation.env == {}
        assert not any("token" in a.lower() for a in invocation.argv)

    def test_the_auth_invocation_does_not_auto_approve_tool_use(self):
        """A sign-in must not be able to edit anything, unlike the monitored
        invocation which does pass `--dangerously-skip-permissions`."""
        argv = Antigravity_Adapter().auth_invocation().argv
        assert "--dangerously-skip-permissions" not in argv

    def test_no_host_gemini_or_keychain_path_reaches_docker(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            _calls, seen = _wire(monkeypatch)
            runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            rendered = json.dumps(seen["create_kwargs"]) + " ".join(seen["auth_argv"])
            for forbidden in (".gemini", "Keychain", "login.keychain", str(Path.home())):
                assert forbidden not in rendered

    def test_the_only_host_file_named_is_the_credential_file(self, monkeypatch):
        """Requirement 13 / the no-host-mount invariant: `.env.sandbox` and
        nothing else."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            _calls, seen = _wire(monkeypatch)
            runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert seen["create_kwargs"]["env_file"].endswith(".env.sandbox")

    def test_the_signin_runs_outside_the_clone(self, monkeypatch):
        """A neutral working directory, so a misbehaving probe could not touch the
        repository."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            _calls, seen = _wire(monkeypatch)
            runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert seen["auth_workdir"] == "/workspace"
            assert seen["auth_workdir"] != REPO_DIR


# --- THE CODE: five channels, all closed -------------------------------------


class TestTheAuthorizationCodeCannotEscape:
    """The code bears the developer's Google identity until redeemed."""

    def _run(self, monkeypatch, runner, **wire):
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire(monkeypatch, **wire)
        result = runner.invoke(
            cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
            catch_exceptions=False,
        )
        return result, calls, seen

    def test_it_reaches_stdin_and_only_stdin(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _result, _calls, seen = self._run(monkeypatch, runner)
            # Delivered to the process...
            assert seen["handle"].sent == [FAKE_CODE]
            # ...and absent from argv.
            assert FAKE_CODE not in " ".join(seen["auth_argv"])

    def test_it_never_appears_in_argv_or_create_arguments(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _result, _calls, seen = self._run(monkeypatch, runner)
            assert FAKE_CODE not in json.dumps(seen["create_kwargs"])
            assert FAKE_CODE not in " ".join(seen["auth_argv"])

    def test_exec_interactive_accepts_no_environment_parameter(self):
        """Structural: there is nowhere to put a credential, so it cannot be put
        there by mistake."""
        import inspect

        from vaultshield.sandbox import Sandbox

        params = inspect.signature(Sandbox.exec_interactive).parameters
        assert "env" not in params
        assert "environment" not in params

    def test_it_is_never_echoed_to_the_terminal(self, monkeypatch):
        """A container TTY echoes stdin back into its own output, and `agy`
        requires a TTY, so the display layer has to scrub it. The fake replays the
        echo precisely so this assertion means something."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result, _calls, seen = self._run(monkeypatch, runner)
            assert seen["handle"].sent == [FAKE_CODE]  # it WAS sent
            assert FAKE_CODE not in result.output  # and never displayed
            assert "***" in result.output  # the echo was redacted, not dropped

    def test_it_never_reaches_the_event_log(self, monkeypatch):
        """Requirements 7-9. Structural rather than filtered: the auth exchange
        goes through `exec_interactive`, which no part of `sandbox_monitor`
        touches, and the monitored path has no stdin at all."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _result, _calls, seen = self._run(monkeypatch, runner)
            log = seen["event_log"].read_text()
            assert FAKE_CODE not in log
            for record in (json.loads(l) for l in log.splitlines() if l.strip()):
                assert FAKE_CODE not in json.dumps(record)

    def test_it_never_reaches_the_diff_artifact(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _result, _calls, seen = self._run(monkeypatch, runner)
            assert FAKE_CODE not in seen["diff"].read_text()

    def test_it_never_appears_in_a_failure_message(self, monkeypatch):
        """Requirement 10: a failed sign-in must not quote the code back."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result, _calls, _seen = self._run(
                monkeypatch, runner, interactive=_FakeInteractiveExec(exit_code=1)
            )
            assert result.exit_code != 0
            assert FAKE_CODE not in result.output
            assert "sign-in failed" in result.output

    def test_no_code_entered_is_refused_without_starting_the_agent(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _seen = _wire(monkeypatch)
            result = runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input="\n",
                catch_exceptions=False,
            )
            assert result.exit_code != 0
            assert "monitor.supervise_in_sandbox" not in calls
            assert "sandbox.teardown" in calls


# --- THE TOKEN: where the session lives --------------------------------------


class TestTheSessionTokenCannotEnterAnArtifact:
    """The token is a file. These are the properties of its location that keep it
    out of every VaultShield artifact — asserted on the constant, so a future
    change to the path is caught here."""

    def test_the_token_path_is_outside_the_repository(self):
        """Requirement 14. The file-change watcher walks the repository, so a path
        outside it cannot become a file_change event."""
        assert not ANTIGRAVITY_TOKEN_PATH.startswith(REPO_DIR)

    def test_the_token_path_cannot_appear_in_a_git_diff(self):
        """Requirement 15. git only sees the work tree at `REPO_DIR`."""
        assert not ANTIGRAVITY_TOKEN_PATH.startswith(f"{REPO_DIR}/")
        assert ANTIGRAVITY_TOKEN_PATH.startswith("/root/")

    def test_the_token_path_is_under_the_container_home(self):
        assert ANTIGRAVITY_TOKEN_PATH == (
            "/root/.gemini/antigravity-cli/antigravity-oauth-token"
        )

    def test_the_scopes_are_declared_and_not_described_as_narrow(self):
        """They are broad, and the banner has to say so."""
        assert "cloud-platform" in ANTIGRAVITY_OAUTH_SCOPES
        assert "userinfo.email" in ANTIGRAVITY_OAUTH_SCOPES
        assert len(ANTIGRAVITY_OAUTH_SCOPES) == 7

    def test_the_banner_discloses_the_scopes_before_asking_for_consent(
        self, monkeypatch
    ):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            _wire(monkeypatch)
            result = runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            for scope in ANTIGRAVITY_OAUTH_SCOPES:
                assert scope in result.output
            # The disclosure must precede the prompt.
            assert result.output.index("cloud-platform") < result.output.index(
                "Authorization code"
            )

    def test_the_banner_states_the_agent_can_read_its_own_session(self, monkeypatch):
        """The claim VaultShield must not overstate."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            _wire(monkeypatch)
            result = runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert "read its own authentication session" in result.output
            assert "broad, not narrow" in result.output
            assert "host Antigravity login is not read" in result.output


# --- TEARDOWN: guaranteed on every auth outcome ------------------------------


class TestTeardownIsGuaranteedAroundTheAuthPhase:
    """Requirement 15 of the brief: teardown on auth failure, Ctrl-C, agent
    failure, browser failure, upload failure."""

    def test_teardown_after_a_failed_signin(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire(monkeypatch, interactive=_FakeInteractiveExec(exit_code=1))
            result = runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert result.exit_code != 0
            assert "sandbox.teardown" in calls
            assert "monitor.supervise_in_sandbox" not in calls

    def test_teardown_after_a_cancelled_signin(self, monkeypatch):
        """Ctrl-C during the sign-in. `KeyboardInterrupt` is a `BaseException`, so
        this asserts the `finally` really does cover it."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire(
                monkeypatch, auth_raises=KeyboardInterrupt()
            )
            result = runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=True,
            )
            assert result.exit_code != 0
            assert "sandbox.teardown" in calls
            assert "monitor.supervise_in_sandbox" not in calls

    def test_teardown_when_the_signin_never_asks_for_a_code(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire(
                monkeypatch,
                interactive=_FakeInteractiveExec(prompt_lines=["Error: network unreachable"]),
            )
            result = runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert result.exit_code != 0
            assert "ended before asking for an authorization code" in result.output
            assert "sandbox.teardown" in calls

    def test_the_exec_handle_is_always_closed(self, monkeypatch):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            handle = _FakeInteractiveExec(exit_code=1)
            _calls, _seen = _wire(monkeypatch, interactive=handle)
            runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert handle.closed is True

    def test_a_failed_signin_message_names_no_event_log(self, monkeypatch):
        """`AuthError` is not an `AdapterError` precisely so the message does not
        claim an Event_Log that was never written."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            _wire(monkeypatch, interactive=_FakeInteractiveExec(exit_code=1))
            result = runner.invoke(
                cli, _RUN_ARGS + ["--interactive-auth"], input=f"{FAKE_CODE}\n",
                catch_exceptions=False,
            )
            assert "Event_Log preserved" not in result.output

    def test_auth_error_is_not_an_adapter_error(self):
        from vaultshield.adapters.base import AdapterError

        assert not issubclass(AuthError, AdapterError)
