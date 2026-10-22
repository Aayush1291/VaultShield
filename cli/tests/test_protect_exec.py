"""Step 5: `vaultshield exec` and the output scrubber (all values are fake)."""

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from vaultshield.cli import cli
from vaultshield.protect import PLACEHOLDER
from vaultshield.protect.execute import StreamScrubber, scrubbable
from vaultshield.protect.vault import Vault
from vaultshield.token_hygiene import SecretSet

KEY_VALUE = "sk-test-AAAA1111BBBB2222"
DB_VALUE = "postgres://u:p@localhost/db"


def run(*args, env=None):
    return CliRunner().invoke(cli, list(args), env=env)


def py(code, *argv):
    """exec arguments that run `code` in a child Python."""
    return ["exec", "--", sys.executable, "-c", code, *argv]


def protect(project, rel=".env", text=None):
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else f"FAKE_KEY={KEY_VALUE}\nDB_URL={DB_VALUE}\n")
    result = run("protect")
    assert result.exit_code == 0, result.output
    return path


CHECK = "import os,sys; sys.exit(0 if os.environ.get(sys.argv[1]) == sys.argv[2] else 7)"


# --- what is redacted --------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (KEY_VALUE, True),
        ("abcd", True),
        ("abc", False),  # shorter than 4
        ("3000", False),  # only digits
        ("0000000000", False),
        ("true", False),
        ("FALSE", False),
        ("Yes", False),
        ("no", False),
        ("on", False),
        ("off", False),
        ("1", False),
        ("0", False),
        ("trueish", True),
        ("3000a", True),
        ("production", True),
    ],
)
def test_scrubbable(value, expected):
    assert scrubbable(value) is expected


# --- the child gets the real values -----------------------------------------


def test_the_child_gets_the_real_values(protect_project):
    protect(protect_project)

    assert run(*py(CHECK, "FAKE_KEY", KEY_VALUE)).exit_code == 0
    assert run(*py(CHECK, "DB_URL", DB_VALUE)).exit_code == 0


def test_quotes_are_removed_and_the_last_duplicate_wins(protect_project):
    protect(protect_project, text='QUOTED="fake quoted value"\nDUP=first-fake-1\nDUP=second-fake-2\n')

    assert run(*py(CHECK, "QUOTED", "fake quoted value")).exit_code == 0
    assert run(*py(CHECK, "DUP", "second-fake-2")).exit_code == 0


def test_an_existing_environment_variable_wins(protect_project):
    protect(protect_project)

    result = run(*py(CHECK, "FAKE_KEY", "from-the-parent"), env={"FAKE_KEY": "from-the-parent"})

    assert result.exit_code == 0
    # the other variable is still injected
    assert run(*py(CHECK, "DB_URL", DB_VALUE), env={"FAKE_KEY": "from-the-parent"}).exit_code == 0


def test_the_childs_arguments_are_passed_through(protect_project):
    protect(protect_project)
    result = run("exec", "--", sys.executable, "-c", "import sys; print(sys.argv[1:])", "-x", "--flag", "v")
    assert result.exit_code == 0
    assert "['-x', '--flag', 'v']" in result.stdout


# --- output is scrubbed -------------------------------------------------------


def test_values_in_stdout_and_stderr_are_replaced_with_stars(protect_project):
    protect(protect_project)
    code = (
        "import os,sys; "
        "print('key is', os.environ['FAKE_KEY'], 'ok'); "
        "print('db', os.environ['DB_URL'], file=sys.stderr)"
    )

    result = run(*py(code))

    assert result.exit_code == 0
    assert result.stdout == "key is *** ok\n"
    assert result.stderr == "db ***\n"
    assert KEY_VALUE not in result.output and DB_VALUE not in result.output


def test_a_value_split_across_writes_is_still_scrubbed(protect_project):
    protect(protect_project)
    code = (
        "import os,sys,time; v=os.environ['FAKE_KEY']; "
        "sys.stdout.write('start '+v[:6]); sys.stdout.flush(); time.sleep(0.3); "
        "sys.stdout.write(v[6:12]); sys.stdout.flush(); time.sleep(0.3); "
        "sys.stdout.write(v[12:]+' end\\n')"
    )

    result = run(*py(code))

    assert result.stdout == "start *** end\n"


def test_short_numeric_and_boolean_values_stay_readable(protect_project):
    protect(protect_project, text="PORT=3000\nDEBUG=true\nSHORT=abc\nSECRET_TOKEN=fake-long-secret-1\n")
    code = "import os; print(os.environ['PORT'], os.environ['DEBUG'], os.environ['SHORT'], os.environ['SECRET_TOKEN'])"

    result = run(*py(code))

    assert result.stdout == "3000 true abc ***\n"


def test_binary_output_passes_through_unchanged(protect_project):
    protect(protect_project)
    code = "import sys; sys.stdout.buffer.write(bytes(range(256)) + b'\\xff\\xfe tail')"

    result = CliRunner().invoke(cli, py(code))

    assert result.stdout_bytes == bytes(range(256)) + b"\xff\xfe tail"


def test_a_partial_value_is_not_redacted_this_is_a_documented_limit(protect_project):
    protect(protect_project)
    code = "import os; print(os.environ['FAKE_KEY'][:10])"

    result = run(*py(code))

    assert result.stdout == KEY_VALUE[:10] + "\n"


# --- exit codes, errors -------------------------------------------------------


def test_the_childs_exit_code_is_returned(protect_project):
    protect(protect_project)
    assert run(*py("raise SystemExit(7)")).exit_code == 7
    assert run(*py("raise SystemExit(0)")).exit_code == 0


def test_a_child_killed_by_a_signal_returns_128_plus_the_signal(protect_project):
    protect(protect_project)
    code = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert run(*py(code)).exit_code == 128 + signal.SIGTERM


def test_a_missing_command_returns_127(protect_project):
    protect(protect_project)
    result = run("exec", "--", "definitely-not-a-command-vs")
    assert result.exit_code == 127
    assert "command not found: definitely-not-a-command-vs" in result.stderr


def test_no_command_is_a_usage_error(protect_project):
    protect(protect_project)
    assert run("exec").exit_code == 2


def test_exec_in_a_project_that_was_never_protected(protect_project):
    result = run("exec", "--", sys.executable, "-c", "print(1)")
    assert result.exit_code == 1
    assert "run `vaultshield protect` first" in result.output


def test_a_missing_keychain_value_is_named_but_does_not_stop_the_run(protect_project, fake_keyring):
    protect(protect_project)
    project_id = (protect_project / ".vaultshield" / "protect.json").read_text().split('"project_id": "')[1].split('"')[0]
    Vault(project_id).remove(".env", "DB_URL")

    result = run(*py(CHECK, "FAKE_KEY", KEY_VALUE))

    assert result.exit_code == 0
    assert "no value in the Keychain for .env: DB_URL" in result.stderr
    assert KEY_VALUE not in result.output


def test_a_signal_sent_to_vaultshield_is_forwarded_to_the_child(protect_project):
    protect(protect_project)
    outer = signal.signal(signal.SIGTERM, lambda *a: None)  # a late signal must not kill pytest
    timer = threading.Timer(1.0, os.kill, [os.getpid(), signal.SIGTERM])
    try:
        timer.start()
        started = time.monotonic()
        result = run(*py("import time; time.sleep(20)"))
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, outer)

    assert result.exit_code == 128 + signal.SIGTERM
    assert elapsed < 15


# --- which files are loaded ---------------------------------------------------


def test_dir_selects_the_files_to_load_and_the_working_directory(protect_project):
    protect(protect_project, ".env", "ROOT_VAR=fake-root-value\n")
    protect(protect_project, "backend/.env", "BACKEND_VAR=fake-backend-value\n")
    code = "import os; print(os.getcwd()); print('ROOT_VAR' in os.environ, 'BACKEND_VAR' in os.environ)"

    here = run(*py(code))
    there = run("exec", "--dir", "backend", "--", sys.executable, "-c", code)

    assert here.stdout.splitlines()[1] == "True False"
    assert Path(here.stdout.splitlines()[0]).resolve() == protect_project.resolve()
    assert there.stdout.splitlines()[1] == "False True"
    assert Path(there.stdout.splitlines()[0]).resolve() == (protect_project / "backend").resolve()


def test_a_directory_with_no_protected_files_is_an_error(protect_project):
    protect(protect_project, ".env", "ROOT_VAR=fake-root-value\n")
    (protect_project / "docs").mkdir()

    result = run("exec", "--dir", "docs", "--", sys.executable, "-c", "print(1)")

    assert result.exit_code == 1
    assert "no protected files in" in result.output


def test_in_one_directory_a_later_file_overrides_an_earlier_one(protect_project):
    protect(protect_project, ".env", "SHARED=fake-from-env\nONLY_ENV=fake-only-env\n")
    protect(protect_project, ".env.local", "SHARED=fake-from-local\n")

    assert run(*py(CHECK, "SHARED", "fake-from-local")).exit_code == 0
    assert run(*py(CHECK, "ONLY_ENV", "fake-only-env")).exit_code == 0


# --- exec writes nothing ------------------------------------------------------


def _tree(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_exec_writes_nothing_to_disk(protect_project, fake_keyring, tmp_path):
    protect(protect_project)
    items = dict(fake_keyring.items)
    project_before = _tree(protect_project)
    home_before = _tree(tmp_path / "home")
    code = "import os; print(os.environ['FAKE_KEY']); import sys; print(os.environ['DB_URL'], file=sys.stderr)"

    result = run(*py(code))

    assert result.exit_code == 0
    assert _tree(protect_project) == project_before
    assert _tree(tmp_path / "home") == home_before
    assert fake_keyring.items == items
    assert not list(protect_project.rglob("*.tmp"))


def test_the_protected_file_on_disk_still_holds_only_placeholders_during_exec(protect_project):
    env = protect(protect_project)
    code = "import sys; print(open('.env').read())"

    result = run(*py(code))

    assert PLACEHOLDER in result.stdout
    assert KEY_VALUE not in env.read_text()


# --- the scrubber itself ------------------------------------------------------

VALUES = [KEY_VALUE, DB_VALUE, "zq9-fake-token"]


def _stream(values, chunks):
    scrubber = StreamScrubber(values)
    out = b"".join(scrubber.feed(c) for c in chunks)
    return out + scrubber.flush()


def _one_shot(values, data):
    secrets = SecretSet()
    for value in values:
        secrets.add_needle(value.encode())
    return secrets.scrub(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")


def test_every_two_way_split_of_a_chunked_stream_gives_the_same_result():
    data = f"head {KEY_VALUE} mid {DB_VALUE} tail {KEY_VALUE}".encode()
    expected = _one_shot(VALUES, data)
    assert b"***" in expected
    for cut in range(len(data) + 1):
        assert _stream(VALUES, [data[:cut], data[cut:]]) == expected, cut


def test_a_byte_at_a_time_gives_the_same_result():
    data = f"a {KEY_VALUE} b {DB_VALUE}\n".encode()
    assert _stream(VALUES, [data[i : i + 1] for i in range(len(data))]) == _one_shot(VALUES, data)


def test_a_match_that_ends_in_something_that_also_starts_a_match_is_not_split():
    scrubber = StreamScrubber(["abab"])
    out = scrubber.feed(b"xxabab") + scrubber.flush()
    assert out == b"xx***"
    assert _stream(["abab"], [b"ab", b"ab"]) == b"***"
    assert _stream(["abab"], [b"xab", b"abab"]) == _one_shot(["abab"], b"xababab")


def test_text_that_only_looks_like_the_start_of_a_value_is_released_at_the_end():
    scrubber = StreamScrubber([KEY_VALUE])
    assert scrubber.feed(b"sk-test-AA") == b""  # held back: might still become the value
    assert scrubber.feed(b"XX done") == b"sk-test-AAXX done"


def test_the_scrubber_does_nothing_without_values():
    assert _stream([], [b"abc", b"def"]) == b"abcdef"


def test_the_scrubber_repr_shows_no_values():
    assert KEY_VALUE not in repr(StreamScrubber([KEY_VALUE]))


_PIECES = [*VALUES, "sk-test-AAAA", "post", "gres://", "zq9-", "x", " ", "\n", "é", "値", "abc"]


@given(
    pieces=st.lists(st.sampled_from(_PIECES), max_size=14),
    cuts=st.lists(st.integers(min_value=0, max_value=400), max_size=6),
)
def test_chunking_never_changes_the_result(pieces, cuts):
    data = "".join(pieces).encode()
    points = sorted({min(c, len(data)) for c in cuts})
    chunks, last = [], 0
    for point in [*points, len(data)]:
        chunks.append(data[last:point])
        last = point
    assert _stream(VALUES, chunks) == _one_shot(VALUES, data)
