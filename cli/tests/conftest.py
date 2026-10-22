"""Shared pytest configuration and fixtures for the VaultShield CLI tests.

Registers a Hypothesis profile so property-based tests written in later
tasks have consistent settings without each test file configuring its own.
"""

import os

import pytest
from hypothesis import settings

settings.register_profile("default", max_examples=100, deadline=None)
settings.register_profile("ci", max_examples=200, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture
def fake_keyring(monkeypatch):
    """Replaces the Keychain with an in-memory fake, so no Protect test can
    read or write the real macOS Keychain. Returns the fake."""
    import keyring
    import keyring.core

    from vaultshield.protect.vault import InMemoryKeyring

    fake = InMemoryKeyring()
    monkeypatch.setattr(keyring.core, "_keyring_backend", fake)
    return fake


@pytest.fixture
def no_real_model_key(monkeypatch):
    """A developer's shell may have a real GEMINI_API_KEY exported. No Orchestrate
    test may see it: remove it before anything runs."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture
def protect_project(tmp_path, monkeypatch, fake_keyring):
    """A fresh, empty project directory for the Protect tests.

    HOME points at an empty temporary directory, the Keychain is the in-memory
    fake, and the working directory is the project, so nothing a Protect test
    does can reach the real home directory, `~/.gemini`, the real Keychain, or
    a real project. Values used in these tests are fake.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project
