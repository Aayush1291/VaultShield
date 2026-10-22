"""Insert into a JSON text without touching any other byte.

Antigravity's `config.json` holds the user's own settings and command grants.
Parsing it and writing it back (`json.dump`) would re-format the whole file and
could change anything the parser does not preserve. These functions instead find
the exact spot in the original text and splice new text in there, so everything
outside that spot stays byte for byte identical: key order, indentation, spacing,
line endings, escapes, a missing final newline.

Only insertions are made, with two exceptions that touch just the value being
managed: an empty `[]` that gets its first items, and a hook entry that already
exists under our own key and is replaced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_WS = " \t\r\n"


class JsonEditError(Exception):
    """The text cannot be edited safely. The message never contains file content."""


@dataclass
class _Node:
    kind: str  # "object" | "array" | "string" | "other"
    start: int
    end: int
    members: list["_Member"] = field(default_factory=list)  # objects
    items: list["_Node"] = field(default_factory=list)  # arrays


@dataclass
class _Member:
    key: str
    key_start: int
    key_end: int
    value: _Node


class _Parser:
    """A JSON parser that records where each value starts and ends. The text is
    already known to be valid JSON, so this only has to find the spans."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def parse(self) -> _Node:
        node = self._value()
        self._skip()
        if self.i != len(self.text):
            raise JsonEditError("unexpected text after the JSON document")
        return node

    def _skip(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in _WS:
            self.i += 1

    def _value(self) -> _Node:
        self._skip()
        c = self.text[self.i]
        if c == "{":
            return self._object()
        if c == "[":
            return self._array()
        if c == '"':
            return self._string()
        return self._literal()

    def _string(self) -> _Node:
        if self.text[self.i] != '"':
            raise JsonEditError("expected a string")
        start = self.i
        self.i += 1
        while self.text[self.i] != '"':
            self.i += 2 if self.text[self.i] == "\\" else 1
        self.i += 1
        return _Node("string", start, self.i)

    def _literal(self) -> _Node:
        start = self.i
        while self.i < len(self.text) and self.text[self.i] not in ',]} \t\r\n':
            self.i += 1
        return _Node("other", start, self.i)

    def _object(self) -> _Node:
        node = _Node("object", self.i, self.i)
        self.i += 1
        self._skip()
        if self.text[self.i] == "}":
            self.i += 1
            node.end = self.i
            return node
        while True:
            self._skip()
            key_node = self._string()
            key = json.loads(self.text[key_node.start : key_node.end])
            self._skip()
            if self.text[self.i] != ":":
                raise JsonEditError("expected ':'")
            self.i += 1
            value = self._value()
            node.members.append(_Member(key, key_node.start, key_node.end, value))
            self._skip()
            c = self.text[self.i]
            self.i += 1
            if c == "}":
                break
            if c != ",":
                raise JsonEditError("expected ',' or '}'")
        node.end = self.i
        return node

    def _array(self) -> _Node:
        node = _Node("array", self.i, self.i)
        self.i += 1
        self._skip()
        if self.text[self.i] == "]":
            self.i += 1
            node.end = self.i
            return node
        while True:
            node.items.append(self._value())
            self._skip()
            c = self.text[self.i]
            self.i += 1
            if c == "]":
                break
            if c != ",":
                raise JsonEditError("expected ',' or ']'")
        node.end = self.i
        return node


def _parse(text: str) -> _Node:
    try:
        json.loads(text)
    except ValueError:
        raise JsonEditError("the file is not valid JSON") from None
    try:
        root = _Parser(text).parse()
    except IndexError:
        raise JsonEditError("the file could not be scanned") from None
    if root.kind != "object":
        raise JsonEditError("the top level of the file is not an object")
    return root


# --- text helpers ------------------------------------------------------------


def _last_member(node: _Node, key: str) -> _Member | None:
    """The member `json.loads` would keep when a key is repeated: the last one."""
    for member in reversed(node.members):
        if member.key == key:
            return member
    return None


def _gap_before(text: str, pos: int) -> str:
    """The whitespace immediately before `pos`."""
    start = pos
    while start > 0 and text[start - 1] in _WS:
        start -= 1
    return text[start:pos]


def _line_indent(text: str, pos: int) -> str:
    """The indentation of the line that contains `pos`."""
    line_start = text.rfind("\n", 0, pos) + 1
    end = line_start
    while end < pos and text[end] in " \t":
        end += 1
    return text[line_start:end]


def _unit(text: str) -> str:
    """One level of indentation, as this document writes it."""
    found = re.search(r"\n([ \t]+)\S", text)
    return found.group(1) if found else "  "


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _dump(value: object, gap: str, unit: str) -> str:
    """`value` as JSON, laid out to sit after `gap` (the whitespace that
    precedes a new member): on one line if the document is compact, otherwise
    indented to match."""
    if "\n" not in gap:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    member_indent = gap.rsplit("\n", 1)[-1]
    nl = "\r\n" if "\r\n" in gap else "\n"
    return json.dumps(value, ensure_ascii=False, indent=unit).replace("\n", nl + member_indent)


def _fresh(path: list[str], value: object) -> str:
    nested = value
    for key in reversed(path):
        nested = {key: nested}
    return json.dumps(nested, indent=2, ensure_ascii=False) + "\n"


def _locate(root: _Node, path: list[str]) -> tuple[_Node, int]:
    """The deepest node along `path` that exists, and how many keys that took."""
    node, depth = root, 0
    while depth < len(path):
        if node.kind != "object":
            where = ".".join(path[:depth]) or "the document"
            raise JsonEditError(f"{where} is not an object")
        member = _last_member(node, path[depth])
        if member is None:
            break
        node, depth = member.value, depth + 1
    return node, depth


def _insert_member(text: str, obj: _Node, key: str, value: object) -> str:
    if obj.kind != "object":
        raise JsonEditError("cannot add a key to something that is not an object")
    base = _line_indent(text, obj.start)
    if obj.members:
        last = obj.members[-1]
        gap = _gap_before(text, last.key_start)
        key_sep = text[last.key_end : last.value.start]
        member_indent = gap.rsplit("\n", 1)[-1]
        unit = member_indent[len(base) :] if member_indent.startswith(base) and member_indent != base else _unit(text)
        member = json.dumps(key, ensure_ascii=False) + key_sep + _dump(value, gap, unit)
        return text[: last.value.end] + "," + gap + member + text[last.value.end :]
    # an empty object
    if "\n" not in text:
        member = json.dumps(key, ensure_ascii=False) + ":" + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return text[: obj.start] + "{" + member + "}" + text[obj.end :]
    nl, unit = _newline(text), _unit(text)
    gap = nl + base + unit
    member = json.dumps(key, ensure_ascii=False) + ": " + _dump(value, gap, unit)
    return text[: obj.start] + "{" + gap + member + nl + base + "}" + text[obj.end :]


def _append_items(text: str, array: _Node, items: list[str]) -> str:
    present = {text_value for text_value in (_string_value(text, i) for i in array.items) if text_value is not None}
    new = []
    for item in items:
        if item not in present and item not in new:
            new.append(item)
    if not new:
        return text
    if array.items:
        last = array.items[-1]
        gap = _gap_before(text, last.start)
        added = "".join("," + gap + json.dumps(item, ensure_ascii=False) for item in new)
        return text[: last.end] + added + text[last.end :]
    # an empty array
    if "\n" not in text:
        return text[: array.start] + json.dumps(new, ensure_ascii=False, separators=(",", ":")) + text[array.end :]
    nl, base = _newline(text), _line_indent(text, array.start)
    inner = base + _unit(text)
    body = ("," + nl).join(inner + json.dumps(item, ensure_ascii=False) for item in new)
    return text[: array.start] + "[" + nl + body + nl + base + "]" + text[array.end :]


def _string_value(text: str, node: _Node) -> str | None:
    return json.loads(text[node.start : node.end]) if node.kind == "string" else None


# --- public ------------------------------------------------------------------


def add_to_string_array(text: str, path: list[str], items: list[str]) -> str:
    """Adds each of `items` to the list at `path` (for example
    `["permissions", "deny"]`) unless it is already there. Creates the list, and
    any object on the way to it, if they are missing. An empty `text` gives a
    new document."""
    if not text.strip():
        return _fresh(path, list(dict.fromkeys(items)))
    root = _parse(text)
    node, depth = _locate(root, path)
    if depth == len(path):
        if node.kind != "array":
            raise JsonEditError(f"{'.'.join(path)} exists but is not a list")
        return _append_items(text, node, items)
    nested: object = list(dict.fromkeys(items))
    for key in reversed(path[depth + 1 :]):
        nested = {key: nested}
    return _insert_member(text, node, path[depth], nested)


def set_member(text: str, parent_path: list[str], key: str, value: object) -> str:
    """Sets `key` to `value` inside the object at `parent_path` (use `[]` for the
    top level): added if missing, replaced only if it holds something else."""
    if not text.strip():
        return _fresh([*parent_path, key], value)
    root = _parse(text)
    parent, depth = _locate(root, parent_path)
    if depth < len(parent_path):
        nested: object = {key: value}
        for missing in reversed(parent_path[depth + 1 :]):
            nested = {missing: nested}
        return _insert_member(text, parent, parent_path[depth], nested)
    if parent.kind != "object":
        raise JsonEditError(f"{'.'.join(parent_path) or 'the document'} is not an object")
    existing = _last_member(parent, key)
    if existing is None:
        return _insert_member(text, parent, key, value)
    if json.loads(text[existing.value.start : existing.value.end]) == value:
        return text
    gap = _gap_before(text, existing.key_start)
    base = _line_indent(text, parent.start)
    member_indent = gap.rsplit("\n", 1)[-1]
    unit = member_indent[len(base) :] if member_indent.startswith(base) and member_indent != base else _unit(text)
    return text[: existing.value.start] + _dump(value, gap, unit) + text[existing.value.end :]
