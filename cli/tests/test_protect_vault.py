"""Step 2: the Keychain vault, tested against an in-memory fake backend."""

import keyring
import keyring.core
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordSetError

from vaultshield.protect.vault import (
    SERVICE,
    InMemoryKeyring,
    Vault,
    VaultError,
    account_name,
)

SECRET = "sk-test-AAAA1111BBBB2222"


def test_service_and_account_format():
    assert SERVICE == "vaultshield"
    assert account_name("pid-1", "backend/.env", "API_KEY") == "pid-1:backend/.env:API_KEY"


def test_store_fetch_remove_roundtrip(fake_keyring):
    vault = Vault("pid-1")
    vault.store(".env", "FAKE_KEY", SECRET)

    assert vault.fetch(".env", "FAKE_KEY") == SECRET
    assert fake_keyring.items == {("vaultshield", "pid-1:.env:FAKE_KEY"): SECRET}

    vault.remove(".env", "FAKE_KEY")
    assert vault.fetch(".env", "FAKE_KEY") is None
    assert fake_keyring.items == {}


def test_fetch_of_a_missing_item_is_none(fake_keyring):
    assert Vault("pid-1").fetch(".env", "NOPE") is None


def test_remove_of_a_missing_item_is_not_an_error(fake_keyring):
    Vault("pid-1").remove(".env", "NOPE")


def test_store_overwrites_an_existing_item(fake_keyring):
    vault = Vault("pid-1")
    vault.store(".env", "K", "one")
    vault.store(".env", "K", "two")
    assert vault.fetch(".env", "K") == "two"


def test_items_are_separated_by_project_and_file(fake_keyring):
    Vault("pid-1").store(".env", "K", "a")
    Vault("pid-2").store(".env", "K", "b")
    Vault("pid-1").store("backend/.env", "K", "c")

    assert Vault("pid-1").fetch(".env", "K") == "a"
    assert Vault("pid-2").fetch(".env", "K") == "b"
    assert Vault("pid-1").fetch("backend/.env", "K") == "c"


def test_store_verified_succeeds_when_the_read_back_matches(fake_keyring):
    Vault("pid-1").store_verified(".env", "K", SECRET)
    assert Vault("pid-1").fetch(".env", "K") == SECRET


class _LossyKeyring(InMemoryKeyring):
    """Accepts writes but stores something else."""

    def set_password(self, service, username, password):
        super().set_password(service, username, password[:-1])


def test_store_verified_detects_a_backend_that_changes_the_value(monkeypatch):
    monkeypatch.setattr(keyring.core, "_keyring_backend", _LossyKeyring())

    with pytest.raises(VaultError) as excinfo:
        Vault("pid-1").store_verified(".env", "K", SECRET)

    assert SECRET not in str(excinfo.value)
    assert "read-back" in str(excinfo.value)


class _DroppingKeyring(InMemoryKeyring):
    """Accepts writes and keeps nothing."""

    def set_password(self, service, username, password):
        return None


def test_store_verified_detects_a_backend_that_drops_the_write(monkeypatch):
    monkeypatch.setattr(keyring.core, "_keyring_backend", _DroppingKeyring())

    with pytest.raises(VaultError):
        Vault("pid-1").store_verified(".env", "K", SECRET)


class _FailingKeyring(InMemoryKeyring):
    def set_password(self, service, username, password):
        raise PasswordSetError(f"could not write {password}")

    def get_password(self, service, username):
        raise PasswordSetError("locked")


def test_backend_errors_become_vault_errors_without_the_value(monkeypatch):
    monkeypatch.setattr(keyring.core, "_keyring_backend", _FailingKeyring())

    with pytest.raises(VaultError) as write_error:
        Vault("pid-1").store(".env", "K", SECRET)
    with pytest.raises(VaultError) as read_error:
        Vault("pid-1").fetch(".env", "K")

    # keyring's own message above contains the value; ours must not.
    assert SECRET not in str(write_error.value)
    assert "PasswordSetError" in str(write_error.value)
    assert write_error.value.__cause__ is None
    assert SECRET not in repr(write_error.value)
    assert "Keychain read failed" in str(read_error.value)


def test_vault_never_prints_or_logs_values(fake_keyring, capsys, caplog):
    caplog.set_level("DEBUG")
    vault = Vault("pid-1")
    vault.store_verified(".env", "K", SECRET)
    vault.fetch(".env", "K")
    vault.remove(".env", "K")

    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert SECRET not in caplog.text
    assert SECRET not in repr(vault)


def test_the_fake_backend_is_never_selected_automatically():
    assert InMemoryKeyring.viable is False
    assert InMemoryKeyring not in list(KeyringBackend.get_viable_backends())
    assert not any(isinstance(k, InMemoryKeyring) for k in keyring.backend.get_all_keyring())


def test_protect_project_fixture_uses_the_fake_keychain(protect_project, fake_keyring):
    assert keyring.get_keyring() is fake_keyring
