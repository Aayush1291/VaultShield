"""Antigravity backstops: exact-path deny rules and a PreToolUse command hook.

Both are secondary to the placeholders. What the spike (spike/RESULT.md)
measured, and what this module is built around:

* `read_file(.env*)` does NOT match anything, so every rule here is an exact
  file path.
* A PreToolUse hook only fires for commands that would otherwise run, so it is
  a backstop, not a gate.
* The CLI and the App/IDE keep permission grants in different files.

The hook reads a JSON payload on stdin:

    {"toolCall": {"name": "run_command",
                  "args": {"CommandLine": "cat .env", "Cwd": "/path"}},
     "stepIdx": 3, "conversationId": "...", "workspacePaths": ["/path"], ...}

and prints `{"decision": "allow"}` or `{"decision": "deny", "reason": "..."}`.
Antigravity fails closed on a missing or malformed decision, and so does this.

The hook never reads the Keychain, so it has no values to leak. Its log holds a
reason code and the protected file's path, never the command and never a value.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from vaultshield.monitor import EventLogRecord, _append_event_log, _now_iso8601
from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect.manifest import Manifest, ManifestError

HOOK_NAME = "vaultshield-protect"
HOOK_COMMAND = "vaultshield hook antigravity"

REASON_PROTECTED_PATH = "protected_path"
REASON_ENV_DUMP = "env_dump"
REASON_KEYCHAIN_READ = "keychain_read"
REASON_VAULTSHIELD_COMMAND = "vaultshield_command"
REASON_UNPARSABLE = "unparsable_payload"
REASON_MANIFEST = "manifest_unreadable"

_MESSAGES = {
    REASON_PROTECTED_PATH: (
        "this command names a file whose secret values VaultShield keeps in the Keychain. "
        "Variable names are available with `vaultshield status`; values are not."
    ),
    REASON_ENV_DUMP: "this command would print the environment, which can hold secret values.",
    REASON_KEYCHAIN_READ: "this command would read the Keychain, where VaultShield keeps secret values.",
    REASON_VAULTSHIELD_COMMAND: (
        "`vaultshield exec` and `vaultshield unprotect` are for the user to run, not the agent."
    ),
    REASON_UNPARSABLE: "the tool call could not be read, so it was not allowed.",
    REASON_MANIFEST: "the VaultShield manifest for this project could not be read, so the command was not allowed.",
}


# --- render ------------------------------------------------------------------


def deny_rules(root: Path, manifest: Manifest) -> list[str]:
    """`read_file(<absolute path>)` for every protected and detected file.
    Exact paths only: Antigravity does not match a glob like `.env*`."""
    base = root.resolve()
    return [f"read_file({base / rel})" for rel in manifest.all_paths()]


def hook_config(command: str = HOOK_COMMAND) -> dict:
    return {
        HOOK_NAME: {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [{"type": "command", "command": command, "timeout": 10}],
                }
            ]
        }
    }


def render(root: Path, manifest: Manifest) -> str:
    """The configuration as text. Prints only; installs nothing."""
    return "\n".join(
        [
            "# VaultShield for Antigravity.",
            "# This only PRINTS the configuration. Nothing was installed or changed.",
            "",
            "## 1. Deny rules (exact file paths; Antigravity does not match a glob like .env*)",
            "",
            json.dumps({"permissions": {"deny": deny_rules(root, manifest)}}, indent=2),
            "",
            "CLI: merge the \"deny\" list into ~/.gemini/antigravity-cli/settings.json",
            "App/IDE: grants are kept in a different file, ~/.gemini/config/config.json,",
            "         under userSettings.globalPermissionGrants.deny. NOT VERIFIED for the",
            "         App/IDE yet (the IDE test has not been run).",
            "",
            "## 2. PreToolUse hook (a backstop: it only sees commands that would otherwise run)",
            "",
            json.dumps(hook_config(), indent=2),
            "",
            "Save as .agents/hooks.json in this project, or merge into ~/.gemini/config/hooks.json",
            "(shared by the CLI, App and IDE). `vaultshield` must be on the PATH Antigravity uses.",
            "",
        ]
    )


# --- the hook ----------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    detail: str = ""  # a protected file's path or a command word from a fixed list; never user text
    root: Path | None = None


ALLOW = Verdict(True)

_SEGMENT_SPLIT = re.compile(r"\$\(|[;&|\n()`]")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUMBERISH = re.compile(r"^\d+(\.\d+)?[smhd]?$")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_WRAPPERS = frozenset(
    {
        "sudo", "command", "builtin", "exec", "nohup", "time", "nice", "stdbuf", "timeout",
        "xargs", "watch", "then", "do", "else", "elif", "if", "while", "until", "{", "!",
    }
)
_KEYRING_CODE = re.compile(r"\bimport\s+keyring\b|\bfrom\s+keyring\b|\bkeyring\.(get|set|delete)_password\b")
_MAX_DEPTH = 3


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _unwrap(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _ASSIGNMENT.match(token):
            i += 1
        elif os.path.basename(token) in _WRAPPERS:
            i += 1
            while i < len(tokens) and (
                tokens[i].startswith("-") or _NUMBERISH.match(tokens[i]) or _ASSIGNMENT.match(tokens[i])
            ):
                i += 1
        else:
            break
    return tokens[i:]


def _simple_commands(command: str, depth: int = 0) -> list[list[str]]:
    """Each simple command in `command` as [name, *args], wrappers removed and
    the text of `sh -c '...'` and `eval` looked into."""
    found: list[list[str]] = []
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = _unwrap(_tokens(segment))
        if not tokens:
            continue
        found.append(tokens)
        if depth >= _MAX_DEPTH:
            continue
        name = os.path.basename(tokens[0])
        if name in _SHELLS:
            for i, token in enumerate(tokens[1:], start=1):
                if token.startswith("-") and not token.startswith("--") and "c" in token[1:] and i + 1 < len(tokens):
                    found += _simple_commands(tokens[i + 1], depth + 1)
                    break
        elif name == "eval":
            found += _simple_commands(" ".join(tokens[1:]), depth + 1)
    return found


def _flags(args: list[str]) -> list[str]:
    return [a for a in args if a.startswith("-") and len(a) > 1 and not a.startswith("--")]


def _check_simple(tokens: list[str]) -> tuple[str, str] | None:
    """(reason, command word) if this simple command dumps the environment,
    reads the Keychain, or runs a user-only VaultShield command."""
    name, args = os.path.basename(tokens[0]), tokens[1:]
    if name in ("env", "printenv"):
        return REASON_ENV_DUMP, name
    if name == "export" and (not args or any("p" in f[1:] for f in _flags(args))):
        return REASON_ENV_DUMP, name
    if name in ("declare", "typeset") and (
        not args or any(set("xp") & set(f[1:]) for f in _flags(args))
    ):
        return REASON_ENV_DUMP, name
    if name == "set" and not args:
        return REASON_ENV_DUMP, name
    if name == "ps" and any(re.fullmatch(r"-?[A-Za-z]+", a) and set("eE") & set(a) for a in args):
        return REASON_ENV_DUMP, name
    if name == "security" and any(re.fullmatch(r"find-.*-password", a) or a == "dump-keychain" for a in args):
        return REASON_KEYCHAIN_READ, name
    words = [os.path.basename(t) for t in tokens]
    for i, word in enumerate(words):
        if (word == "vaultshield" or word.startswith("vaultshield.")) and any(
            later in ("exec", "unprotect") for later in words[i + 1 :]
        ):
            return REASON_VAULTSHIELD_COMMAND, "vaultshield"
    return None


def _names_path(texts: list[str], root: Path, rel: str) -> bool:
    """Whether any of `texts` names the file `rel` of the project at `root`:
    by absolute path, by relative path, or by file name alone (the agent's
    working directory may not be the project root). Case-insensitive, because
    the macOS file system is."""
    base = rel.rsplit("/", 1)[-1]
    for candidate in {str(root.resolve() / rel), str(root / rel), rel, base}:
        pattern = re.compile(r"(?<![A-Za-z0-9_.\-])" + re.escape(candidate) + r"(?![A-Za-z0-9_.\-])", re.IGNORECASE)
        if any(pattern.search(text) for text in texts):
            return True
    return False


def evaluate(command: str, projects: list[tuple[Path, Manifest]]) -> Verdict:
    """Allow or deny one shell command for the given protected projects.
    With no protected project there is nothing to guard: allow."""
    if not projects:
        return ALLOW
    first_root = projects[0][0]
    for simple in _simple_commands(command):
        hit = _check_simple(simple)
        if hit:
            return Verdict(False, hit[0], hit[1], first_root)
    if _KEYRING_CODE.search(command):
        return Verdict(False, REASON_KEYCHAIN_READ, "keyring", first_root)
    # The raw text, and the text with shell quoting removed (`.e""nv` -> `.env`).
    texts = [command, " ".join(t for simple in _simple_commands(command) for t in simple)]
    for root, manifest in projects:
        for rel in manifest.all_paths():
            if _names_path(texts, root, rel):
                return Verdict(False, REASON_PROTECTED_PATH, rel, root)
    return ALLOW


def _projects_for(payload: dict) -> list[tuple[Path, Manifest]]:
    call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    starts = list(payload.get("workspacePaths") or []) + [args.get("Cwd")]
    projects: list[tuple[Path, Manifest]] = []
    seen: set[Path] = set()
    for start in starts:
        if not isinstance(start, str) or not start:
            continue
        root = manifest_mod.find_root(Path(start))
        if root is None or root in seen:
            continue
        seen.add(root)
        manifest = manifest_mod.load(root)
        if manifest is not None:
            projects.append((root, manifest))
    return projects


def _log_violation(root: Path | None, verdict: Verdict) -> None:
    if root is None:
        return
    detail = f" {verdict.detail}" if verdict.detail else ""
    record = EventLogRecord(
        "violation", _now_iso8601(), f"antigravity run_command denied: {verdict.reason}{detail}"
    )
    try:
        _append_event_log(manifest_mod.violations_path(root), [record])
    except OSError:
        pass  # failing to log must never turn a denial into an allow


def _deny(verdict: Verdict) -> str:
    return json.dumps({"decision": "deny", "reason": f"VaultShield: {_MESSAGES[verdict.reason]}"})


def handle_hook(stdin_text: str) -> str:
    """The hook: payload in, decision JSON out. Never raises."""
    try:
        payload = json.loads(stdin_text)
        call = payload["toolCall"]
        if call.get("name") != "run_command":
            return json.dumps({"decision": "allow"})
        # Only the command text decides. Other arguments of the call, such as
        # BypassSandbox (the agent asking to run outside the sandbox), SafeToAutoRun
        # or WaitMsBeforeAsync, are never consulted: a command that names a protected
        # file is denied however the call asks to be run. (Measured: after its file
        # read was blocked, the agent ran `cat` with BypassSandbox:true.)
        command = call["args"]["CommandLine"]
        if not isinstance(command, str):
            raise TypeError("CommandLine is not text")
    except (ValueError, KeyError, TypeError, AttributeError):
        return _deny(Verdict(False, REASON_UNPARSABLE))

    try:
        projects = _projects_for(payload)
    except ManifestError:
        return _deny(Verdict(False, REASON_MANIFEST))
    except (OSError, RuntimeError, ValueError):
        return _deny(Verdict(False, REASON_UNPARSABLE))

    try:
        verdict = evaluate(command, projects)
    except Exception:  # fail closed: an unexpected error must never become an allow
        return _deny(Verdict(False, REASON_UNPARSABLE))
    if verdict.allowed:
        return json.dumps({"decision": "allow"})
    _log_violation(verdict.root, verdict)
    return _deny(verdict)
