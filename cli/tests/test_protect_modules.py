"""Step 8: unit tests for the small Protect modules (envfile, manifest, fsutil,
protector.analyze, execute.prepare). All values are fake."""

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from vaultshield.credentials import parse_env_file
from vaultshield.protect import PLACEHOLDER, execute, fsutil, manifest as manifest_mod, protector
from vaultshield.protect.envfile import (
    decode_record,
    encode_record,
    is_empty,
    join_lines,
    split_lines,
    value_of,
)
from vaultshield.protect.manifest import Manifest, ManifestError, parse_manifest
from vaultshield.protect.vault import Vault

# --- envfile -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "\n", "A=1\n", "A=1", "A=1\r\nB=2\r\n", "A=1\nB=2\r\n\n", "\r\n", "# c\n\nA=1", "A=1\rB=2\n"],
)
def test_split_and_join_are_lossless(text):
    assert join_lines(split_lines(text)) == text


def test_split_lines_keeps_each_lines_own_ending():
    lines = split_lines("A=1\r\nB=2\nC=3")
    assert [(l.text, l.ending) for l in lines] == [("A=1", "\r\n"), ("B=2", "\n"), ("C=3", "")]


def test_lines_are_classified_like_parse_env_file():
    lines = split_lines(
        "# comment\n\n  # indented\nA=1\n B = \"two words\" \nC=\nexport D=4\nE: 5\n9X=6\nF='x'\n"
    )
    assert [(l.key, l.value, l.bad) for l in lines] == [
        (None, None, False),
        (None, None, False),
        (None, None, False),
        ("A", "1", False),
        ("B", "two words", False),
        ("C", "", False),
        (None, None, True),  # export D=4
        (None, None, True),  # E: 5
        (None, None, True),  # 9X=6 (not a valid name)
        ("F", "x", False),
    ]


def test_an_inline_comment_stays_part_of_the_value_like_parse_env_file():
    assert value_of("A=abc # note") == "abc # note"


def test_value_of_rejects_lines_that_are_not_assignments():
    for text in ("# comment", "", "export A=1", "just words"):
        with pytest.raises(ValueError):
            value_of(text)


def test_is_empty():
    assert is_empty("") and is_empty("   ")
    assert not is_empty("x") and not is_empty(" x ")


def test_records_round_trip():
    raws = ['A = "it\'s fake" # note', "A=second", "A=ünï"]
    assert decode_record(encode_record(raws)) == raws


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"v": 2, "raw": ["A=1"]}',
        '{"v": 1}',
        '{"v": 1, "raw": []}',
        '{"v": 1, "raw": [1]}',
        '{"v": 1, "raw": "A=1"}',
    ],
)
def test_bad_records_are_rejected(text):
    with pytest.raises(ValueError):
        decode_record(text)


_LINE_TEMPLATES = [
    "KEY{n}=value{n}",
    "KEY{n}='single {n}'",
    'KEY{n}="double {n}"',
    "KEY{n}=has # hash {n}",
    "  KEY{n} = spaced {n}  ",
    "KEY{n}=",
    "# comment {n}",
    "",
    "   ",
    "KEY{n}=a=b=c",
]


@given(
    lines=st.lists(
        st.tuples(st.sampled_from(_LINE_TEMPLATES), st.sampled_from(["\n", "\r\n"]), st.integers(0, 9)),
        max_size=12,
    ),
    final_newline=st.booleans(),
)
def test_the_line_walk_agrees_with_parse_env_file(lines, final_newline):
    text = "".join(t.format(n=n) + e for t, e, n in lines)
    if text and not final_newline:
        text = text.rstrip("\r\n")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_bytes(text.encode())
        mine = [(l.key, l.value) for l in split_lines(text) if l.key is not None]
        assert mine == parse_env_file(path)
    assert join_lines(split_lines(text)) == text


# --- manifest ----------------------------------------------------------------


def test_manifest_round_trip_is_sorted_and_versioned(tmp_path):
    m = Manifest("pid", {"b/.env": ["Z", "A"], ".env": ["K"]}, ["x.pem", "a.key"])
    manifest_mod.save(tmp_path, m)

    data = json.loads((tmp_path / ".vaultshield" / "protect.json").read_text())
    assert data == {
        "version": 1,
        "project_id": "pid",
        "protected": {".env": ["K"], "b/.env": ["Z", "A"]},  # names keep file order
        "detected": ["a.key", "x.pem"],
    }
    assert manifest_mod.load(tmp_path) == Manifest(
        "pid", {"b/.env": ["Z", "A"], ".env": ["K"]}, ["a.key", "x.pem"]
    )


def test_manifest_load_without_a_file_is_none(tmp_path):
    assert manifest_mod.load(tmp_path) is None


def test_all_paths_is_the_sorted_union(tmp_path):
    m = Manifest("pid", {"b/.env": ["A"], ".env": ["K"]}, ["x.pem", ".env"])
    assert m.all_paths() == [".env", "b/.env", "x.pem"]


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"version": 2, "project_id": "p", "protected": {}, "detected": []},
        {"version": 1, "protected": {}, "detected": []},
        {"version": 1, "project_id": "", "protected": {}, "detected": []},
        {"version": 1, "project_id": "p", "protected": [], "detected": []},
        {"version": 1, "project_id": "p", "protected": {}, "detected": {}},
        {"version": 1, "project_id": "p", "protected": {"/abs/.env": ["A"]}, "detected": []},
        {"version": 1, "project_id": "p", "protected": {"../x/.env": ["A"]}, "detected": []},
        {"version": 1, "project_id": "p", "protected": {"a/../../.env": ["A"]}, "detected": []},
        {"version": 1, "project_id": "p", "protected": {".env": "A"}, "detected": []},
        {"version": 1, "project_id": "p", "protected": {".env": [""]}, "detected": []},
        {"version": 1, "project_id": "p", "protected": {".env": [1]}, "detected": []},
        {"version": 1, "project_id": "p", "protected": {}, "detected": ["../k.pem"]},
        {"version": 1, "project_id": "p", "protected": {}, "detected": [3]},
    ],
)
def test_invalid_manifests_are_rejected(data):
    with pytest.raises(ManifestError):
        parse_manifest(data)


def test_a_manifest_that_is_not_json_is_rejected(tmp_path):
    (tmp_path / ".vaultshield").mkdir()
    (tmp_path / ".vaultshield" / "protect.json").write_bytes(b"\xff\xfe")
    with pytest.raises(ManifestError):
        manifest_mod.load(tmp_path)


def test_find_root_walks_up_to_the_nearest_manifest(tmp_path):
    manifest_mod.save(tmp_path, Manifest("pid"))
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert manifest_mod.find_root(deep) == tmp_path.resolve()
    assert manifest_mod.find_root(tmp_path) == tmp_path.resolve()


def test_find_root_without_a_manifest(tmp_path):
    (tmp_path / "a").mkdir()
    assert manifest_mod.find_root(tmp_path / "a") is None


def test_delete_removes_an_empty_vaultshield_directory_but_not_a_used_one(tmp_path):
    manifest_mod.save(tmp_path, Manifest("pid"))
    manifest_mod.delete(tmp_path)
    assert not (tmp_path / ".vaultshield").exists()

    manifest_mod.save(tmp_path, Manifest("pid"))
    (tmp_path / ".vaultshield" / "runs").mkdir()
    manifest_mod.delete(tmp_path)
    assert (tmp_path / ".vaultshield" / "runs").is_dir()
    assert not manifest_mod.manifest_path(tmp_path).exists()


# --- fsutil ------------------------------------------------------------------


def test_atomic_write_replaces_the_file_and_keeps_its_mode(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"old")
    path.chmod(0o600)

    fsutil.atomic_write_bytes(path, b"new")

    assert path.read_bytes() == b"new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


def test_atomic_write_of_a_new_file_creates_directories_with_a_default_mode(tmp_path):
    path = tmp_path / "a" / "b" / "f"
    fsutil.atomic_write_bytes(path, b"x")
    assert path.read_bytes() == b"x"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_a_failed_atomic_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "f"
    path.write_bytes(b"old")

    def boom(src, dst):
        raise OSError("no")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write_bytes(path, b"new")

    assert path.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


# --- protector.analyze -------------------------------------------------------


def test_analyze_groups_real_placeholder_and_empty_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"A=real-a\nB={PLACEHOLDER}\nC=\nA=real-a2\nD=\"\"\n# note\n")

    result = protector.analyze(env)

    assert result.refusal == ""
    assert result.real == {"A": [0, 3]}
    assert result.placeholders == {"B": [1]}


def test_analyze_refuses_a_partly_protected_variable(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"A={PLACEHOLDER}\nA=real\n")
    result = protector.analyze(env)
    assert "partly protected" in result.refusal and "A" in result.refusal


def test_analyze_of_a_missing_file_refuses(tmp_path):
    assert "could not be read" in protector.analyze(tmp_path / "nope").refusal


# --- execute.prepare ---------------------------------------------------------


def test_prepare_redacts_only_scrubbable_values_and_hides_them_from_repr(protect_project):
    from click.testing import CliRunner

    from vaultshield.cli import cli

    (protect_project / ".env").write_text("PORT=3000\nDEBUG=true\nSECRET=fake-long-secret-1\nTINY=ab\n")
    CliRunner().invoke(cli, ["protect"])

    prepared = execute.prepare(protect_project, {"PATH": "/usr/bin"})

    assert prepared.env["PORT"] == "3000" and prepared.env["TINY"] == "ab"
    assert prepared.secret_values == ["fake-long-secret-1"]
    assert "fake-long-secret-1" not in repr(prepared)
    assert prepared.cwd == protect_project.resolve()


def test_prepare_lets_the_existing_environment_win_and_does_not_redact_that_value(protect_project):
    from click.testing import CliRunner

    from vaultshield.cli import cli

    (protect_project / ".env").write_text("SECRET=fake-long-secret-1\n")
    CliRunner().invoke(cli, ["protect"])

    prepared = execute.prepare(protect_project, {"SECRET": "from-the-parent"})

    assert prepared.env["SECRET"] == "from-the-parent"
    assert prepared.secret_values == []


def test_prepare_names_a_variable_with_no_keychain_value(protect_project, fake_keyring):
    from click.testing import CliRunner

    from vaultshield.cli import cli

    (protect_project / ".env").write_text("A=fake-aaaa-1111\nB=fake-bbbb-2222\n")
    CliRunner().invoke(cli, ["protect"])
    project_id = manifest_mod.load(protect_project).project_id
    Vault(project_id).remove(".env", "A")

    prepared = execute.prepare(protect_project, {})

    assert prepared.missing == [".env: A"]
    assert "A" not in prepared.env
    assert prepared.env["B"] == "fake-bbbb-2222"
