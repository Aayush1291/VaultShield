"""Step 6: `agents render antigravity` and `hook antigravity`.

Run against a temporary HOME and the in-memory Keychain (the `protect_project`
fixture). All values are fake. Nothing here installs into a real Antigravity
configuration.
"""

import json
import os

import pytest
from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.protect.agents import antigravity

KEY_VALUE = "sk-test-AAAA1111BBBB2222"


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def payload(command, *, cwd=None, workspace=None, name="run_command"):
    """The PreToolUse payload shape recorded in spike/RESULT.md."""
    args = {"CommandLine": command}
    if cwd is not None:
        args["Cwd"] = str(cwd)
    return {
        "toolCall": {"name": name, "args": args},
        "stepIdx": 4,
        "conversationId": "ec33ebf9-0cba-4100-8142-c61503f6c587",
        "workspacePaths": [str(workspace)] if workspace is not None else [],
        "modelName": "gemini-3.6-flash-medium",
    }


def hook(body, *, raw=False):
    result = run("hook", "antigravity", input=body if raw else json.dumps(body))
    assert result.exit_code == 0, result.output  # the decision is in the JSON, never the exit code
    return json.loads(result.stdout)


@pytest.fixture
def project(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\nDB_URL=postgres://u:p@localhost/db\n")
    (protect_project / "backend").mkdir()
    (protect_project / "backend" / ".env.local").write_text("TOKEN=fake-token-9999\n")
    (protect_project / "certs").mkdir()
    (protect_project / "certs" / "server.pem").write_text("-----BEGIN FAKE-----\n")
    assert run("protect").exit_code == 0
    return protect_project


def decision(project, command, **kw):
    return hook(payload(command, workspace=project, **kw))


# --- what the hook allows and denies -----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "cat ./.env",
        "head -n 1 .env",
        "cat '.env'",
        'cat ".env"',
        'cat .e""nv',
        "cat .e'n'v",
        "cat .ENV",
        "cat $PWD/.env",
        "cat $(pwd)/.env",
        "cp .env /tmp/x",
        "base64 < .env",
        "source .env",
        "grep FAKE_KEY .env",
        "git add .env",
        "less backend/.env.local",
        "cat backend/.env.local",
        "cat certs/server.pem",
        "bash -c 'cat .env'",
        'sh -c "echo hi; cat .env"',
        'eval "cat .env"',
        "sudo cat .env",
        "FOO=1 cat .env",
        "nohup cat .env",
        "echo hi && cat .env",
        "ls | xargs cat .env",
        "if true; then cat .env; fi",
    ],
)
def test_commands_that_name_a_protected_file_are_denied(project, command):
    answer = decision(project, command)
    assert answer["decision"] == "deny", command
    assert answer["reason"].startswith("VaultShield: ")


def test_the_absolute_path_of_a_protected_file_is_denied(project):
    assert decision(project, f"cat {project.resolve()}/.env")["decision"] == "deny"
    assert decision(project, f"cat {project.resolve()}/backend/.env.local")["decision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "printenv",
        "printenv FAKE_KEY",
        "/usr/bin/env",
        "env FOO=1 true",
        "export -p",
        "export",
        "declare -x",
        "declare -p",
        "declare",
        "typeset -px",
        "set",
        "ps e",
        "ps -E",
        "ps eww",
        "ps -ef",  # also shows the environment on some systems; over-denied on purpose
        "echo hi && env",
        "ls | printenv",
        "echo $(printenv)",
        "bash -c 'env'",
    ],
)
def test_commands_that_dump_the_environment_are_denied(project, command):
    assert decision(project, command)["decision"] == "deny", command


@pytest.mark.parametrize(
    "command",
    [
        "security find-generic-password -s vaultshield -w",
        "security find-internet-password -s example.com",
        "security dump-keychain",
        "security dump-keychain -d login.keychain",
        'python3 -c "import keyring; print(keyring.get_password(\'vaultshield\', \'x\'))"',
        "python3 -c 'from keyring import get_password'",
    ],
)
def test_commands_that_read_the_keychain_are_denied(project, command):
    assert decision(project, command)["decision"] == "deny", command


@pytest.mark.parametrize(
    "command",
    [
        "vaultshield exec -- npm start",
        "vaultshield unprotect",
        "vaultshield --help exec",
        "/usr/local/bin/vaultshield exec -- ls",
        "python -m vaultshield.cli exec -- ls",
        "bash -c 'vaultshield unprotect'",
    ],
)
def test_vaultshield_exec_and_unprotect_are_denied(project, command):
    assert decision(project, command)["decision"] == "deny", command


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls -la",
        "npm test",
        "git status",
        "git diff",
        "cat README.md",
        "cat .env.example",
        "cat backend/.env.example",
        "cat .vaultshield/protect.json",
        "vaultshield status",
        "vaultshield protect --dry-run",
        "vaultshield agents render antigravity",
        "python -m venv .venv",
        "source .venv/bin/activate",
        "export FOO=bar",
        "set -e",
        "set -o pipefail",
        "declare -f my_function",
        "ps aux",
        "ps -p 123",
        "echo environment",
        "cat environment.md",
        "cat config.env",
        "security find-certificate -a",
        'node -e "console.log(1)"',
    ],
)
def test_ordinary_commands_are_allowed(project, command):
    answer = decision(project, command)
    assert answer == {"decision": "allow"}, command


def test_a_file_name_alone_is_enough_when_the_agent_is_in_a_subdirectory(project):
    answer = hook(payload("cat .env.local", cwd=project / "backend"))  # no workspacePaths
    assert answer["decision"] == "deny"


def test_the_project_is_found_from_the_working_directory_alone(project):
    answer = hook(payload("env", cwd=project / "backend"))
    assert answer["decision"] == "deny"


def test_a_project_without_a_manifest_is_not_touched(protect_project):
    for command in ("env", "cat .env", "vaultshield exec -- ls", "printenv"):
        assert hook(payload(command, workspace=protect_project)) == {"decision": "allow"}


def test_other_tools_are_not_the_hooks_business(project):
    answer = hook(payload("cat .env", workspace=project, name="view_file"))
    assert answer == {"decision": "allow"}


# --- fail closed --------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"toolCall": {"name": "run_command"}}',
        '{"toolCall": {"name": "run_command", "args": {}}}',
        '{"toolCall": {"name": "run_command", "args": {"CommandLine": 5}}}',
        '{"toolCall": "run_command"}',
    ],
)
def test_a_payload_that_cannot_be_read_is_denied(project, body):
    answer = hook(body, raw=True)
    assert answer["decision"] == "deny"
    assert "could not be read" in answer["reason"]


def test_an_unreadable_manifest_denies_everything_in_that_project(project):
    (project / ".vaultshield" / "protect.json").write_text("{not json")
    answer = decision(project, "ls")
    assert answer["decision"] == "deny"
    assert "manifest" in answer["reason"]


# --- the violations log -------------------------------------------------------


def _log(project):
    path = project / ".vaultshield" / "violations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_each_block_is_logged_as_a_json_line_without_the_command(project):
    command = "cat .env --a-flag-the-log-must-not-repeat"
    decision(project, command)
    decision(project, "printenv")
    decision(project, "security dump-keychain")
    decision(project, "vaultshield unprotect")

    records = _log(project)

    assert [r["event_type"] for r in records] == ["violation"] * 4
    assert [r["payload"] for r in records] == [
        "antigravity run_command denied: protected_path .env",
        "antigravity run_command denied: env_dump printenv",
        "antigravity run_command denied: keychain_read security",
        "antigravity run_command denied: vaultshield_command vaultshield",
    ]
    for record in records:
        assert set(record) == {"event_type", "timestamp", "payload"}
    text = (project / ".vaultshield" / "violations.jsonl").read_text()
    assert "a-flag-the-log-must-not-repeat" not in text
    assert KEY_VALUE not in text


def test_allowed_commands_are_not_logged(project):
    decision(project, "ls")
    decision(project, "npm test")
    assert _log(project) == []


def test_a_log_that_cannot_be_written_still_denies(project):
    vault_dir = project / ".vaultshield"
    original = vault_dir.stat().st_mode
    vault_dir.chmod(0o500)
    try:
        answer = decision(project, "cat .env")
    finally:
        vault_dir.chmod(original)
    assert answer["decision"] == "deny"


def test_the_hook_does_not_read_the_keychain(project, fake_keyring, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the hook must not touch the Keychain")

    monkeypatch.setattr(type(fake_keyring), "get_password", forbidden)
    assert decision(project, "cat .env")["decision"] == "deny"
    assert decision(project, "ls")["decision"] == "allow"


# --- render -------------------------------------------------------------------


def _json_documents(text):
    decoder = json.JSONDecoder()
    docs, index = [], 0
    while True:
        start = text.find("\n{", index)
        if start == -1:
            return docs
        doc, end = decoder.raw_decode(text, start + 1)
        docs.append(doc)
        index = end


def test_render_prints_exact_path_deny_rules_for_every_protected_and_detected_file(project):
    result = run("agents", "render", "antigravity")

    assert result.exit_code == 0, result.output
    deny_doc, hook_doc = _json_documents(result.output)
    base = project.resolve()
    assert deny_doc == {
        "permissions": {
            "deny": [
                f"read_file({base}/.env)",
                f"read_file({base}/backend/.env.local)",
                f"read_file({base}/certs/server.pem)",
            ]
        }
    }
    assert not any("*" in rule for rule in deny_doc["permissions"]["deny"])
    assert hook_doc == {
        "vaultshield-protect": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [{"type": "command", "command": "vaultshield hook antigravity", "timeout": 10}],
                }
            ]
        }
    }


def test_render_says_where_each_part_goes_and_what_is_not_verified(project):
    output = run("agents", "render", "antigravity").output

    assert "Nothing was installed or changed" in output
    assert "~/.gemini/antigravity-cli/settings.json" in output
    assert "~/.gemini/config/config.json" in output
    assert "NOT VERIFIED for the" in output
    assert ".agents/hooks.json" in output
    assert "~/.gemini/config/hooks.json" in output


def test_render_installs_nothing_and_changes_nothing(project, tmp_path):
    def tree(root):
        return {
            p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    home = tmp_path / "home"
    assert os.environ["HOME"] == str(home)
    project_before, home_before = tree(project), tree(home)

    result = run("agents", "render", "antigravity")

    assert result.exit_code == 0
    assert tree(project) == project_before
    assert tree(home) == home_before
    assert not (home / ".gemini").exists()
    assert not (home / ".config").exists()
    assert not (project / ".agents").exists()


def test_render_output_contains_no_values(project):
    output = run("agents", "render", "antigravity").output
    assert KEY_VALUE not in output
    assert "postgres://u:p" not in output


def test_render_without_protected_files_is_an_error(protect_project):
    result = run("agents", "render", "antigravity")
    assert result.exit_code == 1
    assert "run `vaultshield protect` first" in result.output


def test_render_works_from_a_subdirectory(project):
    os.chdir(project / "backend")
    result = run("agents", "render", "antigravity")
    assert result.exit_code == 0
    assert f"read_file({project.resolve()}/.env)" in result.output


def test_render_only_knows_antigravity(project):
    assert run("agents", "render", "claude").exit_code == 2


def test_rules_are_stable_and_sorted(project):
    manifest = antigravity.manifest_mod.load(project)
    rules = antigravity.deny_rules(project, manifest)
    assert rules == sorted(rules)
