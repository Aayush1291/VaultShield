"""Step 6: .vaultshield/orchestrator.jsonl, one line per model call, never any content."""

import json
from datetime import datetime

import httpx
import pytest
from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.orchestrate import provider as prov
from vaultshield.orchestrate.provider import CallRecord, ledger_path, ledger_recorder

from orchestrate_fakes import ENV_VALUE, FAKE_KEY, FakeGemini

TASK = "Add a login page for TASK-MARKER-7"
README_MARKER = "README-MARKER-4321"
ANSWER = "ANSWER-MARKER-9876"

pytestmark = pytest.mark.usefixtures("no_real_model_key")


@pytest.fixture
def project(protect_project):
    (protect_project / "README.md").write_text(f"# Demo\n\n{README_MARKER}\n")
    (protect_project / ".env").write_text(f"PLAIN={ENV_VALUE}\n")
    assert CliRunner().invoke(cli, ["protect"]).exit_code == 0
    return protect_project


def serve(monkeypatch, server):
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setattr(prov.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def lines(project):
    path = project / ".vaultshield" / "orchestrator.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_each_model_call_is_one_json_line_with_the_agreed_fields(project, monkeypatch):
    server = FakeGemini()
    serve(monkeypatch, server)

    assert run("ask", TASK, input=f"{ANSWER}\n\n\n").exit_code == 0

    records = lines(project)
    assert len(records) == 2
    for record in records:
        assert set(record) == {"timestamp", "provider", "model", "request_bytes", "outcome"}
        assert record["provider"] == "gemini" and record["model"] == "gemini-3.8-flash"
        assert record["outcome"] == "ok"
        assert datetime.fromisoformat(record["timestamp"]).tzinfo is not None
    assert [r["request_bytes"] for r in records] == [len(r.content) for r in server.requests]


def test_the_ledger_holds_no_content_at_all(project, monkeypatch):
    serve(monkeypatch, FakeGemini())

    run("ask", TASK, input=f"{ANSWER}\n\n\n")

    text = (project / ".vaultshield" / "orchestrator.jsonl").read_text()
    for forbidden in (
        "TASK-MARKER", README_MARKER, ANSWER, ENV_VALUE, FAKE_KEY, "PLAIN", ".env", "README", "Which database",
        "Add a route", "system", "input", "Bearer",
    ):
        assert forbidden not in text, forbidden


def test_a_failed_call_is_recorded_once_with_its_outcome(project, monkeypatch):
    serve(monkeypatch, FakeGemini(questions=httpx.Response(503, json={}), approach=httpx.Response(403, json={})))

    run("ask", TASK, input="\n\n\n")

    assert [r["outcome"] for r in lines(project)] == ["http_503", "http_403"]  # the retried 503 is still one call


def test_an_invalid_reply_and_its_second_attempt_are_two_calls(project, monkeypatch):
    serve(monkeypatch, FakeGemini(questions={"questions": []}))

    run("ask", TASK, input="\n\n\n")

    records = lines(project)
    assert [r["outcome"] for r in records] == ["ok", "ok", "ok"]  # JSON arrived each time; the shape was wrong
    assert len(records) == 3  # two question attempts and the approach


def test_a_reply_that_is_not_json_is_recorded_as_invalid(project, monkeypatch):
    bad = {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": "not json"}]}]}
    serve(monkeypatch, FakeGemini(questions=httpx.Response(200, json=bad), approach=httpx.Response(200, json=bad)))

    run("ask", TASK, input="\n\n\n")

    assert [r["outcome"] for r in lines(project)] == ["invalid_reply"] * 3


@pytest.mark.parametrize("error, outcome", [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("down"), "network_error")])
def test_timeouts_and_network_errors_are_recorded(project, monkeypatch, error, outcome):
    serve(monkeypatch, FakeGemini(questions=error, approach=error))
    run("ask", TASK, input="\n\n\n")
    assert [r["outcome"] for r in lines(project)] == [outcome, outcome]


def test_runs_add_to_the_ledger_and_never_rewrite_it(project, monkeypatch):
    serve(monkeypatch, FakeGemini())
    run("ask", "first", "--yes")
    first = (project / ".vaultshield" / "orchestrator.jsonl").read_text()

    run("ask", "second", "--yes")

    second = (project / ".vaultshield" / "orchestrator.jsonl").read_text()
    assert second.startswith(first) and len(lines(project)) == 2


def test_with_yes_only_the_approach_call_is_recorded(project, monkeypatch):
    serve(monkeypatch, FakeGemini())
    run("ask", TASK, "--yes")
    assert len(lines(project)) == 1


def test_no_key_means_no_call_and_no_ledger(project, monkeypatch):
    server = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))

    assert run("ask", TASK, "--yes").exit_code == 1

    assert lines(project) == [] and server.requests == []


def test_the_ledger_is_at_the_project_root_even_from_a_subfolder(project, monkeypatch):
    serve(monkeypatch, FakeGemini())
    (project / "src").mkdir()
    monkeypatch.chdir(project / "src")

    run("ask", TASK, "--yes")

    assert len(lines(project)) == 1 and not (project / "src" / ".vaultshield").exists()


def test_a_ledger_that_cannot_be_written_does_not_stop_the_work(project, monkeypatch):
    serve(monkeypatch, FakeGemini())
    (project / ".vaultshield" / "orchestrator.jsonl").mkdir()  # a folder where the file should be

    result = run("ask", TASK, "--yes")

    assert result.exit_code == 0, result.output
    assert "## Suggested approach" in result.stdout


def test_the_recorder_on_its_own_writes_one_line(tmp_path):
    ledger_recorder(tmp_path)(CallRecord("gemini", "gemini-3.8-flash", 1234, "ok"))

    [record] = [json.loads(line) for line in ledger_path(tmp_path).read_text().splitlines()]
    assert record["request_bytes"] == 1234 and record["outcome"] == "ok"
    assert ledger_path(tmp_path) == tmp_path / ".vaultshield" / "orchestrator.jsonl"
    assert ledger_path(tmp_path).read_text().endswith("\n")


def test_a_stop_at_the_final_check_saves_no_prompt(project, monkeypatch):
    """The final assert_clean refuses: nothing is saved or printed."""
    from vaultshield.orchestrate import commands

    server = FakeGemini()
    serve(monkeypatch, server)
    monkeypatch.setattr(commands, "assert_clean", lambda *a, **k: (_ for _ in ()).throw(commands.LeakError("stop")))

    result = run("ask", TASK, "--yes")

    assert result.exit_code == 1 and "stop" in result.output
    saved = project / ".vaultshield" / "context"
    assert not saved.exists() or list(saved.glob("*.md")) == []
    assert "## Task" not in result.stdout
