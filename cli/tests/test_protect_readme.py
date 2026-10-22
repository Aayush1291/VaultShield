"""Step 9: the README must keep stating the limits that the tests prove, so they
cannot be quietly dropped from the docs."""

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "vaultshield" / "protect" / "README.md"


def test_the_readme_states_the_scrubber_limits_plainly():
    text = README.read_text()
    assert "Honest limits" in text
    assert "base64-encoded is not scrubbed" in text
    assert "not a safety boundary" in text


def test_the_readme_says_the_real_keychain_was_not_tested():
    text = README.read_text()
    assert "this was not tested" in text.lower()
    assert "in-memory fake" in text


def test_the_readme_says_nothing_is_installed_into_antigravity():
    text = README.read_text()
    assert "**only prints**" in text
    assert "not done by this tool" in text


def test_the_readme_documents_every_command():
    text = README.read_text()
    for command in (
        "vaultshield protect --dry-run",
        "vaultshield protect ",
        "vaultshield status",
        "vaultshield exec",
        "vaultshield unprotect",
        "vaultshield agents render antigravity",
        "vaultshield hook antigravity",
    ):
        assert command in text, command
