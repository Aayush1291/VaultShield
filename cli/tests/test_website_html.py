"""website/index.html is a single self-contained page: no external resources,
a working copy button, and a "Measured so far" checklist of only the claims that
were actually measured.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "website" / "index.html"

pytestmark = pytest.mark.skipif(not INDEX.exists(), reason="index.html not present")

# The exact claims the page is allowed to make, matching the task's measured facts.
MEASURED_CLAIMS = [
    "Placeholders held in the Antigravity CLI and App",
    "The command hook blocked printenv live in Antigravity",
    "Claude Code, Kiro and Codex backstops are built from docs, not yet live-tested",
]

INSTALL_LINE = "curl -fsSL http://localhost:8080/install.sh | sh"


def html() -> str:
    return INDEX.read_text()


def test_the_only_external_urls_are_the_localhost_install_line():
    urls = re.findall(r'https?://[^\s"\'<>)]+', html())
    assert urls, "expected at least the localhost install URL"
    for url in urls:
        assert url.startswith("http://localhost:8080"), f"unexpected external URL: {url}"


def test_no_external_resource_tags():
    text = html()
    # no stylesheet/script/font/image pulled from anywhere
    assert "<link" not in text
    assert not re.search(r"<script[^>]+src=", text)
    assert not re.search(r'src="https?://', text)
    assert "@import" not in text


def test_has_the_install_command_in_a_box_with_a_copy_button():
    text = html()
    assert INSTALL_LINE in text
    assert 'id="copy-btn"' in text
    assert "navigator.clipboard" in text  # clipboard API
    assert "execCommand" in text  # with a fallback


def test_every_measured_claim_is_in_a_checked_list():
    text = html()
    # pull the <li class="claim ..."> items out
    items = re.findall(r'<li class="claim[^"]*">(.*?)</li>', text, re.DOTALL)
    assert len(items) == len(MEASURED_CLAIMS), f"expected {len(MEASURED_CLAIMS)} claims, found {len(items)}"
    for claim in MEASURED_CLAIMS:
        matching = [item for item in items if claim in item]
        assert matching, f"claim not found in the list: {claim}"
        # each claim carries a check / status mark
        assert any('class="mark"' in item for item in matching), f"claim not marked: {claim}"


def test_mentions_honest_limits():
    text = html()
    assert "what it does not" in text.lower()
    # the honest same-user caveat
    assert "indirect tricks" in text
    assert "not a sandbox" in text


def test_is_mobile_and_dark_friendly():
    text = html()
    assert 'name="viewport"' in text
    assert "prefers-color-scheme" in text


def test_supported_agents_have_honest_status_labels():
    text = html()
    # "proven live" is claimed for exactly one agent: Antigravity.
    assert text.count("proven live") == 1
    assert re.search(r'Antigravity</span>\s*<span class="badge live">proven live', text), (
        "Antigravity should be the one agent marked proven live"
    )
    # The other three are honestly marked as built-from-docs, not live-tested.
    assert text.count("built from docs, not live-tested") == 3
    for agent in ("Claude Code", "Kiro", "Codex"):
        assert agent in text, f"missing agent: {agent}"


def test_is_macos_only_and_shows_the_secure_terminal_mock():
    text = html()
    assert "macOS only" in text
    # The CSS-only terminal mock runs `vaultshield secure` and shows the
    # placeholder the agent then reads.
    assert "vaultshield secure" in text
    assert "vs_protected" in text
    # The reduced-motion fallback must exist so the mock is not motion-only.
    assert "prefers-reduced-motion" in text
