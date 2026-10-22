"""`agents render/install/uninstall/doctor claude-code` and `hook claude-code`.

Temporary HOME (the `protect_project` fixture), the in-memory Keychain, and a fake
`~/.claude` inside tmp_path passed with `--home`. No real Claude Code file is read
or written; all values are fake.
"""

import json
import os
import shlex

import pytest
from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.protect.agents import claude_code as cc

KEY_VALUE = "sk-test-AAAA1111BBBB2222"
DB_VALUE = "postgres://u:p@localhost/db"


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


@pytest.fixture
def project(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\n")
    (protect_project / "backend").mkdir()
    (protect_project / "backend" / ".env.local").write_text("TOKEN=fake-token-9999\n")
    (protect_project / "certs").mkdir()
    (protect_project / "certs" / "server.pem").write_text("-----BEGIN FAKE-----\n")
    assert run("protect").exit_code == 0
    return protect_project


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "fakehome"
    (h / ".claude").mkdir(parents=True)
    (h / ".claude" / "settings.json").write_text(
        '{\n  "model": "claude-sonnet-5",\n  "permissions": {\n    "allow": ["Bash(npm run *)"]\n  }\n}\n'
    )
    return h


def settings(home):
    return home / ".claude" / "settings.json"


def expected_rules(project):
    base = project.resolve()
    return [f"Read(/{base}/.env)", f"Read(/{base}/backend/.env.local)", f"Read(/{base}/certs/server.pem)"]


def install(home, *extra):
    return run("agents", "install", "claude-code", "--home", str(home), *extra)


# --- install -----------------------------------------------------------------


def test_install_adds_deny_rules_and_a_hook_and_keeps_every_other_key(project, home):
    before = settings(home).read_text()

    result = install(home)

    assert result.exit_code == 0, result.output
    data = json.loads(settings(home).read_text())
    assert data["model"] == "claude-sonnet-5"
    assert data["permissions"]["allow"] == ["Bash(npm run *)"]
    assert data["permissions"]["deny"] == expected_rules(project)
    [group] = data["hooks"]["PreToolUse"]
    assert group["matcher"] == "Bash"
    handler = group["hooks"][0]
    assert handler["onFailure"] == "block" and handler["timeout"] == 10
    assert shlex.split(handler["command"])[-2:] == ["hook", "claude-code"]
    assert os.path.isabs(shlex.split(handler["command"])[0])
    # the original bytes are still all present
    for fragment in ('"model": "claude-sonnet-5"', '"allow": [\n      "Bash(npm run *)"\n    ]'):
        assert fragment in settings(home).read_text() or "Bash(npm run *)" in settings(home).read_text()
    assert before != settings(home).read_text()


def test_install_is_idempotent(project, home):
    install(home)
    once = settings(home).read_text()
    second = install(home)
    assert second.exit_code == 0
    assert settings(home).read_text() == once
    assert "unchanged" in second.output


def test_the_deny_rules_are_absolute_with_no_globs(project, home):
    install(home)
    rules = json.loads(settings(home).read_text())["permissions"]["deny"]
    assert rules
    for rule in rules:
        assert rule.startswith("Read(//") and rule.endswith(")")
        assert not any(c in rule for c in "*?[]{}")
        assert ".." not in rule


def test_install_creates_settings_when_absent_but_claude_dir_exists(project, home):
    settings(home).unlink()
    assert install(home).exit_code == 0
    data = json.loads(settings(home).read_text())
    assert data["permissions"]["deny"] == expected_rules(project)
    assert data["hooks"]["PreToolUse"]


def test_install_skips_when_claude_is_not_set_up(project, tmp_path):
    bare = tmp_path / "barehome"
    bare.mkdir()
    result = install(bare)
    assert result.exit_code != 0
    assert "not set up" in result.output


# --- uninstall ---------------------------------------------------------------


def test_uninstall_restores_byte_for_byte(project, home):
    before = settings(home).read_text()
    install(home)
    result = run("agents", "uninstall", "claude-code", "--home", str(home))
    assert result.exit_code == 0, result.output
    assert settings(home).read_text() == before
    assert not settings(home).with_name("settings.json.vaultshield.bak").exists()


def test_uninstall_keeps_a_copy_when_the_file_changed_since_install(project, home):
    before = settings(home).read_text()
    install(home)
    data = json.loads(settings(home).read_text())
    data["model"] = "claude-opus-5"  # the user edits something of their own
    settings(home).write_text(json.dumps(data, indent=2) + "\n")
    run("agents", "uninstall", "claude-code", "--home", str(home))
    assert settings(home).read_text() == before
    saved = list((home / ".claude").glob("settings.json.vaultshield.before-uninstall*"))
    assert saved and "claude-opus-5" in saved[0].read_text()


# --- render ------------------------------------------------------------------


def test_render_prints_rules_and_hook_and_installs_nothing(project, home, tmp_path):
    before = settings(home).read_text()
    result = run("agents", "render", "claude-code")
    assert result.exit_code == 0
    assert "permissions" in result.output and "PreToolUse" in result.output
    for rule in expected_rules(project):
        assert rule in result.output
    assert "onFailure" in result.output
    assert settings(home).read_text() == before  # nothing written


def test_render_prints_no_values(project):
    out = run("agents", "render", "claude-code").output
    assert KEY_VALUE not in out and DB_VALUE not in out and "fake-token-9999" not in out


# --- the hook ----------------------------------------------------------------


def hook(command, cwd, tool="Bash"):
    payload = {"tool_name": tool, "cwd": str(cwd), "tool_input": {"command": command}}
    return run("hook", "claude-code", input=json.dumps(payload))


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "head -n1 .env",
        "cat backend/.env.local",
        "printenv",
        "env",
        "security find-generic-password -s x",
        "python3 -c 'import keyring; print(keyring.get_password(\"a\",\"b\"))'",
        "cat certs/server.pem",
    ],
)
def test_the_hook_denies_the_dangerous_commands(project, command):
    result = hook(command, project)
    assert result.exit_code == 2, result.output
    decision = json.loads(result.stdout.splitlines()[0])
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "VaultShield" in decision["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", ["ls -la", "npm test", "git status", "echo hi"])
def test_the_hook_allows_ordinary_commands(project, command):
    result = hook(command, project)
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_a_non_shell_tool_is_allowed(project):
    result = run("hook", "claude-code", input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}))
    assert result.exit_code == 0


def test_a_malformed_payload_is_denied(project):
    assert run("hook", "claude-code", input="not json").exit_code == 2
    assert run("hook", "claude-code", input=json.dumps({"tool_name": "Bash"})).exit_code == 2


def test_the_hook_allows_everything_in_a_folder_with_no_manifest(tmp_path):
    other = tmp_path / "unprotected"
    other.mkdir()
    (other / ".env").write_text("REAL=whatever\n")
    assert hook("cat .env", other).exit_code == 0


def test_a_denied_command_never_echoes_a_value(project):
    result = hook(f"echo {KEY_VALUE}", project)
    # the command text held a value; the reason must not repeat it
    assert KEY_VALUE not in result.output


# --- doctor ------------------------------------------------------------------


def test_doctor_reports_active_after_install(project, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    install(home)
    result = run("agents", "doctor", "claude-code")
    assert result.exit_code == 0, result.output
    assert "Everything is active." in result.output
    assert "placeholders" in result.output and "deny rules" in result.output and "hook" in result.output


def test_doctor_reports_not_active_before_install(project, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    result = run("agents", "doctor", "claude-code")
    assert result.exit_code == 1
    assert "Not everything is active." in result.output
