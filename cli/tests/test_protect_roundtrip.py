"""Step 7: install then uninstall gives back every byte, and no other key changes,
across many file layouts. Fake files in a temporary home only."""

import copy
import json

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

import protect_fakes as fakes
from vaultshield.cli import cli
from vaultshield.protect.agents.jsonedit import add_to_string_array, set_member

KEY_VALUE = "sk-test-AAAA1111BBBB2222"

LAYOUTS = {
    "indent 2": dict(indent=2),
    "indent 4": dict(indent=4),
    "tabs": dict(indent="\t"),
    "single line": dict(indent=None),
    "no final newline": dict(indent=2, final_newline=False),
    "CRLF": dict(indent=2, newline="\r\n"),
    "CRLF, no final newline": dict(indent=4, newline="\r\n", final_newline=False),
}


def run(*args):
    return CliRunner().invoke(cli, list(args))


@pytest.fixture
def project(protect_project):
    (protect_project / ".env").write_text(f"FAKE_KEY={KEY_VALUE}\n")
    (protect_project / "backend").mkdir()
    (protect_project / "backend" / ".env.local").write_text("TOKEN=fake-token-9999\n")
    (protect_project / "id_rsa").write_text("-----BEGIN FAKE RSA-----\n")
    assert run("protect").exit_code == 0
    return protect_project


def normalised(obj: dict, kind: str, ours=()) -> dict:
    """`obj` with our rules (and our hook) taken out, and a missing deny list
    treated the same as an empty one, so before and after can be compared."""
    data = copy.deepcopy(obj)
    if kind == "hook":
        data.pop("vaultshield-protect", None)
        return data
    node = data
    for key in (["permissions"] if kind == "cli" else ["userSettings", "globalPermissionGrants"]):
        node = node.setdefault(key, {})
    node["deny"] = [rule for rule in node.get("deny", []) if rule not in ours]
    return data


def our_rules(project) -> list[str]:
    from vaultshield.protect import manifest as manifest_mod
    from vaultshield.protect.agents import antigravity

    return antigravity.deny_rules(project, manifest_mod.load(project))


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
@pytest.mark.parametrize("mine", [None, [], ["read_file(/Users/fake/private)"]], ids=["no-deny", "empty-deny", "has-deny"])
def test_install_then_uninstall_restores_every_byte(project, tmp_path, layout, mine):
    options = LAYOUTS[layout]
    settings = fakes.cli_settings_object()
    if mine is not None:
        settings["permissions"]["deny"] = list(mine)
    home = fakes.make_home(
        tmp_path,
        config_text=fakes.dump(fakes.config_object(deny=None if mine is None else list(mine)), **options),
        settings_text=fakes.dump(settings, **options),
        hooks_text=fakes.dump(fakes.hooks_object(), **options),
    )
    originals = fakes.snapshot(home)

    installed = run("agents", "install", "antigravity", "--home", str(home["home"]))
    assert installed.exit_code == 0, installed.output
    after = fakes.snapshot(home)
    assert after != originals

    # nothing but our additions changed, in meaning...
    rules = our_rules(project)
    for key, kind in (("cli", "cli"), ("app", "app"), ("hooks", "hook")):
        assert normalised(json.loads(after[key]), kind, rules) == normalised(json.loads(originals[key]), kind), key
    assert set(rules) <= set(json.loads(after["cli"])["permissions"]["deny"])
    assert set(rules) <= set(json.loads(after["app"])["userSettings"]["globalPermissionGrants"]["deny"])
    assert "vaultshield-protect" in json.loads(after["hooks"])
    # ...and in bytes: the original text is the new text with one chunk taken out
    for key in ("cli", "app", "hooks"):
        old, new = originals[key].decode(), after[key].decode()
        extra = len(new) - len(old)
        p = 0
        while p < len(old) and old[p] == new[p]:
            p += 1
        assert new[:p] + new[p + extra :] == old, key

    assert run("agents", "uninstall", "antigravity", "--home", str(home["home"])).exit_code == 0
    assert fakes.snapshot(home) == originals


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_install_is_stable_under_every_layout(project, tmp_path, layout):
    options = LAYOUTS[layout]
    home = fakes.make_home(
        tmp_path,
        config_text=fakes.dump(fakes.config_object(), **options),
        settings_text=fakes.dump(fakes.cli_settings_object(), **options),
        hooks_text=fakes.dump(fakes.hooks_object(), **options),
    )
    run("agents", "install", "antigravity", "--home", str(home["home"]))
    once = fakes.snapshot(home)

    run("agents", "install", "antigravity", "--home", str(home["home"]))

    assert fakes.snapshot(home) == once


def test_a_file_with_no_files_at_all_round_trips(project, tmp_path):
    home = fakes.make_home(tmp_path, cli=False, app=True, hooks=False)
    originals = fakes.snapshot(home)

    run("agents", "install", "antigravity", "--home", str(home["home"]))
    run("agents", "uninstall", "antigravity", "--home", str(home["home"]))

    assert fakes.snapshot(home) == originals


# --- a property test of the editor itself ------------------------------------------

_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-10**6, 10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=12),
)
_values = st.recursive(
    _scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=6), inner, max_size=4)),
    max_leaves=12,
)
_layouts = st.sampled_from(
    [
        dict(indent=None),
        dict(indent=None, separators=(",", ":")),
        dict(indent=0),
        dict(indent=1),
        dict(indent=2),
        dict(indent=4),
        dict(indent="\t"),
    ]
)


@st.composite
def documents(draw):
    """A random JSON object that contains {"a": {"b": <missing | [] | list of strings>}} somewhere
    among unrelated keys, written in a random layout."""
    inner = draw(st.dictionaries(st.text(min_size=1, max_size=6), _values, max_size=4))
    state = draw(st.sampled_from(["missing", "empty", "strings"]))
    if state == "empty":
        inner["deny"] = []
    elif state == "strings":
        inner["deny"] = draw(st.lists(st.text(max_size=10), min_size=1, max_size=4))
    else:
        inner.pop("deny", None)
    outer = draw(st.dictionaries(st.text(min_size=1, max_size=6), _values, max_size=4))
    outer["a"] = inner
    options = dict(draw(_layouts))
    options["ensure_ascii"] = draw(st.booleans())
    text = json.dumps(outer, **options)
    if draw(st.booleans()):
        text += "\n"
    if draw(st.booleans()):
        text = text.replace("\n", "\r\n")
    return outer, text


@given(documents(), st.lists(st.text(min_size=1, max_size=12), min_size=1, max_size=4))
def test_the_editor_adds_the_items_and_nothing_else_for_any_layout(doc, items):
    obj, old = doc

    new = add_to_string_array(old, ["a", "deny"], items)

    expected = copy.deepcopy(obj)
    deny = expected["a"].setdefault("deny", [])
    deny.extend(i for i in dict.fromkeys(items) if i not in deny)
    assert json.loads(new) == expected
    # the original is the new text with one contiguous chunk removed
    extra = len(new) - len(old)
    assert extra >= 0
    if extra:
        p = 0
        while p < len(old) and old[p] == new[p]:
            p += 1
        assert new[:p] + new[p + extra :] == old
    else:
        assert new == old
    # adding the same items again changes nothing
    assert add_to_string_array(new, ["a", "deny"], items) == new


@given(documents(), _values)
def test_set_member_adds_one_key_and_nothing_else_for_any_layout(doc, value):
    obj, old = doc
    obj.pop("hook", None)
    old = json.dumps(obj, indent=2) + "\n"

    new = set_member(old, [], "hook", value)

    expected = copy.deepcopy(obj)
    expected["hook"] = value
    assert json.loads(new) == expected
    extra = len(new) - len(old)
    p = 0
    while p < len(old) and old[p] == new[p]:
        p += 1
    assert new[:p] + new[p + extra :] == old
