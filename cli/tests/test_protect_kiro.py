"""`agents render/install/uninstall/doctor kiro` and `hook kiro`.

Temporary HOME, the in-memory Keychain, and a fake `~/.kiro` inside tmp_path passed
with `--home`. No real Kiro file is read or written; all values are fake.
"""

import json
import os
import shlex

import pytest
import yaml
from click.testing import CliRunner

from vaultshield.cli import cli

KEY_VALUE = "sk-test-AAAA1111BBBB2222"

EXISTING_PERMS = (
    "# my kiro permissions\n"
    "rules:\n"
    "  - capability: shell\n"
    '    match: ["git *"]\n'
    "    effect: allow\n"
    "settingsVersion: 2\n"
)


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


@pytest.fixture
def project(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    (protect_project / "backend").mkdir()
    (protect_project / "backend" / ".env.local").write_text("TOKEN=fake-token-9999\n")
    assert run("protect").exit_code == 0
    return protect_project


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "fakehome"
    (h / ".kiro" / "settings").mkdir(parents=True)
    (h / ".kiro" / "settings" / "permissions.yaml").write_text(EXISTING_PERMS)
    return h


def perms(home):
    return home / ".kiro" / "settings" / "permissions.yaml"


def hookfile(home):
    return home / ".kiro" / "hooks" / "vaultshield-protect.json"


def abs_paths(project):
    base = project.resolve()
    return [str(base / ".env"), str(base / "backend" / ".env.local")]


def install(home):
    return run("agents", "install", "kiro", "--home", str(home))


# --- install -----------------------------------------------------------------


def test_install_adds_fs_read_denies_keeping_the_user_rule_and_comment(project, home):
    before = perms(home).read_text()
    assert install(home).exit_code == 0

    text = perms(home).read_text()
    assert "# my kiro permissions" in text
    data = yaml.safe_load(text)
    assert data["settingsVersion"] == 2
    caps = [r["capability"] for r in data["rules"]]
    assert caps[0] == "shell"  # the user's rule stays first
    denied = [r["match"][0] for r in data["rules"] if r["capability"] == "fs_read" and r["effect"] == "deny"]
    assert denied == abs_paths(project)
    assert before != text


def test_install_writes_the_cli_hook_file(project, home):
    install(home)
    obj = json.loads(hookfile(home).read_text())
    assert obj["version"] == "v1"
    [hook] = obj["hooks"]
    assert hook["name"] == "vaultshield-protect"
    assert hook["trigger"] == "PreToolUse"
    assert hook["matcher"] == "^(execute_bash|execute_pwsh)$"
    command = hook["action"]["command"]
    assert shlex.split(command)[-2:] == ["hook", "kiro"]
    assert os.path.isabs(shlex.split(command)[0])


def test_install_is_idempotent(project, home):
    install(home)
    perms_once, hook_once = perms(home).read_text(), hookfile(home).read_text()
    second = install(home)
    assert "unchanged" in second.output
    assert perms(home).read_text() == perms_once and hookfile(home).read_text() == hook_once


def test_install_skips_when_kiro_is_not_set_up(project, tmp_path):
    bare = tmp_path / "barehome"
    bare.mkdir()
    result = install(bare)
    assert result.exit_code != 0 and "not set up" in result.output


# --- uninstall ---------------------------------------------------------------


def test_uninstall_restores_permissions_and_removes_the_hook_file(project, home):
    before = perms(home).read_text()
    install(home)
    assert hookfile(home).exists()
    result = run("agents", "uninstall", "kiro", "--home", str(home))
    assert result.exit_code == 0, result.output
    assert perms(home).read_text() == before
    assert not hookfile(home).exists()
    assert not perms(home).with_name("permissions.yaml.vaultshield.bak").exists()


def test_uninstall_keeps_a_copy_of_an_edited_hook_file(project, home):
    install(home)
    hookfile(home).write_text('{"version": "v1", "hooks": [], "note": "user edited"}\n')
    run("agents", "uninstall", "kiro", "--home", str(home))
    saved = list((home / ".kiro" / "hooks").glob("vaultshield-protect.json.vaultshield.before-uninstall*"))
    assert saved and "user edited" in saved[0].read_text()


# --- render ------------------------------------------------------------------


def test_render_installs_nothing_and_prints_no_values(project, home):
    before = perms(home).read_text()
    out = run("agents", "render", "kiro").output
    assert "fs_read" in out and "PreToolUse" in out
    assert KEY_VALUE not in out and "fake-token-9999" not in out
    assert perms(home).read_text() == before


# --- the hook ----------------------------------------------------------------


def khook(command, cwd, *, field="command", tool="execute_bash"):
    return run("hook", "kiro", input=json.dumps({"tool_name": tool, "cwd": str(cwd), "tool_input": {field: command}}))


@pytest.mark.parametrize("command", ["cat .env", "printenv", "env", "security find-generic-password -s x", "cat backend/.env.local"])
def test_the_hook_denies_dangerous_commands(project, command):
    result = khook(command, project)
    assert result.exit_code == 2
    assert "VaultShield" in result.output


def test_the_hook_finds_the_command_whatever_the_field_is_named(project):
    # Kiro does not document the execute_bash input field, so every string is scanned
    assert khook("cat .env", project, field="shellCommand").exit_code == 2
    assert khook("cat .env", project, field="input").exit_code == 2


def test_the_hook_allows_ordinary_commands_silently(project):
    result = khook("ls -la", project)
    assert result.exit_code == 0 and result.output.strip() == ""


def test_an_empty_shell_call_is_denied(project):
    result = run("hook", "kiro", input=json.dumps({"tool_name": "execute_bash", "cwd": str(project), "tool_input": {}}))
    assert result.exit_code == 2


def test_a_non_shell_tool_is_allowed(project):
    assert run("hook", "kiro", input=json.dumps({"tool_name": "fs_read", "tool_input": {"path": ".env"}})).exit_code == 0


def test_a_malformed_payload_is_denied(project):
    assert run("hook", "kiro", input="nope").exit_code == 2


def test_a_denied_command_never_echoes_a_value(project):
    assert KEY_VALUE not in khook(f"echo {KEY_VALUE}", project).output


# --- doctor ------------------------------------------------------------------


def test_doctor_reports_layers_including_the_ide_na_row(project, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    install(home)
    result = run("agents", "doctor", "kiro")
    assert result.exit_code == 0, result.output
    assert "Everything is active." in result.output
    assert "hook (CLI)" in result.output and "hook (IDE)" in result.output
    assert "N/A" in result.output and "not available" in result.output.replace("not installed", "not available")
