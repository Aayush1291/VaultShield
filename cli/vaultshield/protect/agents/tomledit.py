"""Add or replace the `[permissions.vaultshield]` table in Codex's `config.toml`
without disturbing the rest of the file.

Our whole contribution is one named table (plus its `filesystem` sub-table). The
simplest merge that keeps every other byte is to append the table at the end of
the file, or, if it is already there, to replace exactly its span (from its
header to the next table that is not ours). We never touch `default_permissions`
or `sandbox_mode`, so the profile is defined but inert until the user selects it.

The file is parsed with `tomllib` first to confirm it is valid TOML, and the
result is parsed again to confirm the edit is still valid; anything that cannot
be handled raises `TomlEditError` and the installer changes nothing.
"""

from __future__ import annotations

import re
import tomllib

PROFILE = "permissions.vaultshield"


class TomlEditError(Exception):
    """The file cannot be edited safely. The message never contains file content."""


_HEADER = re.compile(r"^[ \t]*\[\[?([^\]]*)\]\]?[ \t]*(#.*)?$")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def profile_body(paths: list[str], nl: str) -> str:
    """The TOML for our profile: extend :workspace (keeps the baseline) and deny
    reads of each protected file by absolute path."""
    lines = [
        f"[{PROFILE}]",
        'description = "VaultShield: keep protected secret files unreadable."',
        'extends = ":workspace"',
        f"[{PROFILE}.filesystem]",
    ]
    lines += [f'"{_escape(path)}" = "deny"' for path in paths]
    return nl.join(lines) + nl


def _is_single_header(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("[") and not stripped.startswith("[[")


def _header_name(line: str) -> str | None:
    match = _HEADER.match(line)
    return match.group(1).strip() if match else None


def _is_ours(name: str) -> bool:
    return name == PROFILE or name.startswith(PROFILE + ".")


def _our_span(text: str, nl: str) -> tuple[int, int] | None:
    """The character span of our profile region, or None if it is absent. The
    region runs from the `[permissions.vaultshield]` header to the next header
    that is not ours (or end of file)."""
    lines = text.split(nl)
    starts: list[int] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + len(nl)
    start_idx = None
    for i, line in enumerate(lines):
        name = _header_name(line)
        if name == PROFILE and _is_single_header(line):
            start_idx = i
            break
    if start_idx is None:
        return None
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        name = _header_name(lines[j])
        if name is not None and not _is_ours(name):
            end_idx = j
            break
    char_start = starts[start_idx]
    char_end = starts[end_idx] if end_idx < len(lines) else len(text)
    return char_start, char_end


def set_profile(text: str, paths: list[str]) -> str:
    """Adds our profile, or replaces it if it is already present. Everything
    outside our table stays byte for byte."""
    if text.strip():
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            raise TomlEditError("config.toml is not valid TOML") from None
    nl = _newline(text)
    body = profile_body(paths, nl)
    span = _our_span(text, nl)
    if span is None:
        base = text
        if base and not base.endswith("\n"):
            base += nl
        separator = nl if base.strip() else ""
        new = base + separator + body
    else:
        start, end = span
        new = text[:start] + body + text[end:]
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        raise TomlEditError("the edit would not be valid TOML; nothing was changed") from None
    return new


def selected_profile(text: str) -> str | None:
    try:
        data = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError:
        return None
    value = data.get("default_permissions")
    return value if isinstance(value, str) else None


def has_sandbox_mode(text: str) -> bool:
    try:
        data = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError:
        return False
    return "sandbox_mode" in data


def profile_denies(text: str) -> dict[str, str]:
    """The deny entries our profile currently holds: path -> effect."""
    try:
        data = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError:
        return {}
    node = data.get("permissions")
    for key in ("vaultshield", "filesystem"):
        node = node.get(key) if isinstance(node, dict) else None
    return {k: v for k, v in node.items() if isinstance(v, str)} if isinstance(node, dict) else {}


def external_change(original: str, current: str) -> bool:
    """Whether `current` differs from `original` anywhere outside our profile."""
    nl = _newline(current)
    span = _our_span(current, nl)
    if span is None:
        stripped = current
    else:
        start, end = span
        stripped = current[:start] + current[end:]
    try:
        reduced = tomllib.loads(stripped) if stripped.strip() else {}
        orig = tomllib.loads(original) if original.strip() else {}
    except tomllib.TOMLDecodeError:
        return True
    # our table may leave an empty [permissions] behind after stripping; normalise
    if reduced.get("permissions") == {}:
        reduced = {k: v for k, v in reduced.items() if k != "permissions"}
    if orig.get("permissions") == {}:
        orig = {k: v for k, v in orig.items() if k != "permissions"}
    return reduced != orig
