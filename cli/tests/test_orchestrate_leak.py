"""Step 7: the leak test. Build a project with secrets in every awkward place, run the
whole `vaultshield ask` flow over a mocked Gemini service, and look at every byte that
would have been sent.

All secrets are invented. The service is mocked, the Keychain is in memory, HOME is a
temporary folder. Nothing leaves the machine.
"""

import json
import logging
import os
import subprocess

import httpx
import pytest
from click.testing import CliRunner

from orchestrate_fakes import APPROACH, FAKE_KEY, QUESTIONS, FakeGemini
from vaultshield.cli import cli
from vaultshield.orchestrate import commands, context, provider as prov

pytestmark = pytest.mark.usefixtures("no_real_model_key")

# --- the secrets -----------------------------------------------------------------------

SHAPED_KEY = "sk-test-AAAA1111BBBB2222CCCC3333"  # has a known shape
DB_URL = "postgres://dbuser:Sup3rS3cretPw@db.example.invalid:5432/app"
PLAIN_TOKEN = "plainvalue-without-any-known-shape"  # no shape: only the vault knows it
SPACED = "correct horse battery staple"
UNPROTECTED_PLAIN = "unprotected-plain-value-no-shape-9"  # in an unprotected .env: never read
PEM_BODY_LINES = ["MIIEowIBAAKCAQEAfakefakefakefakefakefake", "q1w2e3r4t5y6u7i8o9p0fakefakefakefake"]
README_KEY = "ghp_fakeFAKEfake0123456789abcdefABCDEF"
README_AWS = "AKIAIOSFODNN7EXAMPLE"
COMMIT_KEY = "xoxb-1234567890-abcdefghijkl"
TASK_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx"
ANSWER_KEY = "AKIAABCDEFGHIJKLMNOP"
PACKAGE_TOKEN = "ghp_pkgTOKEN0123456789abcdefghijABCD"
FILENAME_KEY = "sk-test-NAMEKEY1234567890abcdef"

ENV_TEXT = (
    f"FAKE_KEY={SHAPED_KEY}\n"
    f"DB_URL={DB_URL}\n"
    f"PLAIN_TOKEN={PLAIN_TOKEN}\n"
    f'SPACED="{SPACED}"\n'
    "PORT=3000\n"
)
PEM_TEXT = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(PEM_BODY_LINES) + "\n-----END PRIVATE KEY-----\n"

# values that must never be in a request, and why each is a distinct risk
PROJECT_SECRETS = {
    "vault value with a known shape": SHAPED_KEY,
    "vault connection string": DB_URL,
    "vault value with no shape": PLAIN_TOKEN,
    "vault value with spaces": SPACED,
    "key in the README": README_KEY,
    "cloud key in the README": README_AWS,
    "key in a commit message": COMMIT_KEY,
    "key in the task": TASK_KEY,
    "key in an answer": ANSWER_KEY,
    "token in package.json": PACKAGE_TOKEN,
    "key in a file name": FILENAME_KEY,
    "pem body 1": PEM_BODY_LINES[0],
    "pem body 2": PEM_BODY_LINES[1],
    "pem header": "BEGIN PRIVATE KEY",
}


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def git(project, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def project(protect_project):
    """A protected project with a .env, a .pem, and secrets in the README, a commit
    message, package.json and even a file name."""
    p = protect_project
    (p / ".env").write_text(ENV_TEXT)
    (p / "certs").mkdir()
    (p / "certs" / "server.pem").write_text(PEM_TEXT)
    (p / "README.md").write_text(
        "# Demo shop\n\nA small web app.\n\n"
        f"Setup (oops, pasted by accident):\n    export FAKE_KEY={SHAPED_KEY}\n    export PLAIN_TOKEN={PLAIN_TOKEN}\n"
        f"    token = {README_KEY}\n    aws_access_key_id = {README_AWS}\n    DB: {DB_URL}\n"
    )
    (p / "package.json").write_text(
        json.dumps({"name": "demo-shop", "dependencies": {"express": "^4.18.0", "internal": f"git+https://x:{PACKAGE_TOKEN}@github.com/o/r.git"}})
    )
    (p / "src").mkdir()
    (p / "src" / "app.js").write_text("console.log('hi')\n")
    (p / "src" / f"notes-{FILENAME_KEY}.txt").write_text("x")
    assert run("protect").exit_code == 0
    git(p, "init", "-q")
    git(p, "add", "README.md", "package.json", "src")
    git(p, "commit", "-q", "-m", f"initial import, token {COMMIT_KEY}")
    return p


@pytest.fixture
def server(monkeypatch):
    fake = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(fake))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    return fake


def secrets_found_in(data: bytes, secrets: dict[str, str]) -> list[str]:
    return [why for why, value in secrets.items() if value.encode() in data]


def everything_under(folder) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in folder.rglob("*") if p.is_file() and ".git/" not in str(p)}


# --- the positive control: the harness must be able to see a leak ---------------------------


def test_control_the_search_finds_every_secret_in_text_that_contains_them():
    everything = " | ".join(PROJECT_SECRETS.values()).encode()
    assert sorted(secrets_found_in(everything, PROJECT_SECRETS)) == sorted(PROJECT_SECRETS)


def test_control_the_secrets_really_are_in_the_project_before_the_flow_runs(project):
    readme = (project / "README.md").read_text()
    assert SHAPED_KEY in readme and PLAIN_TOKEN in readme and README_KEY in readme and README_AWS in readme
    log = subprocess.run(["git", "-C", str(project), "log", "-1", "--pretty=%s"], capture_output=True, text=True).stdout
    assert COMMIT_KEY in log
    assert PACKAGE_TOKEN in (project / "package.json").read_text()
    assert any(FILENAME_KEY in p.name for p in (project / "src").iterdir())
    assert PEM_BODY_LINES[0] in (project / "certs" / "server.pem").read_text()


def test_control_the_tool_does_hold_the_real_values_in_the_vault(project, fake_keyring):
    stored = "".join(fake_keyring.items.values())
    for value in (SHAPED_KEY, DB_URL, PLAIN_TOKEN, SPACED):
        assert value in stored  # the sanitizer has something real to work with
    assert PLAIN_TOKEN not in (project / ".env").read_text()  # and the file holds placeholders


# --- the leak test --------------------------------------------------------------------------------


def go(project, how, tmp_path):
    """Run the whole flow one of three ways. Returns the CLI result."""
    task = f"Add a login page, use {TASK_KEY} for the test account"
    if how == "interactive":
        return run("ask", task, input=f"We use {ANSWER_KEY} for storage\n\nyes\n")
    if how == "answers-file":
        answers = tmp_path / "answers.json"
        answers.write_text(json.dumps({"db": f"Postgres, connect with {DB_URL}", "auth": f"key {ANSWER_KEY}", "tests": PLAIN_TOKEN}))
        return run("ask", task, "--answers", str(answers))
    return run("ask", task, "--yes")


@pytest.mark.parametrize("how", ["interactive", "answers-file", "yes-flag"])
def test_no_request_to_the_model_contains_any_secret_value_or_secret_file_content(project, server, tmp_path, how):
    result = go(project, how, tmp_path)

    assert result.exit_code == 0, result.output
    assert server.requests, "the flow sent nothing, so there was nothing to check"
    sent = server.all_request_bytes()
    assert secrets_found_in(sent, PROJECT_SECRETS) == []
    # not a whole secret file, and not one of its lines
    assert ENV_TEXT.encode() not in sent and PEM_TEXT.encode() not in sent
    for line in ENV_TEXT.splitlines() + PEM_TEXT.splitlines():
        if len(line) >= 12:
            assert line.encode() not in sent, line
    # what the model DID get is still useful: names, structure, the README text around the secrets
    body = "\n".join(b["input"] for b in server.bodies())
    assert "Add a login page" in body and "demo-shop" in body and "express" in body
    assert "FAKE_KEY, DB_URL, PLAIN_TOKEN, SPACED, PORT" in body  # names, not values
    assert "A small web app." in body


@pytest.mark.parametrize("how", ["interactive", "answers-file", "yes-flag"])
def test_the_saved_context_file_and_everything_printed_are_clean_too(project, server, tmp_path, how):
    result = go(project, how, tmp_path)

    [saved] = list((project / ".vaultshield" / "context").glob("*.md"))
    # The prompts go to stderr, and the test runner echoes what it "types" after each ">" the way
    # a terminal shows your own typing. That echo is not output of the tool, so it is set aside;
    # everything else the tool printed must be clean.
    tool_stderr = "\n".join(l for l in result.stderr.splitlines() if not l.lstrip().startswith(">"))
    assert how != "interactive" or tool_stderr != result.stderr  # control: the echo really was there
    for blob in (saved.read_bytes(), result.stdout.encode(), tool_stderr.encode()):
        assert secrets_found_in(blob, PROJECT_SECRETS) == []
        assert FAKE_KEY.encode() not in blob
    assert saved.read_text() in result.stdout


def test_the_api_key_goes_only_in_the_header_and_appears_nowhere_else(project, server, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)

    result = go(project, "interactive", tmp_path)

    assert result.exit_code == 0, result.output
    # the harness can see it where it is supposed to be...
    assert all(r.headers["x-goog-api-key"] == FAKE_KEY for r in server.requests)
    # ...and nowhere it is not
    assert FAKE_KEY.encode() not in server.all_request_bytes()
    assert all(FAKE_KEY not in str(r.url) for r in server.requests)
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in result.output  # the key was never typed in this flow, so no echo either
    leaked_in = [path for path, data in everything_under(tmp_path).items() if FAKE_KEY.encode() in data]
    assert leaked_in == [], leaked_in  # no file in the project, the home folder or anywhere else


def test_the_api_key_from_the_keychain_is_equally_contained(project, monkeypatch, fake_keyring, tmp_path):
    fake = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(fake))
    assert run("config", "set-key", "gemini", input=f"{FAKE_KEY}\n").exit_code == 0
    shown = run("config", "show-key", "gemini")

    result = go(project, "interactive", tmp_path)

    assert result.exit_code == 0, result.output
    assert FAKE_KEY.encode() not in fake.all_request_bytes()
    assert FAKE_KEY not in result.output and FAKE_KEY not in shown.output
    assert [p for p, d in everything_under(tmp_path).items() if FAKE_KEY.encode() in d] == []


def test_a_readme_that_contains_the_api_key_itself_does_not_send_it(project, server, tmp_path):
    (project / "README.md").write_text(f"Use the key {FAKE_KEY} for the demo.\n")

    result = go(project, "yes-flag", tmp_path)

    assert result.exit_code == 0, result.output
    assert FAKE_KEY.encode() not in server.all_request_bytes()
    assert FAKE_KEY not in result.output


def test_the_clipboard_text_is_clean(project, server, tmp_path, monkeypatch):
    copied = []
    monkeypatch.setattr(commands, "copy_to_clipboard", lambda text: copied.append(text) or True)

    run("ask", f"Add a login page {TASK_KEY}", "--yes", "--copy")

    [text] = copied
    assert secrets_found_in(text.encode(), PROJECT_SECRETS) == [] and FAKE_KEY not in text


# --- a project that was never protected ------------------------------------------------------------


def test_the_contents_of_an_unprotected_env_file_are_never_read_so_even_shapeless_values_stay_out(protect_project, server, tmp_path):
    p = protect_project
    (p / ".env").write_text(f"SOME_TOKEN={UNPROTECTED_PLAIN}\nOTHER=another-shapeless-value-here\n")  # not protected
    (p / ".env.local").write_text("LOCAL=local-shapeless-value-here\n")
    (p / "id_rsa").write_text("not-a-real-key-but-private-looking-text\n")
    (p / "README.md").write_text(f"# Plain project\n\naccidental: {README_KEY}\n")

    result = run("ask", "Add a login page", "--yes")

    assert result.exit_code == 0, result.output
    sent = server.all_request_bytes()
    for value in (UNPROTECTED_PLAIN, "another-shapeless-value-here", "local-shapeless-value-here", "private-looking-text", README_KEY):
        assert value.encode() not in sent, value
    body = server.bodies()[0]["input"]
    assert ".env" in body and "id_rsa" in body  # the model may know the files exist


# --- the protections have teeth: switch them off and the leak is seen ---------------------------------


def test_mutation_without_the_sanitizer_the_leak_test_would_catch_the_secrets(project, server, tmp_path, monkeypatch):
    """If this test ever passes while the one above still passes, the leak test is blind."""
    monkeypatch.setattr(context, "sanitize", lambda text, secrets: text)
    monkeypatch.setattr(commands, "sanitize", lambda text, secrets: text)
    monkeypatch.setattr(context, "assert_clean", lambda payload, secrets: None)
    monkeypatch.setattr(commands, "assert_clean", lambda payload, secrets: None)

    result = go(project, "interactive", tmp_path)

    assert result.exit_code == 0, result.output
    found = secrets_found_in(server.all_request_bytes(), PROJECT_SECRETS)
    assert "key in the README" in found and "vault value with no shape" in found and "key in the task" in found


def test_mutation_without_the_sanitizer_the_final_check_still_stops_everything_before_anything_is_sent(project, server, tmp_path, monkeypatch):
    monkeypatch.setattr(context, "sanitize", lambda text, secrets: text)
    monkeypatch.setattr(commands, "sanitize", lambda text, secrets: text)

    result = go(project, "interactive", tmp_path)

    assert result.exit_code == 1
    assert "Nothing was sent" in result.output
    assert server.requests == []  # not one request left the machine
    error_text = "\n".join(l for l in result.output.splitlines() if not l.lstrip().startswith(">"))
    assert secrets_found_in(error_text.encode(), PROJECT_SECRETS) == []  # and the error does not repeat a secret
    saved = project / ".vaultshield" / "context"
    assert not saved.exists() or list(saved.glob("*.md")) == []


def test_the_scan_does_not_read_secret_files_even_with_the_sanitizer_off(project, server, tmp_path, monkeypatch):
    """The first rule is not the sanitizer: secret files are simply never read."""
    monkeypatch.setattr(context, "sanitize", lambda text, secrets: text)
    monkeypatch.setattr(commands, "sanitize", lambda text, secrets: text)
    monkeypatch.setattr(context, "assert_clean", lambda payload, secrets: None)
    monkeypatch.setattr(commands, "assert_clean", lambda payload, secrets: None)

    go(project, "yes-flag", tmp_path)

    sent = server.all_request_bytes()
    assert PEM_BODY_LINES[0].encode() not in sent and PEM_BODY_LINES[1].encode() not in sent
    assert b"BEGIN PRIVATE KEY" not in sent
    assert ENV_TEXT.encode() not in sent


# --- if the Keychain cannot be read, nothing is sent ------------------------------------------------------


def test_an_unreadable_keychain_stops_the_flow_before_any_request(project, monkeypatch, tmp_path):
    import keyring.core
    from keyring.errors import KeyringError

    from vaultshield.protect.vault import InMemoryKeyring

    class Locked(InMemoryKeyring):
        def get_password(self, service, username):
            raise KeyringError("locked")

    fake = FakeGemini()
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(fake))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(keyring.core, "_keyring_backend", Locked())

    result = go(project, "yes-flag", tmp_path)

    assert result.exit_code == 1
    assert "Nothing was sent" in result.output
    assert fake.requests == []
