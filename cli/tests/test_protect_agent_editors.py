"""The merge-only editors used by the new agents: jsonedit.add_object_to_array,
yamledit (permissions.yaml), tomledit (config.toml).

These check the byte-level guarantees directly: an insertion leaves every other
byte in place, idempotency, and the refusals that keep the installer from
touching a layout it cannot splice safely. No CLI, no Keychain, no real config.
"""

import json

import pytest
import tomllib
import yaml

from vaultshield.protect.agents import jsonedit, tomledit, yamledit


def one_insertion(old: str, new: str) -> str:
    """Asserts `new` is `old` with exactly one contiguous run inserted, and
    returns that run."""
    assert len(new) > len(old)
    extra = len(new) - len(old)
    p = 0
    while p < len(old) and old[p] == new[p]:
        p += 1
    assert new[:p] + new[p + extra :] == old, "more than a single insertion changed the text"
    return new[p : p + extra]


# --- jsonedit.add_object_to_array --------------------------------------------


def test_append_object_to_an_existing_array_is_one_insertion():
    old = '{\n  "hooks": {\n    "PreToolUse": [\n      {"matcher": "Edit"}\n    ]\n  }\n}\n'
    new = jsonedit.add_object_to_array(old, ["hooks", "PreToolUse"], {"matcher": "Bash"})
    one_insertion(old, new)
    data = json.loads(new)
    assert [g["matcher"] for g in data["hooks"]["PreToolUse"]] == ["Edit", "Bash"]


def test_append_creates_the_nested_path_when_missing():
    old = '{\n  "model": "x"\n}\n'
    new = jsonedit.add_object_to_array(old, ["hooks", "PreToolUse"], {"matcher": "Bash"})
    assert json.loads(new) == {"model": "x", "hooks": {"PreToolUse": [{"matcher": "Bash"}]}}
    assert '"model": "x"' in new


def test_append_is_idempotent_for_an_equal_object():
    old = '{"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}'
    assert jsonedit.add_object_to_array(old, ["hooks", "PreToolUse"], {"matcher": "Bash"}) == old


def test_append_into_an_empty_array():
    old = '{\n  "hooks": {\n    "PreToolUse": []\n  }\n}\n'
    new = jsonedit.add_object_to_array(old, ["hooks", "PreToolUse"], {"a": 1})
    assert json.loads(new)["hooks"]["PreToolUse"] == [{"a": 1}]


def test_append_into_an_empty_document():
    new = jsonedit.add_object_to_array("", ["hooks", "PreToolUse"], {"a": 1})
    assert json.loads(new) == {"hooks": {"PreToolUse": [{"a": 1}]}}


def test_append_keeps_crlf_line_endings():
    old = '{\r\n  "hooks": {\r\n    "PreToolUse": [\r\n      {"matcher": "Edit"}\r\n    ]\r\n  }\r\n}\r\n'
    new = jsonedit.add_object_to_array(old, ["hooks", "PreToolUse"], {"matcher": "Bash"})
    assert "\r\n" in one_insertion(old, new)
    assert json.loads(new)["hooks"]["PreToolUse"][-1] == {"matcher": "Bash"}


def test_append_refuses_when_the_target_is_not_a_list():
    with pytest.raises(jsonedit.JsonEditError):
        jsonedit.add_object_to_array('{"hooks": {"PreToolUse": {}}}', ["hooks", "PreToolUse"], {"a": 1})


# --- yamledit (permissions.yaml) ---------------------------------------------

EXISTING = "# my kiro perms\nrules:\n  - capability: shell\n    match: [\"git *\"]\n    effect: allow\nsettingsVersion: 2\n"


def test_yaml_appends_into_an_existing_block_list_keeping_comments_and_other_keys():
    new = yamledit.add_fs_read_denies(EXISTING, ["/p/.env"])
    added = one_insertion(EXISTING, new)
    assert "fs_read" in added
    assert "# my kiro perms" in new
    data = yaml.safe_load(new)
    assert data["settingsVersion"] == 2
    caps = [r["capability"] for r in data["rules"]]
    assert caps == ["shell", "fs_read"]
    assert data["rules"][-1] == {"capability": "fs_read", "effect": "deny", "match": ["/p/.env"]}


def test_yaml_is_idempotent_per_path():
    once = yamledit.add_fs_read_denies(EXISTING, ["/p/.env"])
    twice = yamledit.add_fs_read_denies(once, ["/p/.env"])
    assert twice == once


def test_yaml_adds_only_the_new_paths():
    once = yamledit.add_fs_read_denies(EXISTING, ["/p/.env"])
    two = yamledit.add_fs_read_denies(once, ["/p/.env", "/p/b/.env.local"])
    data = yaml.safe_load(two)
    matches = [r["match"][0] for r in data["rules"] if r["capability"] == "fs_read"]
    assert matches == ["/p/.env", "/p/b/.env.local"]


def test_yaml_fresh_file():
    new = yamledit.add_fs_read_denies("", ["/p/.env"])
    assert yaml.safe_load(new)["rules"][0]["match"] == ["/p/.env"]


def test_yaml_appends_a_rules_block_when_absent():
    old = "settingsVersion: 2\n"
    new = yamledit.add_fs_read_denies(old, ["/p/.env"])
    assert new.startswith("settingsVersion: 2\n")
    assert yaml.safe_load(new)["rules"][0]["capability"] == "fs_read"


def test_yaml_refuses_a_flow_style_rules_list():
    with pytest.raises(yamledit.YamlEditError):
        yamledit.add_fs_read_denies("rules: []\n", ["/p/.env"])


def test_yaml_refuses_a_non_mapping_top_level():
    with pytest.raises(yamledit.YamlEditError):
        yamledit.add_fs_read_denies("- a\n- b\n", ["/p/.env"])


def test_yaml_external_change_detects_an_unrelated_edit():
    installed = yamledit.add_fs_read_denies(EXISTING, ["/p/.env"])
    assert not yamledit.external_change(EXISTING, installed)
    edited = installed.replace("settingsVersion: 2", "settingsVersion: 3")
    assert yamledit.external_change(EXISTING, edited)


# --- tomledit (config.toml) --------------------------------------------------

CONFIG = 'model = "o3"\n\n[tui]\ntheme = "dark"\n'


def test_toml_appends_our_profile_keeping_other_tables():
    new = tomledit.set_profile(CONFIG, ["/p/.env"])
    added = one_insertion(CONFIG, new)
    assert "[permissions.vaultshield]" in added
    data = tomllib.loads(new)
    assert data["model"] == "o3" and data["tui"]["theme"] == "dark"
    assert data["permissions"]["vaultshield"]["extends"] == ":workspace"
    assert data["permissions"]["vaultshield"]["filesystem"]["/p/.env"] == "deny"


def test_toml_never_selects_the_profile_or_sets_sandbox_mode():
    new = tomledit.set_profile(CONFIG, ["/p/.env"])
    assert "default_permissions" not in new
    assert "sandbox_mode" not in new


def test_toml_is_idempotent():
    once = tomledit.set_profile(CONFIG, ["/p/.env"])
    assert tomledit.set_profile(once, ["/p/.env"]) == once


def test_toml_replaces_our_profile_in_place_leaving_other_tables():
    once = tomledit.set_profile(CONFIG, ["/p/.env"])
    tail = once + '\n[other]\nk = 1\n'
    updated = tomledit.set_profile(tail, ["/p/.env", "/p/b/.env"])
    data = tomllib.loads(updated)
    assert set(data["permissions"]["vaultshield"]["filesystem"]) == {"/p/.env", "/p/b/.env"}
    assert data["other"] == {"k": 1}
    assert data["model"] == "o3"


def test_toml_refuses_invalid_toml():
    with pytest.raises(tomledit.TomlEditError):
        tomledit.set_profile("this = = broken\n", ["/p/.env"])


def test_toml_fresh_file():
    new = tomledit.set_profile("", ["/p/.env"])
    assert tomllib.loads(new)["permissions"]["vaultshield"]["filesystem"]["/p/.env"] == "deny"


def test_toml_reads_selection_and_sandbox_and_denies():
    new = tomledit.set_profile(CONFIG, ["/p/.env"])
    assert tomledit.selected_profile(new) is None
    assert tomledit.profile_denies(new) == {"/p/.env": "deny"}
    assert not tomledit.has_sandbox_mode(new)
    # default_permissions / sandbox_mode are top-level keys: they go above any table
    selected = 'default_permissions = "vaultshield"\n' + new
    assert tomledit.selected_profile(selected) == "vaultshield"
    assert tomledit.has_sandbox_mode('sandbox_mode = "workspace-write"\n' + new)


def test_toml_external_change():
    installed = tomledit.set_profile(CONFIG, ["/p/.env"])
    assert not tomledit.external_change(CONFIG, installed)
    edited = installed.replace('theme = "dark"', 'theme = "light"')
    assert tomledit.external_change(CONFIG, edited)
