"""The JSON editor inserts text and leaves every other byte alone (fake data)."""

import copy
import json

import pytest

from protect_fakes import config_object, dump
from vaultshield.protect.agents.jsonedit import JsonEditError, add_to_string_array, set_member

DENY = ["userSettings", "globalPermissionGrants", "deny"]
RULES = ["read_file(/p/.env)", "read_file(/p/backend/.env.local)"]


def insertion(old: str, new: str) -> str:
    """The one chunk that was inserted into `old` to make `new`. Fails if `new`
    is not `old` with a single chunk added."""
    extra = len(new) - len(old)
    assert extra > 0, "nothing was inserted"
    p = 0
    while p < len(old) and old[p] == new[p]:
        p += 1
    assert new[:p] + new[p + extra :] == old, "more than a single insertion changed the text"
    return new[p : p + extra]


def expected_with(obj: dict, path: list[str], items: list[str]) -> dict:
    out = copy.deepcopy(obj)
    node = out
    for key in path[:-1]:
        node = node.setdefault(key, {})
    existing = node.setdefault(path[-1], [])
    existing.extend(i for i in items if i not in existing)
    return out


def test_a_missing_list_is_added_after_its_siblings_and_nothing_else_changes():
    old = dump(config_object())

    new = add_to_string_array(old, DENY, RULES)

    added = insertion(old, new)
    # lined up with its sibling "allow", which sits at 6 spaces
    assert added.startswith(',\n      "deny": [\n        "read_file(/p/.env)",\n        "read_file(/p/backend/.env.local)"\n      ]')
    assert json.loads(new) == expected_with(json.loads(old), DENY, RULES)


def test_items_are_appended_to_an_existing_list():
    obj = config_object(deny=["read_file(/mine/secret)"])
    old = dump(obj)

    new = add_to_string_array(old, DENY, RULES)

    added = insertion(old, new)
    # lined up with the existing item, at 8 spaces
    assert added == ',\n        "read_file(/p/.env)",\n        "read_file(/p/backend/.env.local)"'
    assert json.loads(new)["userSettings"]["globalPermissionGrants"]["deny"] == ["read_file(/mine/secret)", *RULES]


def test_an_empty_list_is_filled_in_place():
    old = dump(config_object(deny=[]))

    new = add_to_string_array(old, DENY, RULES)

    insertion(old, new)  # still a pure insertion, between the brackets
    assert json.loads(new)["userSettings"]["globalPermissionGrants"]["deny"] == RULES
    assert '"deny": [\n        "read_file(/p/.env)",\n        "read_file(/p/backend/.env.local)"\n      ]' in new


def test_an_item_that_is_already_there_is_not_added_again():
    old = dump(config_object(deny=[RULES[0]]))
    new = add_to_string_array(old, DENY, RULES)
    assert json.loads(new)["userSettings"]["globalPermissionGrants"]["deny"] == RULES


def test_adding_twice_changes_nothing_the_second_time():
    once = add_to_string_array(dump(config_object()), DENY, RULES)
    assert add_to_string_array(once, DENY, RULES) == once


def test_missing_objects_on_the_way_are_created():
    old = dump({"colorScheme": "dark"})

    new = add_to_string_array(old, ["permissions", "deny"], RULES)

    insertion(old, new)
    assert json.loads(new) == {"colorScheme": "dark", "permissions": {"deny": RULES}}


def test_a_missing_middle_object_is_created():
    old = dump({"userSettings": {"useAiCredits": True}})

    new = add_to_string_array(old, DENY, RULES)

    insertion(old, new)
    assert json.loads(new)["userSettings"] == {
        "useAiCredits": True,
        "globalPermissionGrants": {"deny": RULES},
    }


@pytest.mark.parametrize(
    "label, kwargs",
    [
        ("two-space indent", {"indent": 2}),
        ("four-space indent", {"indent": 4}),
        ("tab indent", {"indent": "\t"}),
        ("one line", {"indent": None}),
        ("no final newline", {"indent": 2, "final_newline": False}),
        ("CRLF line endings", {"indent": 2, "newline": "\r\n"}),
    ],
)
@pytest.mark.parametrize("deny", [None, [], ["read_file(/mine)"]], ids=["no-deny", "empty-deny", "has-deny"])
def test_every_layout_gets_a_single_insertion_and_the_right_meaning(label, kwargs, deny):
    obj = config_object(deny=deny)
    old = dump(obj, **kwargs)

    new = add_to_string_array(old, DENY, RULES)

    insertion(old, new)
    assert json.loads(new) == expected_with(obj, DENY, RULES)
    if kwargs.get("newline") == "\r\n":
        assert "\n" not in new.replace("\r\n", "")  # new lines use the file's own style
    if kwargs.get("final_newline") is False:
        assert not new.endswith("\n")


def test_unusual_spacing_around_colons_is_copied():
    old = '{\n  "userSettings" : {\n    "globalPermissionGrants" : {\n      "allow" : ["a"]\n    }\n  }\n}\n'

    new = add_to_string_array(old, DENY, RULES)

    assert '"deny" : [' in new
    assert json.loads(new)["userSettings"]["globalPermissionGrants"]["deny"] == RULES


def test_the_last_of_a_repeated_key_is_the_one_edited_like_json_loads_would_read():
    old = '{"permissions": {"deny": ["first"]}, "permissions": {"deny": ["second"]}}'

    new = add_to_string_array(old, ["permissions", "deny"], ["added"])

    assert json.loads(new) == {"permissions": {"deny": ["second", "added"]}}
    assert new.startswith('{"permissions": {"deny": ["first"]}, ')


def test_every_grant_is_left_byte_for_byte():
    obj = config_object()
    old = dump(obj)

    new = add_to_string_array(old, DENY, RULES)

    for grant in obj["userSettings"]["globalPermissionGrants"]["allow"]:
        assert json.dumps(grant, ensure_ascii=False) in new
    assert old.split('"allow"')[1].split("]")[0] in new


def test_empty_text_and_an_empty_object_make_a_new_document():
    assert json.loads(add_to_string_array("", ["permissions", "deny"], RULES)) == {"permissions": {"deny": RULES}}
    assert json.loads(add_to_string_array("  \n", ["permissions", "deny"], RULES)) == {"permissions": {"deny": RULES}}
    assert json.loads(add_to_string_array("{}", ["permissions", "deny"], RULES)) == {"permissions": {"deny": RULES}}
    assert json.loads(add_to_string_array("{\n}\n", ["permissions", "deny"], RULES)) == {"permissions": {"deny": RULES}}


@pytest.mark.parametrize(
    "text, path",
    [
        ("not json", ["a", "b"]),
        ("[1, 2]", ["a"]),
        ("\ufeff{}", ["a"]),
        ('{"permissions": []}', ["permissions", "deny"]),
        ('{"permissions": {"deny": "oops"}}', ["permissions", "deny"]),
        ('{"permissions": "x"}', ["permissions", "deny"]),
        ('{"a": 1} trailing', ["a", "b"]),
    ],
)
def test_text_that_cannot_be_edited_safely_is_refused(text, path):
    with pytest.raises(JsonEditError) as excinfo:
        add_to_string_array(text, path, RULES)
    assert "read_file" not in str(excinfo.value)


ENTRY = {"PreToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "x hook antigravity"}]}]}


def test_set_member_adds_a_key_beside_the_others():
    old = dump({"lint-checker": {"PostToolUse": []}})

    new = set_member(old, [], "vaultshield-protect", ENTRY)

    insertion(old, new)
    assert json.loads(new) == {"lint-checker": {"PostToolUse": []}, "vaultshield-protect": ENTRY}
    assert '\n  "vaultshield-protect": {\n    "PreToolUse": [\n      {' in new


def test_set_member_is_a_no_op_when_the_value_is_already_there():
    once = set_member("{}", [], "vaultshield-protect", ENTRY)
    assert set_member(once, [], "vaultshield-protect", ENTRY) == once


def test_set_member_replaces_only_our_own_key_when_it_differs():
    old = dump({"before": 1, "vaultshield-protect": {"PreToolUse": []}, "after": [1, 2]})

    new = set_member(old, [], "vaultshield-protect", ENTRY)

    assert json.loads(new) == {"before": 1, "vaultshield-protect": ENTRY, "after": [1, 2]}
    assert new.startswith('{\n  "before": 1,\n  "vaultshield-protect": {')
    assert new.endswith('"after": [\n    1,\n    2\n  ]\n}\n')


def test_set_member_into_an_empty_file_and_an_empty_object():
    assert json.loads(set_member("", [], "k", ENTRY)) == {"k": ENTRY}
    assert json.loads(set_member("{}", [], "k", ENTRY)) == {"k": ENTRY}


def test_set_member_refuses_a_top_level_list():
    with pytest.raises(JsonEditError):
        set_member("[]", [], "k", ENTRY)
