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


# --- uninstall (step 2) --------------------------------------------------------


def uninstall(home, *extra):
    return run("agents", "uninstall", "antigravity", "--home", str(home["home"]), *extra)


def leftovers(home):
    folders = {home["cli"].parent, home["app"].parent}
    return sorted(p.name for f in folders for p in f.glob("*.vaultshield.*"))


def test_uninstall_restores_every_file_byte_for_byte_and_removes_the_copies(project, fake_home):
    originals = fakes.snapshot(fake_home)
    install(fake_home)
    assert fakes.snapshot(fake_home) != originals

    result = uninstall(fake_home)

    assert result.exit_code == 0, result.output
    assert fakes.snapshot(fake_home) == originals
    assert leftovers(fake_home) == []
    assert result.output.count("restored") == 3


def test_uninstall_removes_files_that_install_created(project, tmp_path):
    home = fakes.make_home(tmp_path, cli=False, hooks=False)
    originals = fakes.snapshot(home)
    install(home)
    assert home["cli"].exists() and home["hooks"].exists()

    result = uninstall(home)

    assert result.exit_code == 0, result.output
    assert not home["cli"].exists() and not home["hooks"].exists()
    assert fakes.snapshot(home) == originals
    assert leftovers(home) == []
    assert "removed (it did not exist before the install)" in result.output


def test_uninstall_twice_has_nothing_left_to_do(project, fake_home):
    originals = fakes.snapshot(fake_home)
    install(fake_home)
    uninstall(fake_home)

    result = uninstall(fake_home)

    assert result.exit_code == 0
    assert "Nothing to uninstall." in result.output
    assert fakes.snapshot(fake_home) == originals


def test_uninstall_without_an_install_changes_nothing(project, fake_home):
    originals = fakes.snapshot(fake_home)

    result = uninstall(fake_home)

    assert result.exit_code == 0
    assert "Nothing to uninstall." in result.output
    assert fakes.snapshot(fake_home) == originals


def test_uninstall_undoes_several_installs_back_to_the_very_first_state(project, fake_home):
    originals = fakes.snapshot(fake_home)
    install(fake_home)
    (project / ".env.production").write_text("PROD_KEY=fake-prod-key-1234\n")
    run("protect")
    install(fake_home)

    uninstall(fake_home)

    assert fakes.snapshot(fake_home) == originals
    assert leftovers(fake_home) == []


def test_normally_no_extra_copy_is_made(project, fake_home):
    install(fake_home)
    (project / ".env.production").write_text("PROD_KEY=fake-prod-key-1234\n")
    run("protect")
    install(fake_home)

    result = uninstall(fake_home)

    assert "before-uninstall" not in result.output
    assert leftovers(fake_home) == []


def test_a_file_that_changed_since_install_is_restored_but_its_latest_version_is_kept(project, fake_home):
    originals = fakes.snapshot(fake_home)
    install(fake_home)
    # Antigravity adds a grant after the user approves a command.
    config = json.loads(fake_home["app"].read_text())
    config["userSettings"]["globalPermissionGrants"]["allow"].append("command(echo approved-later)")
    changed = fakes.dump(config)
    fake_home["app"].write_text(changed)

    result = uninstall(fake_home)

    assert result.exit_code == 0, result.output
    assert fake_home["app"].read_bytes() == originals["app"]  # restored as specified
    kept = fake_home["app"].with_name("config.json.vaultshield.before-uninstall")
    assert kept.read_text() == changed  # ...and nothing is lost for good
    assert "it had changed since the install; that version is kept as config.json.vaultshield.before-uninstall" in result.output
    # the files that did not change get no extra copy
    assert not fake_home["cli"].with_name("settings.json.vaultshield.before-uninstall").exists()
    assert not fake_home["hooks"].with_name("hooks.json.vaultshield.before-uninstall").exists()


def test_extra_copies_are_never_overwritten(project, fake_home):
    for round_number in (1, 2):
        install(fake_home)
        config = json.loads(fake_home["app"].read_text())
        config["userSettings"]["globalPermissionGrants"]["allow"].append(f"command(echo round-{round_number})")
        fake_home["app"].write_text(fakes.dump(config))
        uninstall(fake_home)

    kept = sorted(p.name for p in fake_home["app"].parent.glob("config.json.vaultshield.before-uninstall*"))
    assert kept == ["config.json.vaultshield.before-uninstall", "config.json.vaultshield.before-uninstall.1"]
    assert "round-1" in fake_home["app"].with_name(kept[0]).read_text()


def test_a_file_the_user_deleted_after_install_comes_back(project, fake_home):
    originals = fakes.snapshot(fake_home)
    install(fake_home)
    fake_home["app"].unlink()

    uninstall(fake_home)

    assert fakes.snapshot(fake_home) == originals


def test_the_restored_file_has_the_permissions_of_the_original(project, fake_home):
    fake_home["app"].chmod(0o600)
    install(fake_home)
    fake_home["app"].chmod(0o644)  # something loosened it in between

    uninstall(fake_home)

    assert stat.S_IMODE(fake_home["app"].stat().st_mode) == 0o600


def test_a_restore_that_does_not_read_back_correctly_keeps_the_copy(project, fake_home, monkeypatch):
    originals = fakes.snapshot(fake_home)
    install(fake_home)
    real_write = agent_install.atomic_write_bytes

    def corrupting(path, data, mode=None):
        real_write(path, data + b" ", mode)

    monkeypatch.setattr(agent_install, "atomic_write_bytes", corrupting)
    result = uninstall(fake_home)
    monkeypatch.setattr(agent_install, "atomic_write_bytes", real_write)

    assert result.exit_code == 1
    assert "did not read back correctly" in result.output
    assert fake_home["cli"].with_name("settings.json.vaultshield.bak").read_bytes() == originals["cli"]


def test_uninstall_touches_nothing_outside_the_given_home(project, fake_home, tmp_path):
    install(fake_home)
    other_home = tmp_path / "home"
    before = sorted(p.name for p in other_home.iterdir())

    uninstall(fake_home)

    assert sorted(p.name for p in other_home.iterdir()) == before


def test_without_home_uninstall_uses_the_home_directory_of_the_process(project, tmp_path, monkeypatch):
    home = fakes.make_home(tmp_path)
    originals = fakes.snapshot(home)
    monkeypatch.setenv("HOME", str(home["home"]))
    run("agents", "install", "antigravity")

    result = run("agents", "uninstall", "antigravity")

    assert result.exit_code == 0, result.output
    assert fakes.snapshot(home) == originals


def test_uninstall_does_not_need_a_project(protect_project, fake_home):
    # uninstall is about the home directory only
    assert uninstall(fake_home).exit_code == 0


def test_uninstall_only_knows_antigravity(fake_home):
    assert run("agents", "uninstall", "claude", "--home", str(fake_home["home"])).exit_code == 2


# --- doctor (step 3) -----------------------------------------------------------

from vaultshield.protect.agents import doctor as agent_doctor  # noqa: E402


@pytest.fixture
def at_home(fake_home, monkeypatch):
    """`doctor` has no --home option: it reads the home directory of the process.
    Point that at the fake home."""
    monkeypatch.setenv("HOME", str(fake_home["home"]))
    return fake_home


def doctor():
    return run("agents", "doctor", "antigravity")


def line(output: str, *needles: str) -> str:
    for text in output.splitlines():
        if all(n in text for n in needles):
            return text
    raise AssertionError(f"no line with {needles} in:\n{output}")


def test_before_install_the_placeholders_hold_but_nothing_else_is_active(project, at_home):
    result = doctor()

    assert result.exit_code == 1
    assert "ACTIVE" in line(result.output, "placeholders")
    assert "NOT ACTIVE" not in line(result.output, "placeholders")
    assert "NOT ACTIVE" in line(result.output, "deny rules")
    assert "NOT ACTIVE" in line(result.output, "hook")
    assert "Not everything is active." in result.output


def test_after_install_everything_is_active_on_both_surfaces(project, at_home):
    install(at_home)

    result = doctor()

    assert result.exit_code == 0, result.output
    assert "Everything is active." in result.output
    assert "CLI  ~/.gemini/antigravity-cli/settings.json" in result.output
    assert "App  ~/.gemini/config/config.json" in result.output
    assert result.output.count("exact-path rules present") == 2
    assert "3 of 3 exact-path rules present" in result.output
    assert "2 files, 3 variables hold vs_protected" in result.output
    assert "~/.gemini/config/hooks.json" in result.output
    assert "NOT ACTIVE" not in result.output


def test_a_missing_rule_is_reported_as_partial(project, at_home):
    install(at_home)
    settings = json.loads(at_home["cli"].read_text())
    settings["permissions"]["deny"].pop()
    at_home["cli"].write_text(fakes.dump(settings))

    result = doctor()

    assert result.exit_code == 1
    assert "PARTIAL" in line(result.output, "deny rules", "2 of 3")
    assert "3 of 3" in result.output  # the App is still complete


def test_after_unprotect_the_placeholders_are_not_active_and_no_value_is_shown(project, at_home):
    install(at_home)
    assert run("unprotect").exit_code == 0

    result = doctor()

    assert result.exit_code == 1
    assert "NOT ACTIVE" in line(result.output, "placeholders")
    assert "no file has been protected yet" in result.output
    for secret in (KEY_VALUE, DB_VALUE, "fake-token-9999"):
        assert secret not in result.output


def test_a_real_value_typed_into_a_protected_file_is_noticed(project, at_home):
    install(at_home)
    (project / ".env").write_text("FAKE_KEY=typed-in-again-1234\nDB_URL=vs_protected\n")

    result = doctor()

    assert "real values on disk for .env: FAKE_KEY" in result.output
    assert "typed-in-again-1234" not in result.output


def test_detected_files_are_listed_as_not_covered_by_placeholders(project, at_home):
    install(at_home)
    result = doctor()
    assert "certs/server.pem" in line(result.output, "Not covered by placeholders")


def test_a_missing_file_and_a_broken_file_are_reported(project, tmp_path, monkeypatch):
    home = fakes.make_home(tmp_path, app=False, settings_text="{broken")
    monkeypatch.setenv("HOME", str(home["home"]))

    result = doctor()

    assert "file not found" in result.output
    assert "file is not valid JSON" in result.output


# --- the hook check ---


def hooks_with(**groups):
    return fakes.dump({"vaultshield-protect": {"PreToolUse": groups.get("pre", []), **groups.get("extra", {})}})


def pre(matcher, command, **handler):
    return [{"matcher": matcher, "hooks": [{"type": "command", "command": command, **handler}]}]


def test_a_hook_in_the_project_counts_too(project, at_home):
    at_home["hooks"].unlink()
    (project / ".agents").mkdir()
    (project / ".agents" / "hooks.json").write_text(hooks_with(pre=pre("run_command", agent_install.hook_command())))

    result = doctor()

    assert "ACTIVE" in line(result.output, "hook", ".agents/hooks.json (this project)")


@pytest.mark.parametrize("matcher", ["run_command", "", "*", ".*", "run_command|view_file"])
def test_matchers_that_cover_run_command_count(project, at_home, matcher):
    at_home["hooks"].write_text(hooks_with(pre=pre(matcher, agent_install.hook_command())))
    assert "NOT ACTIVE" not in line(doctor().output, "  hook")


@pytest.mark.parametrize("matcher", ["view_file", "browser_.*", "run_command_x"])
def test_matchers_that_do_not_cover_run_command_do_not_count(project, at_home, matcher):
    at_home["hooks"].write_text(hooks_with(pre=pre(matcher, agent_install.hook_command())))
    assert "NOT ACTIVE" in line(doctor().output, "  hook")


def test_a_disabled_hook_does_not_count(project, at_home):
    at_home["hooks"].write_text(
        hooks_with(pre=pre("run_command", agent_install.hook_command()), extra={"enabled": False})
    )
    assert "NOT ACTIVE" in line(doctor().output, "  hook")


def test_a_hook_that_points_at_a_program_that_is_gone_is_not_active(project, at_home):
    at_home["hooks"].write_text(hooks_with(pre=pre("run_command", "/nowhere/at/all/vaultshield hook antigravity")))

    result = doctor()

    assert "NOT ACTIVE" in line(result.output, "  hook")
    assert "no longer exists" in result.output


def test_someone_elses_hook_is_not_ours(project, at_home):
    at_home["hooks"].write_text(fakes.dump(fakes.hooks_object()))
    assert "NOT ACTIVE" in line(doctor().output, "  hook")


# --- credentials in the command grants ---


def test_credential_looking_grants_are_flagged_by_position_and_count_only(project, tmp_path, monkeypatch):
    allow = fakes.grants(extra_credentials=True)
    flagged = [i for i, g in enumerate(allow) if g in fakes.CREDENTIAL_GRANTS]
    assert len(flagged) == len(fakes.CREDENTIAL_GRANTS)
    home = fakes.make_home(tmp_path, config_text=fakes.dump(fakes.config_object(allow=allow)))
    monkeypatch.setenv("HOME", str(home["home"]))

    result = doctor()

    assert f"WARN: {len(flagged)} of {len(allow)} grants in ~/.gemini/config/config.json look like they contain credentials" in result.output
    assert "Grant positions (0-based, in userSettings.globalPermissionGrants.allow): " + ", ".join(map(str, flagged)) in result.output
    assert "The values are not shown." in result.output
    for fragment in (
        "fakepass-0000",
        "fakeuser",
        "fake-bearer-token",
        "ghp_fake",
        "AKIAFAKE",
        "fake-api-key-value",
        "sk-test-AAAA",
        "PRIVATE KEY",
        "mongodb",
        "cluster.example",
    ):
        assert fragment not in result.output


def test_no_warning_for_ordinary_grants(project, at_home):
    install(at_home)
    assert "WARN" not in doctor().output


def test_the_warning_does_not_change_the_exit_code(project, tmp_path, monkeypatch):
    home = fakes.make_home(tmp_path, config_text=fakes.dump(fakes.config_object(allow=fakes.grants(extra_credentials=True))))
    monkeypatch.setenv("HOME", str(home["home"]))
    install(home)

    result = doctor()

    assert "WARN" in result.output
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "grant",
    [
        "command(psql postgres://admin:hunter2fake@db.example.invalid/x)",
        "command(echo ghp_fakeFAKEfake0123456789abcdefABCDEF)",
        "command(echo xoxb-1234567890-fakefake)",
        "command(echo AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE12345)",
        "command(echo ya29.fake_token_value_0123456789)",
        "command(echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.fakesignature123)",
        "command(echo -----BEGIN OPENSSH PRIVATE KEY-----)",
        "command(curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwx' x)",
        "command(PASSWORD=fakepassword1 ./run)",
        "command(export TOKEN=abcdefgh12345)",
        "command(echo \"api_key: 'abcdefghij1234'\")",
        "command(echo sk-test-AAAA1111BBBB2222)",
    ],
)
def test_these_look_like_credentials(grant):
    assert agent_doctor.looks_like_a_credential(grant)


@pytest.mark.parametrize(
    "grant",
    [
        "command(git status)",
        "command(npm test)",
        "command(export TOKEN=$(cat ~/.token))",
        "command(curl https://example.com/path)",
        "command(git clone https://github.com/org/repo.git)",
        "command(ssh user@host)",
        "command(echo sk-short)",
        "command(TOKEN=abc ./run)",
        "command(echo password)",
        'command(python3 manage.py shell -c "from app import x")',
        "command(npm config set registry https://registry.npmjs.org/)",
    ],
)
def test_these_do_not_look_like_credentials(grant):
    assert not agent_doctor.looks_like_a_credential(grant)


def test_only_command_grants_are_checked(project, tmp_path, monkeypatch):
    allow = ["read_file(/Users/fake/ghp_fakeFAKEfake0123456789abcdefABCDEF)", "write_file(/x/password=abcdefghijk)"]
    home = fakes.make_home(tmp_path, config_text=fakes.dump(fakes.config_object(allow=allow)))
    monkeypatch.setenv("HOME", str(home["home"]))
    assert "WARN" not in doctor().output


def test_doctor_changes_nothing(project, at_home):
    install(at_home)
    before = fakes.snapshot(at_home)
    stamps = {k: p.stat().st_mtime_ns for k, p in at_home.items() if k != "home"}
    project_before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}

    doctor()

    assert fakes.snapshot(at_home) == before
    assert {k: p.stat().st_mtime_ns for k, p in at_home.items() if k != "home"} == stamps
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == project_before


def test_doctor_needs_a_protected_project(protect_project):
    result = doctor()
    assert result.exit_code == 1
    assert "run `vaultshield protect` first" in result.output


def test_doctor_only_knows_antigravity(project):
    assert run("agents", "doctor", "claude").exit_code == 2
