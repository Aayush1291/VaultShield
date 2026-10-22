"""Step 1: discovery of secret files, and `vaultshield protect --dry-run`."""

import os

import pytest
from click.testing import CliRunner

from vaultshield.cli import cli
from vaultshield.protect.discovery import (
    KIND_ENV,
    KIND_SECRET,
    Discovered,
    classify,
    discover,
)


def _touch(root, rel, text="x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    "rel, kind",
    [
        (".env", KIND_ENV),
        (".env.local", KIND_ENV),
        (".env.production", KIND_ENV),
        (".env.template", KIND_ENV),  # only example and sample are templates
        ("backend/.env", KIND_ENV),
        ("server.pem", KIND_SECRET),
        ("certs/tls.key", KIND_SECRET),
        ("id_rsa", KIND_SECRET),
        ("id_rsa_deploy", KIND_SECRET),
        ("credentials.json", KIND_SECRET),
        (".npmrc", KIND_SECRET),
        (".pypirc", KIND_SECRET),
        (".netrc", KIND_SECRET),
        (".aws/credentials", KIND_SECRET),
        ("deploy/.aws/credentials", KIND_SECRET),
    ],
)
def test_classify_secret_files(rel, kind):
    assert classify(rel) == kind


@pytest.mark.parametrize(
    "rel",
    [
        ".env.example",
        ".env.sample",
        "backend/.env.example",
        "id_rsa.pub",
        "credentials",  # only `.aws/credentials` is a credentials file
        "aws/credentials",
        "config.env",
        "environment.md",
        "README.md",
        "keys.txt",
    ],
)
def test_classify_ignores_templates_and_lookalikes(rel):
    assert classify(rel) is None


def test_discover_finds_nested_files_sorted(protect_project):
    _touch(protect_project, "backend/.env")
    _touch(protect_project, ".env")
    _touch(protect_project, "certs/server.pem")
    _touch(protect_project, ".aws/credentials")

    assert discover(protect_project) == [
        Discovered(".aws/credentials", KIND_SECRET),
        Discovered(".env", KIND_ENV),
        Discovered("backend/.env", KIND_ENV),
        Discovered("certs/server.pem", KIND_SECRET),
    ]


@pytest.mark.parametrize("skipped", ["node_modules", ".git", ".venv", ".vaultshield"])
def test_discover_skips_excluded_directories(protect_project, skipped):
    _touch(protect_project, f"{skipped}/pkg/.env")
    _touch(protect_project, f"{skipped}/server.pem")
    _touch(protect_project, ".env")

    assert [d.path for d in discover(protect_project)] == [".env"]


def test_discover_skips_the_docker_pipeline_dummy_env(protect_project):
    _touch(protect_project, ".vaultshield/runs/20260101-000000-abc/.env.sandbox")
    assert discover(protect_project) == []


def test_discover_does_not_follow_or_report_symlinks(protect_project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _touch(outside, ".env", "OUTSIDE=1\n")
    _touch(outside, "server.pem")
    os.symlink(outside, protect_project / "linked_dir")
    os.symlink(outside / ".env", protect_project / ".env.local")

    assert discover(protect_project) == []


def test_discover_skips_directories_named_like_secrets(protect_project):
    (protect_project / ".env").mkdir()
    assert discover(protect_project) == []


def test_discover_never_opens_files(protect_project, monkeypatch):
    _touch(protect_project, ".env", "KEY=value\n")

    def refuse(*args, **kwargs):
        raise AssertionError("discovery must not open files")

    monkeypatch.setattr("builtins.open", refuse)
    assert [d.path for d in discover(protect_project)] == [".env"]


def _snapshot(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_dry_run_lists_files_and_changes_nothing(protect_project):
    _touch(protect_project, ".env", "FAKE_KEY=sk-test-AAAA1111BBBB2222\n")
    _touch(protect_project, "certs/server.pem", "-----BEGIN FAKE-----\n")
    before = _snapshot(protect_project)
    entries_before = sorted(p.name for p in protect_project.iterdir())

    result = CliRunner().invoke(cli, ["protect", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Would protect" in result.output
    assert "  .env" in result.output
    assert "Detected, not moved" in result.output
    assert "  certs/server.pem" in result.output
    assert "nothing was changed" in result.output
    assert "AAAA1111BBBB2222" not in result.output
    assert _snapshot(protect_project) == before
    assert sorted(p.name for p in protect_project.iterdir()) == entries_before
    assert not (protect_project / ".vaultshield").exists()


def test_dry_run_with_nothing_to_find(protect_project):
    result = CliRunner().invoke(cli, ["protect", "--dry-run"])
    assert result.exit_code == 0
    assert "No secret files found." in result.output


def test_existing_commands_are_still_registered():
    names = set(cli.commands)
    assert {"init", "login", "run", "submit", "teardown", "protect"} <= names
