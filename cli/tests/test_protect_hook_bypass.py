"""Step 4: the hook denies a command that names a protected file whatever the
call says about the sandbox.

Measured in the App: after `read_file` was blocked, the agent ran `cat` through
the terminal with BypassSandbox:true. These tests use that payload shape. Fake
values, temporary HOME, in-memory Keychain.
"""

import json
import os
import shlex
import subprocess
import sys

import pytest
from click.testing import CliRunner

import protect_fakes as fakes
from vaultshield.cli import cli
from vaultshield.protect.agents import install as agent_install

KEY_VALUE = "sk-test-AAAA1111BBBB2222"


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def call(command, project, *, cwd=None, **args):
    """The PreToolUse payload for a run_command call, with the extra arguments the
    App sends (BypassSandbox and friends)."""
    arguments = {"CommandLine": command, "Cwd": str(cwd or project), "WaitMsBeforeAsync": 5000, **args}
    return {
        "toolCall": {"name": "run_command", "args": arguments},
        "stepIdx": 12,
        "conversationId": "ec33ebf9-0cba-4100-8142-c61503f6c587",
        "workspacePaths": [str(project)],
        "modelName": "gemini-3.6-flash-medium",
    }


def decide(payload):
    result = run("hook", "antigravity", input=json.dumps(payload))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.fixture
def project(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    (protect_project / "backend").mkdir()
    (protect_project / "backend" / ".env.local").write_text("TOKEN=fake-token-9999\n")
    (protect_project / "certs").mkdir()
    (protect_project / "certs" / "server.pem").write_text("-----BEGIN FAKE-----\n")
    assert run("protect").exit_code == 0
    return protect_project


def test_cat_of_a_protected_file_with_bypass_sandbox_true_is_denied(project):
    answer = decide(call("cat .env", project, BypassSandbox=True))
    assert answer["decision"] == "deny"
    assert answer["reason"].startswith("VaultShield: ")


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "cat ./.env",
        "cat -- .env",
        "/bin/cat .env",
        "cat backend/.env.local",
        "head -c 200 certs/server.pem",
        "cat '.env' | base64",
        "cp .env /tmp/leak && cat /tmp/leak",
        'python3 -c "print(open(\'.env\').read())"',
        "git show HEAD:.env",
        "tar cf - .env",
    ],
)
def test_protected_files_named_by_relative_path_are_denied_under_bypass(project, command):
    assert decide(call(command, project, BypassSandbox=True))["decision"] == "deny", command


def test_absolute_paths_are_denied_under_bypass(project):
    base = project.resolve()
    for path in (f"{base}/.env", f"{base}/backend/.env.local", f"{base}/certs/server.pem"):
        assert decide(call(f"cat {path}", project, BypassSandbox=True))["decision"] == "deny", path


def test_the_working_directory_can_be_anywhere(project, tmp_path):
    inside = project / "backend"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert decide(call("cat .env.local", project, cwd=inside, BypassSandbox=True))["decision"] == "deny"
    assert decide(call(f"cat {project.resolve()}/.env", project, cwd=outside, BypassSandbox=True))["decision"] == "deny"


@pytest.mark.parametrize(
    "flags",
    [
        {"BypassSandbox": True},
        {"BypassSandbox": False},
        {"BypassSandbox": "true"},
        {"BypassSandbox": 1},
        {"BypassSandbox": None},
        {},
        {"BypassSandbox": True, "SafeToAutoRun": True},
        {"BypassSandbox": True, "SafeToAutoRun": False, "RunPersistent": True},
        {"SomethingNew": {"nested": [1, 2]}, "BypassSandbox": True},
    ],
)
def test_the_answer_does_not_depend_on_the_flags_of_the_call(project, flags):
    assert decide(call("cat .env", project, **flags))["decision"] == "deny"
    assert decide(call("ls -la", project, **flags)) == {"decision": "allow"}


def test_a_flag_at_the_top_level_of_the_payload_changes_nothing_either(project):
    payload = call("cat .env", project)
    payload["BypassSandbox"] = True
    payload["toolCall"]["BypassSandbox"] = True
    assert decide(payload)["decision"] == "deny"


@pytest.mark.parametrize(
    "command",
    ["env", "printenv FAKE_KEY", "export -p", "security find-generic-password -s vaultshield -w", "vaultshield exec -- ls", "vaultshield unprotect"],
)
def test_the_other_denies_still_hold_under_bypass(project, command):
    assert decide(call(command, project, BypassSandbox=True))["decision"] == "deny", command


def test_asking_to_bypass_the_sandbox_is_not_itself_a_reason_to_deny(project):
    # The hook guards the protected files. Whether to allow an unsandboxed command
    # is Antigravity's own permission decision.
    assert decide(call("npm test", project, BypassSandbox=True)) == {"decision": "allow"}


def test_a_denied_bypass_attempt_is_logged_without_the_command(project):
    decide(call("cat .env --a-flag-the-log-must-not-repeat", project, BypassSandbox=True))
    decide(call("cat backend/.env.local", project, BypassSandbox=True))

    log = (project / ".vaultshield" / "violations.jsonl").read_text()
    records = [json.loads(text) for text in log.splitlines()]
    assert [r["payload"] for r in records] == [
        "antigravity run_command denied: protected_path .env",
        "antigravity run_command denied: protected_path backend/.env.local",
    ]
    assert "a-flag-the-log-must-not-repeat" not in log
    assert "BypassSandbox" not in log
    assert KEY_VALUE not in log


def test_with_no_protected_project_nothing_is_blocked_even_under_bypass(protect_project):
    assert decide(call("cat .env", protect_project, BypassSandbox=True)) == {"decision": "allow"}


# --- the installed hook command really runs ------------------------------------


@pytest.fixture
def installed(project, tmp_path):
    home = fakes.make_home(tmp_path)
    result = run("agents", "install", "antigravity", "--home", str(home["home"]))
    assert result.exit_code == 0, result.output
    entry = json.loads(home["hooks"].read_text())["vaultshield-protect"]
    return project, entry["PreToolUse"][0]["hooks"][0]["command"]


def run_installed(command, payload, tmp_path):
    """Run the hook command exactly as written in hooks.json, the way a plain
    process would: through a shell, with almost no PATH and a temporary HOME."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    return subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)


def test_the_installed_hook_command_runs_without_the_shell_path_and_denies_the_bypass_cat(installed, tmp_path):
    project, command = installed
    assert shlex.split(command)[-2:] == ["hook", "antigravity"]

    denied = run_installed(command, call("cat .env", project, BypassSandbox=True), tmp_path)
    allowed = run_installed(command, call("ls -la", project, BypassSandbox=True), tmp_path)

    assert denied.returncode == 0, denied.stderr
    assert json.loads(denied.stdout)["decision"] == "deny"
    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(allowed.stdout) == {"decision": "allow"}
    assert KEY_VALUE not in denied.stdout + denied.stderr


def test_the_installed_hook_command_denies_a_payload_it_cannot_read(installed, tmp_path):
    _project, command = installed
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}

    result = subprocess.run(command, shell=True, input="not json", capture_output=True, text=True, env=env, timeout=60)

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "deny"


def test_the_hook_command_uses_the_interpreter_that_ran_the_install(installed):
    _project, command = installed
    assert shlex.split(command) == [sys.executable, "-m", "vaultshield.cli", "hook", "antigravity"]
    assert os.path.isabs(sys.executable)
    assert agent_install.hook_command() == command
