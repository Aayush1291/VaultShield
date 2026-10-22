"""The vault: secret values in the macOS Keychain, through the `keyring` library.

Every item has service `"vaultshield"` and account
`"<project-id>:<file>:<VAR>"`, where `<file>` is the project-relative posix
path of the file the variable came from.

Values are never logged, printed, or put in an exception message. Errors name
the operation and the keyring error class only.
"""

from __future__ import annotations

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "vaultshield"


class VaultError(Exception):
    """A Keychain operation failed. The message never contains a value."""


def account_name(project_id: str, file: str, var: str) -> str:
    return f"{project_id}:{file}:{var}"


class Vault:
    """Keychain items for one project."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def __repr__(self) -> str:
        return f"Vault(project_id={self.project_id!r})"

    def store(self, file: str, var: str, value: str) -> None:
        account = account_name(self.project_id, file, var)
        try:
            keyring.set_password(SERVICE, account, value)
        except KeyringError as exc:
            raise VaultError(f"Keychain write failed ({type(exc).__name__})") from None

    def fetch(self, file: str, var: str) -> str | None:
        """The stored value, or None if there is no such item."""
        account = account_name(self.project_id, file, var)
        try:
            return keyring.get_password(SERVICE, account)
        except KeyringError as exc:
            raise VaultError(f"Keychain read failed ({type(exc).__name__})") from None

    def remove(self, file: str, var: str) -> None:
        """Deletes the item. A missing item is not an error."""
        account = account_name(self.project_id, file, var)
        try:
            keyring.delete_password(SERVICE, account)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise VaultError(f"Keychain delete failed ({type(exc).__name__})") from None

    def store_verified(self, file: str, var: str, value: str) -> None:
        """Stores `value`, reads it back, and raises unless the read-back is
        exactly what was written."""
        self.store(file, var, value)
        if self.fetch(file, var) != value:
            raise VaultError("Keychain read-back did not match what was written")


class _NeverSelected:
    """A `priority` that raises, which keyring reads as 'not viable'. Keeps
    the test backend out of keyring's automatic backend selection."""

    def __get__(self, obj, owner):
        raise RuntimeError("InMemoryKeyring is only for tests and is never selected")


class InMemoryKeyring(KeyringBackend):
    """A fake Keychain for tests. Holds items in a dict and never touches the
    operating system. Install it with `keyring.set_keyring(InMemoryKeyring())`."""

    priority = _NeverSelected()

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError("no such item")
        del self.items[(service, username)]
