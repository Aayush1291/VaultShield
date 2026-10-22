"""Step 7: `vaultshield status` shows names only (all values are fake)."""

import os

from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.protect.vault import InMemoryKeyring

KEY_VALUE = "sk-test-AAAA1111BBBB2222"
DB_VALUE = "postgres://u:p@localhost/db"


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_status_lists_protected_files_and_variable_names_only(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\n")
    (protect_project / "backend").mkdir()
    (protect_project / "backend" / ".env.local").write_text("TOKEN=fake-token-9999\n")
    run("protect")

    result = run("status")

    assert result.exit_code == 0, result.output
    assert result.output == (
        f"Project: {protect_project.resolve()}\n"
        "Protected (values are in the Keychain):\n"
        "  .env\n"
        "    FAKE_KEY\n"
        "    DB_URL\n"
        "  backend/.env.local\n"
        "    TOKEN\n"
    )


def test_status_never_shows_a_value_and_never_reads_the_keychain(protect_project, monkeypatch):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\n")
    run("protect")

    def forbidden(self, service, username):
        raise AssertionError("status must not read the Keychain")

    monkeypatch.setattr(InMemoryKeyring, "get_password", forbidden)
    result = run("status")

    assert result.exit_code == 0, result.output
    assert KEY_VALUE not in result.output
    assert DB_VALUE not in result.output
    assert "postgres" not in result.output


def test_status_also_lists_detected_files_that_were_not_moved(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    (protect_project / "id_rsa").write_text("-----BEGIN FAKE RSA-----\n")
    (protect_project / ".env.bad").write_text(f"export X={KEY_VALUE}\n")
    run("protect")

    result = run("status")

    assert "  .env\n    FAKE_KEY\n" in result.output
    assert "Detected, not moved (their real contents are still on disk):\n  .env.bad\n  id_rsa\n" in result.output
    assert KEY_VALUE not in result.output


def test_status_with_only_detected_files(protect_project):
    (protect_project / "server.pem").write_text("-----BEGIN FAKE-----\n")
    run("protect")

    result = run("status")

    assert "Protected: none" in result.output
    assert "  server.pem" in result.output


def test_status_when_nothing_is_protected(protect_project):
    result = run("status")
    assert result.exit_code == 0
    assert result.output == "Nothing is protected here.\n"


def test_status_works_from_a_subdirectory(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    (protect_project / "src").mkdir()
    run("protect")
    os.chdir(protect_project / "src")

    result = run("status")

    assert f"Project: {protect_project.resolve()}" in result.output
    assert "    FAKE_KEY" in result.output


def test_status_after_unprotect_shows_nothing_protected(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    run("protect")
    run("unprotect")

    assert run("status").output == "Nothing is protected here.\n"


def test_status_with_a_broken_manifest_is_an_error(protect_project):
    (protect_project / ".vaultshield").mkdir()
    (protect_project / ".vaultshield" / "protect.json").write_text("{nope")

    result = run("status")

    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_status_changes_nothing(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    run("protect")
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protect_project.rglob("*") if p.is_file()
    }

    run("status")

    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protect_project.rglob("*") if p.is_file()
    } == before
