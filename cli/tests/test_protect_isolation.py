"""Step 7: nothing in install / uninstall / doctor reaches the real home directory,
the real ~/.gemini or ~/.config, or the real Keychain; and the two spike scripts,
which are NOT run here, are only checked as text.
"""

import builtins
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import protect_fakes as fakes
from vaultshield.cli import cli

REPO = Path(__file__).resolve().parents[2]
REAL_HOME = Path(os.path.expanduser("~")).resolve()  # captured before any test changes HOME


def run(*args):
    return CliRunner().invoke(cli, list(args))


def under(path, folder: Path) -> bool:
    try:
        resolved = Path(os.path.abspath(os.fspath(path)))
        return resolved == folder or folder in resolved.parents
    except (TypeError, ValueError):
        return False


@pytest.fixture
def watch_real_home(monkeypatch):
    """Records any file-system call whose path is inside the REAL home directory
    but outside the places pytest and Python legitimately read (site-packages,
    the repo, caches)."""
    touched: list[str] = []
    allowed_prefixes = [REPO, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(tempfile.gettempdir()).resolve()]

    def check(kind, path):
        if isinstance(path, int) or path is None:
            return
        try:
            candidate = Path(os.path.abspath(os.fspath(path)))
        except TypeError:
            return
        if not under(candidate, REAL_HOME):
            return
        if any(under(candidate, prefix) for prefix in allowed_prefixes):
            return
        touched.append(f"{kind} {candidate}")

    real_open, real_os_open = builtins.open, os.open
    real_io_open = io.open

    def watched_open(file, *a, **k):
        check("open", file)
        return real_open(file, *a, **k)

    def watched_os_open(path, *a, **k):
        check("os.open", path)
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", watched_open)
    monkeypatch.setattr(io, "open", watched_open)
    monkeypatch.setattr(os, "open", watched_os_open)
    for name in ("replace", "rename", "remove", "unlink", "mkdir", "rmdir", "stat", "listdir", "scandir"):
        real = getattr(os, name)

        def wrap(real=real, name=name):
            def watched(path, *a, **k):
                check(f"os.{name}", path)
                return real(path, *a, **k)

            return watched

        monkeypatch.setattr(os, name, wrap())
    monkeypatch.setattr(shutil, "copy2", (lambda real: lambda s, d, *a, **k: (check("copy2", s), check("copy2", d), real(s, d, *a, **k))[-1])(shutil.copy2))
    yield touched
    io.open = real_io_open


def test_a_full_cycle_never_touches_the_real_home(protect_project, tmp_path, fake_keyring, watch_real_home):
    (protect_project / ".env").write_text("FAKE_KEY=sk-test-AAAA1111BBBB2222\n")
    home = fakes.make_home(tmp_path, config_text=fakes.dump(fakes.config_object(allow=fakes.grants(extra_credentials=True))))
    originals = fakes.snapshot(home)

    assert run("protect").exit_code == 0
    assert run("agents", "install", "antigravity", "--home", str(home["home"])).exit_code == 0
    assert run("status").exit_code == 0
    run("agents", "doctor", "antigravity")  # reads the process home, which is a temp folder here
    assert run("agents", "uninstall", "antigravity", "--home", str(home["home"])).exit_code == 0
    assert run("unprotect").exit_code == 0

    assert fakes.snapshot(home) == originals
    assert watch_real_home == [], "touched the real home directory:\n" + "\n".join(watch_real_home)


def test_the_watcher_itself_does_notice_a_touch_of_the_real_home(watch_real_home):
    """Positive control: without this, an empty list above could mean the watcher
    is blind. This only asks for the metadata of a path in the real home; it reads
    no file."""
    try:
        os.stat(REAL_HOME / ".gemini")
    except OSError:
        pass
    assert any(entry.startswith("os.stat ") and entry.endswith(".gemini") for entry in watch_real_home)


def test_the_process_home_is_a_temporary_folder_during_these_tests(protect_project, tmp_path):
    assert Path(os.environ["HOME"]).resolve() == (tmp_path / "home").resolve()
    assert Path.home().resolve() != REAL_HOME


def test_the_keychain_is_the_in_memory_fake(protect_project, fake_keyring):
    import keyring

    assert keyring.get_keyring() is fake_keyring


def test_the_default_home_is_only_ever_the_process_home(protect_project, tmp_path, monkeypatch):
    """With no --home, install uses HOME. Pointing HOME at a temp folder keeps it away from the real one."""
    (protect_project / ".env").write_text("FAKE_KEY=sk-test-AAAA1111BBBB2222\n")
    run("protect")
    home = fakes.make_home(tmp_path)
    monkeypatch.setenv("HOME", str(home["home"]))

    result = run("agents", "install", "antigravity")

    assert result.exit_code == 0, result.output
    assert str(REAL_HOME) not in result.output or str(REAL_HOME) in str(tmp_path)


# --- the two spike scripts: checked as text, never run ------------------------------

HOOK_PROOF = REPO / "spike" / "hook_proof.sh"
KEYCHAIN_SMOKE = REPO / "spike" / "keychain_smoke.sh"
scripts = pytest.mark.skipif(not HOOK_PROOF.exists() or not KEYCHAIN_SMOKE.exists(), reason="spike scripts not present")


@scripts
@pytest.mark.parametrize("script", [HOOK_PROOF, KEYCHAIN_SMOKE], ids=lambda p: p.name)
def test_the_scripts_are_executable_and_parse(script):
    assert os.access(script, os.X_OK)
    # `bash -n` reads the script and runs nothing
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@scripts
def test_hook_proof_changes_the_real_hooks_file_only_after_a_diff_and_a_typed_yes():
    text = HOOK_PROOF.read_text()
    stage_two = text.split("# --- STAGE 2 (--global)", 1)[1]
    assert "diff -u" in stage_two
    assert 'read -r answer' in stage_two and '"$answer" != "YES"' in stage_two
    # every write to the real file comes after the confirmation
    confirmation = stage_two.index('"$answer" != "YES"')
    for write in ('cp "$proposed" "$GLOBAL_HOOKS"', 'cp -p "$GLOBAL_HOOKS" "$GLOBAL_BACKUP"', ': > "$GLOBAL_ABSENT"'):
        assert stage_two.index(write) > confirmation, write
    # stage 1 never mentions the real file except in text it prints
    stage_one = text.split("# --- STAGE 1", 1)[1].split("# --- STAGE 2", 1)[0]
    assert not re.search(r'^[^#\n]*(cp|mv|rm|tee|>)\s+[^\n]*\$GLOBAL_HOOKS', stage_one, re.M)
    assert "~/.gemini" not in "".join(l for l in stage_one.splitlines() if "say" not in l and not l.lstrip().startswith("#"))


@scripts
def test_hook_proof_only_deletes_a_folder_it_made():
    text = HOOK_PROOF.read_text()
    assert "safe_proof_dir" in text
    assert 'rm -rf "$dir"' in text
    assert text.index("safe_proof_dir \"$dir\"") < text.index('rm -rf "$dir"')


@scripts
def test_the_hook_body_in_hook_proof_logs_no_command_text_and_denies_only_the_marker(tmp_path):
    text = HOOK_PROOF.read_text()
    body = re.search(r"cat <<'HOOK'\n(.*?)\nHOOK\n", text, re.S).group(1)
    log = tmp_path / "hook.log"
    script = tmp_path / "hook.sh"
    script.write_text(body.replace("__LOG__", str(log)) + "\n")
    script.chmod(0o755)

    def call(payload):
        return subprocess.run([str(script)], input=payload, capture_output=True, text=True, timeout=30)

    allowed = call('{"toolCall":{"name":"run_command","args":{"CommandLine":"echo SECRET-LOOKING-TEXT","BypassSandbox":true}}}')
    denied = call('{"toolCall":{"name":"run_command","args":{"CommandLine":"echo VS_HOOK_PROOF_DENY_ME"}}}')

    assert '"decision":"allow"' in allowed.stdout
    assert '"decision":"deny"' in denied.stdout
    logged = log.read_text()
    assert "bypass=true" in logged and "marker=yes" in logged
    assert "SECRET-LOOKING-TEXT" not in logged and "VS_HOOK_PROOF_DENY_ME" not in logged.replace("marker=", "")


@scripts
def test_keychain_smoke_touches_only_its_own_fake_item():
    text = KEYCHAIN_SMOKE.read_text()
    assert 'SERVICE="vaultshield-smoke"' in text
    # every keychain call goes through $SERVICE; the real service name never appears
    assert not re.search(r"""['"]vaultshield['"]""", text)
    assert not re.search(r"-s\s+['\"]?vaultshield(?!-smoke)", text)
    assert "item_exists" in text and "will not touch an existing item" in text
    assert "trap cleanup EXIT" in text
    # it never prints the value it stored
    assert 'echo "$VALUE"' not in text and 'say "$VALUE"' not in text and "printf '%s\\n' \"$VALUE\"" not in text
