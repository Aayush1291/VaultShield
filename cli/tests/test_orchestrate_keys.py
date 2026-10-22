"""Step 1: the model API key. Fake keys, the in-memory Keychain, no network."""

import click
import keyring.core
import pytest
from click.testing import CliRunner
from keyring.errors import KeyringError

from vaultshield.cli import cli
from vaultshield.orchestrate import commands, keys
from vaultshield.protect.vault import InMemoryKeyring

FAKE_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345wxyz"
OTHER_KEY = "AIzaSyOTHEROTHEROTHEROTHEROTHER6789abcd"

pytestmark = pytest.mark.usefixtures("no_real_model_key")


def run(*args, input=None, env=None):
    return CliRunner().invoke(cli, list(args), input=input, env=env)


# --- set-key ------------------------------------------------------------------


def test_set_key_stores_the_key_in_the_keychain_under_the_agreed_names(fake_keyring):
    result = run("config", "set-key", "gemini", input=f"{FAKE_KEY}\n")

    assert result.exit_code == 0, result.output
    assert fake_keyring.items == {("vaultshield", "model-key:gemini"): FAKE_KEY}


def test_the_key_is_read_at_a_hidden_prompt_and_never_echoed(fake_keyring, monkeypatch):
    seen = {}
    real_prompt = click.prompt

    def spy(text, **kwargs):
        seen.update(kwargs, text=text)
        return real_prompt(text, **kwargs)

    monkeypatch.setattr(commands.click, "prompt", spy)

    result = run("config", "set-key", "gemini", input=f"{FAKE_KEY}\n")

    assert seen["hide_input"] is True
    assert "hidden" in seen["text"]
    assert FAKE_KEY not in result.output
    assert FAKE_KEY not in result.stderr


def test_control_a_visible_prompt_would_have_echoed_the_key():
    """Positive control for the test above: in this harness a normal prompt DOES put
    the typed text in the output, so 'not in output' there is a real check."""

    @click.command()
    def visible():
        click.prompt("key")

    result = CliRunner().invoke(visible, input=f"{FAKE_KEY}\n")

    assert FAKE_KEY in result.output


def test_a_key_on_the_command_line_is_refused_and_not_repeated(fake_keyring):
    result = run("config", "set-key", "gemini", FAKE_KEY)

    assert result.exit_code == 2
    assert "Do not put the key on the command line" in result.output
    assert FAKE_KEY not in result.output
    assert fake_keyring.items == {}


def test_surrounding_whitespace_is_removed(fake_keyring):
    run("config", "set-key", "gemini", input=f"  {FAKE_KEY}\t \n")
    assert fake_keyring.items[("vaultshield", "model-key:gemini")] == FAKE_KEY


def test_an_empty_entry_stores_nothing(fake_keyring):
    result = run("config", "set-key", "gemini", input="\n")

    assert result.exit_code == 1
    assert "No key was entered" in result.output
    assert fake_keyring.items == {}


def test_a_new_key_replaces_the_old_one(fake_keyring):
    run("config", "set-key", "gemini", input=f"{FAKE_KEY}\n")
    run("config", "set-key", "gemini", input=f"{OTHER_KEY}\n")
    assert fake_keyring.items == {("vaultshield", "model-key:gemini"): OTHER_KEY}


def test_set_key_only_knows_gemini(fake_keyring):
    assert run("config", "set-key", "openai", input="x\n").exit_code == 2


class _BrokenKeychain(InMemoryKeyring):
    def set_password(self, service, username, password):
        raise KeyringError(f"boom {password}")

    def get_password(self, service, username):
        raise KeyringError("locked")


def test_a_keychain_failure_is_reported_without_the_key(monkeypatch):
    monkeypatch.setattr(keyring.core, "_keyring_backend", _BrokenKeychain())

    result = run("config", "set-key", "gemini", input=f"{FAKE_KEY}\n")

    assert result.exit_code == 1
    assert "Could not write to the Keychain (KeyringError)" in result.output
    assert FAKE_KEY not in result.output


# --- show-key -----------------------------------------------------------------


def test_show_key_says_whether_a_key_exists_and_shows_only_the_last_four(fake_keyring):
    run("config", "set-key", "gemini", input=f"{FAKE_KEY}\n")

    result = run("config", "show-key", "gemini")

    assert result.exit_code == 0
    assert result.output == "Gemini API key: set (from the Keychain), ends with wxyz\n"
    assert FAKE_KEY not in result.output
    assert FAKE_KEY[:-4] not in result.output


def test_show_key_with_no_key_says_how_to_set_one(fake_keyring):
    result = run("config", "show-key", "gemini")

    assert result.exit_code == 1
    assert result.output == "Gemini API key: not set. Run: vaultshield config set-key gemini\n"


def test_show_key_reports_the_environment_variable_when_that_is_the_one_in_use(fake_keyring):
    result = run("config", "show-key", "gemini", env={"GEMINI_API_KEY": OTHER_KEY})

    assert result.output == "Gemini API key: set (from the environment variable GEMINI_API_KEY), ends with abcd\n"
    assert OTHER_KEY not in result.output


def test_a_key_too_short_to_show_any_of_is_not_shown_at_all(fake_keyring):
    result = run("config", "show-key", "gemini", env={"GEMINI_API_KEY": "abc12345"[:7]})

    assert "too short to show any of it" in result.output
    assert "abc1234" not in result.output


def test_show_key_changes_nothing(fake_keyring):
    run("config", "set-key", "gemini", input=f"{FAKE_KEY}\n")
    before = dict(fake_keyring.items)
    run("config", "show-key", "gemini")
    assert fake_keyring.items == before


# --- resolution order ---------------------------------------------------------


def test_the_environment_variable_wins_over_the_keychain(fake_keyring):
    fake_keyring.items[("vaultshield", "model-key:gemini")] = FAKE_KEY

    resolved = keys.resolve_key("gemini", {"GEMINI_API_KEY": OTHER_KEY})

    assert resolved.value == OTHER_KEY and resolved.source == "environment"


def test_the_keychain_is_used_when_the_environment_has_no_key(fake_keyring):
    fake_keyring.items[("vaultshield", "model-key:gemini")] = FAKE_KEY

    resolved = keys.resolve_key("gemini", {})

    assert resolved.value == FAKE_KEY and resolved.source == "keychain"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_environment_variable_counts_as_unset(fake_keyring, blank):
    fake_keyring.items[("vaultshield", "model-key:gemini")] = FAKE_KEY
    assert keys.resolve_key("gemini", {"GEMINI_API_KEY": blank}).source == "keychain"


def test_no_key_anywhere_is_none_and_require_key_gives_a_one_line_fix(fake_keyring):
    assert keys.resolve_key("gemini", {}) is None

    with pytest.raises(keys.ModelKeyError) as excinfo:
        keys.require_key("gemini", {})

    assert str(excinfo.value) == "No Gemini API key found. Run: vaultshield config set-key gemini"
    assert "\n" not in str(excinfo.value)


def test_the_real_process_environment_is_used_by_default(fake_keyring, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", OTHER_KEY)
    assert keys.resolve_key("gemini").value == OTHER_KEY


def test_a_keychain_that_cannot_be_read_is_an_error_without_a_key(monkeypatch):
    monkeypatch.setattr(keyring.core, "_keyring_backend", _BrokenKeychain())

    with pytest.raises(keys.ModelKeyError) as excinfo:
        keys.resolve_key("gemini", {})

    assert "Could not read the Keychain" in str(excinfo.value)
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_a_key_in_the_environment_works_even_when_the_keychain_is_broken(monkeypatch):
    monkeypatch.setattr(keyring.core, "_keyring_backend", _BrokenKeychain())
    assert keys.resolve_key("gemini", {"GEMINI_API_KEY": FAKE_KEY}).value == FAKE_KEY


def test_the_key_never_shows_in_the_text_form_of_a_resolved_key():
    resolved = keys.ResolvedKey("gemini", "keychain", FAKE_KEY)
    for text in (repr(resolved), str(resolved), f"{resolved}", f"{resolved!r}"):
        assert FAKE_KEY not in text


def test_the_account_name_cannot_collide_with_a_project_secret():
    # project secrets are "<project-uuid>:<file>:<VAR>"; this one has two parts
    assert keys.account_for("gemini") == "model-key:gemini"
    assert keys.account_for("gemini").count(":") == 1
