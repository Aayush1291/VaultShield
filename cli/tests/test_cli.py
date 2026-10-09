"""Smoke tests for the VaultShield CLI entry point scaffold.

Proves the pytest harness works and that the top-level Click group exists
with a registered `init` command. `init` now performs real file writes
(detect_framework + write_config, Task 1.2), so its tests run inside an
isolated filesystem to avoid writing `.vaultshield.yml` into the real repo.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from vaultshield import cli as cli_module
from vaultshield import git_capture
from vaultshield.adapters.antigravity import Antigravity_Adapter
from vaultshield.adapters.base import AdapterError, AuthError
from vaultshield.adapters.codex import Codex_Adapter
from vaultshield.adapters.kiro import Kiro_Adapter
from vaultshield.browser_capture import (
    DEFAULT_READY_TIMEOUT_S,
    ReadinessTimeout,
    ScreenshotError,
)
from vaultshield.cli import _AGENT_ADAPTERS, cli
from vaultshield.git_capture import GitCaptureError, run_branch_name
from vaultshield.monitor import OffLimitsViolation
from vaultshield.run_directory import diff_path, event_log_path, screenshots_dir
from vaultshield.sandbox import ExecResult, SandboxError
from vaultshield.sandbox_monitor import SandboxMonitorError
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

# A backend Project id shaped like the real thing (a 24-hex ObjectId). `run`
# now registers its Run under a project before any Sandbox exists, so every
# `run` invocation that is meant to get past step 1 names one; the backend is
# stubbed, so the value is never checked against a real Project.
_PROJECT_ID = "0123456789abcdef01234567"

_RUN_ARGS = [
    "run",
    "--agent=mock",
    "--prompt=do the thing",
    "--repo-url=https://example.com/acme/widgets.git",
    f"--project={_PROJECT_ID}",
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


def _wire_run_pipeline(
    monkeypatch,
    capture,
    commit=None,
    upload=None,
    create=None,
    complete=None,
    mark_failed=None,
):
    """Stands in for `run`'s network- and Docker-dependent stages: the
    Sandbox, the Monitor, Browser_Capture_Module (`capture`), git capture,
    the Uploader, and the backend Run registration, completion and failed
    mark.

    `commit` optionally replaces what `commit_run_branch` does — return a
    branch name, or raise — and defaults to committing successfully to this
    Run's `vaultshield/run-<run_id>`. `upload` optionally makes the upload
    raise; by default it succeeds. A test that wants the token lookup itself
    to fail replaces `uploader.load_token` after calling this, since this
    stub returns a token successfully.

    The three backend Run-control calls (`uploader.create_run`,
    `complete_run`, `mark_run_failed`) are recorded into `seen["backend"]`,
    a list of `("create" | "complete" | "failed", details)`, and NOT into
    `calls`, so every exact `calls == [...]` assertion about the pipeline's
    stages is unaffected by them. By default each succeeds. `create`,
    `complete`, and `mark_failed` optionally take over one of them: when
    given, the stub also appends `"backend.create"` / `"backend.complete"` /
    `"backend.failed"` to `calls` (so a test can assert ordering against
    `"sandbox.teardown"` and `"upload"`), then calls it with the `run_id`. A
    `create`/`complete` that raises is the failure under test; whatever
    `mark_failed` returns is `mark_run_failed`'s return value (`None` or a
    warning), and it may also raise.

    `docker.from_env` is recorded in `seen["docker_from_env_calls"]` (a
    count), again outside `calls`.

    Returns `(calls, seen)`: `calls` is the stage-invocation log, in order,
    and `seen` records what the Sandbox, the capture, git capture, the
    upload, and the backend were handed.
    """
    calls: list[str] = []
    seen: dict[str, object] = {"backend": []}

    class _RecordingSandbox:
        def __init__(self, run_id, client):
            self.run_id = run_id
            seen["sandbox"] = self

        def create(
            self, repo_url, base_image, env_file, *, publish_port=None, project_dir="",
            **egress_kwargs,
        ):
            calls.append("sandbox.create")
            seen["publish_port"] = publish_port
            # Batch 3 D11: the egress guard's extra kwargs; `{}` with egress off.
            seen["create_egress_kwargs"] = egress_kwargs
            # `project_dir` moves where dependencies are installed for a repo
            # whose application is nested. Recorded, not just tolerated, so the
            # test below can assert `run` forwards the validated config value.
            seen["project_dir"] = project_dir

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
        # The agent's working directory inside the container, which `project_dir`
        # moves. Recorded so a test can assert `run` resolves and forwards it.
        seen["agent_workdir"] = kwargs.get("agent_workdir")
        # The real implementation writes the Event_Log before returning, and the
        # failure messages under test name that path, so it has to exist.
        log_path = event_log_path(run_dir)
        # Appends, like the real writer (`_append_event_log`), so an event
        # recorded before the agent (Batch 3 snapshot #1) survives.
        with log_path.open("a"):
            pass
        return log_path

    def _start_and_capture(sandbox, config, run_dir, ready_timeout_s=None, *, published_by=None):
        calls.append("browser_capture")
        seen["capture_args"] = (sandbox, config, run_dir, ready_timeout_s)
        seen["published_by"] = published_by
        return capture(run_dir)

    def _commit_run_branch(sandbox, run_id):
        calls.append("git.commit")
        seen["commit_args"] = (sandbox, run_id)
        if commit is not None:
            return commit(run_id)
        return run_branch_name(run_id)

    def _resolve_diff_base(sandbox, branch):
        # Not recorded in `calls`: choosing the base ref is part of the
        # diff stage, not a stage of its own. `seen["base_args"]` proves it
        # was asked about this Run's Sandbox and branch.
        seen["base_args"] = (sandbox, branch)
        return f"{branch}~1"

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

    def _upload_run(run_dir, run_id, backend_url, token, *, scrub=None):
        calls.append("upload")
        seen["upload_args"] = (run_dir, run_id, backend_url, token)
        # Batch 3 FR5: `run` always hands the upload its SecretSet's scrub.
        seen["upload_scrub"] = scrub
        if upload is not None:
            upload(run_id)

    def _create_run(backend_url, token, *, project, run_id, agent):
        seen["backend"].append(
            (
                "create",
                {
                    "backend_url": backend_url,
                    "token": token,
                    "project": project,
                    "run_id": run_id,
                    "agent": agent,
                },
            )
        )
        if create is not None:
            calls.append("backend.create")
            create(run_id)

    def _complete_run(backend_url, token, run_id):
        seen["backend"].append(("complete", (backend_url, token, run_id)))
        if complete is not None:
            calls.append("backend.complete")
            complete(run_id)

    def _mark_run_failed(backend_url, token, run_id):
        seen["backend"].append(("failed", (backend_url, token, run_id)))
        if mark_failed is not None:
            calls.append("backend.failed")
            return mark_failed(run_id)
        return None

    def _from_env():
        seen["docker_from_env_calls"] = seen.get("docker_from_env_calls", 0) + 1
        return object()

    # Batch 3 trusted capture (§5.4.1). Recorded into `seen["trusted"]`, NOT
    # into `calls`, so every exact `calls == [...]` assertion about the
    # pipeline's stages is unaffected; `test_run_trusted_capture_order` below
    # asserts their order against `calls` through `seen["timeline"]`.
    seen["trusted"] = []
    seen["timeline"] = calls

    class _TrustedBase:
        commit_oid = None

        def close(self):
            seen["trusted"].append("base.close")

    class _Split:
        notices: list = []

        def close(self):
            seen["trusted"].append("split.close")

    class _RecordingCapture:
        def __init__(self, run_id, client, *, image_id):
            seen["capture_helper"] = self
            seen["trusted"].append("capture.init")

        def start(self):
            seen["trusted"].append("capture.start")

        def load(self, split, base):
            seen["trusted"].append("capture.load")

        def remove(self):
            seen["trusted"].append("capture.remove")

    def _record_trusted_base(sandbox, *, caps):
        seen["trusted"].append(("record_trusted_base", len(calls)))
        return _TrustedBase()

    def _kill_sandbox(sandbox):
        seen["trusted"].append(("kill_sandbox", len(calls)))

    def _archive(sandbox, *, caps):
        seen["trusted"].append("archive")
        return _Split()

    def _check_run_outputs(capture_obj, secrets, run_dir, branch, base_ref):
        seen["trusted"].append("check_run_outputs")
        seen["check_args"] = (capture_obj, secrets, run_dir, branch, base_ref)

    def _remove_capture_helper(run_id, client):
        seen["trusted"].append("remove_capture_helper")
        return False

    def _upload_events_best_effort(backend_url, token, run_id, run_dir, *, scrub):
        seen.setdefault("events_upload", []).append(run_id)
        return None

    def _snapshot(sandbox, roots, *, credential_files=None):
        from vaultshield.token_hygiene import SecretSet, Snapshot

        seen["trusted"].append(("snapshot", len(calls), credential_files))
        return Snapshot(
            secrets=SecretSet(),
            credential_files=frozenset() if credential_files is None else credential_files,
            skipped=seen.get("snapshot_skipped", 0),
        )

    monkeypatch.setattr(cli_module.token_hygiene, "snapshot", _snapshot)
    monkeypatch.setattr(cli_module, "record_trusted_base", _record_trusted_base)
    monkeypatch.setattr(cli_module, "kill_sandbox", _kill_sandbox)
    monkeypatch.setattr(cli_module, "sandbox_image_id", lambda sandbox: "sha256:fake")
    monkeypatch.setattr(cli_module, "TrustedCapture", _RecordingCapture)
    monkeypatch.setattr(cli_module, "archive_sandbox_repo", _archive)
    monkeypatch.setattr(cli_module, "check_run_outputs", _check_run_outputs)
    monkeypatch.setattr(
        cli_module.trusted_capture, "remove_capture_helper", _remove_capture_helper
    )
    monkeypatch.setattr(
        cli_module.uploader, "upload_events_best_effort", _upload_events_best_effort
    )
    monkeypatch.setattr(cli_module, "Sandbox", _RecordingSandbox)
    monkeypatch.setattr(cli_module, "supervise_in_sandbox", _supervise_in_sandbox)
    monkeypatch.setattr(cli_module, "start_and_capture", _start_and_capture)
    monkeypatch.setattr(cli_module, "commit_run_branch", _commit_run_branch)
    monkeypatch.setattr(cli_module, "build_structured_diff", _build_structured_diff)
    monkeypatch.setattr(cli_module, "resolve_diff_base", _resolve_diff_base)
    monkeypatch.setattr(cli_module.uploader, "load_token", _load_token)
    monkeypatch.setattr(cli_module.uploader, "upload_run", _upload_run)
    monkeypatch.setattr(cli_module.uploader, "create_run", _create_run)
    monkeypatch.setattr(cli_module.uploader, "complete_run", _complete_run)
    monkeypatch.setattr(cli_module.uploader, "mark_run_failed", _mark_run_failed)
    monkeypatch.setattr(cli_module.docker, "from_env", _from_env)
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
# Run_Directory, that the diff is computed against the base
# `resolve_diff_base` returns (the capture commit's first parent, or the
# empty tree for a root commit), and that a failed capture fails the Run
# while still tearing the Sandbox down.
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

        # Batch 3 FR5 (trusted capture): git capture runs on the capture
        # helper, never on the Sandbox.
        commit_sandbox, run_id = seen["commit_args"]
        assert commit_sandbox is seen["capture_helper"]
        assert commit_sandbox is not seen["sandbox"]

        diff_sandbox, base_ref, branch, run_dir = seen["diff_args"]
        assert diff_sandbox is seen["capture_helper"]
        # `vaultshield/run-<run_id>` for this Run (Requirement 10.1), diffed
        # against the base `resolve_diff_base` chose for it: the capture
        # commit's first parent, or the empty tree for a root commit.
        assert branch == run_branch_name(run_id)
        assert seen["base_args"] == (seen["capture_helper"], branch)
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
                    f"--project={_PROJECT_ID}",
                ],
            )

            assert result.exit_code == 0, result.output
            assert calls[-1] == "upload", agent


def test_run_aborts_before_any_sandbox_without_a_token(monkeypatch):
    """REWRITTEN for Batch 2 (approved premise change). This used to be
    `test_run_reports_a_missing_token_and_preserves_the_artifacts`, which ran
    the whole Run and found the missing token only at upload. The token is
    now needed to register the Run in step 2b, before any Sandbox exists, so
    no Event_Log or diff is ever written to preserve; what this asserts
    instead is that nothing past the Run_Directory was started at all.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        def _no_token():
            raise UploadError(
                f"No dashboard auth token at {token_path()}. Run "
                "`vaultshield login` first."
            )

        monkeypatch.setattr(cli_module.uploader, "load_token", _no_token)

        result = runner.invoke(cli, _RUN_ARGS)

        # Not logged in: one line naming the fix, and nothing was started.
        assert result.exit_code != 0
        assert "vaultshield login" in result.output
        assert "(no Sandbox was created)" in result.output
        assert "Traceback" not in result.output
        assert "sandbox.create" not in calls
        assert "sandbox" not in seen  # `Sandbox` was never constructed
        assert "docker_from_env_calls" not in seen
        assert [b for b in seen["backend"] if b[0] == "create"] == []
        assert "upload" not in calls


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


# --- Run registration, completion and the failed mark (Batch 2) ------------
#
# `run` registers its Run with the backend in step 2b, before any Docker call,
# and aborts there if it cannot. From then on every failure marks the Run
# `failed` exactly once, after the Sandbox teardown, without changing the
# error the developer sees; the success path instead marks it
# `awaiting_review` after the last artifact. What the three uploader calls
# send over the wire is `test_uploader.py`'s; these are the wiring checks.

_CONFIG_PROJECT_ID = "fedcba9876543210fedcba98"


def _failed_marks(seen) -> list:
    """The `mark_run_failed` calls `_wire_run_pipeline` recorded."""
    return [entry for entry in seen["backend"] if entry[0] == "failed"]


def _error_line(output: str) -> str:
    """The single `Error: ...` line Click prints for a ClickException."""
    lines = [line for line in output.splitlines() if line.startswith("Error: ")]
    assert len(lines) == 1, output
    return lines[0]


def _run_args_without_project() -> list[str]:
    return [arg for arg in _RUN_ARGS if not arg.startswith("--project=")]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("backend_down", "All connection attempts failed"),
        ("token_rejected", "vaultshield login --backend-url http://localhost:8000"),
        ("missing_token", "vaultshield login"),
    ],
)
def test_run_aborts_before_any_sandbox_when_the_run_cannot_be_registered(
    monkeypatch, case, expected
):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)

        def _refuse(run_id):
            if case == "backend_down":
                raise UploadError(
                    "Cannot reach the VaultShield backend at "
                    "http://localhost:8000/api/runs/: All connection attempts failed"
                )
            raise UploadError(
                "The backend at http://localhost:8000/api/runs/ rejected the "
                "dashboard auth token (HTTP 401). Run `vaultshield login "
                "--backend-url http://localhost:8000` again."
            )

        calls, seen = _wire_run_pipeline(
            monkeypatch, lambda run_dir: [], create=_refuse
        )
        if case == "missing_token":

            def _no_token():
                raise UploadError(
                    f"No dashboard auth token at {token_path()}. Run "
                    "`vaultshield login` first."
                )

            monkeypatch.setattr(cli_module.uploader, "load_token", _no_token)

        result = runner.invoke(cli, _RUN_ARGS)

        # Acceptance 8: non-zero, one line naming the cause, and nothing was
        # started — no Docker client, no Sandbox, no failed mark (there is no
        # registered Run to mark).
        assert result.exit_code != 0
        line = _error_line(result.output)
        assert line.startswith("Error: Cannot start run ")
        assert expected in line
        assert line.endswith("(no Sandbox was created)")
        assert "Traceback" not in result.output
        assert "sandbox" not in seen
        assert "docker_from_env_calls" not in seen
        assert "sandbox.create" not in calls
        assert _failed_marks(seen) == []


def test_run_without_a_project_stops_before_anything_exists(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, _run_args_without_project())

        assert result.exit_code != 0
        line = _error_line(result.output)
        assert "No project for this Run" in line
        assert "--project <project-id>" in line
        assert "GET /api/projects/" in line
        assert "Traceback" not in result.output
        # Resolved in step 1, so not even the Run_Directory exists.
        assert not Path(".vaultshield").exists()
        assert seen["backend"] == []
        assert "sandbox" not in seen
        assert "docker_from_env_calls" not in seen
        assert calls == []


def test_run_option_project_beats_the_config_project(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(
            _CONFIG_YAML + f"project: {_CONFIG_PROJECT_ID}\n"
        )
        _, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, _RUN_ARGS)

        assert result.exit_code == 0, result.output
        [(kind, details)] = [b for b in seen["backend"] if b[0] == "create"]
        assert details["project"] == _PROJECT_ID


def test_run_uses_the_config_project_without_the_option(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(
            _CONFIG_YAML + f"project: {_CONFIG_PROJECT_ID}\n"
        )
        _, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, _run_args_without_project())

        assert result.exit_code == 0, result.output
        [(kind, details)] = [b for b in seen["backend"] if b[0] == "create"]
        assert details["project"] == _CONFIG_PROJECT_ID


def test_run_rejects_a_config_project_of_the_wrong_type(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML + "project:\n  - a\n  - b\n")
        _, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        result = runner.invoke(cli, _RUN_ARGS)

        # A `ConfigError`, so step 1 stops the Run with nothing on disk, even
        # though `--project` was given.
        assert result.exit_code != 0
        assert "`project`" in result.output
        assert "Traceback" not in result.output
        assert not Path(".vaultshield").exists()
        assert seen["backend"] == []


def test_run_registers_then_completes_the_run_after_the_last_upload(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(
            monkeypatch, lambda run_dir: [], complete=lambda run_id: None
        )

        result = runner.invoke(cli, _RUN_ARGS)

        assert result.exit_code == 0, result.output
        # Acceptance 11: exactly one completion PATCH, after the upload, and
        # no failed mark.
        assert calls[-3:] == ["sandbox.teardown", "upload", "backend.complete"]
        assert [b[0] for b in seen["backend"]] == ["create", "complete"]
        _, created = seen["backend"][0]
        run_id = seen["commit_args"][1]
        assert created == {
            "backend_url": "http://localhost:8000",
            "token": _TEST_TOKEN,
            "project": _PROJECT_ID,
            "run_id": run_id,
            "agent": "mock",
        }
        assert seen["backend"][1] == (
            "complete",
            ("http://localhost:8000", _TEST_TOKEN, run_id),
        )
        assert f"Run {run_id} registered with http://localhost:8000." in result.output


def _arrange_stage_failure(monkeypatch, stage: str):
    """Wires the pipeline so that `stage` fails, with the failed mark recorded
    into `calls` as `"backend.failed"`. Returns `(calls, seen, extra_args)`.
    """
    capture = lambda run_dir: []  # noqa: E731 - the harness's default shape
    wire = {"mark_failed": lambda run_id: None}
    extra: list[str] = []

    if stage == "readiness_timeout":

        def capture(run_dir):
            raise ReadinessTimeout("http://127.0.0.1:54321/healthz", 60)

    elif stage == "screenshot_error":

        def capture(run_dir):
            raise ScreenshotError(
                "Failed to capture a screenshot of http://127.0.0.1:1/: boom",
                saved=[],
            )

    elif stage == "keyboard_interrupt":

        def capture(run_dir):
            raise KeyboardInterrupt

    elif stage == "git_capture":

        def _refused(run_id):
            raise GitCaptureError("Git capture failed: fatal: refused by test")

        wire["commit"] = _refused
    elif stage == "upload":

        def _unreachable(run_id):
            raise UploadError("Upload failed at stage 'events': backend gone")

        wire["upload"] = _unreachable
    elif stage == "completion":

        def _conflict(run_id):
            raise UploadError(
                "The backend refused to mark the run ready for review: it is "
                "no longer running (backend reports `failed`)"
            )

        wire["complete"] = _conflict

    calls, seen = _wire_run_pipeline(monkeypatch, capture, **wire)

    if stage == "credential_file":
        extra.append("--env-file=missing.env")
    elif stage == "docker_from_env":

        def _no_daemon():
            raise cli_module.docker.errors.DockerException("daemon not running")

        monkeypatch.setattr(cli_module.docker, "from_env", _no_daemon)
    elif stage == "sandbox_create":
        recording = cli_module.Sandbox

        class _FailingCreate(recording):
            def create(self, *args, **kwargs):
                super().create(*args, **kwargs)
                raise SandboxError("Sandbox creation failed: clone refused by test")

        monkeypatch.setattr(cli_module, "Sandbox", _FailingCreate)
    elif stage == "interactive_auth":

        def _auth(sandbox, adapter, agent):
            raise AuthError("The mock sign-in failed inside the Sandbox by test")

        monkeypatch.setattr(cli_module, "_authenticate_in_sandbox", _auth)
        extra.append("--interactive-auth")
    elif stage.startswith("supervise_"):
        error = {
            "supervise_adapter_error": AdapterError("mock agent exited 3 by test"),
            "supervise_off_limits": OffLimitsViolation("protected.txt", "protected.txt"),
            "supervise_monitor_error": SandboxMonitorError("no python on PATH by test"),
            "supervise_os_error": OSError("watch could not be established by test"),
        }[stage]

        def _supervise(*args, **kwargs):
            calls.append("monitor.supervise_in_sandbox")
            raise error

        monkeypatch.setattr(cli_module, "supervise_in_sandbox", _supervise)

    return calls, seen, extra


# (stage, text the original error surfaces with, whether a Sandbox existed)
_STAGE_FAILURES = [
    ("credential_file", "--env-file not found", False),
    ("docker_from_env", "Cannot reach the Docker daemon", False),
    ("sandbox_create", "clone refused by test", True),
    ("interactive_auth", "sign-in failed inside the Sandbox by test", True),
    ("supervise_adapter_error", "mock agent exited 3 by test", True),
    ("supervise_off_limits", "off_limits_paths violation: protected.txt", True),
    ("supervise_monitor_error", "no python on PATH by test", True),
    ("supervise_os_error", "watch could not be established by test", True),
    ("readiness_timeout", "No screenshots were captured", True),
    ("screenshot_error", "Failed to capture a screenshot", True),
    ("git_capture", "fatal: refused by test", True),
    ("upload", "could not be uploaded: Upload failed at stage 'events'", True),
    ("completion", "could not be marked ready for review", True),
    ("keyboard_interrupt", "Aborted!", True),
]


@pytest.mark.parametrize(
    ("stage", "expected", "sandbox_existed"),
    _STAGE_FAILURES,
    ids=[case[0] for case in _STAGE_FAILURES],
)
def test_a_failure_at_any_stage_marks_the_run_failed_exactly_once(
    monkeypatch, stage, expected, sandbox_existed
):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen, extra = _arrange_stage_failure(monkeypatch, stage)

        result = runner.invoke(cli, [*_RUN_ARGS, *extra])

        # The original error is what surfaces, unchanged.
        if stage == "keyboard_interrupt":
            assert result.exit_code == 1
        else:
            assert result.exit_code != 0
            assert expected in _error_line(result.output)
        assert expected in result.output
        assert "Traceback" not in result.output
        assert "Warning:" not in result.output

        # Acceptance 9: exactly one failed mark, for the registered Run, and
        # it is the last thing `run` did.
        [created] = [b for b in seen["backend"] if b[0] == "create"]
        run_id = created[1]["run_id"]
        assert _failed_marks(seen) == [
            ("failed", ("http://localhost:8000", _TEST_TOKEN, run_id))
        ]
        assert calls.count("backend.failed") == 1
        assert calls[-1] == "backend.failed"
        if sandbox_existed:
            # Teardown first, then the failed PATCH.
            assert calls.index("sandbox.teardown") < calls.index("backend.failed")
        else:
            assert "sandbox" not in seen
            assert "sandbox.teardown" not in calls
        # The artifacts directory is left as it was: nothing is cleaned up.
        assert (Path(".vaultshield/runs") / run_id).is_dir()


def _invoke_with_a_git_capture_failure(monkeypatch, mark_failed):
    """Runs `run` to a git-capture failure with `mark_failed` standing in for
    `mark_run_failed`'s outcome. Returns `(result, run_id)`."""

    def _refused(run_id):
        raise GitCaptureError("Git capture failed: fatal: refused by test")

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        _, seen = _wire_run_pipeline(
            monkeypatch, lambda run_dir: [], commit=_refused, mark_failed=mark_failed
        )
        result = runner.invoke(cli, _RUN_ARGS)
        assert len(_failed_marks(seen)) == 1
        run_id = seen["backend"][0][1]["run_id"]
        # The error names the Run_Directory, whose path differs per isolated
        # filesystem and per run_id; normalised so two runs can be compared.
        error = _error_line(result.output)
        error = error.replace(str(Path.cwd()), "<cwd>").replace(run_id, "<id>")
        return result, run_id, error


def _raise_runtime_error(run_id):
    raise RuntimeError("detail-that-must-not-be-printed")


@pytest.mark.parametrize(
    ("mark_failed", "warning"),
    [
        (lambda run_id: "backend answered HTTP 500: boom", "backend answered HTTP 500: boom"),
        (_raise_runtime_error, "unexpected RuntimeError"),
    ],
    ids=["returns_a_warning", "raises"],
)
def test_a_failed_mark_that_fails_does_not_change_the_original_error(
    monkeypatch, mark_failed, warning
):
    baseline, _, baseline_error = _invoke_with_a_git_capture_failure(
        monkeypatch, lambda run_id: None
    )
    result, run_id, error = _invoke_with_a_git_capture_failure(monkeypatch, mark_failed)

    # Acceptance 10: same exit code, same error text, at most one warning line,
    # and no traceback — the mark's own failure never replaces the original.
    assert result.exit_code == baseline.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert error == baseline_error
    assert "fatal: refused by test" in error
    assert "Warning:" not in baseline.output
    warnings = [line for line in result.output.splitlines() if "Warning:" in line]
    assert warnings == [
        f"Warning: could not mark run {run_id} as failed on the backend: {warning}"
    ]
    assert "detail-that-must-not-be-printed" not in result.output
    assert "Traceback" not in result.output


class _LocalGitSandbox:
    """A minimal `Sandbox` stand-in whose `exec` runs against a real local
    repository instead of inside a container — the same shape as
    `LocalGitSandbox` in `test_git_capture.py`: the repository is the working
    directory, a non-zero exit is returned rather than raised, and output is
    decoded the way `Sandbox.exec` decodes it."""

    def __init__(self, repo: Path, env: dict[str, str]) -> None:
        self.repo = repo
        self.env = env

    def exec(self, cmd: list[str], *, env: dict[str, str] | None = None) -> ExecResult:
        # `env` mirrors `Sandbox.exec(cmd, env=...)` (Batch 3 FR5).
        completed = subprocess.run(
            cmd, cwd=self.repo, env={**self.env, **(env or {})}, capture_output=True
        )
        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
            stdout_bytes=completed.stdout,
        )


def test_run_reports_diff_metadata_over_the_upload_cap(monkeypatch, tmp_path):
    """Carried item 6(b): the user-facing text when the diff's metadata alone
    is over `MAX_DIFF_REQUEST_BYTES`.

    The REAL `build_structured_diff` runs, against a real single-commit
    repository through a local-git stand-in for the Sandbox, with the cap
    lowered so ten one-line files are over it. Only the pipeline's other
    stages are stubbed.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    _git("init", "-q")
    for index in range(10):
        (repo / f"file{index}.txt").write_text(f"line {index}\n")
    _git("add", "-A")
    _git(
        "-c", "user.name=Dev Eloper", "-c", "user.email=dev@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "ten files",
    )
    local = _LocalGitSandbox(repo, env)
    # A single-commit repository: `HEAD~1` does not resolve, so this is the
    # root-commit fallback, i.e. git's empty tree.
    empty_tree = git_capture.resolve_diff_base(local, "HEAD")

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(
            monkeypatch, lambda run_dir: [], mark_failed=lambda run_id: None
        )
        monkeypatch.setattr(git_capture, "MAX_DIFF_REQUEST_BYTES", 256)
        monkeypatch.setattr(
            cli_module,
            "build_structured_diff",
            lambda sandbox, base_ref, branch, run_dir: git_capture.build_structured_diff(
                local, empty_tree, "HEAD", run_dir
            ),
        )

        result = runner.invoke(cli, _RUN_ARGS)

        assert result.exit_code != 0
        line = _error_line(result.output)
        assert "Run " in line
        assert "failed:" in line
        assert "MAX_DIFF_REQUEST_BYTES" in line
        assert "10 changed files" in line
        assert "events.jsonl" in line
        assert "Traceback" not in result.output
        assert len(_failed_marks(seen)) == 1
        assert calls.index("sandbox.teardown") < calls.index("backend.failed")
        # Raised before `diff.json` is written: no partial diff is left behind.
        assert not list(Path(".vaultshield").rglob("diff.json"))


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


# --- project_dir reaches the stages that need it -----------------------------
#
# `config.project_dir` is validated in `load_config` and then has to arrive at
# two different stages in two different forms: the raw relative value at
# `Sandbox.create` (which resolves it itself for dependency install), and the
# resolved container path at `supervise_in_sandbox` (the agent's working
# directory). These pin the forwarding, which is the part a refactor would
# silently drop — nothing else in the suite would notice a `project_dir` that
# validated correctly and then went nowhere.

_NESTED_PROJECT_DIR = "citizen-samadhan-setu-main"

_NESTED_CONFIG_YAML = f"""\
start_command: npm run dev
port: 3000
ready_path: /
agent: antigravity
project_dir: {_NESTED_PROJECT_DIR}
routes: []
off_limits_paths: []
"""


def test_run_forwards_the_project_dir_to_sandbox_create(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_NESTED_CONFIG_YAML)
        _calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 0, result.output
        # The raw relative value: `create` resolves it for dependency install.
        assert seen["project_dir"] == _NESTED_PROJECT_DIR


def test_run_forwards_the_resolved_agent_workdir_to_the_monitor(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_NESTED_CONFIG_YAML)
        _calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 0, result.output
        # The resolved container path: this becomes the agent process's cwd.
        assert seen["agent_workdir"] == f"/workspace/repo/{_NESTED_PROJECT_DIR}"


def test_run_defaults_both_to_the_repository_root_without_a_project_dir(monkeypatch):
    """Backwards compatibility for every config written before the field
    existed."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        _calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 0, result.output
        assert seen["project_dir"] == ""
        assert seen["agent_workdir"] == "/workspace/repo"


def test_run_rejects_a_traversing_project_dir_before_any_side_effect(monkeypatch):
    """The security case at the CLI boundary: a `project_dir` that escapes the
    clone stops the Run in step 1, so no Run_Directory, no credential file, and
    no container are ever created."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(
            _NESTED_CONFIG_YAML.replace(
                f"project_dir: {_NESTED_PROJECT_DIR}", "project_dir: ../../etc"
            )
        )
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code != 0
        assert "project_dir" in result.output
        # Nothing was created: the same guarantee the other config-rejection
        # tests above assert.
        assert not Path(".vaultshield").exists()


# --- Batch 3: egress, trusted capture, token hygiene in `run` ----------------


class _RecordingGuard:
    """Stands in for `EgressGuard`; records into the pipeline's `calls`."""

    def __init__(self, calls, *, active, fail_at=None):
        self.calls, self.active, self.fail_at = calls, active, fail_at
        self.port_publisher = object() if active else None

    def start(self):
        self.calls.append("guard.start")
        if self.fail_at == "start":
            raise cli_module.EgressError("egress: could not start the egress proxy: boom")

    def sandbox_create_kwargs(self):
        return {"network": "vaultshield-open-x", "cap_drop": ["NET_RAW"]} if self.active else {}

    def isolate(self, sandbox):
        self.calls.append("guard.isolate")
        if self.fail_at == "isolate":
            raise cli_module.EgressError("egress: could not isolate sandbox network: x")

    def teardown(self, run_dir):
        self.calls.append("guard.teardown")
        if self.fail_at == "teardown":  # a real guard never raises; prove it is contained anyway
            raise AssertionError("teardown must never be reached with a raise")


def _install_guard(monkeypatch, calls, *, active=False, fail_at=None):
    holder = {}

    def _factory(run_id, client, policy, *, app_port):
        holder["policy"] = policy
        holder["guard"] = _RecordingGuard(calls, active=active, fail_at=fail_at)
        return holder["guard"]

    monkeypatch.setattr(cli_module, "EgressGuard", _factory)
    return holder


def test_run_egress_choices():
    option = next(p for p in cli.commands["run"].params if p.name == "egress_mode")
    assert list(option.type.choices) == ["off", "log-only", "allowlist"]
    assert option.default is None


def test_off_mode_creates_the_sandbox_exactly_as_before(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 0, result.output
        assert seen["publish_port"] == 3000
        assert seen["create_egress_kwargs"] == {}
        assert seen["docker_from_env_calls"] == 1
        # The scrub reaches the upload on the success path too.
        assert callable(seen["upload_scrub"])


def test_allowlist_call_order(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
        holder = _install_guard(monkeypatch, calls, active=True)
        result = runner.invoke(cli, [*_RUN_ARGS, "--egress=allowlist"])
        assert result.exit_code == 0, result.output
        assert holder["policy"].mode == "allowlist"
        assert calls[:4] == ["guard.start", "sandbox.create", "guard.isolate", "monitor.supervise_in_sandbox"]
        assert seen["publish_port"] is None
        assert seen["create_egress_kwargs"]["cap_drop"] == ["NET_RAW"]
        # Step 7 reaches the app through the proxy's published port (D4).
        assert seen["published_by"] is holder["guard"].port_publisher is not None
        assert calls.index("guard.teardown") == calls.index("sandbox.teardown") + 1


def test_trusted_capture_order(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
        _install_guard(monkeypatch, calls, active=True)
        result = runner.invoke(cli, [*_RUN_ARGS, "--egress=allowlist"])
        assert result.exit_code == 0, result.output
        t = seen["trusted"]
        # `record_trusted_base` after create + isolate, before the agent (index = len(calls) then).
        rec = next(e for e in t if isinstance(e, tuple) and e[0] == "record_trusted_base")
        assert calls[: rec[1]] == ["guard.start", "sandbox.create", "guard.isolate"]
        snaps = [e for e in t if isinstance(e, tuple) and e[0] == "snapshot"]
        assert len(snaps) == 3
        assert snaps[0][2] is None and snaps[1][2] is not None and snaps[2][2] is not None
        assert calls[snaps[0][1] - 1] == "guard.isolate"            # #1 before the agent
        assert calls[snaps[1][1] - 1] == "monitor.supervise_in_sandbox"  # #2a after it
        kill = next(e for e in t if isinstance(e, tuple) and e[0] == "kill_sandbox")
        assert calls[kill[1] - 1] == "browser_capture"
        names = [e if isinstance(e, str) else e[0] for e in t]
        assert names.index("kill_sandbox") < names.index("snapshot", names.index("kill_sandbox"))
        assert names.index("capture.start") < names.index("archive") < names.index("capture.load")
        assert names.index("check_run_outputs") < names.index("capture.remove")
        assert names.index("capture.remove") < names.index("remove_capture_helper")
        assert names.index("split.close") < names.index("base.close")
        assert seen["check_args"][0] is seen["capture_helper"]


@pytest.mark.parametrize("fail_at", ["start", "isolate"])
def test_an_egress_failure_marks_the_run_failed_once_after_teardown(monkeypatch, fail_at):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [], mark_failed=lambda r: None)
        _install_guard(monkeypatch, calls, active=True, fail_at=fail_at)
        result = runner.invoke(cli, [*_RUN_ARGS, "--egress=allowlist"])
        assert result.exit_code == 1
        assert "egress:" in result.output
        assert calls.count("backend.failed") == 1
        assert calls.index("backend.failed") > calls.index("guard.teardown") > calls.index("sandbox.teardown")


def test_a_token_violation_uploads_only_the_scrubbed_timeline(monkeypatch):
    from vaultshield.token_hygiene import Finding, TokenHygieneViolation

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [], mark_failed=lambda r: None)

        def _violate(*a):
            raise TokenHygieneViolation([Finding("credential_bytes", "diff.json", "a.txt")], ["/root/.gemini"])

        monkeypatch.setattr(cli_module, "check_run_outputs", _violate)
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 1
        assert "credential material from /root/.gemini found in diff.json" in result.output
        assert "Event_Log preserved at" in result.output
        assert "upload" not in calls
        assert len(seen["events_upload"]) == 1
        assert calls.count("backend.failed") == 1


def test_the_events_upload_runs_before_the_failed_mark_and_cannot_mask_it(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [], mark_failed=lambda r: None)

        def _boom_events(*a, **k):
            calls.append("events_upload")
            raise RuntimeError("must be swallowed")

        def _git_fail(sandbox, run_id):
            raise GitCaptureError("Git capture failed while creating branch x")

        monkeypatch.setattr(cli_module.uploader, "upload_events_best_effort", _boom_events)
        monkeypatch.setattr(cli_module, "commit_run_branch", _git_fail)
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 1
        assert "Git capture failed while creating branch x" in result.output
        assert calls.index("events_upload") < calls.index("backend.failed")
        assert result.output.count("Warning: could not upload the timeline") == 1


def test_a_failing_capture_helper_backstop_adds_one_warning_only(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])

        def _refuse(run_id, client):
            raise cli_module.docker.errors.APIError("refused")

        monkeypatch.setattr(cli_module.trusted_capture, "remove_capture_helper", _refuse)
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 0, result.output
        assert result.output.count("Warning: could not remove the capture helper") == 1


def test_a_pre_agent_capture_failure_does_not_claim_an_event_log(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML)
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [], mark_failed=lambda r: None)

        def _bad_base(sandbox, *, caps):
            raise GitCaptureError("trusted capture: cannot read the pre-agent base commit: x")

        monkeypatch.setattr(cli_module, "record_trusted_base", _bad_base)
        result = runner.invoke(cli, _RUN_ARGS)
        assert result.exit_code == 1
        assert "(the agent never started)" in result.output
        assert "Event_Log preserved" not in result.output
        assert calls.count("backend.failed") == 1


def test_a_skipped_snapshot_event_is_appended_only_when_needed(monkeypatch):
    import json as _json

    for skipped, expected in ((0, 0), (2, 3)):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".vaultshield.yml").write_text(_CONFIG_YAML)
            calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
            seen["snapshot_skipped"] = skipped
            result = runner.invoke(cli, _RUN_ARGS)
            assert result.exit_code == 0, result.output
            run_dir = seen["upload_args"][0]
            lines = [l for l in event_log_path(run_dir).read_text().splitlines() if l.strip()]
            payloads = [_json.loads(l)["payload"] for l in lines]
            hits = [p for p in payloads if p.startswith("token hygiene: 2 credential-root files")]
            # One per snapshot (#1, #2a, #2) when files were skipped, none otherwise.
            assert len(hits) == expected


def test_an_app_port_that_collides_with_the_proxy_stops_at_step_1(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".vaultshield.yml").write_text(_CONFIG_YAML.replace("port: 3000", "port: 443"))
        calls, seen = _wire_run_pipeline(monkeypatch, lambda run_dir: [])
        result = runner.invoke(cli, [*_RUN_ARGS, "--egress=allowlist"])
        assert result.exit_code == 1
        assert "app port 443 collides with the egress proxy" in result.output
        assert not Path(".vaultshield/runs").exists()
        assert "docker_from_env_calls" not in seen
        assert seen["backend"] == []
