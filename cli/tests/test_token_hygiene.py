"""Unit tests for token hygiene (Batch 3 FR5, design §5.5, §8.1). No Docker:
the credential root is a fake `get_archive` tar stream, and the capture
helper is a fake `exec` answering `diff --raw -z` and `cat-file blob`.
"""

from __future__ import annotations

import io
import json
import tarfile

import docker
import pytest
import requests

from vaultshield import token_hygiene as th
from vaultshield.git_capture import GitCaptureError
from vaultshield.monitor import EventLogRecord, _append_event_log
from vaultshield.run_directory import diff_path, event_log_path
from vaultshield.sandbox import ExecResult
from vaultshield.token_hygiene import (
    Finding,
    SecretSet,
    TokenHygieneViolation,
    check_run_outputs,
    skipped_snapshot_event,
    snapshot,
)

SENTINEL = "ya29.SENTINEL-tokenValue-0123456789abcdefXYZ"
CONV_RUN = "ConvRun0123456789abcdefghijKLMNOPqrstu"
TMP_RUN = "TmpRun0123456789abcdefghijKLMNOPqrstuv"
NEW_RUN = "NewCred0123456789abcdefghijKLMNOPqrstu"


def _tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Container:
    def __init__(self, members=None, exc=None, chunks=None):
        self.members, self.exc, self.chunks = members, exc, chunks

    def get_archive(self, path):
        if self.exc:
            raise self.exc
        if self.chunks is not None:
            return self.chunks, {}
        if self.members is None:
            raise docker.errors.NotFound(path)
        return iter([_tar(self.members)]), {}


class _Sandbox:
    def __init__(self, container):
        self.container = container


ROOT = "/root/.gemini"


def _snap(members, files=None):
    return snapshot(_Sandbox(_Container(members)), (ROOT,), credential_files=files)


def test_needle_extraction_rules():
    snap = _snap([
        (".gemini", None),
        (".gemini/antigravity-cli", None),
        (".gemini/antigravity-cli/token", SENTINEL.encode()),
        (".gemini/antigravity-cli/big.json",
         b"x" * (9 << 10) + b" " + CONV_RUN.encode()
         + b" https://example.com/a/very/long/path/without/digits"),
        (".gemini/antigravity-cli/short", b"tooShort1"),
    ])
    s = snap.secrets
    assert s.contains_secret(SENTINEL.encode())
    assert s.contains_secret(CONV_RUN.encode())  # token-shaped run from a 9 KiB file
    assert not s.contains_secret(b"x" * 100)  # no whole-file needle over 8 KiB
    assert not s.contains_secret(b"tooShort1")
    assert not s.contains_secret(b"https://example.com/a/very/long/path/without/digits")
    assert s.contains_path(b"see /root/.gemini here")
    assert s.contains_path(b"~/.gemini/antigravity-cli")
    assert not s.contains_path(b"a project .gemini/ directory")
    assert repr(s).startswith("SecretSet(needles=") and SENTINEL not in repr(s)


def test_the_credential_file_set_limits_later_needles(tmp_path):
    snap1 = _snap([(".gemini", None), (".gemini/antigravity-cli", None),
                   (".gemini/antigravity-cli/token", SENTINEL.encode())])
    assert snap1.credential_files == frozenset({"antigravity-cli/token"})
    snap2 = _snap([
        (".gemini", None),
        (".gemini/antigravity-cli", None),
        (".gemini/antigravity-cli/token", SENTINEL.encode()),
        (".gemini/antigravity-cli/brain", None),
        (".gemini/antigravity-cli/brain/conv.json", f'{{"t":"{CONV_RUN}"}}'.encode()),
        (".gemini/tmp", None),
        (".gemini/tmp/abc", None),
        (".gemini/tmp/abc/logs.json", f'{{"t":"{TMP_RUN}"}}'.encode()),
        (".gemini/antigravity-cli/oauth_creds.json", f'{{"t":"{NEW_RUN}"}}'.encode()),
    ], snap1.credential_files)
    secrets = SecretSet()
    secrets.add(snap1.secrets)
    secrets.add(snap2.secrets)
    for text, expect in ((CONV_RUN, False), (TMP_RUN, False), (SENTINEL, True), (NEW_RUN, True)):
        run_dir = tmp_path / text[:8]
        run_dir.mkdir()
        diff_path(run_dir).write_text(json.dumps({"files": [{"path": "a.txt", "patch": f"+{text}\n"}]}))
        if expect:
            with pytest.raises(TokenHygieneViolation):
                check_run_outputs(_Capture({}), secrets, run_dir, "b", "a")
        else:
            check_run_outputs(_Capture({}), secrets, run_dir, "b", "a")


def test_snapshot_limits_are_counted_not_silent():
    big = _snap([(".gemini", None), (".gemini/token", b"A1b" * (700 << 10))])
    assert big.skipped == 1
    assert skipped_snapshot_event(1).payload == (
        "token hygiene: 1 credential-root files not scanned for needles (over 1 MiB or past the 8 MiB cap)")
    nine = _snap([(".gemini", None)] + [(f".gemini/token{i}", b"z" * (1 << 20)) for i in range(9)])
    assert nine.skipped == 1
    assert skipped_snapshot_event(nine.skipped).event_type == "violation"


def test_the_snapshot_stream_cap(monkeypatch):
    monkeypatch.setattr(th, "CREDENTIAL_SNAPSHOT_MAX_STREAM_BYTES", 2048)
    with pytest.raises(GitCaptureError, match="^token hygiene: credential snapshot failed: "):
        _snap([(".gemini", None), (".gemini/token", b"x" * 10_000)])


def test_a_missing_root_is_empty():
    snap = snapshot(_Sandbox(_Container(None)), (ROOT,))
    assert not snap.secrets and snap.credential_files == frozenset()


def test_snapshot_docker_errors_are_mapped():
    with pytest.raises(GitCaptureError, match="^token hygiene: credential snapshot failed: APIError"):
        snapshot(_Sandbox(_Container(exc=docker.errors.APIError("500"))), (ROOT,))

    def _chunks():
        yield b"x" * 10
        raise requests.ConnectionError("reset")

    with pytest.raises(GitCaptureError, match="^token hygiene: credential snapshot failed: "):
        snapshot(_Sandbox(_Container(chunks=_chunks())), (ROOT,))


# --- check_run_outputs -------------------------------------------------------


class _Capture:
    """`diff --raw -z` answers with `raw`; `cat-file blob <oid>` with `blobs`."""

    def __init__(self, blobs, raw=b"", fail_cat=False):
        self.blobs, self.raw, self.fail_cat = blobs, raw, fail_cat
        self.commands = []

    def exec(self, cmd, *, env=None):
        self.commands.append(cmd)
        if "--raw" in cmd:
            return ExecResult(0, "", "", stdout_bytes=self.raw)
        if "cat-file" in cmd:
            if self.fail_cat:
                return ExecResult(128, "", "fatal")
            return ExecResult(0, "", "", stdout_bytes=self.blobs[cmd[-1].encode()])
        raise AssertionError(cmd)


def _raw(*records):
    out = b""
    for old_mode, new_mode, new_oid, path in records:
        out += b":" + old_mode + b" " + new_mode + b" " + b"1" * 40 + b" " + new_oid + b" M\0" + path + b"\0"
    return out


def _secrets():
    s = SecretSet()
    s.add_needle(SENTINEL.encode())
    s.add_path_literal("/root/.gemini")
    s.add_root("/root/.gemini")
    return s


def _run_dir(tmp_path, files=(), events=()):
    diff_path(tmp_path).write_text(json.dumps({"files": list(files)}))
    if events:
        _append_event_log(event_log_path(tmp_path), [EventLogRecord("output", "2026-01-01T00:00:00+00:00", e) for e in events])
    return tmp_path


def _events(run_dir):
    return [json.loads(l)["payload"] for l in event_log_path(run_dir).read_text().splitlines()]


@pytest.mark.parametrize("where", ["patch", "path", "event", "blob"])
def test_findings_in_every_artifact(tmp_path, where):
    files = [{"path": "a.txt", "additions": 1, "deletions": 0, "patch": "+ok\n"}]
    events = ["ok"]
    capture = _Capture({})
    if where == "patch":
        files[0]["patch"] = f"+{SENTINEL}\n"
    elif where == "path":
        files[0]["path"] = f"leak-{SENTINEL}.txt"
    elif where == "event":
        events = [f"cat token: {SENTINEL}"]
    else:
        oid = b"c" * 40
        capture = _Capture({oid: SENTINEL.encode()}, raw=_raw((b"000000", b"100644", oid, b"leak.txt")))
    run_dir = _run_dir(tmp_path, files, events)
    with pytest.raises(TokenHygieneViolation) as excinfo:
        check_run_outputs(capture, _secrets(), run_dir, "branch", "base")
    assert SENTINEL not in str(excinfo.value)
    assert "credential material from /root/.gemini found in" in str(excinfo.value)
    assert not diff_path(run_dir).exists()
    assert (run_dir / "diff.quarantined.json").exists()
    payloads = [p for p in _events(run_dir) if p.startswith("token hygiene violation: credential_bytes")]
    assert len(payloads) == 1
    assert all(SENTINEL not in p for p in _events(run_dir) if p.startswith("token hygiene"))
    assert not any("get_archive" in str(c) for c in capture.commands)


def test_a_non_utf8_path_is_read_by_oid_and_named_lossily(tmp_path):
    oid = b"d" * 40
    capture = _Capture({oid: SENTINEL.encode()}, raw=_raw((b"000000", b"100644", oid, b"\xff.txt")))
    with pytest.raises(TokenHygieneViolation) as excinfo:
        check_run_outputs(capture, _secrets(), _run_dir(tmp_path), "b", "a")
    assert excinfo.value.findings == [Finding("credential_bytes", "branch", "\ufffd.txt")]


def test_deletions_and_gitlinks_are_not_read_as_blobs(tmp_path):
    capture = _Capture({}, raw=_raw((b"100644", b"000000", b"0" * 40, b"gone.txt"),
                                    (b"000000", b"160000", b"e" * 40, b"sub")))
    check_run_outputs(capture, _secrets(), _run_dir(tmp_path), "b", "a")
    assert not any("cat-file" in c for c in capture.commands)


def test_a_malformed_raw_record_or_failing_cat_file_is_an_error(tmp_path):
    with pytest.raises(GitCaptureError, match="malformed"):
        check_run_outputs(_Capture({}, raw=b"garbage\0"), _secrets(), _run_dir(tmp_path), "b", "a")
    capture = _Capture({}, raw=_raw((b"000000", b"100644", b"f" * 40, b"x")), fail_cat=True)
    with pytest.raises(GitCaptureError, match="blob"):
        check_run_outputs(capture, _secrets(), _run_dir(tmp_path), "b", "a")


@pytest.mark.parametrize("target, finding", [
    (b"/root/.gemini/antigravity-cli/token", True),
    (b"../../root/.gemini", True),
    (b"sibling.txt", False),
    (b"../docs/readme", False),
])
def test_added_or_retargeted_symlinks(tmp_path, target, finding):
    oid = b"9" * 40
    capture = _Capture({oid: target}, raw=_raw((b"000000", b"120000", oid, b"sub/link")))
    if finding:
        with pytest.raises(TokenHygieneViolation) as excinfo:
            check_run_outputs(capture, _secrets(), _run_dir(tmp_path), "b", "a")
        assert excinfo.value.findings[0].kind == "symlink_outside_repo"
    else:
        check_run_outputs(capture, _secrets(), _run_dir(tmp_path), "b", "a")
    assert not any("ls-tree" in c for c in capture.commands)


def test_a_path_literal_in_a_changed_file_is_a_finding(tmp_path):
    files = [{"path": "doc.md", "additions": 1, "deletions": 0, "patch": "+see /root/.gemini/x\n"}]
    with pytest.raises(TokenHygieneViolation) as excinfo:
        check_run_outputs(_Capture({}), _secrets(), _run_dir(tmp_path, files), "b", "a")
    assert excinfo.value.findings[0].kind == "credential_path"


def test_the_changed_file_scan_cap_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(th, "CHANGED_FILES_SCAN_CAP", 10)
    oid = b"8" * 40
    capture = _Capture({oid: b"x" * 20}, raw=_raw((b"000000", b"100644", oid, b"big.bin")))
    run_dir = _run_dir(tmp_path)
    check_run_outputs(capture, _secrets(), run_dir, "b", "a")
    assert _events(run_dir) == ["token hygiene: 1 changed files not scanned (over 32 MiB)"]


def test_scrub_and_repr():
    s = _secrets()
    assert s.scrub(f"token={SENTINEL}!") == "token=***!"
    assert s.scrub("nothing") == "nothing"
    assert "needles=1" in repr(s) and SENTINEL not in repr(s)


# --- Batch 3 §9 item 14 against the real `/root/.gemini` listing -------------


class _MultiContainer:
    """`get_archive(path)` answers per path; a missing path is NotFound."""

    def __init__(self, archives):
        self.archives = archives

    def get_archive(self, path):
        if path not in self.archives:
            raise docker.errors.NotFound(path)
        return iter([self.archives[path]]), {}


def _sym_tar(name, target):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tf.addfile(info)
    return buf.getvalue()


TOKEN = "/root/.gemini/antigravity-cli/antigravity-oauth-token"
STATE = "/root/.gemini/antigravity-cli/jetski_state.pbtxt"


def test_file_roots_keep_the_root_path_literals_and_define_their_own_set():
    c = _MultiContainer({TOKEN: _tar([("antigravity-oauth-token", SENTINEL.encode())])})
    snap = snapshot(_Sandbox(c), (TOKEN, STATE))
    assert snap.credential_files == frozenset({"antigravity-oauth-token"})
    assert snap.secrets.contains_secret(SENTINEL.encode())
    assert snap.secrets.contains_path(b"/root/.gemini")
    assert snap.secrets.contains_path(b"~/.gemini/antigravity-cli")
    assert snap.secrets.roots == ["/root/.gemini"]
    again = snapshot(_Sandbox(c), (TOKEN, STATE), credential_files=snap.credential_files)
    assert again.secrets.contains_secret(SENTINEL.encode())


def test_a_credential_path_replaced_by_a_symlink_is_never_followed():
    c = _MultiContainer({TOKEN: _sym_tar("antigravity-oauth-token", "/etc/passwd")})
    snap = snapshot(_Sandbox(c), (TOKEN,))
    assert repr(snap.secrets).startswith("SecretSet(needles=0")


def test_the_item_14_rule_on_the_real_probe_listing():
    # Names (not contents) from the developer's log-only probe listing.
    root = ".gemini/antigravity-cli/"
    secret_shaped = lambda i: f"Run{i:02d}abcdefghijKLMNOPqrstuvwxyz0123".encode()  # noqa: E731
    later = [
        "brain/94aa/.system_generated/logs/transcript.jsonl",
        "brain/94aa/.system_generated/logs/chunks/transcript/00000000.jsonl",
        "conversations/94aa.db",
        "conversation_summaries.db",
        "cache/last_conversations.json",
        "cache/onboarding.json",
        "cache/default_project_id.txt",
        "log/cli-20261009_083130.log",
        "installation_id",
    ]
    members = [(".gemini", None), (".gemini/antigravity-cli", None),
               (root + "antigravity-oauth-token", SENTINEL.encode())]
    members += [(root + name, secret_shaped(i)) for i, name in enumerate(later)]
    tar = _tar(members)
    buf = io.BytesIO(tar)
    with tarfile.open(fileobj=buf, mode="a") as tf:  # agy's `cli.log` symlink
        info = tarfile.TarInfo(root + "cli.log")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    snap = snapshot(_Sandbox(_Container(None)), ())  # empty
    c = _MultiContainer({"/root/.gemini": buf.getvalue()})
    snap2 = snapshot(_Sandbox(c), ("/root/.gemini",),
                     credential_files=frozenset({"antigravity-cli/antigravity-oauth-token"}))
    assert snap2.secrets.contains_secret(SENTINEL.encode())
    for i, _name in enumerate(later):
        assert not snap2.secrets.contains_secret(secret_shaped(i)), later[i]
    assert not snap.secrets
