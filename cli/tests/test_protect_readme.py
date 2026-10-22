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


def test_the_readme_separates_printing_from_installing_into_antigravity():
    text = README.read_text()
    assert "`vaultshield agents render antigravity` **only prints**" in text
    assert "merge-only" in text.lower()
    assert "byte for byte" in text
    assert ".vaultshield.bak" in text


def test_the_readme_says_what_is_not_proven_about_the_app_hook_and_install():
    text = README.read_text()
    assert "Not proven yet: that the App calls the hook" in text
    assert "spike/hook_proof.sh" in text
    assert "Deny rules alone are not enough" in text
    assert "has not been run against a real `~/.gemini`" in " ".join(text.split())


def test_the_readme_documents_every_command():
    text = README.read_text()
    for command in (
        "vaultshield protect --dry-run",
        "vaultshield protect ",
        "vaultshield status",
        "vaultshield exec",
        "vaultshield unprotect",
        "vaultshield agents render antigravity",
        "vaultshield agents install antigravity",
        "vaultshield agents uninstall antigravity",
        "vaultshield agents doctor antigravity",
        "vaultshield hook antigravity",
    ):
        assert command in text, command
