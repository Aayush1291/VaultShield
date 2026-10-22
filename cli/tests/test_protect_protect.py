"""Step 3: `vaultshield protect` (all values here are fake)."""

import json
import os
import stat

import pytest
from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.protect import PLACEHOLDER, protector
from vaultshield.protect.envfile import decode_record
from vaultshield.protect.vault import InMemoryKeyring

KEY_VALUE = "sk-test-AAAA1111BBBB2222"
DB_VALUE = "postgres://u:p@localhost/db"
ENV_TEXT = (
    "# header comment\n"
    f"FAKE_KEY={KEY_VALUE}\n"
    "\n"
    f"DB_URL={DB_VALUE}\n"
    "EMPTY=\n"
    "# trailing comment\n"
)
SECRETS = (KEY_VALUE, DB_VALUE)


def run(*args):
    return CliRunner().invoke(cli, list(args))


def write(project, rel, text, mode=None):
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    if mode is not None:
        path.chmod(mode)
    return path


def manifest(project):
    return json.loads((project / ".vaultshield" / "protect.json").read_text())


def records(fake_keyring):
    """account name (after the project id) -> list of original lines"""
    return {
        account.split(":", 1)[1]: decode_record(value)
        for (_service, account), value in fake_keyring.items.items()
    }


def test_protect_replaces_values_and_keeps_order_comments_and_blank_lines(protect_project):
    env = write(protect_project, ".env", ENV_TEXT)

    result = run("protect")

    assert result.exit_code == 0, result.output
    assert env.read_text() == (
        "# header comment\n"
        f"FAKE_KEY={PLACEHOLDER}\n"
        "\n"
        f"DB_URL={PLACEHOLDER}\n"
        "EMPTY=\n"
        "# trailing comment\n"
    )
    assert "Protected .env: FAKE_KEY, DB_URL" in result.output
    assert "2 variables moved to the Keychain." in result.output


def test_protect_stores_each_variable_in_the_keychain(protect_project, fake_keyring):
    write(protect_project, ".env", ENV_TEXT)
    run("protect")

    project_id = manifest(protect_project)["project_id"]
    assert {(s, a.split(":", 1)[0]) for (s, a) in fake_keyring.items} == {("vaultshield", project_id)}
    assert records(fake_keyring) == {
        ".env:FAKE_KEY": [f"FAKE_KEY={KEY_VALUE}"],
        ".env:DB_URL": [f"DB_URL={DB_VALUE}"],
    }


def test_the_manifest_holds_names_and_paths_only(protect_project):
    write(protect_project, ".env", ENV_TEXT)
    write(protect_project, "backend/.env.local", "TOKEN=zzzz-fake-token-9999\n")
    run("protect")

    text = (protect_project / ".vaultshield" / "protect.json").read_text()
    data = json.loads(text)
    assert data["version"] == 1
    assert data["protected"] == {
        ".env": ["FAKE_KEY", "DB_URL"],
        "backend/.env.local": ["TOKEN"],
    }
    assert data["detected"] == []
    assert set(data) == {"version", "project_id", "protected", "detected"}
    for secret in (*SECRETS, "zzzz-fake-token-9999"):
        assert secret not in text


def test_protect_is_idempotent(protect_project, fake_keyring):
    env = write(protect_project, ".env", ENV_TEXT)
    run("protect")
    manifest_file = protect_project / ".vaultshield" / "protect.json"
    snapshot = (
        env.read_bytes(),
        env.stat().st_mtime_ns,
        manifest_file.read_bytes(),
        manifest_file.stat().st_mtime_ns,
        dict(fake_keyring.items),
    )

    result = run("protect")

    assert result.exit_code == 0, result.output
    assert "Unchanged .env" in result.output
    assert "0 variables moved" in result.output
    assert (
        env.read_bytes(),
        env.stat().st_mtime_ns,
        manifest_file.read_bytes(),
        manifest_file.stat().st_mtime_ns,
        dict(fake_keyring.items),
    ) == snapshot


def test_values_are_verified_in_the_keychain_before_the_file_is_touched(protect_project, monkeypatch):
    class Lossy(InMemoryKeyring):
        def set_password(self, service, username, password):
            super().set_password(service, username, password + "x")

    import keyring.core

    lossy = Lossy()
    monkeypatch.setattr(keyring.core, "_keyring_backend", lossy)
    env = write(protect_project, ".env", ENV_TEXT)

    result = run("protect")

    assert result.exit_code == 1
    assert "Refused .env" in result.output
    assert "read-back" in result.output
    assert env.read_text() == ENV_TEXT
    assert lossy.items == {}
    # nothing is recorded as protected; the file still holds real values, so it
    # is recorded as detected and the deny rules and hook will cover it
    assert manifest(protect_project)["protected"] == {}
    assert manifest(protect_project)["detected"] == [".env"]


def test_a_failed_file_write_rolls_back_the_keychain_and_the_manifest(
    protect_project, fake_keyring, monkeypatch
):
    env = write(protect_project, ".env", ENV_TEXT)

    def boom(path, data, mode=None):
        raise OSError("disk full")

    monkeypatch.setattr(protector, "atomic_write_bytes", boom)
    result = run("protect")

    assert result.exit_code == 1
    assert "writing the file failed" in result.output
    assert env.read_text() == ENV_TEXT
    assert fake_keyring.items == {}
    assert manifest(protect_project)["protected"] == {}
    assert manifest(protect_project)["detected"] == [".env"]


def test_a_failed_rewrite_restores_the_previous_keychain_value(
    protect_project, fake_keyring, monkeypatch
):
    env = write(protect_project, ".env", "API_KEY=old-fake-value-1111\n")
    run("protect")
    before = dict(fake_keyring.items)
    env.write_text("API_KEY=new-fake-value-2222\n")

    monkeypatch.setattr(protector, "atomic_write_bytes", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    result = run("protect")

    assert result.exit_code == 1
    assert fake_keyring.items == before
    assert env.read_text() == "API_KEY=new-fake-value-2222\n"


def test_no_temp_files_are_left_behind(protect_project):
    write(protect_project, ".env", ENV_TEXT)
    run("protect")
    leftovers = [p.name for p in protect_project.rglob("*.tmp")]
    assert leftovers == []


def test_a_rotated_value_replaces_the_stored_one(protect_project, fake_keyring):
    env = write(protect_project, ".env", "API_KEY=old-fake-value-1111\n")
    run("protect")
    env.write_text("API_KEY=new-fake-value-2222\n")

    result = run("protect")

    assert result.exit_code == 0, result.output
    assert env.read_text() == f"API_KEY={PLACEHOLDER}\n"
    assert records(fake_keyring) == {".env:API_KEY": ["API_KEY=new-fake-value-2222"]}


def test_a_new_variable_in_a_protected_file_is_added(protect_project, fake_keyring):
    env = write(protect_project, ".env", "ONE=fake-one-1111\n")
    run("protect")
    env.write_text(f"ONE={PLACEHOLDER}\nTWO=fake-two-2222\n")

    run("protect")

    assert env.read_text() == f"ONE={PLACEHOLDER}\nTWO={PLACEHOLDER}\n"
    assert manifest(protect_project)["protected"] == {".env": ["ONE", "TWO"]}
    assert set(records(fake_keyring)) == {".env:ONE", ".env:TWO"}


def test_duplicate_keys_share_one_item_holding_both_lines(protect_project, fake_keyring):
    env = write(protect_project, ".env", "DUP=first-fake-1111\nOTHER=x-fake-0000\nDUP=second-fake-2222\n")

    run("protect")

    assert env.read_text() == f"DUP={PLACEHOLDER}\nOTHER={PLACEHOLDER}\nDUP={PLACEHOLDER}\n"
    assert records(fake_keyring)[".env:DUP"] == ["DUP=first-fake-1111", "DUP=second-fake-2222"]
    assert manifest(protect_project)["protected"][".env"] == ["DUP", "OTHER"]


def test_crlf_and_a_missing_final_newline_are_kept(protect_project):
    env = write(protect_project, ".env", "A=fake-aaaa-1111\r\n# c\r\nB=fake-bbbb-2222")

    run("protect")

    assert env.read_bytes() == f"A={PLACEHOLDER}\r\n# c\r\nB={PLACEHOLDER}".encode()


def test_file_permissions_are_kept(protect_project):
    env = write(protect_project, ".env", ENV_TEXT, mode=0o600)

    run("protect")

    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_files_in_subdirectories_use_their_relative_path_as_the_account(protect_project, fake_keyring):
    write(protect_project, "backend/.env", "X=fake-x-value-1\n")

    run("protect")

    assert set(records(fake_keyring)) == {"backend/.env:X"}
    assert manifest(protect_project)["protected"] == {"backend/.env": ["X"]}


def test_empty_values_are_left_alone_and_nothing_is_stored(protect_project, fake_keyring):
    env = write(protect_project, ".env", "A=\nB=\"\"\nC=   \n")

    result = run("protect")

    assert result.exit_code == 0
    assert "Unchanged .env" in result.output
    assert env.read_text() == "A=\nB=\"\"\nC=   \n"
    assert fake_keyring.items == {}
    assert not (protect_project / ".vaultshield").exists()


@pytest.mark.parametrize(
    "text, bad_lines",
    [
        (f"export FAKE_KEY={KEY_VALUE}\n", [1]),
        (f"OK=fine-fake-1\nKEY: {KEY_VALUE}\n", [2]),
        (f'PEM="-----BEGIN FAKE\n{KEY_VALUE}\n-----END FAKE"\n', [2, 3]),
    ],
)
def test_unparseable_files_are_refused_untouched_and_still_reported(protect_project, text, bad_lines):
    refused = write(protect_project, ".env.bad", text)
    good = write(protect_project, ".env", f"FAKE_KEY={KEY_VALUE}\n")

    result = run("protect")

    assert result.exit_code == 1
    assert refused.read_text() == text
    assert good.read_text() == f"FAKE_KEY={PLACEHOLDER}\n"
    assert "Refused .env.bad" in result.output
    assert ("line " if len(bad_lines) == 1 else "lines ") + ", ".join(map(str, bad_lines)) in result.output
    assert KEY_VALUE not in result.output
    assert manifest(protect_project)["detected"] == [".env.bad"]
    assert manifest(protect_project)["protected"] == {".env": ["FAKE_KEY"]}


def test_a_file_that_is_not_utf8_is_refused(protect_project):
    raw = b"KEY=\xff\xfe-fake-bytes\n"
    env = write(protect_project, ".env", raw)

    result = run("protect")

    assert result.exit_code == 1
    assert "not valid UTF-8" in result.output
    assert env.read_bytes() == raw


def test_a_lone_carriage_return_is_refused_because_the_parsers_disagree(protect_project):
    raw = "A=fake-aaaa-1111\rB=fake-bbbb-2222\n"
    env = write(protect_project, ".env", raw)

    result = run("protect")

    assert result.exit_code == 1
    assert "unusual line separators" in result.output
    assert env.read_bytes() == raw.encode()


def test_a_variable_that_is_both_protected_and_not_is_refused(protect_project, fake_keyring):
    env = write(protect_project, ".env", f"K={PLACEHOLDER}\nK=fake-real-value-1\n")

    result = run("protect")

    assert result.exit_code == 1
    assert "partly protected" in result.output
    assert env.read_text() == f"K={PLACEHOLDER}\nK=fake-real-value-1\n"
    assert fake_keyring.items == {}


def test_key_and_certificate_files_are_reported_but_never_moved(protect_project, fake_keyring):
    pem = write(protect_project, "certs/server.pem", "-----BEGIN FAKE KEY-----\nAAAA\n")
    rsa = write(protect_project, "id_rsa", "-----BEGIN FAKE RSA-----\nBBBB\n")
    npmrc = write(protect_project, ".npmrc", "//registry.example/:_authToken=fake-npm-token\n")
    before = {p: p.read_bytes() for p in (pem, rsa, npmrc)}

    result = run("protect")

    assert result.exit_code == 0, result.output
    assert {p: p.read_bytes() for p in before} == before
    assert fake_keyring.items == {}
    assert "Detected, not moved" in result.output
    assert manifest(protect_project) == {
        "version": 1,
        "project_id": manifest(protect_project)["project_id"],
        "protected": {},
        "detected": [".npmrc", "certs/server.pem", "id_rsa"],
    }


def test_the_detected_list_is_kept_in_sync_on_a_second_run(protect_project):
    pem = write(protect_project, "server.pem", "-----BEGIN FAKE-----\n")
    run("protect")
    assert manifest(protect_project)["detected"] == ["server.pem"]
    pem.unlink()

    result = run("protect")

    assert result.exit_code == 0
    assert "No secret files found." in result.output
    assert manifest(protect_project)["detected"] == []


def test_no_value_appears_in_the_output_or_any_project_file(protect_project):
    write(protect_project, ".env", ENV_TEXT)
    write(protect_project, ".env.bad", f"export X={KEY_VALUE}\n")

    result = run("protect")
    dry = run("protect", "--dry-run")

    for secret in SECRETS:
        assert secret not in result.output
        assert secret not in dry.output
    for path in (protect_project / ".vaultshield").rglob("*"):
        if path.is_file():
            assert not any(s.encode() in path.read_bytes() for s in SECRETS)
    assert KEY_VALUE not in (protect_project / ".env").read_text()


def test_dry_run_reports_variable_counts_and_refusals_and_changes_nothing(protect_project, fake_keyring):
    write(protect_project, ".env", ENV_TEXT)
    write(protect_project, ".env.bad", "export X=fake-1\n")
    write(protect_project, ".env.local", "A=\n")
    before = {p: p.read_bytes() for p in protect_project.iterdir()}

    result = run("protect", "--dry-run")

    assert result.exit_code == 0
    assert ".env  (2 variables)" in result.output
    assert "Cannot protect" in result.output
    assert ".env.bad" in result.output and "line 1" in result.output
    assert "Already protected, or no values to protect" in result.output
    assert {p: p.read_bytes() for p in protect_project.iterdir()} == before
    assert fake_keyring.items == {}
    assert not (protect_project / ".vaultshield").exists()


def test_a_tampered_manifest_path_is_rejected(protect_project):
    write(protect_project, ".env", ENV_TEXT)
    run("protect")
    path = protect_project / ".vaultshield" / "protect.json"
    data = json.loads(path.read_text())
    data["protected"]["../outside/.env"] = ["X"]
    path.write_text(json.dumps(data))

    result = run("protect")

    assert result.exit_code == 1
    assert "invalid protected entry" in result.output


def test_protect_leaves_files_it_does_not_manage_alone(protect_project):
    readme = write(protect_project, "README.md", "hello\n")
    write(protect_project, ".env", ENV_TEXT)

    run("protect")

    assert readme.read_text() == "hello\n"
    assert sorted(p.name for p in protect_project.iterdir()) == [".env", ".vaultshield", "README.md"]
