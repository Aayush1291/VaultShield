"""Docker-gated integration tests for egress control, trusted capture and
token hygiene (Batch 3 FR2-FR7, design §8.2 tests 1-8).

Nothing is mocked below the Docker API: real networks, the real proxy sidecar
(pinned `python:3.11.16-slim`), the real `vaultshield-sandbox:local` image, a
real clone from a host `git daemon`, and the real capture helper. Skipped,
not faked, without Docker, the image, the proxy image or (test 2) internet.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import docker
import httpx
import pytest

from test_sandbox_execution import (  # noqa: F401 - fixtures are used by name
    SANDBOX_IMAGE,
    _offline_backend,
    _wait_for_port,
    host_repo,
    requires_docker,
    requires_sandbox_image,
    served_repo,
)
from vaultshield import egress
from vaultshield.browser_capture import resolve_base_url
from vaultshield.config import EgressConfig
from vaultshield.egress import EgressGuard, resolve_policy
from vaultshield.egress_proxy import parse_allow_entry
from vaultshield.git_capture import (
    _GIT_ENV,
    _git,
    build_structured_diff,
    commit_run_branch,
    resolve_diff_base,
)
from vaultshield.monitor import _append_event_log
from vaultshield.run_directory import diff_path, event_log_path, generate_run_id
from vaultshield.sandbox import Sandbox, container_name
from vaultshield.token_hygiene import SecretSet, TokenHygieneViolation, check_run_outputs, snapshot
from vaultshield.trusted_capture import (
    TrustedCapture,
    archive_sandbox_repo,
    capture_container_name,
    kill_sandbox,
    notices_to_events,
    record_trusted_base,
    sandbox_image_id,
)

TOKEN_PATH = "/root/.gemini/antigravity-cli/antigravity-oauth-token"
# Sentinel only: token-shaped (digits, upper, lower, ≥ 32 chars), never real.
SENTINEL = "ya29.VsSentinelTokenAbc0123456789XYZdefGHIjkl"
_SYMLINK_DAEMON_PORT = 9421


def _proxy_image_available() -> bool:
    try:
        client = docker.from_env()
        try:
            client.images.get(egress.EGRESS_PROXY_IMAGE)
        except docker.errors.ImageNotFound:
            client.images.pull(egress._EGRESS_PROXY_PULL_REF)
        return True
    except Exception:  # noqa: BLE001
        return False


def _internet() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        return True
    except OSError:
        return False


requires_proxy_image = pytest.mark.skipif(
    not _proxy_image_available(),
    reason=f"needs the egress proxy image {egress.EGRESS_PROXY_IMAGE} (present or pullable)",
)
requires_internet = pytest.mark.skipif(not _internet(), reason="needs internet (1.1.1.1:443)")


# --- helpers -----------------------------------------------------------------


def _leftovers(client, run_id):
    names = [c.name for c in client.containers.list(all=True, filters={"label": f"vaultshield.run_id={run_id}"})]
    names += [c.name for c in client.containers.list(all=True, filters={"name": run_id})]
    names += [n.name for n in client.networks.list(filters={"label": f"vaultshield.run_id={run_id}"})]
    return sorted(set(names))


def _force_cleanup(client, run_id):
    for c in client.containers.list(all=True, filters={"name": run_id}):
        c.remove(force=True, v=True)
    for n in client.networks.list(filters={"label": f"vaultshield.run_id={run_id}"}):
        n.remove()


@pytest.fixture
def client():
    return docker.from_env()


@pytest.fixture
def run_id(client):
    rid = generate_run_id()
    yield rid
    _force_cleanup(client, rid)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env.sandbox"
    path.write_text("")
    return path


def _guarded(client, run_id, repo_url, env_file, *, allow=("example.com",), app_port=None, mode="allowlist"):
    entries = tuple(parse_allow_entry(a) for a in allow)
    policy = resolve_policy("mock", EgressConfig(allow=entries), mode, app_port=app_port)
    guard = EgressGuard(run_id, client, policy, app_port=app_port)
    sandbox = Sandbox(run_id, client)
    guard.start()
    sandbox.create(repo_url, SANDBOX_IMAGE, env_file, **guard.sandbox_create_kwargs())
    guard.isolate(sandbox)
    return sandbox, guard


_IN_SANDBOX = r"""
import json, socket, struct, sys
proxy, port = sys.argv[1], int(sys.argv[2])
out = {}
def tcp(host, p, fam=socket.AF_INET):
    try:
        s = socket.socket(fam); s.settimeout(2); s.connect((host, p)); s.close(); return "open"
    except socket.gaierror: return "unresolvable"
    except OSError as e: return type(e).__name__
def connect(host, p):
    s = socket.create_connection((proxy, port), timeout=5)
    s.sendall(f"CONNECT {host}:{p} HTTP/1.1\r\n\r\n".encode())
    head = s.recv(1024).split(b"\r\n")[0].decode(); s.close(); return head
for t in ("example.org:443", "1.1.1.1:443", "host.docker.internal:8000", sys.argv[3] + ":443"):
    h, p = t.rsplit(":", 1); out["connect " + t] = connect(h, int(p))
out["direct 1.1.1.1:443"] = tcp("1.1.1.1", 443)
try:
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.settimeout(2)
    u.sendto(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01", ("8.8.8.8", 53))
    u.recvfrom(512); out["udp 8.8.8.8:53"] = "answered"
except OSError as e: out["udp 8.8.8.8:53"] = type(e).__name__
out["ipv6"] = tcp("2001:4860:4860::8888", 443, socket.AF_INET6)
try:
    socket.getaddrinfo(sys.argv[3], 443); out["resolve label"] = "resolved"
except socket.gaierror: out["resolve label"] = "gaierror"
net = int(open("/proc/net/route").read().split("\n")[1].split()[1], 16)
gw = socket.inet_ntoa(struct.pack("<L", net)).rsplit(".", 1)[0] + ".1"
for p in [8000, 27018, 111, 63289] + [int(x) for x in sys.argv[4:]]:
    out[f"int gateway {p}"] = tcp(gw, p)
# Every TCP port on the int gateway: none may accept (the Docker VM's rpcbind
# :111 and a Docker Desktop daemon :63289 do on a plain internal bridge).
import concurrent.futures
def quick(p):
    try:
        socket.create_connection((gw, p), timeout=0.3).close(); return p
    except OSError: return None
with concurrent.futures.ThreadPoolExecutor(256) as ex:
    out["int gateway open ports"] = [p for p in ex.map(quick, range(1, 65536)) if p]
# With `inhibit_ipv4` the bridge has no address, so `.1` is handed to the
# first container on the network: the proxy.
out["int .1 is the proxy"] = socket.gethostbyname(proxy) == gw
try:
    socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close(); out["raw icmp"] = "allowed"
except PermissionError: out["raw icmp"] = "PermissionError"
print(json.dumps(out))
"""


def _probe(sandbox, label, *extra_ports):
    result = sandbox.exec(["python3", "-c", _IN_SANDBOX, "vaultshield-egress", "3128", label,
                           *[str(p) for p in extra_ports]])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def _stream(sandbox, cmd):
    stream = sandbox.exec_stream(cmd, tty=False)
    out = b"".join(stream)
    return stream.exit_code, out.decode("utf-8", "replace")


# --- tests 1-5: egress -------------------------------------------------------


@requires_docker
@requires_sandbox_image
@requires_proxy_image
def test_a_blocked_connection_is_blocked_and_recorded(client, run_id, served_repo, env_file, tmp_path):
    # A host listener on all interfaces, to show the int gateway does not lead to it.
    with socket.socket() as host_listener:
        host_listener.bind(("0.0.0.0", 0))
        host_listener.listen(1)
        host_port = host_listener.getsockname()[1]
        sandbox, guard = _guarded(client, run_id, served_repo.for_container, env_file)
        try:
            int_ipam = client.networks.get(egress.int_network_name(run_id)).attrs["IPAM"]["Config"]
            label = "vs-probe-" + os.urandom(8).hex() + ".example.com"
            report = _probe(sandbox, label, host_port)
            sandbox.exec_detached(["python3", "-m", "http.server", "8765", "--bind", "127.0.0.1"])
            time.sleep(1.5)
            code, out = _stream(sandbox, ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                          "http://127.0.0.1:8765/"])
        finally:
            sandbox.teardown()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            lines_before = guard.proxy_log_lines()
            guard.teardown(run_dir)
    print("in-sandbox report:", json.dumps(report, indent=1))
    assert report["connect example.org:443"].startswith("HTTP/1.1 403")
    assert report["connect 1.1.1.1:443"].startswith("HTTP/1.1 403")
    assert report["connect host.docker.internal:8000"].startswith("HTTP/1.1 403")
    assert report["connect " + label + ":443"].startswith("HTTP/1.1 403")
    assert report["direct 1.1.1.1:443"] != "open"
    assert report["udp 8.8.8.8:53"] != "answered"
    assert report["ipv6"] != "open"
    assert report["resolve label"] == "gaierror"
    assert report["int gateway 8000"] != "open"
    assert report["int gateway 27018"] != "open"
    assert report[f"int gateway {host_port}"] != "open"
    assert report["int gateway 111"] != "open"
    assert report["int gateway 63289"] != "open"
    # No Docker VM listener is reachable on the int network: the only open
    # ports at `.1` are the proxy's own (it holds that address), and the
    # network has no gateway at all.
    assert report["int .1 is the proxy"] is True
    assert report["int gateway open ports"] == [egress.PROXY_SNI_PORT, egress.PROXY_PORT]
    assert int_ipam and all(not cfg.get("Gateway") for cfg in int_ipam), int_ipam
    assert report["raw icmp"] == "PermissionError"
    # Loopback bypasses the proxy (NO_PROXY), and the proxy never saw it.
    assert (code, out) == (0, "200")
    assert not any("127.0.0.1" in l for l in lines_before)
    # The post-switch check passed although example.com is allowlisted (it probed example.net).
    assert guard.policy.probe_name == "example.net"
    events = [json.loads(l) for l in event_log_path(run_dir).read_text().splitlines()]
    payloads = [e["payload"] for e in events]
    assert ("egress violation: CONNECT example.org:443 denied "
            "(not_in_allowlist; agent mock, egress allowlist)") in payloads
    assert all(e["event_type"] == "violation" for e in events)
    assert (run_dir / "egress.jsonl").is_file()
    assert _leftovers(client, run_id) == []


@requires_docker
@requires_sandbox_image
@requires_proxy_image
@requires_internet
def test_an_allowlisted_host_is_reachable(client, run_id, served_repo, env_file, tmp_path):
    sandbox, guard = _guarded(client, run_id, served_repo.for_container, env_file)
    script = r"""
import socket, ssl
s = socket.create_connection(("vaultshield-egress", 3128), timeout=5)
s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
head = s.recv(1024).split(b"\r\n")[0].decode()
t = ssl.create_default_context().wrap_socket(s, server_hostname="example.com")
print(head, "|", t.version())
"""
    try:
        result = sandbox.exec(["python3", "-c", script])
    finally:
        sandbox.teardown()
        guard.teardown(tmp_path)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("HTTP/1.1 200"), result.stdout
    assert "TLS" in result.stdout


@requires_docker
@requires_sandbox_image
@requires_proxy_image
def test_dns_exfil_is_blocked(client, run_id, served_repo, env_file, tmp_path):
    sandbox, guard = _guarded(client, run_id, served_repo.for_container, env_file)
    label = "vs-exfil-" + os.urandom(8).hex() + ".example.com"
    try:
        report = _probe(sandbox, label)
        lines = [json.loads(l) for l in guard.proxy_log_lines() if l.startswith("{")]
    finally:
        sandbox.teardown()
        guard.teardown(tmp_path)
    assert report["resolve label"] == "gaierror"
    assert report["connect " + label + ":443"].startswith("HTTP/1.1 403")
    mine = [l for l in lines if l.get("host") == label]
    assert [(l["decision"], l["reason"]) for l in mine] == [("deny", "not_in_allowlist")]


@requires_docker
@requires_sandbox_image
@requires_proxy_image
def test_the_app_port_is_reachable_through_the_proxy(client, run_id, served_repo, env_file, tmp_path):
    sandbox, guard = _guarded(client, run_id, served_repo.for_container, env_file, app_port=8000)
    try:
        sandbox.exec_detached(["python3", "-m", "http.server", "8000"])
        base = resolve_base_url(sandbox, 8000, published_by=guard.port_publisher)
        status = None
        for _ in range(40):
            try:
                status = httpx.get(base + "/", timeout=2).status_code
                break
            except httpx.HTTPError:
                time.sleep(0.25)
    finally:
        sandbox.teardown()
        guard.teardown(tmp_path)
    assert base.startswith("http://127.0.0.1:")
    assert status == 200


@requires_docker
@requires_sandbox_image
@requires_proxy_image
def test_run_with_egress_allowlist_end_to_end(host_repo, served_repo, monkeypatch, client):
    import vaultshield.cli as cli_module
    from click.testing import CliRunner

    monkeypatch.chdir(host_repo)
    marks = _offline_backend(monkeypatch, cli_module)
    monkeypatch.setattr(cli_module.uploader, "upload_run", lambda *a, **k: None)
    result = CliRunner().invoke(
        cli_module.cli,
        ["run", "--agent=mock", "--prompt=egress e2e", f"--repo-url={served_repo.for_container}",
         "--backend-url=http://127.0.0.1:1", "--project=0123456789abcdef01234567",
         f"--base-image={SANDBOX_IMAGE}", "--egress=allowlist"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert marks == []
    run_dir = sorted((host_repo / ".vaultshield" / "runs").iterdir())[-1]
    assert event_log_path(run_dir).is_file()
    assert (run_dir / "egress.jsonl").is_file()
    assert diff_path(run_dir).is_file()
    assert _leftovers(client, run_dir.name) == []


# --- tests 6-8: trusted capture and token hygiene ----------------------------


def _off_sandbox(client, run_id, repo_url, env_file):
    sandbox = Sandbox(run_id, client)
    sandbox.create(repo_url, SANDBOX_IMAGE, env_file)
    return sandbox


def _sh(sandbox, script):
    result = sandbox.exec(["sh", "-c", script])
    assert result.exit_code == 0, (script, result.stdout, result.stderr)
    return result


def _plant_token(sandbox):
    _sh(sandbox, f"mkdir -p {os.path.dirname(TOKEN_PATH)} && printf %s '{SENTINEL}' > {TOKEN_PATH}")


def _run_trusted_step8(sandbox, client, run_dir, secrets, base, *, credential_files, inspect=None):
    """Exactly `cli.py`'s step 8 (§7): kill, snapshot #2, helper, archive,
    notices, load, the three git_capture calls, check_run_outputs."""
    kill_sandbox(sandbox)
    snap = snapshot(sandbox, ("/root/.gemini",), credential_files=credential_files)
    secrets.add(snap.secrets)
    capture = TrustedCapture(sandbox.run_id, client, image_id=sandbox_image_id(sandbox))
    split = None
    try:
        capture.start()
        split = archive_sandbox_repo(sandbox)
        if split.notices:
            _append_event_log(event_log_path(run_dir), notices_to_events(split.notices))
        capture.load(split, base)
        branch = commit_run_branch(capture, sandbox.run_id)
        base_ref = resolve_diff_base(capture, branch)
        build_structured_diff(capture, base_ref, branch, run_dir)
        if inspect is not None:
            inspect(capture, branch, base_ref)
        check_run_outputs(capture, secrets, run_dir, branch, base_ref)
        return branch, base_ref
    finally:
        capture.remove()
        if split is not None:
            split.close()


def _git_out(capture, *args):
    r = capture.exec(_git(*args), env=_GIT_ENV)
    return r.exit_code, r.stdout


@pytest.fixture
def served_repo_with_tracked_symlink(tmp_path_factory):
    """`served_repo`'s pattern, plus a tracked `docs/latest -> /usr/share/doc/x`."""
    root = tmp_path_factory.mktemp("served-symlink")
    source = root / "source"
    source.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=source, check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "Probe")
    (source / "README.md").write_text("# symlink target\n")
    (source / "docs").mkdir()
    os.symlink("/usr/share/doc/x", source / "docs" / "latest")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(root / "target.git")], check=True)
    daemon = subprocess.Popen(
        ["git", "daemon", "--reuseaddr", f"--port={_SYMLINK_DAEMON_PORT}", f"--base-path={root}",
         "--export-all", str(root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(_SYMLINK_DAEMON_PORT):  # pragma: no cover
            pytest.skip("git daemon did not come up")
        yield f"git://host.docker.internal:{_SYMLINK_DAEMON_PORT}/target.git"
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)


@requires_docker
@requires_sandbox_image
@pytest.mark.parametrize("vector", ["copy", "core_worktree", "symlink", "gitignored", "tracked_symlink"])
def test_token_hygiene_vectors_in_a_real_container(
    vector, client, run_id, served_repo, served_repo_with_tracked_symlink, env_file, tmp_path
):
    url = served_repo_with_tracked_symlink if vector == "tracked_symlink" else served_repo.for_container
    sandbox = _off_sandbox(client, run_id, url, env_file)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    secrets = SecretSet()
    seen = {}
    try:
        base = record_trusted_base(sandbox)
        _plant_token(sandbox)
        snap1 = snapshot(sandbox, ("/root/.gemini",))
        secrets.add(snap1.secrets)
        assert snap1.credential_files == frozenset({"antigravity-cli/antigravity-oauth-token"})
        if vector == "copy":
            _sh(sandbox, f"cp {TOKEN_PATH} leak.txt")
        elif vector == "core_worktree":
            _sh(sandbox, "git config core.worktree /root && echo x > added.txt")
        elif vector == "symlink":
            _sh(sandbox, f"ln -s {TOKEN_PATH} link")
        elif vector == "gitignored":
            _sh(sandbox, f"echo leak.txt >> .gitignore && cp {TOKEN_PATH} leak.txt")

        def _inspect(capture, branch, base_ref):
            seen["ls"] = _git_out(capture, "ls-tree", "-r", "--full-tree", "--name-only", branch)[1].split("\n")
            seen["worktree"] = _git_out(capture, "config", "--get", "core.worktree")[0]
            seen["status"] = _git_out(capture, "diff", "--name-status", base_ref, branch)[1]
            print("helper", capture.exec(["git", "version"]).stdout.strip())

        try:
            _run_trusted_step8(sandbox, client, run_dir, secrets, base,
                               credential_files=snap1.credential_files, inspect=_inspect)
            outcome = None
        except TokenHygieneViolation as exc:
            outcome = exc
        base.close()
    finally:
        sandbox.teardown()
    if vector in ("copy", "symlink"):
        assert outcome is not None, vector
        kinds = {f.kind for f in outcome.findings}
        assert ("credential_bytes" if vector == "copy" else "symlink_outside_repo") in kinds
        assert SENTINEL not in str(outcome)
        assert SENTINEL not in event_log_path(run_dir).read_text()
        assert (run_dir / "diff.quarantined.json").exists() and not diff_path(run_dir).exists()
    else:
        assert outcome is None, getattr(outcome, "findings", None)
    if vector == "core_worktree":
        assert "added.txt" in seen["ls"]
        assert not any(p.startswith((".gemini", "root", "/")) for p in seen["ls"] if p)
        assert seen["worktree"] == 1
    if vector == "gitignored":
        entries = {e["path"] for e in json.loads(diff_path(run_dir).read_text())["files"]}
        assert "leak.txt" not in entries and "leak.txt" not in seen["ls"]
    if vector == "tracked_symlink":
        assert "docs/latest" in seen["ls"]
        assert "docs/latest" not in seen["status"]
    assert _leftovers(client, run_id) == []


@requires_docker
@requires_sandbox_image
def test_credential_snapshot_never_follows_symlinks_out_of_the_root(
    client, run_id, served_repo, env_file
):
    # agy's `antigravity-cli/cli.log` is a symlink. Real `get_archive` on the
    # whole root and on the narrowed `credential_paths()` file must read only
    # the root's own regular files, never a symlink's target outside it.
    from vaultshield.adapters.antigravity import Antigravity_Adapter

    outside = "OutsideRootAbc0123456789XYZdefGHIjklmnop"
    sandbox = _off_sandbox(client, run_id, served_repo.for_container, env_file)
    try:
        _plant_token(sandbox)
        _sh(sandbox, f"mkdir -p /srv/outside && printf %s '{outside}' > /srv/outside/secret.log && "
                     "ln -s /srv/outside/secret.log /root/.gemini/antigravity-cli/cli.log && "
                     "ln -s /srv/outside /root/.gemini/antigravity-cli/outside-dir")
        whole = snapshot(sandbox, ("/root/.gemini",))
        assert whole.secrets.contains_secret(SENTINEL.encode())
        assert not whole.secrets.contains_secret(outside.encode())
        assert whole.credential_files == frozenset({"antigravity-cli/antigravity-oauth-token"})
        narrowed = snapshot(sandbox, Antigravity_Adapter().credential_paths())
        assert narrowed.secrets.contains_secret(SENTINEL.encode())
        assert not narrowed.secrets.contains_secret(outside.encode())
        # The confirmed token file itself replaced by an escaping symlink.
        _sh(sandbox, f"rm {TOKEN_PATH} && ln -s /srv/outside/secret.log {TOKEN_PATH}")
        swapped = snapshot(sandbox, Antigravity_Adapter().credential_paths())
        assert not swapped.secrets.contains_secret(outside.encode())
    finally:
        sandbox.teardown()
    assert _leftovers(client, run_id) == []


_FAKE_GIT = """#!/bin/sh
touch /tmp/fake-git-ran
case "$1" in
  add) exit 0 ;;
  diff|ls-tree) /usr/bin/git.real "$@" | grep -v hidden; exit 0 ;;
esac
exec /usr/bin/git.real "$@"
"""


@requires_docker
@requires_sandbox_image
@pytest.mark.parametrize("with_secret", [True, False])
def test_a_tampered_in_container_git_cannot_hide_a_file_from_the_branch_or_diff(
    with_secret, client, run_id, served_repo, env_file, tmp_path
):
    sandbox = _off_sandbox(client, run_id, served_repo.for_container, env_file)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    secrets = SecretSet()
    seen = {}
    outcome = None
    try:
        base = record_trusted_base(sandbox)
        _plant_token(sandbox)
        snap1 = snapshot(sandbox, ("/root/.gemini",))
        secrets.add(snap1.secrets)
        hidden = SENTINEL if with_secret else "harmless hidden change"
        forged = _sh(sandbox, "git -c user.name=x -c user.email=x@x commit-tree HEAD^{tree} -m forged").stdout.strip()
        _sh(sandbox, " && ".join([
            "echo modified >> README.md",
            "git update-index --assume-unchanged README.md",
            f"printf %s '{hidden}' > hidden.txt",
            "echo hidden.txt >> .git/info/exclude",
            "mkdir -p /tmp/evil-objects && echo /tmp/evil-objects > .git/objects/info/alternates",
            f"git update-ref refs/replace/$(git rev-parse HEAD) {forged}",
            "git init -q sub && echo sub-secret > sub/secret.txt",
            "rm -f .git/objects/pack/*.pack",
            "printf '#!/bin/sh\\ntouch /tmp/hook-ran\\n' > .git/hooks/post-checkout && chmod +x .git/hooks/post-checkout",
            "mv /usr/bin/git /usr/bin/git.real",
        ]))
        sandbox.write_file("/usr/bin/git", _FAKE_GIT)
        sandbox.exec_detached(["sh", "-c", "mkdir -p .cache; while :; do date >> .cache/grow.log; sleep 0.05; done"])
        time.sleep(0.5)

        def _inspect(capture, branch, base_ref):
            sandbox.container.reload()
            seen["state"] = sandbox.container.attrs["State"]["Status"]
            seen["tree"] = _git_out(capture, "ls-tree", "-r", "--full-tree", branch)[1]
            seen["parent"] = _git_out(capture, "rev-parse", f"{branch}~1")[1].strip()
            seen["forged"] = _git_out(capture, "cat-file", "-e", forged)[0]
            seen["hook"] = capture.exec(["test", "-e", "/tmp/hook-ran"]).exit_code
            seen["diff"] = {e["path"] for e in json.loads(diff_path(run_dir).read_text())["files"]}

        try:
            _run_trusted_step8(sandbox, client, run_dir, secrets, base,
                               credential_files=snap1.credential_files, inspect=_inspect)
        except TokenHygieneViolation as exc:
            outcome = exc
        base.close()
        for path in ("/tmp/fake-git-ran", "/tmp/hook-ran"):
            with pytest.raises(docker.errors.NotFound):
                sandbox.container.get_archive(path)
        print("docker", client.version()["Version"])
    finally:
        sandbox.teardown()
    assert seen["state"] == "exited"
    tree = {line.split("\t", 1)[1]: line.split()[0] for line in seen["tree"].splitlines()}
    for path in ("hidden.txt", "README.md", "sub/secret.txt", ".cache/grow.log"):
        assert path in tree, (path, tree)
    assert tree["sub/secret.txt"] == "100644"
    assert "160000" not in tree.values()
    assert seen["parent"] == base.commit_oid
    assert seen["forged"] != 0
    assert seen["hook"] != 0
    assert {"hidden.txt", "README.md", "sub/secret.txt"} <= seen["diff"] | {
        e["path"] for e in json.loads(((run_dir / "diff.quarantined.json") if with_secret else diff_path(run_dir)).read_text())["files"]}
    events = event_log_path(run_dir).read_text()
    assert "trusted capture: nested git repository at sub was captured as plain files" in events
    if with_secret:
        assert outcome is not None
        assert ("credential_bytes", "hidden.txt") in {(f.kind, f.repo_path) for f in outcome.findings}
        assert SENTINEL not in str(outcome) and SENTINEL not in events
    else:
        assert outcome is None
    assert not client.containers.list(all=True, filters={"name": capture_container_name(run_id)})
    assert _leftovers(client, run_id) == []


@requires_docker
@requires_sandbox_image
@pytest.mark.parametrize("case", ["A", "B"])
def test_trusted_capture_preserves_non_utf8_and_case_variant_names(case, host_repo, served_repo, monkeypatch, client):
    import vaultshield.cli as cli_module
    from click.testing import CliRunner
    from vaultshield.adapters.mock import Mock_Adapter

    real = cli_module.supervise_in_sandbox
    writer = (
        "import os\n"
        "os.chdir('/workspace/repo')\n"
        "open(os.fsencode('\\udcff.txt'), 'w').write(%r)\n"
        "open('A.txt', 'w').write('upper\\n'); open('a.txt', 'w').write('lower\\n')\n"
    ) % ((SENTINEL + "\n") if case == "B" else "plain\n")
    if case == "B":
        writer += (f"os.makedirs('{os.path.dirname(TOKEN_PATH)}', exist_ok=True)\n"
                   f"open('{TOKEN_PATH}', 'w').write({SENTINEL!r})\n")
        monkeypatch.setattr(Mock_Adapter, "credential_paths", lambda self: ("/root/.gemini",))

    def _wrapped(sandbox, *a, **k):
        r = sandbox.exec(["python3", "-c", writer])
        assert r.exit_code == 0, r.stderr
        return real(sandbox, *a, **k)

    monkeypatch.setattr(cli_module, "supervise_in_sandbox", _wrapped)
    monkeypatch.chdir(host_repo)
    marks = _offline_backend(monkeypatch, cli_module)
    monkeypatch.setattr(cli_module.uploader, "upload_run", lambda *a, **k: None)
    monkeypatch.setattr(cli_module.uploader, "upload_events_best_effort", lambda *a, **k: None)
    result = CliRunner().invoke(
        cli_module.cli,
        ["run", "--agent=mock", "--prompt=names", f"--repo-url={served_repo.for_container}",
         "--backend-url=http://127.0.0.1:1", "--project=0123456789abcdef01234567",
         f"--base-image={SANDBOX_IMAGE}"],
        catch_exceptions=False,
    )
    run_dir = sorted((host_repo / ".vaultshield" / "runs").iterdir())[-1]
    events = event_log_path(run_dir).read_text()
    if case == "A":
        assert result.exit_code == 0, result.output
        entries = {e["path"]: e for e in json.loads(diff_path(run_dir).read_text())["files"]}
        assert {"A.txt", "a.txt", "\ufffd.txt"} <= set(entries)
        assert entries["\ufffd.txt"]["patch_truncated"] is True
        assert "patch" not in entries["\ufffd.txt"]
    else:
        assert result.exit_code != 0
        assert "credential material from /root/.gemini" in result.output
        assert "token hygiene violation: credential_bytes in branch (\ufffd.txt)" in events
        assert (run_dir / "diff.quarantined.json").exists() and not diff_path(run_dir).exists()
        assert SENTINEL not in result.output and SENTINEL not in events
        assert len(marks) == 1
    assert not client.containers.list(all=True, filters={"name": capture_container_name(run_dir.name)})
    assert _leftovers(client, run_dir.name) == []
