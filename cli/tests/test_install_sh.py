"""website/install.sh, exercised offline with fake `python`/`curl`/`uname` shims.

A controlled PATH (the shim dir, then /usr/bin and /bin for real coreutils) lets
the whole installer run without network and without a real Python: the fake
`python3.x` shims report a version and "create" a venv by copying a stub
interpreter; `curl` writes a dummy wheel; the stub "pip install" is a no-op; the
stub `vaultshield` prints a version. Nothing touches the real HOME, Keychain, or
agent configs — HOME, VAULTSHIELD_HOME, and VAULTSHIELD_BIN are all under tmp_path.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO / "website" / "install.sh"

pytestmark = pytest.mark.skipif(not INSTALL_SH.exists(), reason="install.sh not present")


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


_PY_SHIM = """#!/bin/sh
printf '%s|%s\\n' "{name}" "$*" >> "$VS_SHIM_LOG"
if [ "$1" = "-c" ]; then exit {version_exit}; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$VS_VENV_PY_TEMPLATE" "$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
exit 0
"""

_VENV_PY = """#!/bin/sh
printf 'VENVPY|%s\\n' "$*" >> "$VS_SHIM_LOG"
if [ "$1" = "-m" ] && [ "$2" = "vaultshield.cli" ]; then
  if [ "$3" = "--version" ]; then printf '%s\\n' "vaultshield, version 0.1.0"; exit 0; fi
  if [ "$3" = "agents" ]; then printf '%s\\n' "no protected files found; run vaultshield protect first"; exit 1; fi
  exit 0
fi
exit 0
"""

_CURL_OK = """#!/bin/sh
printf 'curl|%s\\n' "$*" >> "$VS_SHIM_LOG"
target=""
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && target="$2"
  shift
done
[ -n "$target" ] && printf 'dummy-wheel' > "$target"
exit 0
"""

_CURL_FAIL = """#!/bin/sh
printf 'curl|%s\\n' "$*" >> "$VS_SHIM_LOG"
exit 1
"""

_UNAME_LINUX = """#!/bin/sh
printf '%s\\n' "Linux"
"""


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bindir = tmp_path / "shims"
        self.bindir.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.vhome = tmp_path / "vshome" / "venv"
        self.vbin = tmp_path / "localbin" / "vaultshield"
        self.log = tmp_path / "shim.log"
        self.log.write_text("")
        self.template = tmp_path / "venv_py_template"
        _write(self.template, _VENV_PY)

    def python(self, name: str, *, good: bool = True) -> None:
        _write(self.bindir / name, _PY_SHIM.format(name=name, version_exit=0 if good else 1))

    def curl(self, *, ok: bool = True) -> None:
        _write(self.bindir / "curl", _CURL_OK if ok else _CURL_FAIL)

    def uname_linux(self) -> None:
        _write(self.bindir / "uname", _UNAME_LINUX)

    def environ(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bindir}:/usr/bin:/bin",
            "HOME": str(self.home),
            "VAULTSHIELD_HOME": str(self.vhome),
            "VAULTSHIELD_BIN": str(self.vbin),
            "VAULTSHIELD_BASE": "http://localhost:8080",
            "VS_SHIM_LOG": str(self.log),
            "VS_VENV_PY_TEMPLATE": str(self.template),
        }

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(INSTALL_SH), *args],
            env=self.environ(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def log_text(self) -> str:
        return self.log.read_text()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _default_success(env: Env) -> None:
    env.python("python3.12", good=True)
    env.curl(ok=True)


# --- macOS gate --------------------------------------------------------------


def test_refuses_on_non_macos_and_installs_nothing(env):
    _default_success(env)
    env.uname_linux()
    result = env.run()
    assert result.returncode != 0
    assert "macOS only" in result.stdout
    assert not env.vhome.exists()
    assert not env.vbin.exists()


# --- Python selection --------------------------------------------------------


def test_missing_python_prints_brew_and_installs_nothing(env):
    # every candidate present but too old
    for name in ("python3.13", "python3.12", "python3.11", "python3"):
        env.python(name, good=False)
    env.curl(ok=True)
    result = env.run()
    assert result.returncode != 0
    assert "brew install python@3.12" in result.stdout
    assert not env.vhome.exists()


def test_selects_the_newest_available_explicit_python(env):
    env.python("python3.12", good=True)
    env.python("python3.11", good=True)
    env.curl(ok=True)
    result = env.run()
    assert result.returncode == 0, result.stdout + result.stderr
    # the venv was created by 3.12 (tried before 3.11), not 3.11
    venv_lines = [ln for ln in env.log_text().splitlines() if "|-m venv" in ln]
    assert venv_lines and venv_lines[0].startswith("python3.12|")


def test_prefers_an_explicit_version_over_bare_python3(env):
    env.python("python3.11", good=True)
    env.python("python3", good=True)
    env.curl(ok=True)
    result = env.run()
    assert result.returncode == 0
    venv_lines = [ln for ln in env.log_text().splitlines() if "|-m venv" in ln]
    assert venv_lines and venv_lines[0].startswith("python3.11|")


# --- the full offline install ------------------------------------------------


def test_full_install_creates_one_venv_and_a_launcher(env):
    _default_success(env)
    result = env.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.vhome / "bin" / "python").exists()
    assert env.vbin.exists() and os.access(env.vbin, os.X_OK)
    launcher = env.vbin.read_text()
    assert "-m vaultshield.cli" in launcher
    assert str(env.vhome / "bin" / "python") in launcher  # exactly one Python
    assert "vaultshield, version 0.1.0" in result.stdout
    assert "VENVPY|-m pip install" in env.log_text()
    # bin dir is not on the controlled PATH, so the one-line PATH hint is printed
    assert "export PATH=" in result.stdout


def test_rerun_upgrades_without_recreating_the_venv(env):
    _default_success(env)
    first = env.run()
    assert first.returncode == 0
    second = env.run()
    assert second.returncode == 0
    assert "Reusing the existing" in second.stdout
    # venv was created exactly once across both runs
    assert env.log_text().count("|-m venv") == 1


def test_never_edits_shell_startup_files(env):
    _default_success(env)
    rc_files = {name: (env.home / name) for name in (".zshrc", ".bashrc", ".bash_profile", ".profile")}
    for path in rc_files.values():
        path.write_text("# user content\n")
    before = {name: path.read_text() for name, path in rc_files.items()}

    result = env.run()
    assert result.returncode == 0

    for name, path in rc_files.items():
        assert path.read_text() == before[name], f"{name} was modified"
    # and no new rc file was created
    assert not (env.home / ".zprofile").exists()


# --- uninstall ---------------------------------------------------------------


def test_uninstall_removes_only_the_venv_and_launcher(env):
    _default_success(env)
    assert env.run().returncode == 0
    assert env.vhome.exists() and env.vbin.exists()

    # Sentinels that stand in for Keychain state and a protected project.
    token = env.home / ".vaultshield" / "token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("fake-token")
    project = env.tmp / "some-project" / ".vaultshield" / "protect.json"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text('{"project_id": "x"}')

    result = env.run("--uninstall")
    assert result.returncode == 0
    assert not env.vhome.exists()
    assert not env.vbin.exists()
    # the reminder, and the untouched state
    assert "vaultshield unprotect" in result.stdout
    assert token.read_text() == "fake-token"
    assert project.read_text() == '{"project_id": "x"}'


# --- version stays in step ---------------------------------------------------


def test_install_sh_version_matches_the_package():
    import tomllib

    text = INSTALL_SH.read_text()
    match = re.search(r'^VERSION="([^"]+)"', text, re.MULTILINE)
    assert match, "install.sh has no VERSION= line"
    installer_version = match.group(1)

    pyproject = tomllib.loads((REPO / "cli" / "pyproject.toml").read_text())
    assert installer_version == pyproject["project"]["version"]

    from vaultshield import __version__

    assert installer_version == __version__


def test_build_sh_uses_pip_wheel_no_deps_into_dist():
    build = (REPO / "website" / "build.sh").read_text()
    assert "pip wheel --no-deps" in build
    assert "website/dist" in build or "/dist" in build
