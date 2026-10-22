"""The model API key: where it is stored and how it is found.

Stored in the macOS Keychain through `keyring`, service "vaultshield", account
"model-key:<provider>". Looked up in this order: the environment variable
(GEMINI_API_KEY), then the Keychain.

The full key is never printed, logged or put in an exception message. The only
part that is ever shown is its last 4 characters, and only for a key long enough
that 4 characters give little away.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

import keyring
from keyring.errors import KeyringError

from vaultshield.protect.vault import SERVICE

PROVIDER_GEMINI = "gemini"
PROVIDERS = (PROVIDER_GEMINI,)
LABELS = {PROVIDER_GEMINI: "Gemini"}
ENV_VARS = {PROVIDER_GEMINI: "GEMINI_API_KEY"}

SOURCE_ENVIRONMENT = "environment"
SOURCE_KEYCHAIN = "keychain"

# A key shorter than this is not given even 4 characters of itself.
MIN_LENGTH_TO_SHOW_SUFFIX = 8


class ModelKeyError(Exception):
    """There is no usable key, or the Keychain failed. Never contains a key."""


def account_for(provider: str) -> str:
    return f"model-key:{provider}"


def missing_key_message(provider: str) -> str:
    return f"No {LABELS[provider]} API key found. Run: vaultshield config set-key {provider}"


@dataclass(frozen=True)
class ResolvedKey:
    """A key and where it came from. Its text form never shows the key."""

    provider: str
    source: str
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return f"ResolvedKey(provider={self.provider!r}, source={self.source!r})"

    __str__ = __repr__

    @property
    def suffix(self) -> str | None:
        """The last 4 characters, or None if the key is too short to show any."""
        return self.value[-4:] if len(self.value) >= MIN_LENGTH_TO_SHOW_SUFFIX else None

    def describe(self) -> str:
        label = LABELS[self.provider]
        where = (
            f"the environment variable {ENV_VARS[self.provider]}"
            if self.source == SOURCE_ENVIRONMENT
            else "the Keychain"
        )
        tail = f", ends with {self.suffix}" if self.suffix else ", too short to show any of it"
        return f"{label} API key: set (from {where}){tail}"


def store_key(provider: str, key: str) -> None:
    """Puts `key` in the Keychain, replacing any earlier one."""
    try:
        keyring.set_password(SERVICE, account_for(provider), key)
    except KeyringError as exc:
        raise ModelKeyError(f"Could not write to the Keychain ({type(exc).__name__}).") from None


def resolve_key(provider: str, environ: Mapping[str, str] | None = None) -> ResolvedKey | None:
    """The key to use, or None if there is none: the environment variable
    first, then the Keychain. A blank environment variable counts as unset."""
    environ = os.environ if environ is None else environ
    from_env = environ.get(ENV_VARS[provider], "").strip()
    if from_env:
        return ResolvedKey(provider, SOURCE_ENVIRONMENT, from_env)
    try:
        stored = keyring.get_password(SERVICE, account_for(provider))
    except KeyringError as exc:
        raise ModelKeyError(
            f"Could not read the Keychain ({type(exc).__name__}). "
            f"Set {ENV_VARS[provider]} instead, or fix the Keychain."
        ) from None
    if stored and stored.strip():
        return ResolvedKey(provider, SOURCE_KEYCHAIN, stored.strip())
    return None


def require_key(provider: str, environ: Mapping[str, str] | None = None) -> ResolvedKey:
    """Like `resolve_key`, but a missing key is an error with a one-line fix."""
    resolved = resolve_key(provider, environ)
    if resolved is None:
        raise ModelKeyError(missing_key_message(provider))
    return resolved
