"""`vaultshield agents install antigravity` (step 1) and its siblings.

Every test passes `--home` pointing at a fake home inside `tmp_path`, built from
invented files (see protect_fakes.py). HOME itself is also a temporary folder
(the `protect_project` fixture). No real Antigravity file is read or written.
"""

import copy
import json
import os
import shlex
import stat

import pytest
from click.testing import CliRunner

import protect_fakes as fakes
from vaultshield.cli import cli
from vaultshield.protect.agents import install as agent_install

KEY_VALUE = "sk-test-AAAA1111BBBB2222"
DB_VALUE = "postgres://u:p@localhost/db"


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def insertion(old: str, new: str) -> str:
    extra = len(new) - len(old)
    assert extra > 0
    p = 0
    while p < len(old) and old[p] == new[p]:
        p += 1
    assert new[:p] + new[p + extra :] == old, "more than a single insertion changed the text"
    return new[p : p + extra]


@pytest.fixture
def project(protect_project):
    """A protected project: .env and backend/.env.local protected, certs/server.pem detected."""
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\n")
    (protect_project / "backend").mkdir()
    (protect_project / "backend" / ".env.local").write_text("TOKEN=fake-token-9999\n")
    (protect_project / "certs").mkdir()
    (protect_project / "certs" / "server.pem").write_text("-----BEGIN FAKE-----\n")
    assert run("protect").exit_code == 0
    return protect_project


@pytest.fixture
def fake_home(tmp_path):
    return fakes.make_home(tmp_path)


def expected_rules(project):
    base = project.resolve()
    return [f"read_file({base}/.env)", f"read_file({base}/backend/.env.local)", f"read_file({base}/certs/server.pem)"]


def install(home, *extra):
    return run("agents", "install", "antigravity", "--home", str(home["home"]), *extra)


def without_ours(settings: dict, kind: str) -> dict:
    """The parsed file with our additions removed."""
    data = copy.deepcopy(settings)
    if kind == "cli":
        data.get("permissions", {}).pop("deny", None)
        if data.get("permissions") == {}:
            del data["permissions"]
    elif kind == "app":
        grants = data["userSettings"]["globalPermissionGrants"]
        grants.pop("deny", None)
    else:
        data.pop("vaultshield-protect", None)
    return data


# --- what gets written --------------------------------------------------------


def test_install_writes_exact_absolute_deny_rules_to_the_cli_settings(project, fake_home):
    before = fake_home["cli"].read_text()

    result = install(fake_home)

    assert result.exit_code == 0, result.output
    after = fake_home["cli"].read_text()
    assert json.loads(after)["permissions"]["deny"] == expected_rules(project)
    insertion(before, after)  # one insertion; every other byte untouched
    assert without_ours(json.loads(after), "cli") == json.loads(before)


def test_install_writes_the_same_rules_to_the_app_config(project, fake_home):
    before = fake_home["app"].read_text()

    install(fake_home)

    after = fake_home["app"].read_text()
    grants = json.loads(after)["userSettings"]["globalPermissionGrants"]
    assert grants["deny"] == expected_rules(project)
    insertion(before, after)
    assert without_ours(json.loads(after), "app") == json.loads(before)


def test_every_existing_grant_is_left_byte_for_byte(project, fake_home):
    old_grants = fakes.config_object()["userSettings"]["globalPermissionGrants"]["allow"]
    assert len(old_grants) >= 150

    install(fake_home)

    after = fake_home["app"].read_text()
    for grant in old_grants:
        assert json.dumps(grant, ensure_ascii=False) in after
    assert json.loads(after)["userSettings"]["globalPermissionGrants"]["allow"] == old_grants


def test_the_rules_are_exact_absolute_paths_with_no_globs(project, fake_home):
    install(fake_home)

    for key in ("cli", "app"):
        rules = [r for r in json.loads(fake_home[key].read_text()).get("permissions", {}).get("deny", [])]
        if key == "app":
            rules = json.loads(fake_home[key].read_text())["userSettings"]["globalPermissionGrants"]["deny"]
        assert rules
        for rule in rules:
            assert rule.startswith("read_file(/") and rule.endswith(")")
            assert not any(ch in rule for ch in "*?[]{}")
            assert ".." not in rule


def test_protected_and_detected_files_both_get_a_rule(project, fake_home):
    install(fake_home)
    rules = json.loads(fake_home["cli"].read_text())["permissions"]["deny"]
    assert any(r.endswith("/.env)") for r in rules)  # protected
    assert any(r.endswith("/server.pem)") for r in rules)  # detected, not moved


def test_install_adds_one_global_hook_on_run_command(project, fake_home):
    before = fake_home["hooks"].read_text()

    install(fake_home)

    after = fake_home["hooks"].read_text()
    data = json.loads(after)
    insertion(before, after)
    assert data["lint-checker"] == json.loads(before)["lint-checker"]  # someone else's hook, untouched
    entry = data["vaultshield-protect"]
    assert list(entry) == ["PreToolUse"] and len(entry["PreToolUse"]) == 1
    group = entry["PreToolUse"][0]
    assert group["matcher"] == "run_command"
    [hook] = group["hooks"]
    assert hook["type"] == "command"
    words = shlex.split(hook["command"])
    assert words[-2:] == ["hook", "antigravity"]


def test_the_hook_command_uses_an_absolute_path_that_exists(project, fake_home):
    install(fake_home)
    command = json.loads(fake_home["hooks"].read_text())["vaultshield-protect"]["PreToolUse"][0]["hooks"][0]["command"]
    words = shlex.split(command)
    assert os.path.isabs(words[0]) and os.path.exists(words[0])


def test_the_hook_command_quotes_a_path_with_spaces(monkeypatch):
    monkeypatch.setattr(agent_install, "vaultshield_command", lambda: ["/Users/a b/venv/bin/vaultshield"])
    assert shlex.split(agent_install.hook_command()) == ["/Users/a b/venv/bin/vaultshield", "hook", "antigravity"]


def test_install_prints_where_it_wrote_and_warns_to_quit_the_app(project, fake_home):
    result = install(fake_home)

    assert "Quit Antigravity" in result.output
    assert "CLI settings ~/.gemini/antigravity-cli/settings.json: updated (3 deny rules added)" in result.output
    assert "App config ~/.gemini/config/config.json: updated (3 deny rules added)" in result.output
    assert "Hook ~/.gemini/config/hooks.json: updated" in result.output
    assert KEY_VALUE not in result.output and DB_VALUE not in result.output


# --- backups, idempotence -----------------------------------------------------


def test_a_backup_of_each_changed_file_is_kept_next_to_it(project, fake_home):
    originals = fakes.snapshot(fake_home)

    install(fake_home)

    for key in ("cli", "app", "hooks"):
        backup = fake_home[key].with_name(fake_home[key].name + ".vaultshield.bak")
        assert backup.read_bytes() == originals[key]
        assert fake_home[key].read_bytes() != originals[key]


def test_a_backup_keeps_the_original_permissions(project, fake_home):
    fake_home["app"].chmod(0o600)

    install(fake_home)

    backup = fake_home["app"].with_name("config.json.vaultshield.bak")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(fake_home["app"].stat().st_mode) == 0o600


def test_install_is_idempotent(project, fake_home):
    install(fake_home)
    first = fakes.snapshot(fake_home)
    stamps = {k: p.stat().st_mtime_ns for k, p in fake_home.items() if k != "home"}
    backups = {p: p.read_bytes() for p in fake_home["app"].parent.glob("*.vaultshield.bak")}

    result = install(fake_home)

    assert result.exit_code == 0, result.output
    assert fakes.snapshot(fake_home) == first
    assert {k: p.stat().st_mtime_ns for k, p in fake_home.items() if k != "home"} == stamps
    assert {p: p.read_bytes() for p in fake_home["app"].parent.glob("*.vaultshield.bak")} == backups
    assert result.output.count("unchanged") == 3


def test_a_later_install_adds_only_the_new_rules_and_keeps_the_first_backup(project, fake_home):
    originals = fakes.snapshot(fake_home)
    install(fake_home)
    (project / ".env.production").write_text("PROD_KEY=fake-prod-key-1234\n")
    assert run("protect").exit_code == 0

    result = install(fake_home)

    assert "1 deny rule added" in result.output
    rules = json.loads(fake_home["cli"].read_text())["permissions"]["deny"]
    assert rules[:3] == expected_rules(project) and rules[3].endswith("/.env.production)")
    assert fake_home["cli"].with_name("settings.json.vaultshield.bak").read_bytes() == originals["cli"]


def test_rules_the_user_already_had_are_kept_and_ours_are_added_after_them(project, tmp_path):
    mine = ["read_file(/Users/fake/private)"]
    home = fakes.make_home(
        tmp_path,
        config_text=fakes.dump(fakes.config_object(deny=mine)),
        settings_text=fakes.dump({"permissions": {"deny": mine}}),
    )

    install(home)

    assert json.loads(home["cli"].read_text())["permissions"]["deny"] == mine + expected_rules(project)
    grants = json.loads(home["app"].read_text())["userSettings"]["globalPermissionGrants"]
    assert grants["deny"] == mine + expected_rules(project)


# --- files that are missing ---------------------------------------------------


def test_a_missing_cli_settings_file_is_created_and_remembered_as_absent(project, tmp_path):
    home = fakes.make_home(tmp_path, cli=False)

    install(home)

    assert json.loads(home["cli"].read_text()) == {"permissions": {"deny": expected_rules(project)}}
    assert home["cli"].with_name("settings.json.vaultshield.absent").exists()
    assert not home["cli"].with_name("settings.json.vaultshield.bak").exists()


def test_a_missing_app_config_is_not_invented(project, tmp_path):
    home = fakes.make_home(tmp_path, app=False)

    result = install(home)

    assert result.exit_code == 0, result.output
    assert not home["app"].exists()
    assert "App config: skipped, config.json not found" in result.output
    assert json.loads(home["cli"].read_text())["permissions"]["deny"] == expected_rules(project)


def test_a_missing_hooks_file_is_created_with_only_our_hook(project, tmp_path):
    home = fakes.make_home(tmp_path, hooks=False)

    install(home)

    assert list(json.loads(home["hooks"].read_text())) == ["vaultshield-protect"]
    assert home["hooks"].with_name("hooks.json.vaultshield.absent").exists()


def test_without_the_cli_folder_the_cli_is_skipped(project, tmp_path):
    home = fakes.make_home(tmp_path, cli=False, cli_dir=False)

    result = install(home)

    assert result.exit_code == 0, result.output
    assert not (home["home"] / ".gemini" / "antigravity-cli").exists()
    assert "CLI settings: skipped" in result.output


def test_a_home_without_antigravity_installs_nothing_and_says_so(project, tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()

    result = run("agents", "install", "antigravity", "--home", str(home))

    assert result.exit_code == 1
    assert "nothing was installed" in result.output
    assert list(home.iterdir()) == []


# --- refusals leave everything alone ------------------------------------------


def test_a_config_that_is_not_json_is_refused_and_nothing_changes(project, tmp_path):
    home = fakes.make_home(tmp_path, config_text="{ this is not json")
    before = fakes.snapshot(home)

    result = install(home)

    assert result.exit_code == 1
    assert "config.json" in result.output and "nothing was changed" in result.output
    assert fakes.snapshot(home) == before
    assert not list(home["app"].parent.glob("*.vaultshield.*"))


def test_a_permissions_entry_of_the_wrong_type_is_refused(project, tmp_path):
    home = fakes.make_home(tmp_path, settings_text=fakes.dump({"permissions": ["not", "an", "object"]}))
    before = fakes.snapshot(home)

    result = install(home)

    assert result.exit_code == 1
    assert fakes.snapshot(home) == before


def test_a_config_that_is_not_utf8_is_refused(project, tmp_path):
    home = fakes.make_home(tmp_path)
    home["app"].write_bytes(b'{"userSettings": "\xff\xfe"}')
    before = fakes.snapshot(home)

    result = install(home)

    assert result.exit_code == 1
    assert "not valid UTF-8" in result.output
    assert fakes.snapshot(home) == before


def test_a_write_that_fails_puts_every_file_back(project, fake_home, monkeypatch):
    originals = fakes.snapshot(fake_home)
    real_write = agent_install.atomic_write_bytes
    calls = []

    def flaky(path, data, mode=None):
        calls.append(path.name)
        if len(calls) == 3:  # the third file
            raise OSError("disk full")
        real_write(path, data, mode)

    monkeypatch.setattr(agent_install, "atomic_write_bytes", flaky)
    result = install(fake_home)
    monkeypatch.setattr(agent_install, "atomic_write_bytes", real_write)

    assert result.exit_code == 1
    assert "put back" in result.output
    assert fakes.snapshot(fake_home) == originals
    assert not list(fake_home["app"].parent.glob("*.vaultshield.*"))
    assert not list(fake_home["cli"].parent.glob("*.vaultshield.*"))


def test_install_needs_a_protected_project(protect_project, fake_home):
    result = install(fake_home)
    assert result.exit_code == 1
    assert "run `vaultshield protect` first" in result.output


def test_install_only_knows_antigravity(project, fake_home):
    assert run("agents", "install", "claude", "--home", str(fake_home["home"])).exit_code == 2


# --- the home directory -------------------------------------------------------


def test_install_touches_nothing_outside_the_given_home(project, fake_home, tmp_path):
    other_home = tmp_path / "home"  # HOME for this test, from the fixture
    before_other = sorted(p.name for p in other_home.iterdir())

    install(fake_home)

    assert sorted(p.name for p in other_home.iterdir()) == before_other
    assert os.environ["HOME"] == str(other_home)


def test_without_home_the_default_is_the_home_directory_of_the_process(project, tmp_path, monkeypatch):
    home = fakes.make_home(tmp_path)
    monkeypatch.setenv("HOME", str(home["home"]))

    result = run("agents", "install", "antigravity")

    assert result.exit_code == 0, result.output
    assert "deny" in json.loads(home["cli"].read_text())["permissions"]


def test_install_never_prints_or_stores_a_secret_value(project, fake_home):
    result = install(fake_home)

    blob = result.output + "".join(
        p.read_text() for p in fake_home["app"].parent.iterdir() if p.is_file() and p.name.startswith(("hooks", "config"))
    )
    for secret in (KEY_VALUE, DB_VALUE, "fake-token-9999"):
        assert secret not in blob
