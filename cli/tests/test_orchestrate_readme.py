"""Step 8: the README must keep saying what is sent, what is not, and where the
protection is only a heuristic, so those lines cannot be dropped quietly. It must also
agree with the code on the facts a reader would rely on."""

from pathlib import Path

from vaultshield.orchestrate import commands, context, keys, provider

README = Path(__file__).resolve().parents[1] / "vaultshield" / "orchestrate" / "README.md"


def text() -> str:
    return " ".join(README.read_text().split())


def test_it_states_the_one_rule_and_that_no_agent_is_started():
    t = text()
    assert "the model never gets a secret value or the contents of a secret file" in t
    assert "does **not** start any agent" in t


def test_it_lists_what_is_sent_including_the_names_of_secret_files():
    t = text()
    assert "What is sent to the model" in t
    assert "**This includes the names of your secret files**" in t
    assert "at most 200 entries" in t and "first 4000 characters of the README" in t
    assert "last 10 git commit subjects" in t


def test_it_lists_what_is_never_sent_and_how_that_is_enforced():
    t = text()
    assert "What is never sent" in t and "never opened" in t
    assert "`assert_clean`" in t and "nothing is sent and nothing is saved" in t
    assert "cut to 4000 characters **after** sanitizing" in t


def test_it_says_plainly_that_the_sanitizer_is_not_a_guarantee():
    t = text()
    assert "Honest limits" in t
    assert "It is not a guarantee" in t
    assert "unusual shape" in t and "slip through" in t
    assert "over-redacts" in t


def test_it_says_nothing_was_checked_against_the_live_service():
    t = text()
    assert "What has not been checked against the real service" in t
    assert "mocked HTTP transport" in t
    assert "spike/orchestrator_smoke.sh" in t


def test_it_warns_that_the_model_output_goes_into_the_prompt():
    assert "is **written into the prompt**" in text()


def test_the_constraint_in_the_readme_is_the_one_in_the_code():
    assert commands.CONSTRAINT in text().replace("> ", "")


def test_the_facts_in_the_readme_match_the_code():
    t = text()
    assert provider.DEFAULT_MODEL in t
    assert "x-goog-api-key" in t and "`store: false`" in t
    assert f"account `{keys.account_for('gemini')}`" in t
    assert str(context.README_MAX_CHARS) in t and str(context.TREE_MAX_ENTRIES) in t
    assert f"last {context.COMMIT_COUNT} git commit subjects" in t
    for skipped in sorted(context.TREE_SKIP):
        assert f"`{skipped}`" in t, skipped
    assert provider.LEDGER_NAME in t
    for flag in ("--yes", "--answers", "--copy"):
        assert flag in t


def test_every_command_in_the_usage_exists():
    from vaultshield.cli import cli

    assert {"set-key", "show-key"} <= set(cli.commands["config"].commands)
    assert "ask" in cli.commands
