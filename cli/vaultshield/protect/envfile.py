"""Line-level handling of `.env` files, for a lossless protect/unprotect.

`credentials.parse_env_file` returns only (key, value) pairs, which is not
enough to put a file back byte for byte. These helpers walk the file line by
line using the SAME classification rules (`_ASSIGNMENT_RE`, `_unquote`, blank
and `#` lines), and `protector` cross-checks the result against
`parse_env_file` itself, so the two cannot drift apart silently.

Pure functions: no file or Keychain access.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from vaultshield.credentials import _ASSIGNMENT_RE, _unquote

_LINE_WITH_ENDING = re.compile(r"[^\n]*\n|[^\n]+")


@dataclass(frozen=True)
class Line:
    """One line of a file. `text` excludes the line ending, `ending` is
    `"\\r\\n"`, `"\\n"` or `""` (last line without a newline).

    `key` and `value` are set for `KEY=value` lines. `bad` marks a line that
    is not blank, not a `#` comment, and not `KEY=value`.
    """

    text: str
    ending: str
    key: str | None = None
    value: str | None = None
    bad: bool = False


def _classify(text: str) -> tuple[str | None, str | None, bool]:
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return None, None, False
    match = _ASSIGNMENT_RE.match(stripped)
    if not match:
        return None, None, True
    return match.group(1), _unquote(match.group(2)), False


def split_lines(content: str) -> list[Line]:
    """Splits on `\\n` only, keeping each line's own ending, so CRLF, LF and
    a missing final newline all survive a round trip."""
    lines: list[Line] = []
    for chunk in _LINE_WITH_ENDING.findall(content):
        if chunk.endswith("\r\n"):
            text, ending = chunk[:-2], "\r\n"
        elif chunk.endswith("\n"):
            text, ending = chunk[:-1], "\n"
        else:
            text, ending = chunk, ""
        key, value, bad = _classify(text)
        lines.append(Line(text, ending, key, value, bad))
    return lines


def join_lines(lines: list[Line]) -> str:
    return "".join(line.text + line.ending for line in lines)


def value_of(raw_line: str) -> str:
    """The value of one original `KEY=value` line, by `parse_env_file`'s rules."""
    _key, value, bad = _classify(raw_line)
    if bad or value is None:
        raise ValueError("not a KEY=value line")
    return value


def is_empty(value: str) -> bool:
    """An empty value has nothing to protect (same rule as `generate_dummy`)."""
    return not value.strip()


# What a Keychain item holds: the original line(s) of one variable, in file
# order, so duplicates, quotes, spacing and inline comments come back exactly.
def encode_record(raw_lines: list[str]) -> str:
    return json.dumps({"v": 1, "raw": raw_lines}, ensure_ascii=False, separators=(",", ":"))


def decode_record(text: str) -> list[str]:
    data = json.loads(text)
    raw = data["raw"] if isinstance(data, dict) and data.get("v") == 1 else None
    if not isinstance(raw, list) or not raw or not all(isinstance(r, str) for r in raw):
        raise ValueError("unrecognised Keychain record")
    return raw
