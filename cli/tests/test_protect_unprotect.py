"""Step 4: `vaultshield unprotect` restores files byte for byte (fake values)."""

import json
import stat

import pytest
from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.credentials import parse_env_file
from vaultshield.protect import PLACEHOLDER, protector
from vaultshield.protect.vault import Vault

SAMPLES = {
    "plain": "A=fake-aaaa-1111\nB=fake-bbbb-2222\n",
    "double quotes": 'A="fake quoted value"\n',
    "single quotes": "A='fake single quoted'\n",
    "quotes keep inner quotes": "A=\"it's fake\"\nB='say \"fake\"'\n",
    "inline comment": "A=fake-aaaa-1111 # a note\n",
    "hash inside quotes": 'A="fake # not a comment"\n',
    "empty values": "A=\nB=\"\"\nC=''\nD=   \nE=fake-eeee-5555\n",
    "duplicate keys": "A=first-fake-1\nB=fake-bbbb-2222\nA=second-fake-2\n",
    "duplicate keys in a row": "A=one-fake-1\nA=two-fake-2\nA=three-fake-3\n",
    "CRLF": "A=fake-aaaa-1111\r\n# c\r\n\r\nB=\"fake b\"\r\n",
    "no final newline": "A=fake-aaaa-1111\nB=fake-bbbb-2222",
    "mixed line endings": "A=fake-aaaa-1111\r\nB=fake-bbbb-2222\nC=fake-cccc-3333\r\n",
    "spacing around =": "A = fake-aaaa-1111\n  B =  \"fake b\"  \n\tC=fake-c\n",
    "equals and hash in value": "URL=postgres://u:p@h/db?x=1#frag\n",
    "unicode": "A=fäke-ünï-値\n",
    "comments and blank lines": "# top\n\n  # indented comment\nA=fake-aaaa-1111\n\n\n# end\n",
    "only comments": "# nothing here\n",
    "lowercase and digits in names": "a_b2=fake-lower-1\n_X=fake-underscore-2\n",
}


def run(*args):
    return CliRunner().invoke(cli, list(args))


def write(project, rel, text):
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_unprotect_restores_the_file_byte_for_byte(protect_project, name):
    original = SAMPLES[name].encode("utf-8")
    env = protect_project / ".env"
    env.write_bytes(original)

    assert run("protect").exit_code == 0
    result = run("unprotect")

    assert result.exit_code == 0, result.output
    assert env.read_bytes() == original


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_parse_env_file_sees_the_same_values_before_and_after(protect_project, name):
    env = write(protect_project, ".env", SAMPLES[name])
    before = parse_env_file(env)

    run("protect")
    assert all(v in ("", "   ", PLACEHOLDER) or v.strip() == "" for _k, v in parse_env_file(env))
    run("unprotect")

    assert parse_env_file(env) == before


def test_unprotect_forgets_the_values_in_the_keychain(protect_project, fake_keyring):
    write(protect_project, ".env", SAMPLES["plain"])
    run("protect")
    assert len(fake_keyring.items) == 2

    result = run("unprotect")

    assert "Restored .env: A, B" in result.output
    assert fake_keyring.items == {}


def test_unprotect_removes_the_file_from_the_manifest_but_keeps_the_project_id(protect_project):
    write(protect_project, ".env", SAMPLES["plain"])
    run("protect")
    manifest_file = protect_project / ".vaultshield" / "protect.json"
    project_id = json.loads(manifest_file.read_text())["project_id"]

    run("unprotect")

    data = json.loads(manifest_file.read_text())
    assert data["protected"] == {}
    assert data["project_id"] == project_id


def test_a_second_unprotect_has_nothing_to_do(protect_project):
    env = write(protect_project, ".env", SAMPLES["plain"])
    run("protect")
    run("unprotect")

    result = run("unprotect")

    assert result.exit_code == 0
    assert "Nothing is protected here." in result.output
    assert env.read_text() == SAMPLES["plain"]


def test_unprotect_in_a_project_that_was_never_protected(protect_project):
    result = run("unprotect")
    assert result.exit_code == 0
    assert "Nothing is protected here." in result.output


def test_protect_again_after_unprotect_reuses_the_project_id(protect_project, fake_keyring):
    env = write(protect_project, ".env", SAMPLES["plain"])
    run("protect")
    project_id = json.loads((protect_project / ".vaultshield" / "protect.json").read_text())["project_id"]
    run("unprotect")

    run("protect")

    assert env.read_text() == f"A={PLACEHOLDER}\nB={PLACEHOLDER}\n"
    assert {a.split(":", 1)[0] for (_s, a) in fake_keyring.items} == {project_id}


def test_file_permissions_survive_a_round_trip(protect_project):
    env = write(protect_project, ".env", SAMPLES["plain"])
    env.chmod(0o600)
    run("protect")
    run("unprotect")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_subdirectory_files_round_trip(protect_project):
    original = SAMPLES["duplicate keys"]
    env = write(protect_project, "backend/.env.local", original)
    run("protect")
    run("unprotect")
    assert env.read_text() == original


def test_variables_added_after_protecting_are_left_alone(protect_project):
    env = write(protect_project, ".env", "A=fake-aaaa-1111\n")
    run("protect")
    env.write_text(f"A={PLACEHOLDER}\nNEW=typed-by-hand\n")

    result = run("unprotect")

    assert result.exit_code == 0, result.output
    assert env.read_text() == "A=fake-aaaa-1111\nNEW=typed-by-hand\n"


def test_a_missing_keychain_item_refuses_the_file_and_keeps_everything(protect_project, fake_keyring):
    env = write(protect_project, ".env", SAMPLES["plain"])
    run("protect")
    project_id = json.loads((protect_project / ".vaultshield" / "protect.json").read_text())["project_id"]
    Vault(project_id).remove(".env", "B")
    protected_text = env.read_text()

    result = run("unprotect")

    assert result.exit_code == 1
    assert "Keychain item for B is missing" in result.output
    assert env.read_text() == protected_text
    assert len(fake_keyring.items) == 1  # A is still there
    assert json.loads((protect_project / ".vaultshield" / "protect.json").read_text())["protected"] == {
        ".env": ["A", "B"]
    }


def test_an_unreadable_keychain_record_refuses_the_file(protect_project, fake_keyring):
    env = write(protect_project, ".env", "A=fake-aaaa-1111\n")
    run("protect")
    for key in list(fake_keyring.items):
        fake_keyring.items[key] = "not a record"
    protected_text = env.read_text()

    result = run("unprotect")

    assert result.exit_code == 1
    assert "unreadable" in result.output
    assert env.read_text() == protected_text


def test_a_changed_placeholder_count_refuses_the_file(protect_project, fake_keyring):
    env = write(protect_project, ".env", SAMPLES["duplicate keys"])
    run("protect")
    env.write_text(f"A={PLACEHOLDER}\nB={PLACEHOLDER}\n")  # one A line was deleted
    edited = env.read_text()

    result = run("unprotect")

    assert result.exit_code == 1
    assert "A has 1 placeholder line(s)" in result.output
    assert env.read_text() == edited
    assert len(fake_keyring.items) == 2


def test_a_placeholder_replaced_by_a_real_value_refuses_the_file(protect_project):
    env = write(protect_project, ".env", "A=fake-aaaa-1111\n")
    run("protect")
    env.write_text("A=typed-a-new-value\n")

    result = run("unprotect")

    assert result.exit_code == 1
    assert "0 placeholder line(s)" in result.output
    assert env.read_text() == "A=typed-a-new-value\n"


def test_a_deleted_file_is_reported_and_its_values_are_kept(protect_project, fake_keyring):
    env = write(protect_project, ".env", "A=fake-aaaa-1111\n")
    run("protect")
    env.unlink()

    result = run("unprotect")

    assert result.exit_code == 1
    assert "no longer exists" in result.output
    assert len(fake_keyring.items) == 1


def test_one_bad_file_does_not_stop_the_others(protect_project, fake_keyring):
    good = write(protect_project, ".env", "A=fake-aaaa-1111\n")
    bad = write(protect_project, ".env.local", "B=fake-bbbb-2222\n")
    run("protect")
    bad.write_text("B=typed-over\n")

    result = run("unprotect")

    assert result.exit_code == 1
    assert good.read_text() == "A=fake-aaaa-1111\n"
    assert "Restored .env: A" in result.output
    assert "Refused .env.local" in result.output
    assert list(a.split(":", 1)[1] for (_s, a) in fake_keyring.items) == [".env.local:B"]


def test_a_failed_write_keeps_the_values_in_the_keychain(protect_project, fake_keyring, monkeypatch):
    env = write(protect_project, ".env", "A=fake-aaaa-1111\n")
    run("protect")
    items = dict(fake_keyring.items)
    protected_text = env.read_text()

    def boom(path, data, mode=None):
        raise OSError("disk full")

    monkeypatch.setattr(protector, "atomic_write_bytes", boom)
    result = run("unprotect")

    assert result.exit_code == 1
    assert "writing the file failed" in result.output
    assert env.read_text() == protected_text
    assert fake_keyring.items == items


def test_a_restored_file_that_reads_back_wrong_is_put_back(protect_project, fake_keyring, monkeypatch):
    env = write(protect_project, ".env", "A=fake-aaaa-1111\n")
    run("protect")
    items = dict(fake_keyring.items)
    protected_text = env.read_text()
    real_write = protector.atomic_write_bytes
    calls = []

    def corrupting(path, data, mode=None):
        calls.append(data)
        real_write(path, data + b"#corrupted\n" if len(calls) == 1 else data, mode)

    monkeypatch.setattr(protector, "atomic_write_bytes", corrupting)
    result = run("unprotect")

    assert result.exit_code == 1
    assert "did not read back correctly" in result.output
    assert env.read_text() == protected_text
    assert fake_keyring.items == items


def test_unprotect_output_never_contains_values(protect_project):
    secret = "fake-unique-value-7777"
    write(protect_project, ".env", f"A={secret}\n")
    run("protect")
    result = run("unprotect")
    assert secret not in result.output
