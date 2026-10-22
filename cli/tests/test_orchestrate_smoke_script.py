"""Step 9: spike/orchestrator_smoke.sh is NEVER run here (it calls the live Gemini API).

What is tested: the script is valid shell and is safe by inspection; and its assumptions
hold. The project it builds is extracted from the script (that part only writes local
files), and the real `vaultshield ask` flow is run on it over a MOCKED service, with the
same stdin the script uses and the same conditions the script then checks.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from orchestrate_fakes import FAKE_KEY, FakeGemini, gemini_2_5_flash_reply
from vaultshield.cli import cli
from vaultshield.orchestrate import provider as prov

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "spike" / "orchestrator_smoke.sh"

pytestmark = [
    pytest.mark.skipif(not SCRIPT.exists(), reason="spike script not present"),
    pytest.mark.usefixtures("no_real_model_key"),
]

FIVE_QUESTIONS = {
    "questions": [{"id": f"q{i}", "text": f"Question number {i}?", "why": "It matters."} for i in range(1, 6)]
}


def script_text() -> str:
    return SCRIPT.read_text()


def test_the_script_is_executable_and_valid_shell():
    assert os.access(SCRIPT, os.X_OK)
    # `bash -n` parses the script and runs nothing
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_the_script_makes_no_network_call_of_its_own_and_never_protects_anything():
    text = script_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("curl ", "wget ", "nc ", "ssh ", "scp ", " protect", "unprotect", "agents install"):
        assert forbidden not in code, forbidden
    # the only commands it hands to VaultShield
    calls = re.findall(r"\bvs ([a-z-]+(?: [a-z-]+)?)", code)
    assert set(calls) == {"config show-key", "ask"}


def test_the_script_never_prints_a_key():
    code = "\n".join(line for line in script_text().splitlines() if not line.lstrip().startswith("#"))
    assert "GEMINI_API_KEY" not in code
    assert "vs config show-key gemini" in code  # the one place the key is touched: last 4 characters only
    assert "find-generic-password" not in code and "security " not in code


def test_it_asks_for_confirmation_before_the_first_model_call():
    code = script_text()
    assert code.index("Press Enter to continue") < code.index('vs ask "Add a login page"')
    assert "REAL calls" in code


def test_it_only_deletes_a_folder_it_made():
    text = script_text()
    assert "safe_dir" in text and text.index('safe_dir "$DIR"') < text.index('rm -rf "$DIR"')
    assert text.count("rm -rf") == 1
    assert '/tmp/${PREFIX}.*' in text


# --- its assumptions, checked against the real flow over a mocked service -----------------


def build_fixture(tmp_path: Path) -> Path:
    """Run ONLY the script's fixture function (it just writes local files)."""
    text = script_text()
    invented = [line for line in text.splitlines() if line.startswith("FAKE_")]
    fixture = text.split("# --- fixture begins ---")[1].split("# --- fixture ends ---")[0]
    folder = tmp_path / "smoke-project"
    subprocess.run(
        ["bash", "-c", "\n".join(invented) + "\n" + fixture + '\nmake_fixture "$1"', "bash", str(folder)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return folder


def invented_secrets() -> dict[str, str]:
    values = {}
    for line in script_text().splitlines():
        match = re.match(r'^(FAKE_[A-Z_]+)="(.*)"$', line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def run_like_the_script(folder: Path, monkeypatch, questions):
    server = FakeGemini(questions=questions)
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.chdir(folder)
    return server, CliRunner().invoke(cli, ["ask", "Add a login page"], input="\n\n\n\n\n\n")


def test_the_fixture_has_the_invented_secrets_where_the_script_says(protect_project, tmp_path):
    folder = build_fixture(tmp_path)
    secrets = invented_secrets()
    assert set(secrets) == {"FAKE_ENV_KEY", "FAKE_ENV_DB", "FAKE_ENV_PLAIN", "FAKE_README_TOKEN"}
    env = (folder / ".env").read_text()
    readme = (folder / "README.md").read_text()
    for name in ("FAKE_ENV_KEY", "FAKE_ENV_DB", "FAKE_ENV_PLAIN"):
        assert secrets[name] in env
    assert secrets["FAKE_README_TOKEN"] in readme
    assert (folder / "package.json").exists() and (folder / "src" / "app.js").exists()


@pytest.mark.parametrize("questions", [None, FIVE_QUESTIONS], ids=["three questions", "five questions"])
def test_every_condition_the_script_checks_holds_on_its_own_project(protect_project, tmp_path, monkeypatch, questions):
    folder = build_fixture(tmp_path)
    secrets = invented_secrets()
    server, result = run_like_the_script(folder, monkeypatch, questions or FakeGemini().questions)

    assert result.exit_code == 0, result.output
    stdout, stderr = result.stdout, result.stderr
    # "the questions came from the model"
    assert "the model was unavailable" not in stderr and "[1/" in stderr
    # "a suggested approach was produced"
    assert re.search(r"^## Suggested approach", stdout, re.M)
    # "none of the invented secrets is in the prompt or the questions"
    for name, value in secrets.items():
        assert value not in stdout and value not in stderr, name
        assert value.encode() not in server.all_request_bytes(), name
    # "the project context is in the prompt"
    assert "demo-shop" in stdout or "Express" in stdout
    # the call log the script prints exists, with one line per call
    ledger = (folder / ".vaultshield" / "orchestrator.jsonl").read_text().splitlines()
    assert len(ledger) == 2


def test_six_blank_lines_are_enough_for_five_questions(protect_project, tmp_path, monkeypatch):
    folder = build_fixture(tmp_path)
    _server, result = run_like_the_script(folder, monkeypatch, FIVE_QUESTIONS)
    assert "[5/5]" in result.stderr and result.exit_code == 0


def test_the_fixture_is_not_protected_so_the_keychain_is_not_written(protect_project, tmp_path, monkeypatch, fake_keyring):
    folder = build_fixture(tmp_path)
    before = dict(fake_keyring.items)
    run_like_the_script(folder, monkeypatch, FakeGemini().questions)
    assert fake_keyring.items == before
    assert not (folder / ".vaultshield" / "protect.json").exists()


# --- the failure explanation: which of the outcomes happened, never any content -------------


def classify(ledger: Path) -> str:
    """Run ONLY the script's classify function (it reads one local file and prints)."""
    block = script_text().split("# --- classify begins ---")[1].split("# --- classify ends ---")[0]
    result = subprocess.run(
        ["bash", "-c", block + '\nclassify_ledger "$1"', "bash", str(ledger)],
        env={**os.environ, "PY": sys.executable},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


def write_ledger(path: Path, *outcomes, **extra) -> Path:
    lines = [
        json.dumps(
            {"timestamp": "2026-10-22T10:00:00+00:00", "provider": "gemini", "model": "gemini-2.5-flash",
             "request_bytes": 100, "outcome": outcome, **extra}
        )
        for outcome in outcomes
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.mark.parametrize(
    "outcome, said",
    [
        ("http_400", "call 1 of 1: api rejected (HTTP 400)"),
        ("http_403", "call 1 of 1: api rejected (HTTP 403)"),
        ("http_429", "call 1 of 1: api rejected (HTTP 429)"),
        ("http_503", "call 1 of 1: api rejected (HTTP 503)"),
        ("invalid_reply", "call 1 of 1: reply not understood"),
        ("timeout", "call 1 of 1: timeout"),
        ("network_error", "call 1 of 1: could not reach the service"),
    ],
)
def test_each_way_a_call_can_end_is_named(tmp_path, outcome, said):
    assert classify(write_ledger(tmp_path / "l.jsonl", outcome)) == said + "\n"


def test_only_the_failed_calls_are_listed_with_their_place(tmp_path):
    ledger = write_ledger(tmp_path / "l.jsonl", "invalid_reply", "invalid_reply", "ok", "timeout")
    assert classify(ledger).splitlines() == [
        "call 1 of 4: reply not understood",
        "call 2 of 4: reply not understood",
        "call 4 of 4: timeout",
    ]


def test_when_every_call_ended_ok_it_says_so(tmp_path):
    assert classify(write_ledger(tmp_path / "l.jsonl", "ok", "ok")) == "all 2 recorded calls ended ok\n"


def test_a_missing_or_empty_call_log_is_reported_as_no_calls(tmp_path):
    assert classify(tmp_path / "missing.jsonl") == "no calls were recorded\n"
    (tmp_path / "empty.jsonl").write_text("")
    assert classify(tmp_path / "empty.jsonl") == "no calls were recorded\n"


def test_lines_that_are_not_call_records_are_skipped(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text('not json\n[1, 2]\n{"outcome": 7}\n{"no": "outcome"}\n{"outcome": "timeout"}\n')
    assert classify(path) == "call 1 of 1: timeout\n"


def test_nothing_from_the_log_but_the_outcome_class_is_ever_printed(tmp_path):
    secret = "LEDGER-CONTENT-MARKER"
    path = write_ledger(tmp_path / "l.jsonl", "invalid_reply", text=secret, error=secret, input=secret)
    with path.open("a") as handle:
        handle.write(json.dumps({"outcome": f"http_400 {secret}"}) + "\n")
        handle.write(json.dumps({"outcome": f"surprise; echo {secret}"}) + "\n")
    out = classify(path)
    assert secret not in out and "echo" not in out
    assert out.splitlines() == [
        "call 1 of 3: reply not understood",
        "call 2 of 3: unknown outcome",
        "call 3 of 3: unknown outcome",
    ]


@pytest.mark.parametrize(
    "answer, said",
    [
        (lambda: httpx.Response(400, json={"error": {"code": "invalid_argument", "message": "SERVICE-FREE-TEXT"}}),
         "api rejected (HTTP 400)"),
        (lambda: httpx.Response(200, json=gemini_2_5_flash_reply("I cannot do that. SERVICE-FREE-TEXT")),
         "reply not understood"),
        (lambda: httpx.ReadTimeout("SERVICE-FREE-TEXT"), "timeout"),
        (lambda: httpx.ConnectError("SERVICE-FREE-TEXT"), "could not reach the service"),
    ],
    ids=["400", "invalid reply", "timeout", "unreachable"],
)
def test_the_names_match_what_the_real_tool_writes_for_a_failing_service(
    protect_project, tmp_path, monkeypatch, answer, said
):
    folder = build_fixture(tmp_path)
    run_like_the_script(folder, monkeypatch, answer())

    ledger = folder / ".vaultshield" / "orchestrator.jsonl"
    out = classify(ledger)

    total = len(ledger.read_text().splitlines())
    assert out.splitlines()[0] == f"call 1 of {total}: {said}"
    assert "SERVICE-FREE-TEXT" not in out


def test_the_explanation_is_printed_only_when_a_check_failed_and_before_the_result():
    text = script_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    call = 'classify_ledger "$DIR/.vaultshield/orchestrator.jsonl"'
    assert code.count(call) == 1
    assert code.index("== 5. checks ==") < code.index(call) < code.index("RESULT:")
    guard = code[: code.index(call)].rsplit("\nif ", 1)[1]
    assert guard.startswith('[ "$FAILED" != 0 ]')
    for label in ("api rejected", "reply not understood", "timeout"):
        assert label in text
