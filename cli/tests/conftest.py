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
def protect_project(tmp_path, monkeypatch):
    """A fresh, empty project directory for the Protect tests.

    HOME points at an empty temporary directory and the working directory is
    the project, so nothing a Protect test does can reach the real home
    directory, `~/.gemini`, or a real project. Values used in these tests are
    fake.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project
