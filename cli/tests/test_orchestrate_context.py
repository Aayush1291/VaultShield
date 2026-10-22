"""Step 3: the project scanner, the sanitizer and assert_clean. Fake secrets only."""

import builtins
import io
import json
import os
import subprocess

import keyring.core
import pytest
from click.testing import CliRunner
from keyring.errors import KeyringError

from vaultshield.cli import cli
from vaultshield.orchestrate import context as ctx
from vaultshield.orchestrate.context import (
    LeakError,
    Secrets,
    assert_clean,
    load_secrets,
    sanitize,
    scan_project,
)
from vaultshield.protect.vault import InMemoryKeyring

KEY_VALUE = "sk-test-AAAA1111BBBB2222"  # also sk-shaped
DB_VALUE = "postgres://u:p@localhost/db"
PASSWORD_VALUE = "correct horse battery staple"
PLAIN_VALUE = "plainvalue-without-any-known-shape"  # only the vault knows this one
PEM_BODY = "MIIEowIBAAKCAQEAfakefakefakefakefakefake"
API_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345wxyz"


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def git(project, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def project(protect_project):
    """A project with a protected .env, a .pem, a README, dependency files and git history."""
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\nPLAIN={PLAIN_VALUE}\nPORT=3000\nPASSWORD=\"{PASSWORD_VALUE}\"\n")
    (protect_project / "certs").mkdir()
    (protect_project / "certs" / "server.pem").write_text(f"-----BEGIN PRIVATE KEY-----\n{PEM_BODY}\n-----END PRIVATE KEY-----\n")
    (protect_project / "README.md").write_text("# Demo app\n\nA small web app. Run it with `npm start`.\n")
    (protect_project / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4.18.0", "left-pad": "git+https://user:ghp_fakeFAKEfake0123456789abcdefABCDEF@github.com/x/y.git"}, "devDependencies": {"jest": "29.7.0"}})
    )
    (protect_project / "requirements.txt").write_text("flask>=2.0\nrequests==2.31.0  # http\n-r other.txt\n-e git+https://token@github.com/a/b.git#egg=b\nprivate @ https://user:pw@example.invalid/p.whl\n\n")
    (protect_project / "src").mkdir()
    (protect_project / "src" / "app.js").write_text("console.log('hi')\n")
    assert run("protect").exit_code == 0
    git(protect_project, "init", "-q")
    git(protect_project, "add", "README.md", "package.json", "src")
    git(protect_project, "commit", "-q", "-m", "first commit")
    git(protect_project, "commit", "-q", "--allow-empty", "-m", "add login page")
    return protect_project


# --- positive control: the scanner must find a planted secret first ----------------


def test_control_the_checks_find_planted_secrets_before_we_trust_them_to_find_none():
    secrets = Secrets([PLAIN_VALUE])
    planted = [
        f"the value is {PLAIN_VALUE}",  # a vault value with no known shape
        "key sk-proj-AbCdEfGhIjKlMnOpQrStUvWx",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_fakeFAKEfake0123456789abcdefABCDEF",
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        "Xk9fQ2mP7vLw4nRtZ8yBhA3jD6sUe1CgVb0aMq5W",  # 40 mixed-case/digit characters
    ]
    for text in planted:
        with pytest.raises(LeakError):
            assert_clean(text, secrets)
        assert sanitize(text, secrets) != text, text  # and sanitize really changes it


def test_control_assert_clean_passes_ordinary_text():
    assert_clean("Add a login page to src/components/LoginForm.tsx using express 4.18", Secrets([PLAIN_VALUE]))


# --- shapes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx",
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "sk-test-AAAA1111BBBB2222",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "ghp_fakeFAKEfake0123456789abcdefABCDEF",
        "gho_fakeFAKEfake0123456789abcdefABCDEF",
        "github_pat_11ABCDEFG0fakefakefakefake_fakefake",
        "xoxb-1234567890-abcdefghijkl",
        API_KEY,
        "ya29.fake_token_value_0123456789abcdef",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.fakesignature123",
        "Xk9fQ2mP7vLw4nRtZ8yBhA3jD6sUe1CgVb0aMq5W",
    ],
)
def test_secret_shapes_are_replaced(secret):
    text = f"before {secret} after"
    cleaned = sanitize(text, Secrets())
    assert secret not in cleaned
    assert cleaned == "before *** after"
    with pytest.raises(LeakError):
        assert_clean(text, Secrets())
    assert_clean(cleaned, Secrets())


def test_a_private_key_block_is_replaced_whole():
    text = f"x\n-----BEGIN OPENSSH PRIVATE KEY-----\n{PEM_BODY}\nline2\n-----END OPENSSH PRIVATE KEY-----\ny"
    assert sanitize(text, Secrets()) == "x\n***\ny"


def test_an_unterminated_private_key_block_is_replaced_to_the_end():
    text = f"x\n-----BEGIN PRIVATE KEY-----\n{PEM_BODY}"
    assert sanitize(text, Secrets()) == "x\n***"


def test_a_password_inside_a_url_is_replaced_but_the_rest_of_the_url_stays():
    assert sanitize("clone https://alice:hunter2pass@github.com/org/repo.git now", Secrets()) == "clone https://***@github.com/org/repo.git now"
    assert sanitize("https://github.com/org/repo.git", Secrets()) == "https://github.com/org/repo.git"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("PASSWORD=fakepassword1", "PASSWORD=***"),
        ("export API_KEY='abcdefghij1234'", "export API_KEY='***'"),
        ("db_password: hunter2hunter2", "db_password: ***"),
        ("--token=abcdefgh12345 run", "--token=*** run"),
        ("TOKEN=$(cat file)", "TOKEN=$(cat file)"),  # a shell expansion, not a value
        ("TOKEN=short", "TOKEN=short"),  # under 8 characters
    ],
)
def test_assigned_secrets_are_replaced(text, expected):
    assert sanitize(text, Secrets()) == expected


@pytest.mark.parametrize(
    "text",
    [
        "src/components/Dashboard2Panel/index.tsx",
        "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4",  # a git commit id: lower case and digits only
        "123e4567-e89b-12d3-a456-426614174000",
        "https://api.example.com/v1/users",
        "see docs/reference/very-long-lowercase-file-name-with-dashes-in-it.md",
        "react 18.2.0, express ^4.18.0",
        "Add a login page",
        "VeryLongComponentName2Wrapper",  # 29 characters
        "/Users/someone/Projects/MyApp2/src/Components/UserProfile9/Avatar.tsx",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert sanitize(text, Secrets()) == text
    assert_clean(text, Secrets())


def test_a_long_mixed_identifier_is_replaced_which_is_a_known_false_positive():
    long_name = "ProcessUserAccountSettingsHandler2Impl"
    assert len(long_name) >= 32
    assert sanitize(long_name, Secrets()) == "***"


# --- vault values ---------------------------------------------------------------


def test_every_vault_value_is_replaced_even_one_with_no_secret_shape():
    secrets = Secrets([PLAIN_VALUE, PASSWORD_VALUE, DB_VALUE])
    cleaned = sanitize(f"a {PLAIN_VALUE} b {PASSWORD_VALUE} c {DB_VALUE} d", secrets)
    assert cleaned == "a *** b *** c *** d"


def test_the_longest_value_is_replaced_first():
    secrets = Secrets(["abcdef-longer-value", "abcdef"])
    assert sanitize("x abcdef-longer-value y", secrets) == "x *** y"


def test_values_too_short_or_too_plain_to_redact_are_not_replaced_just_like_exec():
    secrets = Secrets(["3000", "true", "no", "abc", "12345678"])
    assert len(secrets) == 0
    assert sanitize("port 3000, debug true, 12345678", secrets) == "port 3000, debug true, 12345678"


def test_the_always_list_is_replaced_whatever_it_looks_like():
    secrets = Secrets(["3000"], always=["k3y"])
    assert len(secrets) == 1
    assert sanitize("port 3000 and k3y", secrets) == "port 3000 and ***"


def test_secrets_never_show_their_values():
    secrets = Secrets([PLAIN_VALUE], always=[API_KEY])
    for text in (repr(secrets), str(secrets), f"{secrets}"):
        assert PLAIN_VALUE not in text and API_KEY not in text
    assert len(secrets) == 2


def test_a_leak_error_says_what_kind_and_how_many_but_never_what():
    with pytest.raises(LeakError) as vault_value:
        assert_clean(f"x {PLAIN_VALUE} y", Secrets([PLAIN_VALUE]))
    with pytest.raises(LeakError) as shaped:
        assert_clean(f"{KEY_VALUE} and ghp_fakeFAKEfake0123456789abcdefABCDEF", Secrets())
    assert PLAIN_VALUE not in str(vault_value.value) and "stored secret value" in str(vault_value.value)
    assert KEY_VALUE not in str(shaped.value) and "2 secret-shaped strings" in str(shaped.value)


def test_assert_clean_accepts_text_bytes_and_json_data():
    secrets = Secrets([PLAIN_VALUE])
    for bad in (f"x {PLAIN_VALUE}", f"x {PLAIN_VALUE}".encode(), {"a": [f"x {PLAIN_VALUE}"]}):
        with pytest.raises(LeakError):
            assert_clean(bad, secrets)
    assert_clean({"a": ["fine"]}, secrets)
    assert_clean(b"fine", secrets)


def test_assert_clean_copes_with_text_that_is_not_valid_unicode():
    assert_clean("a lone surrogate \udc80 is fine", Secrets([PLAIN_VALUE]))


# --- loading the vault's values --------------------------------------------------


def test_load_secrets_reads_every_value_stored_for_the_project(project):
    secrets = load_secrets(project)
    cleaned = sanitize(" | ".join([KEY_VALUE, DB_VALUE, PLAIN_VALUE, PASSWORD_VALUE]), secrets)
    assert cleaned == " | ".join(["***"] * 4)
    assert len(secrets) == 4  # PORT=3000 is not redacted


def test_duplicate_keys_both_count(protect_project):
    (protect_project / ".env").write_text("TOKEN_A=first-duplicate-value\nTOKEN_A=second-duplicate-value\n")
    run("protect")
    secrets = load_secrets(protect_project)
    assert sanitize("first-duplicate-value second-duplicate-value", secrets) == "*** ***"


def test_a_project_that_was_never_protected_has_no_vault_values(protect_project):
    assert len(load_secrets(protect_project)) == 0
    assert len(load_secrets(protect_project, always=[API_KEY])) == 1


def test_an_unreadable_keychain_stops_everything_rather_than_guessing(project, monkeypatch):
    class Locked(InMemoryKeyring):
        def get_password(self, service, username):
            raise KeyringError("locked")

    monkeypatch.setattr(keyring.core, "_keyring_backend", Locked())

    with pytest.raises(ctx.ContextError) as excinfo:
        load_secrets(project)
    with pytest.raises(ctx.ContextError):
        scan_project(project)
    assert "Nothing was sent" in str(excinfo.value)


def test_a_broken_manifest_stops_everything(protect_project):
    (protect_project / ".vaultshield").mkdir()
    (protect_project / ".vaultshield" / "protect.json").write_text("{nope")
    with pytest.raises(ctx.ContextError):
        load_secrets(protect_project)


# --- scanning --------------------------------------------------------------------


def test_the_framework_is_detected_with_the_same_code_as_init(project):
    context = scan_project(project)
    assert context.framework == {"start_command": "npm start", "port": "3000"}


def test_a_django_project_is_detected(protect_project):
    (protect_project / "manage.py").write_text("print('x')\n")
    assert scan_project(protect_project).framework["start_command"] == "python manage.py runserver 0.0.0.0:8000"


def test_a_project_nothing_is_detected_in_has_an_empty_framework(protect_project):
    assert scan_project(protect_project).framework == {}


def test_the_tree_lists_names_sorted_with_folders_marked(project):
    tree = scan_project(project).tree
    assert tree == sorted(tree)
    assert "src/" in tree and "src/app.js" in tree and "README.md" in tree
    # names of secret files are visible; their contents are not (tested below)
    assert ".env" in tree and "certs/server.pem" in tree


def test_the_tree_stops_at_depth_three(protect_project):
    deep = protect_project / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("x")
    (protect_project / "a" / "b" / "c" / "three.txt").write_text("x")
    (protect_project / "a" / "b" / "level3.txt").write_text("x")
    tree = scan_project(protect_project).tree
    # a/ is level 1, a/b/ level 2, a/b/c/ and a/b/three.txt level 3
    assert "a/" in tree and "a/b/" in tree and "a/b/c/" in tree and "a/b/level3.txt" in tree
    assert "a/b/c/three.txt" not in tree  # level 4
    assert not any(entry.startswith("a/b/c/d") for entry in tree)


def test_the_tree_is_capped_at_200_entries_and_says_so(protect_project):
    for i in range(260):
        (protect_project / f"file_{i:03}.txt").write_text("x")
    context = scan_project(protect_project)
    assert len(context.tree) == 200 and context.tree_truncated
    assert "only the first 200 entries" in context.render()


@pytest.mark.parametrize("skipped", ["node_modules", ".git", ".venv", "__pycache__", "dist", "build", ".vaultshield"])
def test_skipped_folders_are_not_listed(protect_project, skipped):
    (protect_project / skipped).mkdir()
    (protect_project / skipped / "inside.txt").write_text("x")
    (protect_project / "kept.txt").write_text("x")
    tree = scan_project(protect_project).tree
    assert "kept.txt" in tree
    assert not any(entry.startswith(skipped) for entry in tree)


def test_symlinks_are_not_listed_or_followed(protect_project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x")
    os.symlink(outside, protect_project / "linked")
    os.symlink(outside / "x.txt", protect_project / "link.txt")
    assert not any("link" in entry for entry in scan_project(protect_project).tree)


def test_the_readme_is_cut_to_4000_characters(protect_project):
    (protect_project / "README.md").write_text("word " * 3000)
    readme = scan_project(protect_project).readme
    assert len(readme) == 4000


@pytest.mark.parametrize("name", ["README.md", "readme.MD", "Readme.rst", "README.txt", "README"])
def test_readme_files_are_found_whatever_the_case_or_extension(protect_project, name):
    (protect_project / name).write_text("hello from the readme")
    assert scan_project(protect_project).readme == "hello from the readme"


def test_a_key_split_by_the_4000_character_cut_leaves_no_fragment(protect_project):
    (protect_project / "README.md").write_text("a " * 1995 + "sk-test-AAAA1111BBBB2222CCCC3333 trailing text")
    readme = scan_project(protect_project).readme
    assert "sk-test" not in readme and "AAAA1111" not in readme
    assert "***" in readme


def test_dependencies_are_names_with_plain_versions_only(project):
    deps = scan_project(project).dependencies
    assert deps["package.json"]["name"] == "demo"
    assert deps["package.json"]["groups"]["dependencies"] == ["express ^4.18.0", "left-pad"]  # the git URL is dropped
    assert deps["package.json"]["groups"]["devDependencies"] == ["jest 29.7.0"]
    assert deps["requirements.txt"]["groups"]["dependencies"] == ["flask >=2.0", "requests ==2.31.0", "private"]


def test_pyproject_dependencies_are_read(protect_project):
    (protect_project / "pyproject.toml").write_text(
        '[project]\nname = "demo-py"\ndependencies = ["click>=8.1", "httpx>=0.27,<1.0", "pkg @ https://user:pw@h.invalid/p.whl"]\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n'
    )
    info = scan_project(protect_project).dependencies["pyproject.toml"]
    assert info["name"] == "demo-py"
    assert info["groups"]["dependencies"] == ["click >=8.1", "httpx >=0.27,<1.0", "pkg"]
    assert info["groups"]["optional:dev"] == ["pytest >=8.0"]


def test_broken_dependency_files_are_ignored(protect_project):
    (protect_project / "package.json").write_text("{not json")
    (protect_project / "pyproject.toml").write_text("not = [toml")
    assert scan_project(protect_project).dependencies == {}


def test_the_last_ten_commit_subjects_are_included_newest_first(protect_project):
    git(protect_project, "init", "-q")
    for i in range(12):
        git(protect_project, "commit", "-q", "--allow-empty", "-m", f"change number {i}")
    commits = scan_project(protect_project).commits
    assert commits == [f"change number {i}" for i in range(11, 1, -1)]


def test_a_folder_that_is_not_a_git_repository_has_no_commits(protect_project):
    assert scan_project(protect_project).commits == []


def test_protected_variables_appear_by_name_only(project):
    context = scan_project(project)
    assert context.protected == {".env": ["FAKE_KEY", "DB_URL", "PLAIN", "PORT", "PASSWORD"]}
    text = context.render()
    assert "- .env: FAKE_KEY, DB_URL, PLAIN, PORT, PASSWORD" in text


# --- the hard rules ----------------------------------------------------------------


@pytest.fixture
def opened(monkeypatch):
    """Records the path of every file opened for reading while it is active."""
    paths: list[str] = []
    real = builtins.open

    def spy(file, *a, **k):
        if not isinstance(file, int):
            paths.append(os.path.abspath(os.fspath(file)))
        return real(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    return paths


def test_the_content_of_a_secret_file_is_never_opened(project, opened):
    for name in ("id_rsa", ".npmrc", "credentials.json", ".env.local", ".pypirc", ".netrc"):
        (project / name).write_text("SECRET-CONTENT-OF-" + name)
    (project / "backend").mkdir()
    (project / "backend" / ".env").write_text("X=SECRET-CONTENT-OF-backend-env\n")
    opened.clear()  # the lines above WROTE those files; only the scan's own opens count

    scan_project(project)

    touched = {os.path.basename(p) for p in opened}
    for secret_file in ("server.pem", "id_rsa", ".npmrc", "credentials.json", ".env.local", ".pypirc", ".netrc"):
        assert secret_file not in touched, secret_file
    assert not any(p.endswith(os.sep + ".env") for p in opened)
    assert not any(p.endswith(os.path.join("certs", "server.pem")) for p in opened)


def test_the_scanner_does_open_the_files_it_is_meant_to_so_the_spy_is_not_blind(project, opened):
    scan_project(project)
    touched = {os.path.basename(p) for p in opened}
    assert {"README.md", "package.json", "requirements.txt"} <= touched
    assert "protect.json" in touched


def test_no_secret_file_content_reaches_the_context(project):
    (project / "id_rsa").write_text("SECRET-CONTENT-OF-id_rsa")
    text = scan_project(project).render()
    for secret in ("SECRET-CONTENT-OF-id_rsa", PEM_BODY, KEY_VALUE, DB_VALUE, PLAIN_VALUE, PASSWORD_VALUE):
        assert secret not in text


def test_a_readme_that_is_a_symlink_to_a_secret_file_is_not_read(protect_project):
    (protect_project / "README.md").unlink() if (protect_project / "README.md").exists() else None
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    os.symlink(protect_project / ".env", protect_project / "README.md")
    os.symlink(protect_project / ".env", protect_project / "package.json")

    context = scan_project(protect_project)

    assert context.readme == "" and context.dependencies == {}
    assert KEY_VALUE not in context.render()


def test_a_file_named_in_the_manifest_is_never_read_even_if_it_looks_harmless(project):
    manifest = json.loads((project / ".vaultshield" / "protect.json").read_text())
    manifest["detected"].append("README.md")
    (project / ".vaultshield" / "protect.json").write_text(json.dumps(manifest))
    assert scan_project(project).readme == ""


def test_secret_values_planted_in_every_free_text_source_are_removed(project):
    (project / "README.md").write_text(
        f"Setup: export FAKE_KEY={KEY_VALUE}\nDB at {DB_VALUE}\nplain: {PLAIN_VALUE}\nghp_fakeFAKEfake0123456789abcdefABCDEF\n"
    )
    git(project, "add", "README.md")
    git(project, "commit", "-q", "-m", f"fix: use {PLAIN_VALUE} for {KEY_VALUE}")

    text = scan_project(project).render()

    for secret in (KEY_VALUE, DB_VALUE, PLAIN_VALUE, "ghp_fakeFAKE"):
        assert secret not in text
    assert "***" in text
    assert_clean(text, load_secrets(project))


def test_the_whole_summary_passes_assert_clean(project):
    assert_clean(scan_project(project).render(), load_secrets(project))
