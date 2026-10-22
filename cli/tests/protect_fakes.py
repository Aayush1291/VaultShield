"""Fake Antigravity configuration files for the Protect tests.

Everything here is invented: no real grant, path or credential. The shapes follow
what the real files look like (a settings file with a `permissions` object, an
App `config.json` with `userSettings.globalPermissionGrants.allow` holding many
long command strings, a `hooks.json` keyed by hook name).
"""

from __future__ import annotations

import json
from pathlib import Path

# Command grants that are just ordinary commands. Some are awkward on purpose:
# quotes, backslashes, newlines, tabs, unicode, and a very long line.
CLEAN_GRANTS = [
    "command(git status)",
    "command(npm test)",
    "command(regex:npm run (build|lint|test))",
    "read_file(/Users/fake/Codes/App)",
    "write_file(src/)",
    "mcp(linter/*)",
    'command(python3 -c "import json; print(json.dumps({\'a\': 1}))")',
    "command(echo 'line one\nline two\n\ttabbed')",
    "command(grep -rn \"\\.env\\.example\" docs/)",
    "command(echo 'caf\u00e9 \u00fcber \u5024')",
    "command(" + "x" * 3000 + ")",
]

# Command grants that contain things that look like credentials. All FAKE.
CREDENTIAL_GRANTS = [
    "command(python3 -c \"uri = 'mongodb+srv://fakeuser:fakepass-0000@cluster.example.invalid/db'\")",
    'command(curl -H "Authorization: Bearer fake-bearer-token-0123456789abcdef" https://example.invalid)',
    "command(GITHUB_TOKEN=ghp_fakeFAKEfake0123456789abcdefABCDEF git push)",
    "command(AWS_ACCESS_KEY_ID=AKIAFAKEFAKEFAKE1234 aws s3 ls)",
    "command(export API_KEY=fake-api-key-value-123456 && ./run.sh)",
    "command(echo sk-test-AAAA1111BBBB2222CCCC)",
    "command(echo -----BEGIN RSA PRIVATE KEY----- > key.pem)",
]


def grants(extra_credentials: bool = False, count: int = 150) -> list[str]:
    """About `count` fake grants; the credential-like ones only if asked."""
    out = []
    for i in range(count):
        base = CLEAN_GRANTS[i % len(CLEAN_GRANTS)]
        out.append(base if i < len(CLEAN_GRANTS) else f"command(echo fake-grant-{i})")
    if extra_credentials:
        for i, grant in enumerate(CREDENTIAL_GRANTS):
            out.insert(3 + i * 7, grant)
    return out


def config_object(allow: list[str] | None = None, deny: list[str] | None = None) -> dict:
    grants_block: dict = {"allow": grants() if allow is None else allow}
    if deny is not None:
        grants_block["deny"] = deny
    return {
        "plugins": {"fake-plugin": {"enabled": True}},
        "userSettings": {
            "globalPermissionGrants": grants_block,
            "permissionGrantsV2Migrated": True,
            "remoteControlHostname": "fake-host",
            "sandboxEnabledAtV2Migration": True,
            "useAiCredits": True,
        },
    }


def dump(obj: object, indent: object = 2, final_newline: bool = True, newline: str = "\n") -> str:
    text = json.dumps(obj, indent=indent, ensure_ascii=False)
    if newline != "\n":
        text = text.replace("\n", newline)
    return text + (newline if final_newline else "")


def cli_settings_object() -> dict:
    return {
        "colorScheme": "dark",
        "toolPermission": "request-review",
        "enableTerminalSandbox": True,
        "permissions": {"allow": ["command(git)"], "ask": ["command(*)"]},
    }


def hooks_object() -> dict:
    return {
        "lint-checker": {
            "PostToolUse": [
                {"matcher": "run_command", "hooks": [{"type": "command", "command": "./lint.sh", "timeout": 10}]}
            ]
        }
    }


def make_home(
    base: Path,
    *,
    cli: bool = True,
    app: bool = True,
    hooks: bool = True,
    cli_dir: bool = True,
    config_dir: bool = True,
    config_text: str | None = None,
    settings_text: str | None = None,
    hooks_text: str | None = None,
) -> dict[str, Path]:
    """Builds a fake home with fake Antigravity files. Returns their paths."""
    home = base / "fakehome"
    cli_folder = home / ".gemini" / "antigravity-cli"
    config_folder = home / ".gemini" / "config"
    if cli_dir:
        cli_folder.mkdir(parents=True)
    if config_dir:
        config_folder.mkdir(parents=True)
    paths = {
        "home": home,
        "cli": cli_folder / "settings.json",
        "app": config_folder / "config.json",
        "hooks": config_folder / "hooks.json",
    }
    if cli:
        paths["cli"].write_text(settings_text if settings_text is not None else dump(cli_settings_object()))
    if app:
        paths["app"].write_text(config_text if config_text is not None else dump(config_object()))
    if hooks:
        paths["hooks"].write_text(hooks_text if hooks_text is not None else dump(hooks_object()))
    if not home.exists():
        home.mkdir(parents=True)
    return paths


def snapshot(paths: dict[str, Path]) -> dict[str, bytes | None]:
    return {k: (p.read_bytes() if p.is_file() else None) for k, p in paths.items() if k != "home"}
