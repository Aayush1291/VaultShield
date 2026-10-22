"""The per-agent leak spike scripts are NEVER run here (they make real agent
calls). What is tested: each script is valid shell and safe by inspection, it
isolates the agent via its config-dir override and a temp keyring, it never
writes to a real config or the Keychain, and it greps for the fake canary and
prints LEAKED/SAFE.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "claude-code": (REPO / "spike" / "leak_claude_code.sh", "CLAUDE_CONFIG_DIR"),
    "kiro": (REPO / "spike" / "leak_kiro.sh", "KIRO_HOME"),
    "codex": (REPO / "spike" / "leak_codex.sh", "CODEX_HOME"),
}

pytestmark = pytest.mark.skipif(
    not all(path.exists() for path, _ in SCRIPTS.values()), reason="leak scripts not present"
)


def text(agent):
    return SCRIPTS[agent][0].read_text()


def code(agent):
    """Non-comment lines only."""
    return "\n".join(line for line in text(agent).splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_the_script_is_executable_and_valid_shell(agent):
    path, _ = SCRIPTS[agent]
    assert os.access(path, os.X_OK)
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_it_isolates_the_agent_config_with_the_override_into_a_temp_dir(agent):
    override = SCRIPTS[agent][1]
    body = code(agent)
    # the override is exported, pointed at the temp config home, never at the real home
    assert re.search(rf'export {override}="\$CFGHOME/', body), body
    assert f"{override}=$HOME" not in body
    assert f'{override}="$HOME' not in body
    # no write ever targets the real home config (redirections / installs go to $CFGHOME or $DIR)
    assert "--home \"$HOME\"" not in body
    assert not re.search(r'>\s*"?\$HOME/\.', body)
    assert not re.search(r'>\s*~/\.', body)


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_it_installs_into_the_temp_config_home_only(agent):
    body = code(agent)
    assert re.search(rf'agents install {agent} --home "\$CFGHOME"', body), body


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_it_never_touches_the_real_keychain(agent):
    body = code(agent)
    # VaultShield is pointed at a temp plaintext keyring, never the macOS Keychain
    assert 'PYTHON_KEYRING_BACKEND="keyrings.alt.file.PlaintextKeyring"' in body
    assert "keyrings.alt" in body  # and it refuses if that backend is missing
    assert "security " not in body and "find-generic-password" not in body


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_it_has_a_single_guarded_rm_rf(agent):
    body = code(agent)
    assert body.count("rm -rf") == 1
    assert "safe_dir" in text(agent)
    assert "/tmp/${PREFIX}." in text(agent)


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_it_uses_a_fake_canary_and_decides_leaked_or_safe(agent):
    body = code(agent)
    assert "LEAKCANARY" in body  # an invented value, never a real secret
    assert 'grep -qF -- "$FAKE_ENV_KEY"' in body
    assert "LEAKED" in body and "SAFE" in body


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_it_prints_manual_steps_when_it_cannot_run_safely(agent):
    body = code(agent)
    assert "manual_steps" in body
    # the adversarial prompt is present
    assert "FAKE_KEY" in body


@pytest.mark.parametrize("agent", list(SCRIPTS))
def test_it_sets_u_and_uses_the_vault_python(agent):
    assert "set -u" in text(agent)
    assert 'PY="${VS_PYTHON:-$REPO/cli/.venv/bin/python}"' in text(agent)
