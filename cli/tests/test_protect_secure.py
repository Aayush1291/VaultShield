"""`vaultshield secure` — the one friendly command.

Temporary HOME and the in-memory Keychain (the `protect_project` fixture), Click's
input simulation for the prompts. No real HOME, Keychain, or agent config is touched.
"""

import json

import pytest
from click.testing import CliRunner

from vaultshield.cli import cli

KEY_VALUE = "sk-test-SECUREFLOW-1111-2222"
DB_VALUE = "postgres://u:p@localhost/db"


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


@pytest.fixture
def project(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\n")
    return protect_project


def env_text(project):
    return (project / ".env").read_text()


def manifest_exists(project):
    return (project / ".vaultshield" / "protect.json").is_file()


# --- discovery + confirm -----------------------------------------------------


def test_lists_names_and_counts_but_never_a_value(project):
    result = run("secure", input="n\n")  # decline, so nothing changes
    assert result.exit_code == 0, result.output
    assert "FAKE_KEY" in result.output and "DB_URL" in result.output  # names shown
    assert "2 variables" in result.output
    assert KEY_VALUE not in result.output and DB_VALUE not in result.output  # values never


def test_declining_changes_nothing(project):
    result = run("secure", input="n\n")
    assert result.exit_code == 0
    assert "Nothing was changed." in result.output
    assert env_text(project) == f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\n"
    assert not manifest_exists(project)


def test_confirming_protects_and_ends_with_the_undo_hint(project):
    result = run("secure", input="y\n")
    assert result.exit_code == 0, result.output
    assert "vs_protected" in env_text(project)
    assert KEY_VALUE not in env_text(project)
    assert manifest_exists(project)
    assert "To undo: vaultshield unprotect" in result.output
    assert "Agent backstops added: none." in result.output


def test_no_secret_files_says_nothing_to_protect(protect_project):
    result = run("secure")
    assert result.exit_code == 0
    assert "No secret files found" in result.output and "Nothing to protect" in result.output


# --- --yes protects only, never installs a backstop --------------------------


def test_yes_protects_without_prompting_or_installing_any_backstop(project):
    # Every agent looks installed, but --yes must not prompt or install.
    for dirname in (".gemini", ".claude", ".kiro", ".codex"):
        (project.parent / "home" / dirname).mkdir(parents=True, exist_ok=True)

    result = run("secure", "--yes")
    assert result.exit_code == 0, result.output
    assert "vs_protected" in env_text(project)
    assert "Add backstops for" not in result.output  # no per-agent prompt at all
    assert "Agent backstops added: none." in result.output
    # nothing was written into any agent config
    assert not (project.parent / "home" / ".claude" / "settings.json").exists()


# --- per-agent prompts default to no -----------------------------------------


def test_per_agent_prompt_appears_and_defaults_to_no(project):
    home = project.parent / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)

    # y to protect, then just Enter at the claude-code prompt (default = no)
    result = run("secure", input="y\n\n")
    assert result.exit_code == 0, result.output
    assert "Add backstops for claude-code?" in result.output
    assert "Agent backstops added: none." in result.output
    assert not (home / ".claude" / "settings.json").exists()  # nothing installed


def test_saying_yes_to_an_agent_installs_its_backstop_and_runs_doctor(project):
    home = project.parent / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)

    result = run("secure", input="y\ny\n")  # protect, then yes to claude-code
    assert result.exit_code == 0, result.output
    settings = home / ".claude" / "settings.json"
    assert settings.exists()
    data = json.loads(settings.read_text())
    assert data["permissions"]["deny"]  # deny rules were written
    assert data["hooks"]["PreToolUse"]  # and the hook
    assert "claude-code" in result.output
    assert "Claude Code protection for" in result.output  # doctor ran for it
    assert "Agent backstops added: claude-code." in result.output
