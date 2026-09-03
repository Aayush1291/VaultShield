"""Shared pytest configuration and fixtures for the VaultShield CLI tests.

Registers a Hypothesis profile so property-based tests written in later
tasks have consistent settings without each test file configuring its own.
"""

import os

from hypothesis import settings

settings.register_profile("default", max_examples=100, deadline=None)
settings.register_profile("ci", max_examples=200, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
