"""Add `fs_read` deny rules to Kiro's `permissions.yaml` without disturbing the
rest of the file.

Like `jsonedit`, this only inserts: it finds the `rules:` block sequence and
appends our rule items after the last one, leaving every existing byte in place.
It refuses (raising `YamlEditError`, so the installer changes nothing) any layout
it cannot splice safely: a top level that is not a mapping, a `rules:` written in
flow style (`rules: [ ... ]`), or a `rules:` that has no block items to extend.

Each rule we add is a one-line flow mapping, which is valid as an item of a block
sequence and is trivial to detect for idempotency:

    - {capability: fs_read, effect: deny, match: ["/abs/path"]}
"""

from __future__ import annotations

import re

import yaml


class YamlEditError(Exception):
    """The file cannot be edited safely. The message never contains file content."""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _flow_item(indent: str, path: str) -> str:
    return f'{indent}- {{capability: fs_read, effect: deny, match: ["{_escape(path)}"]}}'


def _rules(parsed: object) -> list:
    if isinstance(parsed, dict) and isinstance(parsed.get("rules"), list):
        return parsed["rules"]
    return []


def _looks_ours(rule: object) -> bool:
    return (
        isinstance(rule, dict)
        and rule.get("capability") == "fs_read"
        and rule.get("effect") == "deny"
        and isinstance(rule.get("match"), list)
    )


def already_denied(parsed: object, path: str) -> bool:
    return any(_looks_ours(rule) and path in rule["match"] for rule in _rules(parsed))


def add_fs_read_denies(text: str, paths: list[str]) -> str:
    """Adds one `fs_read` deny rule per path, unless that path is already denied
    for reading. Creates `rules:` if it is missing. Empty `text` gives a new file."""
    wanted = list(dict.fromkeys(paths))
    if not text.strip():
        items = "\n".join(_flow_item("  ", p) for p in wanted)
        return "rules:\n" + items + "\n"

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        raise YamlEditError("permissions.yaml is not valid YAML") from None
    if parsed is not None and not isinstance(parsed, dict):
        raise YamlEditError("the top level of permissions.yaml is not a mapping")

    new = [p for p in wanted if not already_denied(parsed, p)]
    if not new:
        return text
    if not isinstance(parsed, dict) or "rules" not in parsed:
        return _append_rules_block(text, new)
    if not _rules(parsed):
        raise YamlEditError("`rules` is empty or not a block list; add the fs_read deny rule by hand")
    return _append_items(text, new)


def _append_rules_block(text: str, new: list[str]) -> str:
    nl = _newline(text)
    base = text if text.endswith("\n") else text + nl
    items = nl.join(_flow_item("  ", p) for p in new)
    return base + "rules:" + nl + items + nl


def _append_items(text: str, new: list[str]) -> str:
    nl = _newline(text)
    lines = text.split(nl)
    rules_line = None
    for i, line in enumerate(lines):
        if re.match(r"^rules:[ \t]*$", line) or re.match(r"^rules:[ \t]*#", line):
            rules_line = i
            break
    if rules_line is None:
        raise YamlEditError("`rules:` is written inline; add the fs_read deny rule by hand")

    item_indent: str | None = None
    last = rules_line
    j = rules_line + 1
    while j < len(lines):
        line = lines[j]
        if line.strip() == "":
            j += 1
            continue
        if not line[:1].isspace():
            break  # a new top-level key ends the rules block
        match = re.match(r"^(\s+)-\s", line)
        if match and item_indent is None:
            item_indent = match.group(1)
        last = j
        j += 1
    if item_indent is None:
        raise YamlEditError("`rules:` has no block items to extend; add the fs_read deny rule by hand")

    additions = [_flow_item(item_indent, p) for p in new]
    lines[last + 1 : last + 1] = additions
    return nl.join(lines)


def external_change(original: str, current: str) -> bool:
    """Whether `current` holds anything beyond the original plus our own fs_read
    deny rules. Used to decide whether to keep a copy before uninstall."""
    try:
        orig = yaml.safe_load(original) if original.strip() else None
        cur = yaml.safe_load(current) if current.strip() else None
    except yaml.YAMLError:
        return True
    orig = orig if isinstance(orig, dict) else {}
    if not isinstance(cur, dict):
        return cur != (orig or None)
    orig_rules = _rules(orig)
    cur_rules = _rules(cur)
    ours = [r for r in cur_rules if _looks_ours(r) and r not in orig_rules]
    reduced = [r for r in cur_rules if r not in ours]
    cur2 = dict(cur)
    if reduced or "rules" in orig:
        cur2["rules"] = reduced
    else:
        cur2.pop("rules", None)
    return cur2 != orig
