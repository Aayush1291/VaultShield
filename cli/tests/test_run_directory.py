"""Unit tests for `run_id` generation and the Run_Directory helpers in
`cli/run_directory.py` (Task 8.1).

Scoped to this module's own behavior: the shape/uniqueness/safety of a
generated `run_id`, the validation guard that keeps a command-line
`run_id` from escaping the repository, and the path/creation helpers for
the Run_Directory itself and the three artifacts design.md places inside
it (`events.jsonl`, `diff.json`, `screenshots/`).

No property-based test here: design.md's 34 numbered Correctness
Properties include none covering `run_id` generation or Run_Directory
paths, and the designated test task for this pipeline stage is Task 8.3's
end-to-end unit test of the full mock `run` (a separate, optional task).
Everything below is an ordinary unit test over fixed examples.

The last test guards the one seam this task created rather than
`run_directory`'s own surface: `monitor.py` no longer keeps its own copy
of the `.vaultshield/runs/<run_id>/` convention, so the Event_Log path
`Monitor.supervise` defaults to and the one `run_directory` computes must
stay the same path.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vaultshield.monitor import Monitor, _default_run_dir
from vaultshield.run_directory import (
    DIFF_FILENAME,
    EVENT_LOG_FILENAME,
    RUN_ID_MAX_LENGTH,
    RunDirectoryError,
    create_run_dir,
    diff_path,
    event_log_path,
    generate_run_id,
    is_valid_run_id,
    run_dir_path,
    runs_root,
    screenshots_dir,
    validate_run_id,
)

# Characters a generated run_id is allowed to contain: lowercase hex plus
# the two `-` separators. Deliberately spelled out here rather than
# imported, so a change to the generator's character set has to be
# restated in this test instead of silently agreeing with itself.
_GENERATED_RUN_ID_CHARS = re.compile(r"\A[0-9a-f-]+\Z")

# Every character `git check-ref-format` rejects inside a ref component,
# plus the path separators no single path component may contain. A
# generated run_id becomes both a directory name and part of the branch
# name `vaultshield/run-<run_id>` (Task 12.1), so it must avoid all of
# these.
_UNSAFE_CHARS = ' ~^:?*[\\/."\'' + "\x00\n\t"


# --- generate_run_id -------------------------------------------------------


def test_generated_run_id_is_valid_and_safe():
    run_id = generate_run_id()

    assert is_valid_run_id(run_id)
    assert _GENERATED_RUN_ID_CHARS.match(run_id)
    assert not any(char in run_id for char in _UNSAFE_CHARS)
    assert ".." not in run_id
    assert not run_id.startswith((".", "-"))
    assert not run_id.endswith((".", ".lock"))


def test_generated_run_id_fits_the_backend_column():
    """design.md's data model: `run_id = CharField(max_length=64)`."""
    assert len(generate_run_id()) <= RUN_ID_MAX_LENGTH


def test_generated_run_ids_are_unique_across_rapid_calls():
    """Two runs started within the same second still get distinct ids —
    the random suffix, not the timestamp, is what makes an id unique.
    """
    ids = {generate_run_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_generated_run_id_starts_with_the_current_utc_date():
    """The timestamp prefix is what makes `ls .vaultshield/runs/` sort
    chronologically, and it is UTC rather than local time.
    """
    run_id = generate_run_id()
    assert run_id.startswith(datetime.now(timezone.utc).strftime("%Y%m%d-"))


def test_generated_run_id_has_a_fixed_width_sortable_prefix():
    """`<YYYYmmdd>-<HHMMSS>-<16 hex chars>`: the timestamp is fixed-width
    and leads, so lexicographic order equals creation order.
    """
    assert re.match(r"\A\d{8}-\d{6}-[0-9a-f]{16}\Z", generate_run_id())


# --- is_valid_run_id / validate_run_id -------------------------------------


@pytest.mark.parametrize(
    "run_id",
    [
        "20250614-091530-3f9a2b7c8d1e4f60",
        "smoke-a1b2c3d4",  # cli.py's `_sandbox-smoke` debug id shape
        "run-merge-1",
        "run_with_underscores",
        "a",
        "0",
        "A" * RUN_ID_MAX_LENGTH,
    ],
)
def test_is_valid_run_id_accepts_safe_single_components(run_id: str):
    assert is_valid_run_id(run_id)
    assert validate_run_id(run_id) == run_id


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "..",
        "../../../etc",
        "../sibling",
        "nested/run",
        "run\\id",
        ".hidden",
        "run.lock",
        "run.id",
        "-leading-hyphen",
        "_leading-underscore",
        "has space",
        "has\ttab",
        "has\nnewline",
        "tilde~",
        "caret^",
        "colon:",
        "question?",
        "star*",
        "bracket[",
        "quote'",
        "A" * (RUN_ID_MAX_LENGTH + 1),
    ],
)
def test_is_valid_run_id_rejects_unsafe_values(run_id: str):
    assert not is_valid_run_id(run_id)


def test_is_valid_run_id_rejects_non_string_input():
    assert not is_valid_run_id(None)  # type: ignore[arg-type]
    assert not is_valid_run_id(42)  # type: ignore[arg-type]


def test_validate_run_id_raises_naming_the_rejected_value():
    with pytest.raises(RunDirectoryError) as exc_info:
        validate_run_id("../../etc")
    assert "'../../etc'" in str(exc_info.value)


# --- path helpers ----------------------------------------------------------


def test_runs_root_matches_the_glossary_layout(tmp_path: Path):
    assert runs_root(tmp_path) == tmp_path / ".vaultshield" / "runs"


def test_run_dir_path_matches_the_glossary_layout(tmp_path: Path):
    """requirements.md's Glossary: `.vaultshield/runs/<run_id>/`."""
    assert run_dir_path(tmp_path, "run-1") == tmp_path / ".vaultshield" / "runs" / "run-1"


def test_run_dir_path_creates_nothing(tmp_path: Path):
    run_dir_path(tmp_path, "run-1")
    assert not (tmp_path / ".vaultshield").exists()


def test_run_dir_path_refuses_to_build_a_traversing_path(tmp_path: Path):
    """The guard that lets `submit`/`teardown` pass a command-line
    argument straight in: an unvalidated id would otherwise resolve
    outside the repository.
    """
    with pytest.raises(RunDirectoryError):
        run_dir_path(tmp_path, "../../../etc")


def test_artifact_helpers_match_the_design_layout(tmp_path: Path):
    run_dir = run_dir_path(tmp_path, "run-1")

    assert event_log_path(run_dir) == run_dir / "events.jsonl"
    assert diff_path(run_dir) == run_dir / "diff.json"
    assert screenshots_dir(run_dir) == run_dir / "screenshots"


def test_artifact_helpers_create_nothing(tmp_path: Path):
    run_dir = create_run_dir(tmp_path, "run-1")

    event_log_path(run_dir)
    diff_path(run_dir)
    screenshots_dir(run_dir)

    assert list(run_dir.iterdir()) == []


# --- create_run_dir --------------------------------------------------------


def test_create_run_dir_creates_the_directory_and_its_parents(tmp_path: Path):
    run_dir = create_run_dir(tmp_path, "run-1")

    assert run_dir == tmp_path / ".vaultshield" / "runs" / "run-1"
    assert run_dir.is_dir()


def test_create_run_dir_is_idempotent_and_preserves_contents(tmp_path: Path):
    """Requirement 14.4 requires a failed upload to preserve the
    Run_Directory's contents, so nothing here may ever remove them.
    """
    run_dir = create_run_dir(tmp_path, "run-1")
    event_log_path(run_dir).write_text('{"event_type": "output"}\n')

    again = create_run_dir(tmp_path, "run-1")

    assert again == run_dir
    assert event_log_path(again).read_text() == '{"event_type": "output"}\n'


def test_create_run_dir_validates_the_run_id_before_creating_anything(tmp_path: Path):
    with pytest.raises(RunDirectoryError):
        create_run_dir(tmp_path, "../escaped")
    assert not (tmp_path / ".vaultshield").exists()
    assert not (tmp_path.parent / "escaped").exists()


def test_two_generated_runs_get_separate_directories(tmp_path: Path):
    first = create_run_dir(tmp_path, generate_run_id())
    second = create_run_dir(tmp_path, generate_run_id())

    assert first != second
    assert sorted(p.name for p in runs_root(tmp_path).iterdir()) == sorted(
        [first.name, second.name]
    )


# --- alignment with monitor.py ---------------------------------------------


def test_monitor_default_run_dir_agrees_with_run_directory(tmp_path: Path):
    """`monitor.py` no longer keeps its own copy of this convention, so
    the Event_Log path `Monitor.supervise` defaults to must be the one
    `run_directory` computes — this is what lets Task 8.2 resolve the
    Run_Directory itself without agreeing with a second copy of the
    layout.
    """
    run_id = generate_run_id()
    monitor = Monitor(tmp_path, run_id)

    assert _default_run_dir(monitor.workdir, monitor.run_id) == run_dir_path(tmp_path, run_id)
    assert EVENT_LOG_FILENAME == "events.jsonl"
    assert DIFF_FILENAME == "diff.json"
