"""`agents render/install/uninstall/doctor codex` and `hook codex`.

Temporary HOME, the in-memory Keychain, and a fake `~/.codex` inside tmp_path passed
with `--home`. No real Codex file is read or written; all values are fake.
"""

import json
import os
import shlex
import tomllib

import pytest
from click.testing import CliRunner

from vaultshield.cli import cli

KEY_VALUE = "sk-test-AAAA1111BBBB2222"
CONFIG = 'model = "o3"\n\n[tui]\ntheme = "dark"\n'


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


@pytest.fixture
def project(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    (protect_project / "certs").mkdir()
    (protect_project / "certs" / "server.pem").write_text("-----BEGIN FAKE-----\n")
    assert run("protect").exit_code == 0
    return protect_project


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "fakehome"
    (h / ".codex").mkdir(parents=True)
    (h / ".codex" / "config.toml").write_text(CONFIG)
    return h


def config(home):
    return home / ".codex" / "config.toml"


def hooks(home):
    return home / ".codex" / "hooks.json"


def abs_paths(project):
    base = project.resolve()
    return [str(base / ".env"), str(base / "certs" / "server.pem")]


def install(home):
    return run("agents", "install", "codex", "--home", str(home))


# --- install -----------------------------------------------------------------


def test_install_adds_the_profile_keeping_other_tables(project, home):
    before = config(home).read_text()
    result = install(home)
    assert result.exit_code == 0, result.output
    text = config(home).read_text()
    data = tomllib.loads(text)
    assert data["model"] == "o3" and data["tui"]["theme"] == "dark"
    assert data["permissions"]["vaultshield"]["extends"] == ":workspace"
    fs = data["permissions"]["vaultshield"]["filesystem"]
    assert set(fs) == set(abs_paths(project)) and all(v == "deny" for v in fs.values())
    assert before != text


def test_install_never_selects_the_profile_or_sets_sandbox_mode(project, home):
    install(home)
    text = config(home).read_text()
    assert "default_permissions" not in text and "sandbox_mode" not in text


def test_install_notes_the_one_line_to_turn_it_on(project, home):
    out = install(home).output
    assert 'default_permissions = "vaultshield"' in out


def test_install_writes_the_hook_file(project, home):
    install(home)
    [group] = json.loads(hooks(home).read_text())["hooks"]["PreToolUse"]
    assert group["matcher"] == "Bash"
    command = group["hooks"][0]["command"]
    assert shlex.split(command)[-2:] == ["hook", "codex"]
    assert os.path.isabs(shlex.split(command)[0])
    assert "onFailure" not in group["hooks"][0]  # Codex has no fail-closed flag


def test_install_is_idempotent(project, home):
    install(home)
    cfg_once, hook_once = config(home).read_text(), hooks(home).read_text()
    second = install(home)
    assert "unchanged" in second.output
    assert config(home).read_text() == cfg_once and hooks(home).read_text() == hook_once


def test_install_skips_when_codex_is_not_set_up(project, tmp_path):
    bare = tmp_path / "barehome"
    bare.mkdir()
    result = install(bare)
    assert result.exit_code != 0 and "not set up" in result.output


# --- uninstall ---------------------------------------------------------------


def test_uninstall_restores_config_byte_for_byte_and_removes_the_hook(project, home):
    before = config(home).read_text()
    install(home)
    result = run("agents", "uninstall", "codex", "--home", str(home))
    assert result.exit_code == 0, result.output
    assert config(home).read_text() == before
    assert not hooks(home).exists()
    assert not config(home).with_name("config.toml.vaultshield.bak").exists()


# --- render ------------------------------------------------------------------


def test_render_prints_the_profile_the_enable_line_and_no_values(project, home):
    before = config(home).read_text()
    out = run("agents", "render", "codex").output
    assert "[permissions.vaultshield]" in out
    assert 'default_permissions = "vaultshield"' in out
    assert KEY_VALUE not in out
    assert config(home).read_text() == before


# --- the hook ----------------------------------------------------------------


def chook(command, cwd, tool="Bash"):
    return run("hook", "codex", input=json.dumps({"tool_name": tool, "cwd": str(cwd), "tool_input": {"command": command}}))


@pytest.mark.parametrize("command", ["cat .env", "printenv", "security find-generic-password -s x", "cat certs/server.pem"])
def test_the_hook_denies_dangerous_commands_on_stderr(project, command):
    result = chook(command, project)
    assert result.exit_code == 2
    assert "VaultShield" in result.output
    # Codex blocks via exit 2 + stderr, no JSON decision on stdout
    assert result.stdout.strip() == ""


def test_the_hook_allows_ordinary_commands(project):
    result = chook("ls -la", project)
    assert result.exit_code == 0 and result.output.strip() == ""


def test_a_malformed_payload_is_denied(project):
    assert run("hook", "codex", input="nope").exit_code == 2


def test_a_denied_command_never_echoes_a_value(project):
    assert KEY_VALUE not in chook(f"echo {KEY_VALUE}", project).output


# --- doctor ------------------------------------------------------------------


def test_doctor_reports_defined_not_selected_after_install(project, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    install(home)
    result = run("agents", "doctor", "codex")
    assert result.exit_code == 0, result.output
    assert "Everything is active." in result.output
    assert "defined, not selected" in result.output
    assert 'default_permissions = "vaultshield"' in result.output


def test_doctor_warns_when_sandbox_mode_is_set(project, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    install(home)
    # sandbox_mode is a top-level key: prepend it, above any table
    config(home).write_text('sandbox_mode = "workspace-write"\n' + config(home).read_text())
    result = run("agents", "doctor", "codex")
    assert "sandbox_mode is set" in result.output


def test_doctor_reports_selected_when_the_user_turns_it_on(project, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    install(home)
    # default_permissions is a top-level key: prepend it, above any table
    config(home).write_text('default_permissions = "vaultshield"\n' + config(home).read_text())
    result = run("agents", "doctor", "codex")
    assert "selected" in result.output and "in force" in result.output
