"""Step 5: `vaultshield ask`, the whole flow, over a mocked Gemini service. Fake key,
in-memory Keychain, temporary HOME, no network."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.orchestrate import commands, provider as prov
from orchestrate_fakes import APPROACH, ENV_VALUE, FAKE_KEY, QUESTIONS, FakeGemini, completed
from vaultshield.orchestrate.commands import CONSTRAINT

@pytest.fixture(autouse=True)
def _no_real_key(no_real_model_key):
    pass


@pytest.fixture
def project(protect_project):
    (protect_project / "README.md").write_text("# Demo\n\nA small web app.\n")
    (protect_project / "package.json").write_text(json.dumps({"name": "demo", "dependencies": {"express": "^4.18.0"}}))
    (protect_project / ".env").write_text(f"DB_URL=postgres://u:p@localhost/db\nPLAIN={ENV_VALUE}\n")
    assert CliRunner().invoke(cli, ["protect"]).exit_code == 0
    return protect_project


@pytest.fixture
def gemini(monkeypatch):
    server = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    return server


def run(*args, input=None, env=None):
    return CliRunner().invoke(cli, list(args), input=input, env=env)


def saved_files(project: Path):
    return sorted((project / ".vaultshield" / "context").glob("*.md"))


# --- the flow ---------------------------------------------------------------------


def test_the_flow_asks_each_question_and_builds_the_prompt(project, gemini):
    result = run("ask", "Add a login page", input="Postgres\n\nYes please\n")

    assert result.exit_code == 0, result.output
    [saved] = saved_files(project)
    prompt = saved.read_text()
    assert result.stdout.endswith(prompt) or prompt in result.stdout
    assert gemini.kinds() == ["questions", "approach"]
    assert prompt.index("## Task") < prompt.index("## Project summary") < prompt.index("## Answers") < prompt.index("## Constraints") < prompt.index("## Suggested approach")
    assert "## Task\n\nAdd a login page" in prompt
    assert "- Which database should it use?\n  Postgres" in prompt
    assert "- Should tests be added?\n  Yes please" in prompt
    assert "Which login method" not in prompt  # skipped with Enter
    assert "1. Add a login route in src/app.js.\n2. Write a test.\n3. Update the README." in prompt


def test_the_constraint_text_is_there_word_for_word(project, gemini):
    run("ask", "Add a login page", input="\n\n\n")
    prompt = saved_files(project)[0].read_text()
    assert (
        "- Secret values are protected placeholders like vs_protected. Never ask for, print, or hardcode real secrets; "
        "refer to variables by NAME and read them from the environment. If a command needs real values, "
        "tell the user to run it with `vaultshield exec`."
    ) in prompt
    assert CONSTRAINT in prompt


def test_the_questions_go_to_stderr_and_the_prompt_alone_goes_to_stdout(project, gemini):
    result = run("ask", "Add a login page", input="a\nb\nc\n")

    assert "[1/3] Which database should it use?" in result.stderr
    assert "why: It changes the queries." in result.stderr
    assert "Press Enter to skip" in result.stderr
    assert re.search(r"Saved: .*\.vaultshield/context/\d{8}-\d{6}\.md", result.stderr)
    assert result.stdout.startswith("## Task")  # so `vaultshield ask ... | pbcopy` copies just the prompt
    assert "[1/3]" not in result.stdout


def test_the_saved_file_is_named_by_timestamp_and_never_overwritten(project, gemini, monkeypatch):
    monkeypatch.setattr(commands, "_now", lambda: datetime(2026, 10, 22, 9, 30, 5, tzinfo=timezone.utc))

    run("ask", "one", "--yes")
    run("ask", "two", "--yes")

    names = [p.name for p in saved_files(project)]
    assert names == ["20261022-093005-2.md", "20261022-093005.md"]
    assert "one" in (project / ".vaultshield/context/20261022-093005.md").read_text()


def test_no_answers_at_all_says_so(project, gemini):
    run("ask", "Add a login page", input="\n\n\n")
    assert "_No answers were given._" in saved_files(project)[0].read_text()


def test_it_works_from_a_subfolder_and_saves_at_the_project_root(project, gemini, monkeypatch):
    (project / "src").mkdir()
    monkeypatch.chdir(project / "src")

    result = run("ask", "Add a login page", "--yes")

    assert result.exit_code == 0, result.output
    assert saved_files(project) and not (project / "src" / ".vaultshield").exists()


def test_a_project_that_was_never_protected_still_works(protect_project, gemini):
    (protect_project / "README.md").write_text("# Plain\n")
    result = run("ask", "Add a login page", "--yes")
    assert result.exit_code == 0, result.output
    assert (protect_project / ".vaultshield" / "context").is_dir()


# --- --yes and --answers ----------------------------------------------------------------


def test_yes_skips_every_question_and_the_questions_call(project, gemini):
    result = run("ask", "Add a login page", "--yes")

    assert result.exit_code == 0, result.output
    assert gemini.kinds() == ["approach"]  # the second call still happens
    assert "[1/" not in result.stderr
    assert "_The clarifying questions were skipped._" in saved_files(project)[0].read_text()
    assert "## Suggested approach" in result.stdout


def test_answers_from_a_file_are_matched_by_question_id(project, gemini, tmp_path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"db": "Postgres 16", "tests": "Yes, pytest", "unknown": "ignored"}))

    result = run("ask", "Add a login page", "--answers", str(answers))

    assert result.exit_code == 0, result.output
    prompt = saved_files(project)[0].read_text()
    assert "- Which database should it use?\n  Postgres 16" in prompt
    assert "- Should tests be added?\n  Yes, pytest" in prompt
    assert "Which login method" not in prompt
    assert "1 answer in the file matched no question and was ignored" in result.stderr
    assert "[1/3]" not in result.stderr  # nothing is asked in the terminal


def test_the_answers_are_part_of_the_approach_request(project, gemini, tmp_path):
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"db": "Postgres 16"}))

    run("ask", "Add a login page", "--answers", str(answers))

    approach = gemini.bodies()[1]
    assert "Postgres 16" in approach["input"] and "Add a login page" in approach["input"]


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"db": 5}', '{"db": ["a"]}', ""])
def test_a_bad_answers_file_stops_before_any_model_call(project, gemini, tmp_path, content):
    answers = tmp_path / "a.json"
    answers.write_text(content)

    result = run("ask", "Add a login page", "--answers", str(answers))

    assert result.exit_code == 1
    assert "a.json" in result.output
    assert gemini.requests == []


def test_yes_and_answers_together_are_refused(project, gemini, tmp_path):
    answers = tmp_path / "a.json"
    answers.write_text("{}")
    result = run("ask", "Add a login page", "--yes", "--answers", str(answers))
    assert result.exit_code == 2
    assert "either --yes or --answers" in result.output
    assert gemini.requests == []


def test_an_empty_task_is_refused(project, gemini):
    result = run("ask", "   ", "--yes")
    assert result.exit_code == 2
    assert gemini.requests == []


def test_a_very_long_answer_is_cut(project, gemini, tmp_path):
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"db": "x " * 3000}))
    run("ask", "Add a login page", "--answers", str(answers))
    answer_line = [l for l in saved_files(project)[0].read_text().splitlines() if l.startswith("  x x")][0]
    assert len(answer_line.strip()) <= 2000


# --- when the model is unavailable -----------------------------------------------------------


def test_when_the_questions_call_fails_three_generic_questions_are_asked(project, monkeypatch):
    server = FakeGemini(questions=httpx.Response(503, json={"error": {"code": "unavailable"}}))
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setattr(prov.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    result = run("ask", "Add a login page", input="small\n\nnot the billing code\n")

    assert result.exit_code == 0, result.output
    assert (
        "Note: the model was unavailable (The model service returned HTTP 503 (unavailable)); "
        "using 3 generic questions instead."
    ) in result.stderr
    prompt = saved_files(project)[0].read_text()
    assert "- What exactly should change, and what should stay as it is?\n  small" in prompt
    assert "- Which files or folders must not be touched?\n  not the billing code" in prompt


def test_two_invalid_question_replies_fall_back_after_asking_twice(project, monkeypatch):
    server = FakeGemini(questions={"questions": []})
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    result = run("ask", "Add a login page", input="\n\n\n")

    assert server.kinds().count("questions") == 2
    assert "using 3 generic questions" in result.stderr and result.exit_code == 0


@pytest.mark.parametrize(
    "approach",
    [
        httpx.Response(500, json={}),
        httpx.ConnectError("down"),
        {"steps": []},
        {"steps": ["ok", 5]},
        {"steps": ["x" * 400]},
        {"steps": ["a\nb"]},
        {"steps": [f"s{i}" for i in range(9)]},
        {"other": 1},
    ],
)
def test_when_the_approach_call_fails_or_is_invalid_the_section_is_left_out(project, monkeypatch, approach):
    server = FakeGemini(approach=approach)
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setattr(prov.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    result = run("ask", "Add a login page", "--yes")

    assert result.exit_code == 0, result.output
    assert "## Suggested approach" not in result.stdout
    assert "## Constraints" in result.stdout
    assert "the suggested approach was left out" in result.stderr


# --- the key ---------------------------------------------------------------------------------


def test_no_key_is_a_one_line_error_and_nothing_is_sent(project, monkeypatch):
    server = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))

    result = run("ask", "Add a login page", "--yes")

    assert result.exit_code == 1
    assert result.output.strip() == "Error: No Gemini API key found. Run: vaultshield config set-key gemini"
    assert server.requests == []
    assert saved_files(project) == []


def test_a_key_from_the_keychain_is_used(project, monkeypatch, fake_keyring):
    server = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    fake_keyring.items[("vaultshield", "model-key:gemini")] = FAKE_KEY

    result = run("ask", "Add a login page", "--yes")

    assert result.exit_code == 0, result.output
    assert server.requests[0].headers["x-goog-api-key"] == FAKE_KEY


def test_the_environment_key_wins_over_the_keychain_key(project, monkeypatch, fake_keyring):
    server = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    fake_keyring.items[("vaultshield", "model-key:gemini")] = "AIzaKEYCHAINKEYCHAINKEYCHAINKEYCHAIN00"
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    run("ask", "Add a login page", "--yes")

    assert server.requests[0].headers["x-goog-api-key"] == FAKE_KEY


def test_the_configured_model_is_used(project, gemini, tmp_path):
    folder = tmp_path / "home" / ".vaultshield"
    folder.mkdir()
    (folder / "config.yml").write_text("orchestrator:\n  model: gemini-3.5-flash-lite\n")

    run("ask", "Add a login page", "--yes")

    assert gemini.bodies()[0]["model"] == "gemini-3.5-flash-lite"


def test_a_bad_user_config_is_a_clean_error(project, gemini, tmp_path):
    folder = tmp_path / "home" / ".vaultshield"
    folder.mkdir()
    (folder / "config.yml").write_text("orchestrator:\n  model: ../x\n")

    result = run("ask", "Add a login page", "--yes")

    assert result.exit_code == 1 and "config.yml" in result.output
    assert gemini.requests == []


# --- the clipboard -------------------------------------------------------------------------------


def test_copy_puts_exactly_the_saved_prompt_on_the_clipboard(project, gemini, monkeypatch):
    copied = []
    monkeypatch.setattr(commands, "copy_to_clipboard", lambda text: copied.append(text) or True)

    result = run("ask", "Add a login page", "--yes", "--copy")

    assert copied == [saved_files(project)[0].read_text()]
    assert "Copied to the clipboard." in result.stderr


def test_copy_without_pbcopy_is_a_note_not_a_failure(project, gemini, monkeypatch):
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)

    result = run("ask", "Add a login page", "--yes", "--copy")

    assert result.exit_code == 0
    assert "could not copy" in result.stderr


def test_nothing_is_copied_unless_asked(project, gemini, monkeypatch):
    monkeypatch.setattr(commands, "copy_to_clipboard", lambda text: pytest.fail("copied without --copy"))
    assert run("ask", "Add a login page", "--yes").exit_code == 0


def test_pbcopy_is_called_with_the_prompt_on_its_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(commands.shutil, "which", lambda name: "/usr/bin/pbcopy")
    monkeypatch.setattr(commands.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))

    assert commands.copy_to_clipboard("hello \u00e9") is True
    assert calls[0][0] == ["pbcopy"] and calls[0][1]["input"] == "hello \u00e9".encode()


# --- no agent is started -----------------------------------------------------------------------------


def test_the_orchestrator_never_imports_the_sandbox_or_the_agent_adapters():
    folder = Path(commands.__file__).parent
    for source in folder.glob("*.py"):
        text = source.read_text()
        for forbidden in ("vaultshield.adapters", "vaultshield.sandbox", "import docker", "vaultshield.monitor"):
            assert forbidden not in text, f"{source.name} mentions {forbidden}"
