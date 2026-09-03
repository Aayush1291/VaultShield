"""Smoke tests for the VaultShield CLI entry point scaffold.

Proves the pytest harness works and that the top-level Click group exists
with a registered `init` command. `init` now performs real file writes
(detect_framework + write_config, Task 1.2), so its tests run inside an
isolated filesystem to avoid writing `.vaultshield.yml` into the real repo.
"""

from pathlib import Path

from click.testing import CliRunner

from vaultshield import cli as cli_module
from vaultshield.adapters.antigravity import Antigravity_Adapter
from vaultshield.adapters.codex import Codex_Adapter
from vaultshield.adapters.kiro import Kiro_Adapter
from vaultshield.browser_capture import (
    DEFAULT_READY_TIMEOUT_S,
    ReadinessTimeout,
    ScreenshotError,
)
from vaultshield.cli import _AGENT_ADAPTERS, cli
from vaultshield.git_capture import GitCaptureError, run_branch_name
from vaultshield.run_directory import diff_path, event_log_path, screenshots_dir
from vaultshield.submit import CredentialError, SubmitError
from vaultshield.teardown import TeardownError
from vaultshield.uploader import UploadError, token_path


def test_cli_group_help_exits_zero():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0


def test_init_is_a_registered_subcommand():
    assert "init" in cli.commands


def test_init_command_runs_without_error():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0


def test_init_writes_config_and_reports_manual_fill_in_when_undetected():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert Path(".vaultshield.yml").is_file()
        assert "manually" in result.output


def test_init_reports_detected_config_path():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("manage.py").write_text("")
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "Detected project configuration" in result.output
        assert ".vaultshield.yml" in result.output


# `run` wiring: the adapter dispatch table (Task 6.4, Requirement 5.3) and
# the full pipeline's Config_Module gate (Task 8.2, Requirements 7.1-7.3).
# design.md's Testing Strategy lists this wiring as unit-test-covered rather
# than as a numbered Correctness Property, so it's exercised here with
# CliRunner rather than with a Hypothesis property test. The pipeline's
# later stages (Sandbox, Monitor, Event_Log) need a real Docker daemon and
# are covered by Task 8.3's own test; the checks here are the ones that are
# meaningful without one.

_RUN_ARGS = [
    "run",
    "--agent=mock",
    "--prompt=do the thing",
    "--repo-url=https://example.com/acme/widgets.git",
]

# An `--agent` value the dispatch table will never hold, used to exercise
# Click's rejection of an unregistered choice. Requirement 22.1 fixes the
# MVP's adapter set at Codex, Kiro, and Antigravity (plus `mock`), so
# nothing in the plan can register this one and the test below never has to
# move again.
_UNREGISTERED_AGENT = "no-such-agent"


def test_run_is_a_registered_subcommand():
    assert "run" in cli.commands


def test_run_rejects_unregistered_agent_choice():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            f"--agent={_UNREGISTERED_AGENT}",
            "--prompt=do the thing",
            "--repo-url=https://example.com/acme/widgets.git",
        ],
    )
    # An `--agent` value with no entry in the dispatch table must be
    # rejected by Click's own Choice validation, before `run` executes and
    # before anything is created on disk. The name is deliberately one the
    # plan never registers (see `_UNREGISTERED_AGENT`) so this check stays
    # about the dispatch table's gatekeeping rather than about any one agent
    # name — it used to name the next adapter due to be wired in, which
    # meant moving it with every adapter task.
    assert result.exit_code != 0
    assert _UNREGISTERED_AGENT in result.output.lower()


def test_run_dispatches_codex_to_the_codex_adapter():
    # Task 10.5, Requirement 8.1: `--agent=codex` selects `Codex_Adapter`,
    # so a Run made with it actually invokes the Codex CLI. Asserted on the
    # dispatch table plus the `--agent` choices Click derives from it,
    # rather than by starting a Run: reaching `launch` needs a real Docker
    # daemon and a real `codex` executable.
    assert _AGENT_ADAPTERS["codex"] is Codex_Adapter

    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "codex" in result.output


def test_run_dispatches_kiro_to_the_kiro_adapter():
    # Task 19.5, Requirement 15.1: `--agent=kiro` selects `Kiro_Adapter`, so
    # a Run made with it invokes `kiro-cli chat --no-interactive` with
    # `KIRO_API_KEY`. Asserted the same way `codex` is, and for the same
    # reason: reaching `launch` needs a real Docker daemon and a real
    # `kiro-cli` executable, while what `launch` then does is Tasks
    # 19.1/19.3's concern and is covered against `Kiro_Adapter` directly.
    assert _AGENT_ADAPTERS["kiro"] is Kiro_Adapter
    # Constructible with no arguments, which is the one thing this table
    # requires of a value: `run` instantiates it as `_AGENT_ADAPTERS[agent]()`
    # before any Sandbox exists. No `KIRO_API_KEY` is needed here — the
    # adapter reads it at `launch()`, not at construction.
    assert isinstance(_AGENT_ADAPTERS["kiro"](), Kiro_Adapter)

    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "kiro" in result.output


def test_run_dispatches_antigravity_to_the_antigravity_adapter():
    # Task 20.4, Requirement 16.1: `--agent=antigravity` selects
    # `Antigravity_Adapter`, so a Run made with it invokes `agy -p
    # "<prompt>" --non-interactive`. Asserted the same way `codex` and
    # `kiro` are, and for the same reason: reaching `launch` needs a real
    # Docker daemon and a real `agy` executable, while what `launch` then
    # does is Tasks 20.1/20.2's concern and is covered against
    # `Antigravity_Adapter` directly in `test_antigravity.py`.
    assert _AGENT_ADAPTERS["antigravity"] is Antigravity_Adapter
    # Constructible with no arguments, which is the one thing this table
    # requires of a value: `run` instantiates it as
    # `_AGENT_ADAPTERS[agent]()` before any Sandbox exists. This adapter
    # takes no constructor arguments at all — Requirement 16.1 names no
    # credential for `agy`.
    assert isinstance(_AGENT_ADAPTERS["antigravity"](), Antigravity_Adapter)

    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "antigravity" in result.output


def test_run_agent_choices_are_the_mvp_adapter_set():
    # Requirement 22.1: the MVP supports exactly three Agent_Adapter
    # implementations — Codex, Kiro, and Antigravity — plus `mock`, the
    # Monitor's own integration target (Requirement 5.3). With Task 20.4
    # the table is complete, so `--agent`'s choices are final; this pins
    # them so a fourth agent cannot be registered without the requirement
    # being revisited.
    assert set(_AGENT_ADAPTERS) == {"antigravity", "codex", "kiro", "mock"}


def test_run_exposes_the_pipeline_options():
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    for option in ("--repo-url", "--base-image", "--env-file", "--backend-url"):
        assert option in result.output


def test_run_stops_on_missing_config_without_any_side_effect():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, _RUN_ARGS)
        # Config_Module is the pipeline's first stage: with no
        # `.vaultshield.yml` present the Run stops there, naming the file,
        # and nothing has been created on disk yet — no Run_Directory, no
        # Sandbox_Credential_File, and no Docker call attempted.
        assert result.exit_code != 0
        assert ".vaultshield.yml" in result.output
        assert not Path(".vaultshield").exists()


def test_run_reports_missing_required_config_fields():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(
            "start_command: npm run dev\nready_path: /\n"
        )
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code != 0
        assert "port" in result.output
        assert not Path(".vaultshield").exists()


# Browser_Capture_Module's place in the pipeline (Task 11.5, Requirement
# 9.1): that `start_and_capture` runs immediately after the Monitor returns
# and before teardown, that it is handed this Run's Sandbox/config/
# Run_Directory, that `config.port` is published so the started application
# is reachable from the host, and that both of its failures fail the Run
# while still tearing the Sandbox down (Requirement 9.4, design.md's Error
# Handling section).
#
# What each stage *does* is covered elsewhere — `start_and_capture`'s own
# behavior by Tasks 11.1-11.4's tests, the Sandbox/Monitor by Tasks 4 and 7
# — so only those three stages are stood in for here, and the rest of the
# pipeline (config load, Run_Directory, credential file, adapter lookup,
# every exception handler under test) runs for real against a real
# filesystem.

_CONFIG_YAML = """\
start_command: npm run dev
port: 3000
ready_path: /healthz
routes:
  - /
  - /dashboard
"""

# Stand-in for the Dashboard_Auth_Token `uploader.load_token` reads out of
# `~/.vaultshield/token`. Never a real token, and never written anywhere:
# `load_token` is substituted, so no test here touches the developer's own
# token file.
_TEST_TOKEN = "test-dashboard-token"


def _wire_run_pipeline(monkeypatch, capture, commit=None, upload=None):
    """Stands in for `run`'s network- and Docker-dependent stages: the
    Sandbox, the Monitor, Browser_Capture_Module (`capture`), git capture,
    and the Uploader.

    `commit` optionally replaces what `commit_run_branch` does — return a
    branch name, or raise — and defaults to committing successfully to this
    Run's `vaultshield/run-<run_id>`. `upload` optionally makes the upload
    raise; by default it succeeds. A test that wants the token lookup itself
    to fail replaces `uploader.load_token` after calling this, since this
    stub returns a token successfully.

    Returns `(calls, seen)`: `calls` is the stage-invocation log, in order,
    and `seen` records what the Sandbox, the capture, git capture, and the
    upload were handed.
    """
    calls: list[str] = []
    seen: dict[str, object] = {}

    class _RecordingSandbox:
        def __init__(self, run_id, client):
            self.run_id = run_id
            seen["sandbox"] = self

        def create(self, repo_url, base_image, env_file, *, publish_port=None):
            calls.append("sandbox.create")
            seen["publish_port"] = publish_port

        def teardown(self):
            calls.append("sandbox.teardown")

    def _supervise_in_sandbox(
        sandbox, adapter, prompt, run_dir, *, off_limits_paths=None, **kwargs
    ):
        # Stands in for the in-Sandbox execution stage. `run` used to call
        # `Monitor(repo_root, ...).supervise(...)`, which ran the agent in the
        # CLI's own process against the host checkout; it now calls
        # `supervise_in_sandbox`, which runs it inside the container. This stub
        # follows that seam. Every assertion about pipeline ordering and about
        # the messages that name the Event_Log is unchanged — only the name of
        # the stage being stubbed moved.
        calls.append("monitor.supervise_in_sandbox")
        seen["supervise_args"] = (sandbox, adapter, prompt, run_dir, off_limits_paths)
        # The real implementation writes the Event_Log before returning, and the
        # failure messages under test name that path, so it has to exist.
        log_path = event_log_path(run_dir)
        log_path.write_text("")
        return log_path

    def _start_and_capture(sandbox, config, run_dir, ready_timeout_s=None):
        calls.append("browser_capture")
        seen["capture_args"] = (sandbox, config, run_dir, ready_timeout_s)
        return capture(run_dir)

    def _commit_run_branch(sandbox, run_id):
        calls.append("git.commit")
        seen["commit_args"] = (sandbox, run_id)
        if commit is not None:
            return commit(run_id)
        return run_branch_name(run_id)

    def _build_structured_diff(sandbox, base_ref, branch, run_dir):
        calls.append("git.diff")
        seen["diff_args"] = (sandbox, base_ref, branch, run_dir)
        # The real `build_structured_diff` writes the artifact and returns
        # its path, and the success message under test names that path.
        written = diff_path(run_dir)
        written.write_text('{"files": []}\n')
        return written

    def _load_token():
        # Not recorded in `calls`: that log is the pipeline's stages, and
        # reading a local file is not one. `seen["upload_args"]` proves the
        # token this returned is the one the upload was given.
        return _TEST_TOKEN

    def _upload_run(run_dir, run_id, backend_url, token):
        calls.append("upload")
        seen["upload_args"] = (run_dir, run_id, backend_url, token)
        if upload is not None:
            upload(run_id)

    monkeypatch.setattr(cli_module, "Sandbox", _RecordingSandbox)
    monkeypatch.setattr(cli_module, "supervise_in_sandbox", _supervise_in_sandbox)
    monkeypatch.setattr(cli_module, "start_and_capture", _start_and_capture)
    monkeypatch.setattr(cli_module, "commit_run_branch", _commit_run_branch)
    monkeypatch.setattr(cli_module, "build_structured_diff", _build_structured_diff)
    monkeypatch.setattr(cli_module.uploader, "load_token", _load_token)
    monkeypatch.setattr(cli_module.uploader, "upload_run", _upload_run)
    monkeypatch.setattr(cli_module.docker, "from_env", lambda: object())
    return calls, seen


def test_run_captures_screenshots_after_the_monitor_and_before_teardown(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(
            monkeypatch,
            lambda run_dir: [
                screenshots_dir(run_dir) / "root.png",
                screenshots_dir(run_dir) / "dashboard.png",
            ],
        )

        result = runner.invoke(cli, _RUN_ARGS)

        assert result.exit_code == 0, result.output
        # Requirement 9.1's "starts after agent execution": browser capture
        # sits between the Monitor and git capture, in that order.
        assert calls == [
            "sandbox.create",
            "monitor.supervise_in_sandbox",
            "browser_capture",
            "git.commit",
            "git.diff",
            "sandbox.teardown",
            "upload",
        ]

        sandbox, config, run_dir, ready_timeout = seen["capture_args"]
        assert sandbox is seen["sandbox"]
        # The loaded `.vaultshield.yml`, not a re-detected one (Requirement
        # 1.4), carrying the `start_command`/`port` Requirement 9.1 resolves.
        assert (config.start_command, config.port, config.ready_path) == (
            "npm run dev",
            3000,
            "/healthz",
        )
        assert run_dir.is_dir()
        assert ready_timeout == DEFAULT_READY_TIMEOUT_S
        # Published so `resolve_base_url` has a host-side origin to poll.
        assert seen["publish_port"] == 3000
        assert "Captured 2 screenshot" in result.output


def test_run_passes_an_explicit_ready_timeout_through_to_browser_capture(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        _, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, [*_RUN_ARGS, "--ready-timeout=7"])

        assert result.exit_code == 0, result.output
        assert seen["capture_args"][3] == 7


def test_run_reports_a_readiness_timeout_and_still_tears_the_sandbox_down(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)

        def _timeout(run_dir):
            raise ReadinessTimeout("http://127.0.0.1:54321/healthz", 60)

        calls, _ = _wire_run_pipeline(monkeypatch, _timeout)

        result = runner.invoke(cli, _RUN_ARGS)

        # Requirement 9.4: the Run fails, no screenshots exist, and the
        # container is still removed rather than leaked.
        assert result.exit_code != 0
        assert "readiness timed out" in result.output.lower()
        assert "No screenshots were captured" in result.output
        assert "events.jsonl" in result.output
        assert calls[-1] == "sandbox.teardown"
        assert not list(Path(".vaultshield").rglob("screenshots"))


def test_run_reports_a_screenshot_failure_distinctly_from_a_timeout(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)

        def _screenshot_error(run_dir):
            raise ScreenshotError(
                "Failed to capture a screenshot of http://127.0.0.1:1/dashboard: boom",
                saved=[screenshots_dir(run_dir) / "root.png"],
            )

        calls, _ = _wire_run_pipeline(monkeypatch, _screenshot_error)

        result = runner.invoke(cli, _RUN_ARGS)

        # The application came up; only the capture failed, so the message is
        # about the route and the partial evidence, not about a timeout.
        assert result.exit_code != 0
        assert "Failed to capture a screenshot" in result.output
        assert "1 screenshot(s) kept" in result.output
        assert "timed out" not in result.output.lower()
        assert calls[-1] == "sandbox.teardown"


def test_run_succeeds_with_no_configured_routes(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        # `init` writes `routes` as an empty placeholder, so a Run with none
        # configured is the common case, not an error: zero screenshots.
        Path(".vaultshield.yml").write_text(
            "start_command: npm run dev\nport: 3000\nready_path: /healthz\n"
        )
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, _RUN_ARGS)

        assert result.exit_code == 0, result.output
        assert seen["capture_args"][1].routes == []
        assert "no screenshots were captured" in result.output.lower()
        # Still torn down, and the Run still uploads: a screenshot-less Run
        # is a complete Run, so the upload is not conditional on there being
        # screenshots to send.
        assert calls[-2:] == ["sandbox.teardown", "upload"]


# Git branch + diff capture's place in the pipeline (Task 12.4,
# Requirements 10.1 and 10.2): that `commit_run_branch` and then
# `build_structured_diff` run immediately after Browser_Capture_Module and
# before teardown, that they are handed this Run's Sandbox, `run_id`, and
# Run_Directory, that the diff is computed against the capture commit's
# first parent, and that a failed capture fails the Run while still tearing
# the Sandbox down.
#
# What the two functions actually do to a repository — the branch name, the
# commit contents, `diff.json`'s shape and counts — is Task 12.1/12.2's own
# test file, which exercises them against a real git repository. These are
# the wiring checks only.


def test_run_captures_the_branch_and_diff_after_browser_capture(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, _RUN_ARGS)

        assert result.exit_code == 0, result.output
        # Requirement 10.1's "when agent execution completes inside a
        # Sandbox": commit, then diff, both after browser capture and both
        # while the container is still up.
        assert calls == [
            "sandbox.create",
            "monitor.supervise_in_sandbox",
            "browser_capture",
            "git.commit",
            "git.diff",
            "sandbox.teardown",
            "upload",
        ]

        commit_sandbox, run_id = seen["commit_args"]
        assert commit_sandbox is seen["sandbox"]

        diff_sandbox, base_ref, branch, run_dir = seen["diff_args"]
        assert diff_sandbox is seen["sandbox"]
        # `vaultshield/run-<run_id>` for this Run (Requirement 10.1), diffed
        # against the capture commit's first parent — the pre-agent state of
        # the Sandbox's clone.
        assert branch == run_branch_name(run_id)
        assert base_ref == f"{branch}~1"
        # Requirement 10.2: saved into this Run's Run_Directory, the same one
        # browser capture was handed.
        assert run_dir == seen["capture_args"][2]
        assert diff_path(run_dir).is_file()

        assert branch in result.output
        assert "diff.json" in result.output


def test_run_reports_a_git_capture_failure_and_still_tears_the_sandbox_down(
    monkeypatch,
):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)

        def _already_captured(run_id):
            raise GitCaptureError(
                f"Git capture failed while creating branch "
                f"{run_branch_name(run_id)} (exit code 128): fatal: a branch "
                f"named '{run_branch_name(run_id)}' already exists"
            )

        calls, _ = _wire_run_pipeline(
            monkeypatch, lambda run_dir: [], commit=_already_captured
        )

        result = runner.invoke(cli, _RUN_ARGS)

        # The Run fails on git's own message, no diff was written, and the
        # container is still removed rather than leaked — with the Event_Log
        # named, since it is already on disk by this point.
        assert result.exit_code != 0
        assert "already exists" in result.output
        assert "events.jsonl" in result.output
        assert calls == [
            "sandbox.create",
            "monitor.supervise_in_sandbox",
            "browser_capture",
            "git.commit",
            "sandbox.teardown",
        ]
        assert not list(Path(".vaultshield").rglob("diff.json"))


# --- Uploader wiring (Task 18.5) -------------------------------------------
#
# The Uploader's place in the pipeline (Requirements 14.2, 14.3, 14.4): that
# `upload_run` runs last — after git capture and after teardown, per
# design.md's flowchart — that it is handed this Run's Run_Directory,
# `run_id`, backend URL, and the stored token, that it runs for every agent
# in the dispatch table rather than for one of them, and that a failed
# upload is reported without a traceback while the Run's artifacts stay on
# disk.
#
# What `upload_run` actually posts — the batching, the endpoints, the
# multipart screenshots, the read-only guarantee — is `test_uploader.py`'s,
# against a real local HTTP server. These are the wiring checks only.


def test_run_uploads_the_run_artifacts_last(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, _RUN_ARGS)

        assert result.exit_code == 0, result.output
        # Requirement 14.2's "when a local Run completes": everything the
        # Run captures is captured first, and the Sandbox is already gone.
        assert calls[-2:] == ["sandbox.teardown", "upload"]

        run_dir, run_id, backend_url, token = seen["upload_args"]
        # The same Run_Directory every other stage wrote into, addressed by
        # the same `run_id` the backend's nested artifact routes expect.
        assert run_dir == seen["capture_args"][2]
        assert run_id == seen["commit_args"][1]
        # Requirement 24.2: the CLI reaches `web` over `localhost` from the
        # host by default, not over the compose-internal hostname.
        assert backend_url == "http://localhost:8000"
        # Requirement 14.2's "using the stored dashboard authentication
        # token": read from `~/.vaultshield/token`, not prompted for.
        assert token == _TEST_TOKEN
        assert backend_url in result.output


def test_run_uploads_with_an_overridden_backend_url(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        _, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(
            cli, [*_RUN_ARGS, "--backend-url=https://vaultshield.example.com/"]
        )

        assert result.exit_code == 0, result.output
        assert seen["upload_args"][2] == "https://vaultshield.example.com/"


def test_run_uploads_for_every_registered_agent(monkeypatch):
    # The upload is `run`'s own last stage, not something an adapter opts
    # into, so it happens for every entry in the dispatch table. Checked
    # against each registered agent rather than against `mock` alone, so the
    # adapters Tasks 19.5 and 20.4 add are covered the moment they are
    # registered.
    runner = CliRunner()
    for agent in _AGENT_ADAPTERS:
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, _ = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

            result = runner.invoke(
                cli,
                [
                    "run",
                    f"--agent={agent}",
                    "--prompt=do the thing",
                    "--repo-url=https://example.com/acme/widgets.git",
                ],
            )

            assert result.exit_code == 0, result.output
            assert calls[-1] == "upload", agent


def test_run_reports_a_missing_token_and_preserves_the_artifacts(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, _ = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        def _no_token():
            raise UploadError(
                f"No dashboard auth token at {token_path()}. Run "
                "`vaultshield login` first."
            )

        monkeypatch.setattr(cli_module.uploader, "load_token", _no_token)

        result = runner.invoke(cli, _RUN_ARGS)

        # Not logged in: the Run is reported as unuploaded, in one line, and
        # nothing was posted.
        assert result.exit_code != 0
        assert "vaultshield login" in result.output
        assert "Traceback" not in result.output
        assert "upload" not in calls
        # Requirement 14.4: the artifacts the Run did produce are all still
        # there, so the same Run can be uploaded once the token exists.
        run_dir = next(Path(".vaultshield/runs").iterdir())
        assert event_log_path(run_dir).is_file()
        assert diff_path(run_dir).is_file()


def test_run_reports_an_unreachable_backend_and_preserves_the_artifacts(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)

        def _unreachable(run_id):
            raise UploadError(
                "Upload failed at stage 'events': cannot reach the VaultShield "
                "backend at http://localhost:8000/api/runs/"
                f"{run_id}/events/: All connection attempts failed"
            )

        calls, _ = _wire_run_pipeline(
            monkeypatch, lambda run_dir: [], upload=_unreachable
        )

        result = runner.invoke(cli, _RUN_ARGS)

        # Requirement 14.4: the failure is reported (one line, no traceback)
        # and every artifact the Run wrote is still on local disk, byte for
        # byte — the upload never touches `run_dir`, so there is nothing to
        # clean up and nothing is cleaned up.
        assert result.exit_code != 0
        assert "All connection attempts failed" in result.output
        assert "Traceback" not in result.output
        assert calls[-1] == "upload"

        run_dir = next(Path(".vaultshield/runs").iterdir())
        assert str(run_dir) in result.output
        assert event_log_path(run_dir).is_file()
        assert diff_path(run_dir).read_text() == '{"files": []}\n'


# --- `login` command wiring (Task 18.1) ------------------------------------
#
# What `login` actually does — post the credential, read the token back,
# store it at `~/.vaultshield/token` with mode 600 — is tested against a
# real local HTTP server and a real file in `test_uploader.py`. The tests
# here cover only what this module owns: the options, the prompt, and the
# mapping of `UploadError` onto a one-line `ClickException`. `uploader.login`
# is substituted the same way `_wire_run_pipeline` substitutes the Run
# pipeline's Docker-dependent stages above.


def _record_login(monkeypatch, raises=None):
    """Records the arguments `login` forwards to `uploader.login`, or makes
    that call raise `raises`. Returns the recording list.
    """
    calls = []

    def _login(backend_url, *, username=None, password=None, home=None):
        calls.append({"backend_url": backend_url, "username": username})
        if raises is not None:
            raise raises

    monkeypatch.setattr(cli_module.uploader, "login", _login)
    return calls


def test_login_forwards_the_backend_url_and_username(monkeypatch):
    calls = _record_login(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["login", "--backend-url=https://vaultshield.example.com", "--username=dev"],
    )

    assert result.exit_code == 0
    assert calls == [
        {"backend_url": "https://vaultshield.example.com", "username": "dev"}
    ]
    # The developer is told where the token landed, by its canonical path.
    assert str(token_path()) in result.output


def test_login_leaves_the_username_to_be_prompted_for_when_not_given(monkeypatch):
    calls = _record_login(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(cli, ["login", "--backend-url=http://localhost:8000"])

    assert result.exit_code == 0
    assert calls == [{"backend_url": "http://localhost:8000", "username": None}]


def test_login_requires_a_backend_url():
    runner = CliRunner()

    result = runner.invoke(cli, ["login"])

    # Click's own usage error, before anything is asked or posted.
    assert result.exit_code == 2
    assert "--backend-url" in result.output


def test_login_takes_no_password_option():
    runner = CliRunner()

    result = runner.invoke(cli, ["login", "--help"])

    assert result.exit_code == 0
    # A password on the command line would land in shell history and in the
    # process list, so it is prompted for instead — see `uploader.py`.
    assert "--password" not in result.output


def test_login_reports_an_upload_error_as_a_one_line_message(monkeypatch):
    _record_login(
        monkeypatch,
        raises=UploadError(
            "Cannot reach the VaultShield backend at "
            "http://localhost:8000/api/auth/login/: All connection attempts failed"
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["login", "--backend-url=http://localhost:8000"])

    assert result.exit_code != 0
    assert "All connection attempts failed" in result.output
    assert "Traceback" not in result.output

# --- `submit` command wiring (Task 29.6) -----------------------------------
#
# What `submit` actually does — the status gate, the push, the pull/merge
# request, the `pr_url` PATCH, and the credential resolution behind all of it
# — is `test_submit.py`'s, against a real local backend, a real local git
# remote, and real files. The tests here cover only what `cli.py` owns: the
# `<run-id>` argument, the backend URL, reading the stored
# Dashboard_Auth_Token instead of prompting for one, echoing the resulting
# URL, and mapping each failure onto a one-line `ClickException`
# (Requirements 22.1 and 22.4). `submit.submit` is substituted the same way
# `_record_login` substitutes `uploader.login` above.

_PR_URL = "https://github.com/acme/widgets/pull/42"


def _record_submit(monkeypatch, raises=None, pr_url=_PR_URL):
    """Records the arguments `submit` forwards to `submit.submit`, or makes
    that call raise `raises`, and stands in for the stored token lookup.

    Returns the recording list; each entry also carries the keyword arguments
    it was called with, which must stay empty — `submit`'s keyword-only
    parameters are testability seams that `cli.py` deliberately never passes.
    """
    calls = []

    def _load_token():
        return _TEST_TOKEN

    def _submit(run_id, backend_url, token, **kwargs):
        calls.append(
            {
                "run_id": run_id,
                "backend_url": backend_url,
                "token": token,
                "kwargs": kwargs,
            }
        )
        if raises is not None:
            raise raises
        return pr_url

    monkeypatch.setattr(cli_module.uploader, "load_token", _load_token)
    monkeypatch.setattr(cli_module, "submit_run", _submit)
    return calls


def test_submit_is_a_registered_subcommand():
    assert "submit" in cli.commands


def test_submit_exposes_the_run_id_argument_and_the_backend_url_option():
    runner = CliRunner()

    result = runner.invoke(cli, ["submit", "--help"])

    assert result.exit_code == 0
    # Requirement 22.1 spells the command `vaultshield submit <run-id>`, so
    # the run id is a positional argument, not an option.
    assert "RUN_ID" in result.output
    assert "--backend-url" in result.output


def test_submit_requires_a_run_id():
    runner = CliRunner()

    result = runner.invoke(cli, ["submit"])

    # Click's own usage error, before the token is read or the backend is
    # called.
    assert result.exit_code == 2
    assert "RUN_ID" in result.output


def test_submit_forwards_the_run_id_and_prints_the_pull_request_url(monkeypatch):
    calls = _record_submit(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(cli, ["submit", "20240101120000-abcd1234"])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "run_id": "20240101120000-abcd1234",
            # Same default and same env var as `run`'s upload stage, so a
            # submission addresses the backend the Run was uploaded to.
            "backend_url": "http://localhost:8000",
            # Requirement 22.1's local credential half is `submit`'s; what
            # this command supplies is the Dashboard_Auth_Token, read from
            # `~/.vaultshield/token` rather than prompted for.
            "token": _TEST_TOKEN,
            # None of the keyword-only testability seams are passed.
            "kwargs": {},
        }
    ]
    # The URL of the opened request is the command's output, since it is what
    # the developer goes to next.
    assert _PR_URL in result.output


def test_submit_uses_an_overridden_backend_url(monkeypatch):
    calls = _record_submit(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "submit",
            "20240101120000-abcd1234",
            "--backend-url=https://vaultshield.example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["backend_url"] == "https://vaultshield.example.com"


# --- the shared `--backend-url` option (Task 32.2, Requirement 24.2) -------
#
# `run`'s upload stage, `submit`, and `teardown` all carry one option
# definition (`cli._backend_url_option`), so the default that reaches the
# compose stack's `web` service over `localhost` — asserted per command
# above — and the env var that overrides it are defined once. `submit` is
# the vehicle for the override checks below because it is the shortest of
# the three; what is being checked belongs to the shared definition, not to
# `submit`.


def test_backend_url_can_be_set_from_the_environment(monkeypatch):
    # The override for a backend that is not on `localhost:8000` — including
    # a compose stack brought up with a different `WEB_PUBLISHED_PORT`, which
    # the CLI does not read itself.
    calls = _record_submit(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["submit", "20240101120000-abcd1234"],
        env={"VAULTSHIELD_BACKEND_URL": "http://localhost:8080"},
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["backend_url"] == "http://localhost:8080"


def test_an_explicit_backend_url_wins_over_the_environment(monkeypatch):
    calls = _record_submit(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "submit",
            "20240101120000-abcd1234",
            "--backend-url=https://vaultshield.example.com",
        ],
        env={"VAULTSHIELD_BACKEND_URL": "http://localhost:8080"},
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["backend_url"] == "https://vaultshield.example.com"


def test_submit_reports_a_run_that_is_not_approved_with_its_status(monkeypatch):
    # Requirement 22.4: the rejection reaches the developer as one line
    # naming the Run's current Run_Status, with nothing pushed and no request
    # opened.
    _record_submit(
        monkeypatch,
        raises=SubmitError(
            "Run 20240101120000-abcd1234 is 'awaiting_review', not "
            "'approved', so it cannot be submitted. Nothing was pushed and no "
            "pull request was opened."
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["submit", "20240101120000-abcd1234"])

    assert result.exit_code != 0
    assert "awaiting_review" in result.output
    assert "Traceback" not in result.output
    assert _PR_URL not in result.output


def test_submit_reports_a_missing_credential_as_a_one_line_message(monkeypatch):
    _record_submit(
        monkeypatch,
        raises=CredentialError(
            "No usable github credential. The gh CLI is not installed or not "
            "authenticated, and /home/dev/.vaultshield/credentials.yml holds "
            "no token for 'github'. Run `gh auth login`, or add a `github:` "
            "entry with a `token:` value to it and `chmod 600` it."
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["submit", "20240101120000-abcd1234"])

    assert result.exit_code != 0
    assert "gh auth login" in result.output
    assert "Traceback" not in result.output


def test_submit_reports_a_missing_dashboard_token_without_calling_submit(
    monkeypatch,
):
    calls = _record_submit(monkeypatch)

    def _no_token():
        raise UploadError(
            f"No dashboard auth token at {token_path()}. Run "
            "`vaultshield login` first."
        )

    monkeypatch.setattr(cli_module.uploader, "load_token", _no_token)
    runner = CliRunner()

    result = runner.invoke(cli, ["submit", "20240101120000-abcd1234"])

    # Not logged in: reported in one line, and the submission never starts —
    # no backend call, no push, no request.
    assert result.exit_code != 0
    assert "vaultshield login" in result.output
    assert "Traceback" not in result.output
    assert calls == []


# --- `teardown` command wiring (Task 30.3) ---------------------------------
#
# What `teardown` actually does — removing the container by the name
# `Sandbox.create` gave it, reading the Run_Status, the `rejected` gate, the
# `ls-remote` pre-check, and the branch deletion — is `test_teardown.py`'s,
# against a fake Docker client, a real local backend, and a real local git
# remote. The tests here cover only what `cli.py` owns: the `<run-id>`
# argument, the backend URL, reading the stored Dashboard_Auth_Token instead
# of prompting for one, reporting the end state, and mapping each failure onto
# a one-line `ClickException` (Requirements 23.1 and 23.2).
# `teardown.teardown` is substituted the same way `_record_submit` substitutes
# `submit.submit` above.


def _record_teardown(monkeypatch, raises=None):
    """Records what `teardown` forwards to each of its two stages, or makes the
    remote stage raise `raises`, and stands in for the stored token lookup.

    `teardown` runs the LOCAL stage (`teardown_sandbox`) before the REMOTE one
    (`sync_rejected_branch`), so reclaiming a stranded container never depends
    on being logged in. It used to call one combined `teardown_run` behind a
    token read; these two seams follow that split.

    Returns the recording list. The remote entry also carries the keyword
    arguments it was called with, which must stay empty — `sync_rejected_branch`
    's keyword-only parameters are testability seams that `cli.py` deliberately
    never passes.
    """
    calls = []

    def _load_token():
        return _TEST_TOKEN

    def _teardown_sandbox(run_id, **kwargs):
        # The LOCAL stage. Recorded separately because it must run before the
        # token is even read.
        calls.append({"stage": "sandbox", "run_id": run_id, "kwargs": kwargs})
        return True

    def _sync_rejected_branch(run_id, backend_url, token, **kwargs):
        calls.append(
            {
                "run_id": run_id,
                "backend_url": backend_url,
                "token": token,
                # `sandbox_removed` is real data flowing from the local stage,
                # not a testability seam, so it is dropped from this assertion
                # while every other keyword-only parameter must still be absent.
                "kwargs": {k: v for k, v in kwargs.items() if k != "sandbox_removed"},
            }
        )
        if raises is not None:
            raise raises
        return None

    monkeypatch.setattr(cli_module.uploader, "load_token", _load_token)
    monkeypatch.setattr(cli_module, "teardown_sandbox", _teardown_sandbox)
    monkeypatch.setattr(cli_module, "sync_rejected_branch", _sync_rejected_branch)
    return calls


def test_teardown_is_a_registered_subcommand():
    assert "teardown" in cli.commands


def test_teardown_is_listed_in_the_group_help():
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "teardown" in result.output


def test_teardown_exposes_the_run_id_argument_and_the_backend_url_option():
    runner = CliRunner()

    result = runner.invoke(cli, ["teardown", "--help"])

    assert result.exit_code == 0
    # Requirements 23.1 and 23.2 both spell the command `vaultshield teardown
    # <run-id>`, so the run id is a positional argument, not an option.
    assert "RUN_ID" in result.output
    assert "--backend-url" in result.output


def test_teardown_requires_a_run_id():
    runner = CliRunner()

    result = runner.invoke(cli, ["teardown"])

    # Click's own usage error, before the token is read, Docker is contacted,
    # or the backend is called.
    assert result.exit_code == 2
    assert "RUN_ID" in result.output


def test_teardown_forwards_the_run_id_and_reports_the_end_state(monkeypatch):
    calls = _record_teardown(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(cli, ["teardown", "20240101120000-abcd1234"])

    assert result.exit_code == 0, result.output
    assert calls == [
        # LOCAL stage first, and it takes only the run id: no token, no backend
        # URL, nothing that could make reclaiming a container fail for a reason
        # unrelated to Docker.
        {"stage": "sandbox", "run_id": "20240101120000-abcd1234", "kwargs": {}},
        {
            "run_id": "20240101120000-abcd1234",
            # Same default and same env var as `run`'s upload stage and
            # `submit`, so all three address the same backend.
            "backend_url": "http://localhost:8000",
            # Requirement 23.2's local git credential is `teardown.py`'s; what
            # this command supplies is the Dashboard_Auth_Token, read from
            # `~/.vaultshield/token` rather than prompted for.
            "token": _TEST_TOKEN,
            # None of the keyword-only testability seams are passed.
            "kwargs": {},
        },
    ]
    assert "20240101120000-abcd1234" in result.output


def test_teardown_uses_an_overridden_backend_url(monkeypatch):
    calls = _record_teardown(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "teardown",
            "20240101120000-abcd1234",
            "--backend-url=https://vaultshield.example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    # `calls[0]` is now the local stage, which takes no backend URL by design;
    # the override belongs to the remote stage.
    remote = next(call for call in calls if call.get("stage") != "sandbox")
    assert remote["backend_url"] == "https://vaultshield.example.com"


def test_teardown_reports_a_teardown_failure_as_a_one_line_message(monkeypatch):
    # `TeardownError`'s own message names what the Sandbox step already did,
    # which is the part the developer needs; the command must not bury it in a
    # traceback.
    _record_teardown(
        monkeypatch,
        raises=TeardownError(
            "The VaultShield backend has no run '20240101120000-abcd1234' "
            "(http://localhost:8000/api/runs/20240101120000-abcd1234/ answered "
            "HTTP 404), so there is no review status to act on. Check the run "
            "id. The Sandbox for run 20240101120000-abcd1234 was removed."
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["teardown", "20240101120000-abcd1234"])

    assert result.exit_code != 0
    assert "HTTP 404" in result.output
    assert "The Sandbox for run 20240101120000-abcd1234 was removed." in result.output
    assert "Traceback" not in result.output


def test_teardown_reports_a_missing_credential_as_a_one_line_message(monkeypatch):
    # Requirement 23.2's credential is resolved inside `teardown.py`, so a
    # missing one surfaces here as `submit`'s `CredentialError` — the same
    # class, mapped the same way.
    _record_teardown(
        monkeypatch,
        raises=CredentialError(
            "No usable github credential. The gh CLI is not installed or not "
            "authenticated, and /home/dev/.vaultshield/credentials.yml holds "
            "no token for 'github'. Run `gh auth login`, or add a `github:` "
            "entry with a `token:` value to it and `chmod 600` it."
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["teardown", "20240101120000-abcd1234"])

    assert result.exit_code != 0
    assert "gh auth login" in result.output
    assert "Traceback" not in result.output


def test_teardown_removes_the_sandbox_without_a_dashboard_token(monkeypatch):
    """A missing token must not stop the LOCAL cleanup.

    DELIBERATE BEHAVIOUR CHANGE. This test previously asserted the opposite —
    that a missing token left everything untouched and failed the command — and
    in doing so it encoded the defect it now guards against: the container
    survived, so the command documented for reclaiming a stranded Sandbox could
    not reclaim one whenever the developer was not logged in or the backend was
    unreachable. That is precisely the situation it is most needed in.

    What still holds, and is asserted below: the message names `vaultshield
    login`, there is no traceback, and the branch step is skipped rather than
    attempted without credentials.
    """
    calls = _record_teardown(monkeypatch)

    def _no_token():
        raise UploadError(
            f"No dashboard auth token at {token_path()}. Run "
            "`vaultshield login` first."
        )

    monkeypatch.setattr(cli_module.uploader, "load_token", _no_token)
    runner = CliRunner()

    result = runner.invoke(cli, ["teardown", "20240101120000-abcd1234"])

    # The local half ran and succeeded, so the command succeeds.
    assert result.exit_code == 0, result.output
    assert [call["stage"] for call in calls if "stage" in call] == ["sandbox"]
    # The remote half was skipped, not attempted unauthenticated.
    assert not [call for call in calls if call.get("stage") != "sandbox"]
    # And the developer is told what was and was not done, and how to finish it.
    assert "vaultshield login" in result.output
    assert "Sandbox for run 20240101120000-abcd1234 removed" in result.output
    assert "Skipped the branch check" in result.output
    assert "Traceback" not in result.output
