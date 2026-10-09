"""Unit tests for trusted capture (Batch 3 FR5, design §5.4.1, §8.1). No
Docker: archives are built in memory with `tarfile`, and the fake
`get_archive` returns `(iter(chunks), stat)` like docker-py.
"""

from __future__ import annotations

import io
import tarfile

import docker
import pytest
import requests

from vaultshield import trusted_capture as tc
from vaultshield.git_capture import GitCaptureError, UnsafeGitConfig
from vaultshield.sandbox import ExecResult, SandboxError
from vaultshield.trusted_capture import (
    TrustedCapture,
    TrustedCaptureCaps,
    _ChunkReader,
    kill_sandbox,
    notices_to_events,
    record_trusted_base,
    sandbox_image_id,
    split_archive,
)

RUN_ID = "20250614-091530-3f9a2b7c8d1e4f60"
OID = "a" * 40
OID2 = "b" * 40


def _tar(members, *, fmt=tarfile.PAX_FORMAT) -> bytes:
    """members: (name, kind, payload) with kind in file/dir/sym/lnk/fifo/chr/blk."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt, encoding="utf-8",
                      errors="surrogateescape") as tf:
        for spec in members:
            name, kind, payload = (spec + (None,))[:3] if len(spec) == 2 else spec
            mode = 0o644
            if isinstance(payload, tuple):
                payload, mode = payload
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "file":
                data = payload if isinstance(payload, bytes) else (payload or "").encode()
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "lnk": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE, "chr": tarfile.CHRTYPE,
                         "blk": tarfile.BLKTYPE}[kind]
            if kind in ("sym", "lnk"):
                info.linkname = payload
            if kind == "dir":
                info.mode = mode if isinstance(spec[-1], tuple) else 0o755
            tf.addfile(info)
    return buf.getvalue()


def _repo(*extra):
    return [("repo", "dir"), ("repo/.git", "dir"), *extra]


def _split(members, caps=TrustedCaptureCaps()):
    return split_archive(io.BufferedReader(_ChunkReader(iter([_tar(members)]), caps.stream)), caps=caps)


def _read_out(spool):
    spool.seek(0)
    with tarfile.open(fileobj=spool, mode="r", encoding="utf-8", errors="surrogateescape") as tf:
        return {m.name: m for m in tf.getmembers()}, tf.format if hasattr(tf, "format") else None


# --- split_archive rejections ------------------------------------------------


@pytest.mark.parametrize(
    "members",
    [
        _repo(("/abs", "file", "x")),
        _repo(("repo/../x", "file", "x")),
        _repo(("other/x", "file", "x")),
        _repo(("repo/a", "file", "x"), ("repo/a", "file", "y")),
        _repo(("repo/f", "fifo")),
        _repo(("repo/c", "chr")),
        _repo(("repo/b", "blk")),
        _repo(("repo/h", "lnk", "repo/unknown")),
        _repo(("repo/h", "lnk", "repo/later"), ("repo/later", "file", "x")),
        _repo(("repo/x", "lnk", "repo/.git/objects/ab/" + "c" * 38)),
        _repo(("repo/x", "lnk", "/root/.gemini/x")),
        _repo(("repo/a", "file", "x"), ("repo/x", "lnk", "repo/../repo/a")),
        _repo(("repo/sub", "dir"), ("repo/link", "sym", "sub"), ("repo/link/x", "file", "x")),
        _repo(("repo/abs", "sym", "/root/.gemini"), ("repo/abs/x", "file", "x")),
    ],
)
def test_unsafe_members_are_rejected(members):
    with pytest.raises(GitCaptureError, match="unsafe archive member"):
        _split(members)


def test_a_nul_in_a_name_is_rejected():
    state = tc._TarFilter("repo", TrustedCaptureCaps())
    info = tarfile.TarInfo("repo/a\x00b")
    with pytest.raises(GitCaptureError, match="NUL"):
        state.check(info)


# --- split_archive acceptance ------------------------------------------------


def test_hardlinks_modes_and_symlinks_are_normalised():
    split = _split(_repo(
        ("repo/a", "file", ("a", 0o4755)),
        ("repo/b", "lnk", "repo/a"),
        ("repo/c", "file", ("c", 0o4644)),
        ("repo/d", "file", ("d", 0o000)),
        ("repo/locked", "dir", (None, 0o000)),
        ("repo/in", "sym", "c"),
        ("repo/abs", "sym", "/root/.gemini/antigravity-cli/token"),
        ("repo/esc", "sym", "../../root/.gemini"),
    ))
    out, _ = _read_out(split.worktree_tar)
    assert out["repo/b"].islnk() and out["repo/b"].linkname == "repo/a"
    assert out["repo/a"].mode == 0o755
    assert out["repo/c"].mode == 0o644
    assert out["repo/d"].mode == 0o644
    assert out["repo/locked"].mode == 0o755
    assert out["repo/in"].mode == 0o777 and out["repo/in"].linkname == "c"
    assert out["repo/abs"].linkname == "/root/.gemini/antigravity-cli/token"
    assert out["repo/esc"].linkname == "../../root/.gemini"
    assert all(m.uid == 0 and m.gid == 0 for m in out.values())
    assert not any(name.startswith("repo/.git") for name in out)


def test_nested_git_is_dropped_and_its_files_kept_with_one_notice():
    split = _split(_repo(
        ("repo/sub", "dir"),
        ("repo/sub/.git", "dir"),
        ("repo/sub/.git/HEAD", "file", "ref: refs/heads/main\n"),
        ("repo/sub/.git/config", "file", "[core]\n"),
        ("repo/sub/secret.txt", "file", "s"),
    ))
    out, _ = _read_out(split.worktree_tar)
    assert "repo/sub/secret.txt" in out
    assert not any("/.git" in name for name in out)
    assert split.notices == ["sub"]
    [event] = notices_to_events(split.notices)
    assert event.event_type == "violation"
    assert event.payload == "trusted capture: nested git repository at sub was captured as plain files"


def test_a_non_utf8_name_round_trips_byte_for_byte_in_pax_format():
    name = b"repo/\xff.txt".decode("utf-8", "surrogateescape")
    split = _split(_repo((name, "file", "x")))
    split.worktree_tar.seek(0)
    with tarfile.open(fileobj=split.worktree_tar, mode="r", encoding="utf-8",
                      errors="surrogateescape") as tf:
        assert tf.format == tarfile.PAX_FORMAT if hasattr(tf, "format") else True
        names = [m.name.encode("utf-8", "surrogateescape") for m in tf.getmembers()]
    assert b"repo/\xff.txt" in names


def test_chunk_reader_reassembles_and_enforces_its_cap():
    data = bytes(range(256)) * 10
    chunks = [data[:1], data[1:700], data[700:701], data[701:]]
    assert io.BufferedReader(_ChunkReader(iter(chunks), 10_000)).read() == data
    with pytest.raises(GitCaptureError, match=r"exceeds 1000 bytes \(\d+\+ bytes\)"):
        io.BufferedReader(_ChunkReader(iter(chunks), 1000)).read()


# --- archive root and .git ---------------------------------------------------


@pytest.mark.parametrize("kind", ["sym", "file"])
def test_the_repo_root_must_be_a_directory(kind):
    with pytest.raises(GitCaptureError, match="repo directory is not a directory"):
        _split([("repo", kind, "x"), ("repo/.git", "dir")])


def test_no_dot_git_fails_closed():
    with pytest.raises(UnsafeGitConfig, match="has no .git directory"):
        _split([("repo", "dir"), ("repo/a", "file", "x")])


@pytest.mark.parametrize("kind", ["file", "sym"])
def test_a_gitfile_or_symlinked_dot_git_is_unsafe(kind):
    with pytest.raises(UnsafeGitConfig):
        _split([("repo", "dir"), ("repo/.git", kind, "gitdir: /elsewhere")])


def test_no_config_means_no_untrusted_config():
    assert _split(_repo()).untrusted_config is None


@pytest.mark.parametrize("kind, payload", [("sym", "/etc/passwd"), ("dir", None)])
def test_a_non_regular_config_is_unsafe(kind, payload):
    with pytest.raises(UnsafeGitConfig, match="not a regular file"):
        _split(_repo(("repo/.git/config", kind, payload)))


def test_a_hardlinked_config_is_unsafe():
    with pytest.raises(UnsafeGitConfig, match="not a regular file"):
        _split(_repo(("repo/a", "file", "x"), ("repo/.git/config", "lnk", "repo/a")))


def test_the_untrusted_config_is_a_one_member_pax_tar():
    config = b"[core]\n\tbare = false\n"
    split = _split(_repo(("repo/.git/config", "file", config)))
    split.untrusted_config.seek(0)
    with tarfile.open(fileobj=split.untrusted_config, mode="r") as tf:
        [member] = tf.getmembers()
        assert member.name == "untrusted-config"
        assert member.isreg() and member.mode == 0o644
        assert member.uid == member.gid == 0
        assert tf.extractfile(member).read() == config
    split.untrusted_config.seek(0)
    assert b"untrusted-config" in split.untrusted_config.read(4096)  # PAX/ustar header


def test_everything_under_dot_git_but_config_is_dropped():
    split = _split(_repo(
        ("repo/.git/config", "file", "[core]\n"),
        ("repo/.git/hooks", "dir"),
        ("repo/.git/hooks/post-checkout", "file", "#!/bin/sh\ntouch /tmp/x\n"),
        ("repo/.git/index", "file", "DIRC"),
        ("repo/.git/info", "dir"),
        ("repo/.git/info/exclude", "file", "hidden.txt\n"),
        ("repo/.git/refs", "dir"),
        ("repo/.git/refs/replace", "dir"),
        ("repo/.git/refs/replace/" + OID, "file", OID2),
        ("repo/.git/objects", "dir"),
        ("repo/.git/objects/info", "dir"),
        ("repo/.git/objects/info/alternates", "file", "/tmp/evil\n"),
        ("repo/.git/objects/info/http-alternates", "file", "http://x\n"),
        ("repo/.git/objects/ab", "dir"),
        ("repo/.git/objects/ab/" + "c" * 38, "file", "obj"),
        ("repo/.git/objects/pack", "dir"),
        ("repo/.git/objects/pack/pack-" + OID + ".pack", "file", "PACK"),
        ("repo/.git/objects/pack/pack-" + OID + ".idx", "file", "IDX"),
    ))
    out, _ = _read_out(split.worktree_tar)
    assert set(out) == {"repo"}
    assert split.untrusted_config is not None


def test_malformed_streams_are_reported_as_such():
    good = _tar(_repo(("repo/a", "file", "x" * 2000), ("repo/b", "file", "y")))
    for data in (good[:1600], good[:512] + b"\x01" * 512 + good[1024:]):
        with pytest.raises(GitCaptureError, match="truncated or malformed"):
            split_archive(io.BufferedReader(_ChunkReader(iter([data]), 1 << 20)))


def test_caps_fail_loudly_with_the_measured_value():
    members = _repo(*[(f"repo/f{i}", "file", "x") for i in range(5)])
    with pytest.raises(GitCaptureError, match=r"member count exceeds 4 \(5\)"):
        _split(members, TrustedCaptureCaps(members=4))
    with pytest.raises(GitCaptureError, match=r"exceeds 10 bytes \(11 bytes\)"):
        _split(_repo(("repo/big", "file", "x" * 11)), TrustedCaptureCaps(member=10))
    with pytest.raises(GitCaptureError, match=r"exceeds 1000 bytes"):
        _split(_repo(("repo/big", "file", "x" * 2000)), TrustedCaptureCaps(stream=1000))
    with pytest.raises(GitCaptureError, match=r"\.git/config exceeds 64 KiB \(66560 bytes\)"):
        _split(_repo(("repo/.git/config", "file", "x" * (65 << 10))))


# --- record_trusted_base -----------------------------------------------------


class _ArchiveContainer:
    """Answers `get_archive(path)` from `files` (path → tar members)."""

    def __init__(self, files, *, raise_for=None):
        self.files = files
        self.requested = []
        self.raise_for = raise_for or {}

    def get_archive(self, path):
        self.requested.append(path)
        if path in self.raise_for:
            raise self.raise_for[path]
        if path not in self.files:
            raise docker.errors.NotFound(path)
        return iter([_tar(self.files[path])]), {}


class _Holder:
    def __init__(self, container):
        self.container = container


G = "/workspace/repo/.git"


def _objects(*extra):
    return [("objects", "dir"), ("objects/ab", "dir"), ("objects/ab/" + "c" * 38, "file", "loose"),
            ("objects/pack", "dir"), ("objects/pack/pack-" + OID + ".pack", "file", "PACK"),
            ("objects/pack/pack-" + OID + ".idx", "file", "IDX"),
            ("objects/info", "dir"), ("objects/info/alternates", "file", "/tmp/evil\n"),
            ("objects/info/commit-graph", "file", "CG"), *extra]


def _base_files(head=b"ref: refs/heads/main\n", **extra):
    files = {f"{G}/HEAD": [("HEAD", "file", head)], f"{G}/objects": _objects()}
    files.update(extra)
    return files


def test_symbolic_head_with_a_loose_ref():
    c = _ArchiveContainer(_base_files(**{f"{G}/refs/heads/main": [("main", "file", OID + "\n")]}))
    base = record_trusted_base(_Holder(c))
    assert (base.object_format, base.commit_oid) == ("sha1", OID)
    base.objects_tar.seek(0)
    with tarfile.open(fileobj=base.objects_tar) as tf:
        names = {m.name for m in tf.getmembers() if m.isreg()}
    assert names == {"objects/ab/" + "c" * 38, "objects/pack/pack-" + OID + ".pack",
                     "objects/pack/pack-" + OID + ".idx"}
    base.close()


def test_symbolic_head_with_packed_refs_only():
    packed = f"# pack-refs with: peeled fully-peeled sorted\n{OID2} refs/heads/other\n{OID} refs/heads/main\n^{OID2}\n"
    c = _ArchiveContainer(_base_files(**{f"{G}/packed-refs": [("packed-refs", "file", packed)]}))
    assert record_trusted_base(_Holder(c)).commit_oid == OID


def test_detached_and_unborn_heads():
    assert record_trusted_base(_Holder(_ArchiveContainer(_base_files(head=OID.encode())))).commit_oid == OID
    assert record_trusted_base(_Holder(_ArchiveContainer(_base_files()))).commit_oid is None


@pytest.mark.parametrize(
    "config, fmt",
    [
        ("[extensions]\n\tobjectFormat = sha256\n", "sha256"),
        ("[Extensions]\n\tobjectformat = sha256\n", "sha256"),
        ("[extensions]\nobjectformat = sha1\nobjectformat = sha256\n", "sha256"),
        ("[core]\n\tobjectformat = sha256\n", "sha1"),
        ("[core]\n\tbare = false\n", "sha1"),
    ],
)
def test_object_format_grammar(config, fmt):
    head = ("b" * 64 if fmt == "sha256" else OID).encode()
    files = _base_files(head=head, **{f"{G}/config": [("config", "file", config)]})
    assert record_trusted_base(_Holder(_ArchiveContainer(files))).object_format == fmt


def test_an_unknown_object_format_is_refused():
    files = _base_files(**{f"{G}/config": [("config", "file", "[extensions]\nobjectformat = md5\n")]})
    with pytest.raises(GitCaptureError, match="objectformat"):
        record_trusted_base(_Holder(_ArchiveContainer(files)))


@pytest.mark.parametrize(
    "files",
    [
        _base_files(head=b"garbage\n"),
        _base_files(head=b"ref: refs/heads/main\n" + b"x" * 5000),
        _base_files(**{f"{G}/refs/heads/main": [("main", "sym", "/root/.gemini/x")]}),
        {f"{G}/HEAD": [("HEAD", "sym", "/etc/passwd")], f"{G}/objects": _objects()},
    ],
)
def test_bad_base_files_are_refused(files):
    with pytest.raises(GitCaptureError, match="pre-agent base commit"):
        record_trusted_base(_Holder(_ArchiveContainer(files)))


@pytest.mark.parametrize(
    "ref",
    ["refs/heads/../../../../root/.gemini/x", "refs/heads/a//b", "refs/heads/.hidden",
     "refs/heads/a.lock", "refs/heads/a@{1}", "refs/heads/a/", "refs/heads/a.", "refs/tags/v1"],
)
def test_hostile_head_refs_are_refused_without_reading_the_ref(ref):
    c = _ArchiveContainer(_base_files(head=f"ref: {ref}\n".encode()))
    with pytest.raises(GitCaptureError):
        record_trusted_base(_Holder(c))
    assert all(p in (f"{G}/HEAD", f"{G}/config") for p in c.requested)


def test_the_object_snapshot_stream_cap_applies():
    c = _ArchiveContainer(_base_files())
    with pytest.raises(GitCaptureError, match="exceeds"):
        record_trusted_base(_Holder(c), caps=TrustedCaptureCaps(stream=1024))


# --- Docker exception mapping ------------------------------------------------


def test_record_trusted_base_maps_a_docker_error():
    c = _ArchiveContainer(_base_files(), raise_for={f"{G}/HEAD": docker.errors.APIError("boom")})
    with pytest.raises(GitCaptureError, match=r"trusted capture: read \.git/HEAD failed: APIError"):
        record_trusted_base(_Holder(c))


def test_a_mid_stream_transport_error_is_mapped():
    def _chunks():
        yield b"x" * 10
        raise requests.ConnectionError("reset")

    with pytest.raises(GitCaptureError, match="trusted capture: archive the Sandbox repo failed: ConnectionError"):
        io.BufferedReader(_ChunkReader(_chunks(), 1 << 20)).read()


def test_archive_sandbox_repo_maps_errors():
    c = _ArchiveContainer({}, raise_for={"/workspace/repo": docker.errors.APIError("x")})
    with pytest.raises(GitCaptureError, match="archive the Sandbox repo failed: APIError"):
        tc.archive_sandbox_repo(_Holder(c))
    with pytest.raises(GitCaptureError, match="container is gone"):
        tc.archive_sandbox_repo(_Holder(_ArchiveContainer({})))


class _KillContainer:
    def __init__(self, exc=None):
        self.exc = exc

    def kill(self):
        if self.exc:
            raise self.exc


def test_kill_sandbox_treats_not_running_as_success():
    class _Resp:
        status_code = 409
        reason = "Conflict"
    kill_sandbox(_Holder(_KillContainer()))
    kill_sandbox(_Holder(_KillContainer(docker.errors.APIError("Container x is not running", response=_Resp()))))
    with pytest.raises(GitCaptureError, match="kill the Sandbox failed: APIError"):
        kill_sandbox(_Holder(_KillContainer(docker.errors.APIError("boom"))))
    with pytest.raises(GitCaptureError, match="container is gone"):
        kill_sandbox(_Holder(_KillContainer(docker.errors.NotFound("gone"))))


def test_the_image_id_comes_from_attrs_without_an_api_call():
    class _C:
        attrs = {"Image": "sha256:abc"}

        @property
        def image(self):
            raise AssertionError("must not touch .image")

    assert sandbox_image_id(_Holder(_C())) == "sha256:abc"


# --- TrustedCapture with a recording fake client -----------------------------


class _Helper:
    def __init__(self, log, answers):
        self.log, self.answers = log, answers

    def start(self):
        self.log.append(("start",))

    def put_archive(self, path, data):
        self.log.append(("put_archive", path, data[:0]))
        return True

    def exec_run(self, cmd, **kw):
        self.log.append(("exec", list(cmd), kw.get("workdir")))
        for key, answer in self.answers.items():
            if key in cmd:
                code, out = answer
                return code, (out, None)
        return 0, (b"", None)

    def remove(self, **kw):
        self.log.append(("remove", kw))
        if self.answers.get("__remove_raises"):
            raise docker.errors.APIError("refused")


class _CaptureClient:
    def __init__(self, answers=None, raise_on=None):
        self.log = []
        self.kwargs = None
        client = self
        answers = {"version": (0, b"git version 2.39.5\n"), **(answers or {})}

        class _Containers:
            def create(self, image, **kw):
                if raise_on == "create":
                    raise docker.errors.APIError("no")
                client.kwargs = {"image": image, **kw}
                helper = _Helper(client.log, answers)
                if raise_on in ("start", "put_archive"):
                    def _boom(*a, **k):
                        raise docker.errors.APIError("no")
                    setattr(helper, raise_on, _boom)
                if raise_on == "exec_run":
                    def _boom_exec(*a, **k):
                        raise docker.errors.APIError("no")
                    helper.exec_run = _boom_exec
                return helper

        self.containers = _Containers()


def _git_args(entry):
    """The git subcommand args after the `_git` prefix."""
    cmd = entry[1]
    return cmd[cmd.index("protocol.allow=never") + 1:] if "protocol.allow=never" in cmd else cmd


def test_the_helper_is_created_exactly_as_specified():
    client = _CaptureClient()
    capture = TrustedCapture(RUN_ID, client, image_id="sha256:imageid")
    capture.start()
    assert client.kwargs == {
        "image": "sha256:imageid",
        "command": ["tail", "-f", "/dev/null"],
        "name": f"vaultshield-capture-{RUN_ID}",
        "working_dir": "/workspace",
        "network_mode": "none",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "mem_limit": "1g",
        "pids_limit": 256,
        "labels": {"vaultshield.run_id": RUN_ID, "vaultshield.role": "capture"},
    }


@pytest.mark.parametrize("out, ok", [
    (b"git version 2.28.0\n", False), (b"git version 2.31.8\n", False),
    (b"git version 2.32.0\n", True), (b"git version 2.39.5\n", True),
    (b"git version 2.45.2.windows.1\n", True), (b"nonsense\n", False),
])
def test_the_helper_requires_git_2_32(out, ok):
    capture = TrustedCapture(RUN_ID, _CaptureClient({"version": (0, out)}), image_id="x")
    if ok:
        capture.start()
    else:
        with pytest.raises(GitCaptureError, match="older than 2.32"):
            capture.start()


def _loaded(answers=None, *, config=True, oid=OID):
    client = _CaptureClient(answers)
    capture = TrustedCapture(RUN_ID, client, image_id="x")
    capture.start()
    members = _repo(("repo/.git/config", "file", "[core]\n")) if config else _repo()
    split = _split(members)
    split.worktree_tar.seek(5)  # must be rewound before put_archive
    base = tc.TrustedBase("sha1", oid, tc._new_spool())
    capture.load(split, base)
    return client, capture


def test_load_runs_the_specified_sequence():
    client, _ = _loaded()
    seq = []
    for e in client.log:
        if e[0] == "put_archive":
            seq.append(("put", e[1]))
        elif e[0] == "exec":
            seq.append(tuple(_git_args(e)[:2]))
    assert seq == [
        ("git", "version"),
        ("put", "/workspace"),
        ("init", "-q"),
        ("put", "/workspace/repo/.git"),
        ("put", "/tmp"),
        ("config", "--file"),
        ("cat-file", "-e"),
        ("rev-list", "--quiet"),
        ("update-ref", "--no-deref"),
        ("read-tree", OID),
    ]
    assert not any("fsck" in e[1] for e in client.log if e[0] == "exec")


def test_no_config_means_no_config_put_or_check():
    client, _ = _loaded(config=False)
    assert ("put_archive", "/tmp", b"") not in client.log
    assert not any("--file" in e[1] for e in client.log if e[0] == "exec")


@pytest.mark.parametrize("code, out, error", [
    (3, b"", "could not be parsed \\(git config exit 3\\)"),
    (0, b"filter.x.clean cat\n", "defines a filter or diff driver"),
    (1, b"", None),
    (0, b"", None),
])
def test_untrusted_config_check_exit_codes(code, out, error):
    answers = {"--file": (code, out)}
    if error:
        with pytest.raises(UnsafeGitConfig, match=error):
            _loaded(answers)
    else:
        _loaded(answers)


@pytest.mark.parametrize("key", ["cat-file", "rev-list"])
def test_an_incomplete_object_snapshot_is_reported(key):
    with pytest.raises(GitCaptureError, match="pre-agent object snapshot is incomplete"):
        _loaded({key: (1, b"")})


def test_an_unborn_base_skips_the_rebuild():
    client, _ = _loaded(oid=None)
    assert not any("update-ref" in e[1] for e in client.log if e[0] == "exec")


def test_commit_run_branch_runs_its_safety_checks_on_the_helper():
    from vaultshield.git_capture import commit_run_branch

    client, capture = _loaded({"--absolute-git-dir": (0, b"/workspace/repo/.git\n"),
                               "--show-toplevel": (0, b"/workspace/repo\n"),
                               "--get-regexp": (1, b"")})
    commit_run_branch(capture, RUN_ID)
    subcommands = [_git_args(e)[:2] for e in client.log if e[0] == "exec"]
    assert ["rev-parse", "--absolute-git-dir"] in subcommands
    assert ["rev-parse", "--show-toplevel"] in subcommands
    assert any(s[0] == "config" and s[1] == "--get-regexp" for s in subcommands)


@pytest.mark.parametrize("step, label", [
    ("create", "create the capture helper"),
    ("start", "start the capture helper"),
])
def test_helper_docker_errors_are_mapped(step, label):
    with pytest.raises(GitCaptureError, match=f"trusted capture: {label} failed: APIError"):
        TrustedCapture(RUN_ID, _CaptureClient(raise_on=step), image_id="x").start()


def test_put_archive_and_exec_errors_are_mapped():
    client = _CaptureClient(raise_on="put_archive")
    capture = TrustedCapture(RUN_ID, client, image_id="x")
    capture.start()
    with pytest.raises(GitCaptureError, match="put_archive work tree failed: APIError"):
        capture.load(_split(_repo()), tc.TrustedBase("sha1", None, tc._new_spool()))
    capture = TrustedCapture(RUN_ID, _CaptureClient(raise_on="exec_run"), image_id="x")
    with pytest.raises(SandboxError, match="trusted capture exec failed for run"):
        capture.start()


def test_remove_warns_once_and_never_raises(capsys):
    client = _CaptureClient({"__remove_raises": True})
    capture = TrustedCapture(RUN_ID, client, image_id="x")
    capture.start()
    capture.remove()
    capture.remove()
    err = capsys.readouterr().err
    assert err.count("Warning: could not remove the capture helper") == 1
    assert "APIError" in err


def test_exec_has_sandbox_exec_s_contract():
    capture = TrustedCapture(RUN_ID, _CaptureClient({"echo": (0, b"\xffhi")}), image_id="x")
    capture.start()
    result = capture.exec(["echo"], env={"A": "1"})
    assert isinstance(result, ExecResult)
    assert result.stdout_bytes == b"\xffhi" and result.stdout == "\ufffdhi"


def test_remove_capture_helper_by_name():
    class _C:
        removed = False

        def remove(self, **kw):
            _C.removed = True

    class _Client:
        class containers:
            @staticmethod
            def get(name):
                if name == f"vaultshield-capture-{RUN_ID}":
                    return _C()
                raise docker.errors.NotFound(name)

    assert tc.remove_capture_helper(RUN_ID, _Client()) is True
    assert tc.remove_capture_helper("other", _Client()) is False
