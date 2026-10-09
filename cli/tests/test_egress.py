"""Unit tests for the egress allowlist, proxy decisions and guard (Batch 3
FR2-FR4, FR6; design §8.1). No Docker: the guard runs against a recording
fake client, and the proxy server runs in a thread on 127.0.0.1.
"""

from __future__ import annotations

import io
import json
import socket
import ssl
import threading
from pathlib import Path

import docker
import pytest

from vaultshield import egress, egress_proxy
from vaultshield.config import EgressConfig
from vaultshield.egress import (
    EgressConfigError,
    EgressError,
    EgressGuard,
    isolation_probe_name,
    proxy_lines_to_events,
    remove_egress_resources,
    resolve_policy,
)
from vaultshield.egress_proxy import (
    AllowEntry,
    AllowEntryError,
    ProxyConfig,
    ProxyServer,
    decide,
    is_public_address,
    matches,
    normalise_host,
    parse_allow_entry,
    parse_client_hello_sni,
    parse_connect_request,
)
from vaultshield.monitor import _timestamp_sort_key
from vaultshield.sandbox import ExecResult
from vaultshield.token_hygiene import SecretSet

RUN_ID = "20250614-091530-3f9a2b7c8d1e4f60"


# --- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("api.example.com", AllowEntry("api.example.com", 443)),
        ("API.Example.COM", AllowEntry("api.example.com", 443)),
        ("api.example.com.", AllowEntry("api.example.com", 443)),
        ("api.example.com:8443", AllowEntry("api.example.com", 8443)),
        ("*.example.com", AllowEntry("example.com", 443, wildcard=True)),
        ("*.example.org:8443", AllowEntry("example.org", 8443, wildcard=True)),
        ("8.8.8.8", AllowEntry("8.8.8.8", 443)),
        ("[2001:4860:4860::8888]:443", AllowEntry("2001:4860:4860::8888", 443)),
    ],
)
def test_parse_allow_entry_accepts(text, expected):
    assert parse_allow_entry(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://x.com",
        "x.com/path",
        "u@x.com",
        "*",
        "*.com",
        "a.*.b.com",
        "10.0.0.1",
        "192.168.65.254",
        "169.254.169.254",
        "[::1]",
        "[fc00::1]",
        "x.com:0",
        "x.com:65536",
        "x.com:abc",
        "",
        "a" * 250 + ".com",
        "a" * 64 + ".com",
        "x .com",
        "localhost",
        42,
    ],
)
def test_parse_allow_entry_rejects(text):
    with pytest.raises(AllowEntryError):
        parse_allow_entry(text)


# --- matching ----------------------------------------------------------------


def _entries(*texts):
    return tuple(parse_allow_entry(t) for t in texts)


def test_matching_rules():
    entries = _entries("api.example.com", "*.example.org", "8.8.8.8", "alt.example.net:8443")
    assert matches(entries, "api.example.com", 443)
    assert matches(entries, "a.example.org", 443)
    assert matches(entries, "a.b.example.org", 443)
    assert not matches(entries, "example.org", 443)  # wildcard never matches its apex
    assert not matches(entries, "badexample.org", 443)
    assert not matches(entries, "api.example.com", 80)  # port mismatch
    assert matches(entries, normalise_host("API.Example.com."), 443)
    assert matches(entries, "8.8.8.8", 443)
    assert matches(entries, normalise_host("::ffff:8.8.8.8"), 443)
    assert not matches(entries, "8.8.4.4", 443)
    assert matches(entries, "alt.example.net", 8443)
    # A name entry never matches an IP target.
    assert not matches(_entries("dns.google"), "8.8.8.8", 443)


@pytest.mark.parametrize(
    "address",
    ["10.0.0.1", "172.16.0.1", "192.168.1.1", "192.168.65.254", "169.254.169.254",
     "127.0.0.1", "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:10.0.0.1", "garbage"],
)
def test_non_public_addresses_are_denied(address):
    assert not is_public_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2001:4860::8888", "1.1.1.1"])
def test_public_addresses_are_allowed(address):
    assert is_public_address(address)


# --- decide ------------------------------------------------------------------


class _RecordingResolver:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        return self.answer


def test_a_non_allowlisted_name_is_denied_without_any_dns_lookup():
    resolver = _RecordingResolver(["8.8.8.8"])
    d = decide(_entries("api.example.com"), "allowlist", "vs-probe-abc123.example.com", 443, resolver)
    assert (d.decision, d.reason) == ("deny", "not_in_allowlist")
    assert resolver.calls == []


def test_an_allowlisted_name_resolving_private_is_denied():
    d = decide(_entries("api.example.com"), "allowlist", "api.example.com", 443,
               _RecordingResolver(["192.168.65.254"]))
    assert (d.decision, d.reason) == ("deny", "private_address")


def test_mixed_public_and_private_records_are_denied():
    d = decide(_entries("api.example.com"), "allowlist", "api.example.com", 443,
               _RecordingResolver(["8.8.8.8", "10.0.0.1"]))
    assert (d.decision, d.reason) == ("deny", "private_address")


def test_an_allowlisted_public_name_is_allowed():
    d = decide(_entries("api.example.com"), "allowlist", "api.example.com", 443,
               _RecordingResolver(["2001:4860::8888", "8.8.8.8"]))
    assert d.decision == "allow"
    assert d.addresses == ("8.8.8.8", "2001:4860::8888")  # IPv4 preferred


def test_ip_literal_and_port_rules():
    r = _RecordingResolver(["8.8.8.8"])
    assert decide(_entries("api.example.com"), "allowlist", "1.1.1.1", 443, r).reason == "ip_literal"
    assert decide(_entries("api.example.com"), "allowlist", "api.example.com", 80, r).reason == "port_not_allowed"
    assert r.calls == []


def test_log_only_relaxes_only_the_allowlist_rule():
    entries = _entries("api.example.com")
    d = decide(entries, "log-only", "other.example.com", 443, _RecordingResolver(["8.8.8.8"]))
    assert (d.decision, d.reason) == ("would_deny", "not_in_allowlist")
    assert d.allowed
    assert decide(entries, "log-only", "1.1.1.1", 443, _RecordingResolver([])).decision == "deny"
    assert decide(entries, "log-only", "other.example.com", 443,
                  _RecordingResolver(["10.1.2.3"])).reason == "private_address"


# --- request parsing ---------------------------------------------------------


def test_connect_request_parsing():
    ok = parse_connect_request(b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert (ok.host, ok.port, ok.reason) == ("api.example.com", 443, None)
    v6 = parse_connect_request(b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\n\r\n")
    assert (v6.host, v6.port) == ("[2001:db8::1]", 443)
    assert parse_connect_request(b"GET http://x.com/ HTTP/1.1\r\n\r\n").reason == "plain_http"
    assert parse_connect_request(b"CONNECT x.com HTTP/1.1\r\n\r\n").reason == "bad_request"
    big = b"CONNECT x.com:443 HTTP/1.1\r\n" + b"X: " + b"a" * 9000 + b"\r\n\r\n"
    assert parse_connect_request(big).reason == "bad_request"
    assert parse_connect_request(b"CONNECT x.com:443 HTTP/1.1\r\n").reason == "bad_request"


def _client_hello(server_hostname):
    ctx = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(incoming, outgoing, server_hostname=server_hostname)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return outgoing.read()


def test_sni_parser():
    hello = _client_hello("oauth2.googleapis.com")
    assert parse_client_hello_sni(hello) == "oauth2.googleapis.com"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(incoming, outgoing, server_hostname=None)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    assert parse_client_hello_sni(outgoing.read()) is None
    assert parse_client_hello_sni(hello[:40]) is None
    assert parse_client_hello_sni(b"\x00garbage" * 10) is None


# --- the proxy server, in a thread -------------------------------------------


@pytest.fixture
def echo_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    stop = threading.Event()

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(1024)
                conn.sendall(b"echo:" + data)

    threading.Thread(target=_serve, daemon=True).start()
    yield listener.getsockname()[1]
    stop.set()
    listener.close()


def _server(config, out):
    return ProxyServer(
        config,
        resolver=lambda host, port: ["127.0.0.1"],
        address_policy=lambda address: True,
        bind_host="127.0.0.1",
        connect_port=0,
        sni_port=None,
        forward_listen_port=0,
        out=out,
    ).start()


def _recv_head(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


def test_an_allowed_connect_relays_both_ways(echo_server):
    out = io.StringIO()
    server = _server(ProxyConfig("mock", "allowlist", _entries(f"up.example.com:{echo_server}")), out)
    try:
        with socket.create_connection(("127.0.0.1", server.bound["connect"]), timeout=5) as c:
            c.sendall(f"CONNECT up.example.com:{echo_server} HTTP/1.1\r\n\r\n".encode())
            assert _recv_head(c).startswith(b"HTTP/1.1 200")
            c.sendall(b"ping")
            assert c.recv(1024) == b"echo:ping"
    finally:
        server.stop()
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l["decision"] for l in lines] == ["allow"]


def test_a_denied_connect_gets_403_and_one_deny_line():
    out = io.StringIO()
    server = _server(ProxyConfig("mock", "allowlist", _entries("api.example.com")), out)
    try:
        with socket.create_connection(("127.0.0.1", server.bound["connect"]), timeout=5) as c:
            c.sendall(b"CONNECT Evil.Example.org:443 HTTP/1.1\r\n\r\n")
            assert _recv_head(c).startswith(b"HTTP/1.1 403")
    finally:
        server.stop()
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert len(lines) == 1
    assert lines[0]["decision"] == "deny"
    assert lines[0]["reason"] == "not_in_allowlist"
    assert lines[0]["host"] == "evil.example.org"
    assert lines[0]["raw_host"] == "Evil.Example.org"


def test_the_forwarder_relays_to_its_target(echo_server):
    out = io.StringIO()
    config = ProxyConfig("mock", "allowlist", (), forward_port=echo_server, forward_target="127.0.0.1")
    server = _server(config, out)
    try:
        with socket.create_connection(("127.0.0.1", server.bound["forward"]), timeout=5) as c:
            c.sendall(b"hello")
            assert c.recv(1024) == b"echo:hello"
    finally:
        server.stop()


# --- proxy log → violation events --------------------------------------------

_TS = "2026-01-01T00:00:00.000001+00:00"


def _line(**kw):
    base = {"ts": _TS, "kind": "connect", "host": "x.example.org", "raw_host": "x.example.org",
            "port": 443, "decision": "deny", "reason": "not_in_allowlist"}
    base.update(kw)
    return json.dumps(base)


def test_proxy_lines_map_to_the_exact_payloads():
    lines = [
        json.dumps({"kind": "ready"}),
        "not json",
        _line(decision="allow", reason=None),
        _line(),
        _line(kind="sni", host="y.example.org", raw_host="y.example.org", reason="private_address"),
        _line(decision="would_deny"),
        _line(decision="error", reason="upstream_unreachable"),
    ]
    events = proxy_lines_to_events(lines, "antigravity", "allowlist")
    assert [e.payload for e in events] == [
        "egress violation: CONNECT x.example.org:443 denied (not_in_allowlist; agent antigravity, egress allowlist)",
        "egress violation: TLS SNI y.example.org:443 denied (private_address; agent antigravity, egress allowlist)",
        "egress violation (log-only, allowed): CONNECT x.example.org:443 not in allowlist for antigravity",
    ]
    assert all(e.event_type == "violation" and e.timestamp == _TS for e in events)
    _timestamp_sort_key(events[0])


def test_payloads_use_the_sanitised_raw_host_so_scrub_still_matches():
    needle = "AbCdEfGh1234567890IjKlMnOp12345678"
    lines = [_line(host="abc.example.com", raw_host="AbC.example.com"),
             _line(host=needle.lower() + ".example.com", raw_host=needle + ".example.com"),
             _line(raw_host="bad\x01host\u00e9.example.com")]
    events = proxy_lines_to_events(lines, "mock", "allowlist")
    assert "CONNECT AbC.example.com:443" in events[0].payload
    secrets = SecretSet()
    secrets.add_needle(needle.encode())
    assert "CONNECT ***.example.com:443" in secrets.scrub(events[1].payload)
    assert "bad?host?.example.com" in events[2].payload


def test_events_are_capped_at_200_plus_a_summary():
    events = proxy_lines_to_events([_line() for _ in range(250)], "mock", "allowlist")
    assert len(events) == 201
    assert events[-1].payload == (
        "egress violation: 50 further denials not listed (see egress.jsonl in the Run_Directory)"
    )


# --- resolve_policy ----------------------------------------------------------


def test_resolve_policy_precedence_and_union():
    assert resolve_policy("antigravity", EgressConfig(), None, app_port=3000).mode == "off"
    cfg = EgressConfig(mode="log-only", allow=_entries("api.example.com"))
    assert resolve_policy("antigravity", cfg, None, app_port=3000).mode == "log-only"
    policy = resolve_policy("antigravity", cfg, "allowlist", app_port=3000)
    assert policy.mode == "allowlist"
    assert [e.host for e in policy.entries] == [
        "oauth2.googleapis.com", "daily-cloudcode-pa.googleapis.com",
        "cloudcode-pa.googleapis.com", "api.example.com"]


def test_resolve_policy_errors():
    with pytest.raises(EgressConfigError):
        resolve_policy("antigravity", EgressConfig(), "bogus", app_port=3000)
    with pytest.raises(EgressConfigError):
        resolve_policy("unknown-agent", EgressConfig(), "allowlist", app_port=3000)
    # mock's built-in policy is an explicit deny-all.
    assert resolve_policy("mock", EgressConfig(), "allowlist", app_port=3000).entries == ()


@pytest.mark.parametrize("port", [443, 3128])
def test_resolve_policy_refuses_an_app_port_that_collides_with_the_proxy(port):
    with pytest.raises(EgressConfigError, match=f"app port {port} collides"):
        resolve_policy("mock", EgressConfig(), "allowlist", app_port=port)


def test_resolve_policy_port_rules_do_not_apply_off_or_elsewhere():
    assert resolve_policy("mock", EgressConfig(), "allowlist", app_port=3000).active
    assert not resolve_policy("mock", EgressConfig(), "off", app_port=443).active


def test_isolation_probe_name():
    assert isolation_probe_name(_entries("api.other.com")) == "example.com"
    assert isolation_probe_name(_entries("example.com")) == "example.net"
    assert isolation_probe_name(_entries("*.example.com")) == "example.com"
    assert isolation_probe_name(_entries("example.com", "example.net", "example.org")) == (
        "vs-isolation-check.example.com")
    assert isolation_probe_name(_entries("*.example.com", "example.com", "example.net", "example.org")) == (
        "vs-isolation-check.invalid")
    with pytest.raises(EgressConfigError):
        isolation_probe_name(_entries("*.example.com", "example.com", "example.net",
                                      "example.org", "vs-isolation-check.invalid"))


# --- EgressGuard with a recording fake client --------------------------------


class _Obj:
    def __init__(self, log, name, *, raise_on_remove=None):
        self.log, self.name = log, name
        self.raise_on_remove = raise_on_remove
        self.attrs = {}

    def connect(self, container, **kw):
        self.log.append(("connect", self.name, kw))

    def disconnect(self, container, **kw):
        self.log.append(("disconnect", self.name, kw))

    def remove(self, **kw):
        self.log.append(("remove", self.name))
        if self.raise_on_remove:
            raise self.raise_on_remove


class _Proxy(_Obj):
    def __init__(self, log, name, lines, **kw):
        super().__init__(log, name, **kw)
        self.lines = lines

    def start(self):
        self.log.append(("start", self.name))

    def reload(self):
        self.attrs = {"NetworkSettings": {"Networks": {egress.int_network_name(RUN_ID): {"IPAddress": "10.9.0.2"}}}}

    def logs(self, **kw):
        self.log.append(("logs", self.name))
        return "".join(l + "\n" for l in self.lines).encode()

    def put_archive(self, *a, **k):  # pragma: no cover - must never be called
        self.log.append(("put_archive", self.name))


class _FakeClient:
    def __init__(self, lines=None, raise_on_remove=None):
        self.log = []
        self.created = {}
        self.objects = {}
        client = self
        lines = lines if lines is not None else [json.dumps({"kind": "ready"})]

        class _Images:
            def get(self, ref):
                client.log.append(("images.get", ref))

        class _Networks:
            def create(self, name, **kw):
                client.log.append(("networks.create", name))
                client.created[name] = kw
                obj = _Obj(client.log, name, raise_on_remove=raise_on_remove)
                client.objects[name] = obj
                return obj

            def get(self, name):
                if name in client.objects:
                    return client.objects[name]
                raise docker.errors.NotFound(name)

        class _Containers:
            def create(self, image, **kw):
                client.log.append(("containers.create", kw["name"]))
                client.created[kw["name"]] = {"image": image, **kw}
                obj = _Proxy(client.log, kw["name"], lines, raise_on_remove=raise_on_remove)
                client.objects[kw["name"]] = obj
                return obj

            def get(self, name):
                if name in client.objects:
                    return client.objects[name]
                raise docker.errors.NotFound(name)

        self.images, self.networks, self.containers = _Images(), _Networks(), _Containers()


class _FakeSandbox:
    def __init__(self, log, exit_code=0):
        self.container = object()
        self.exec_env_defaults = {}
        self.log, self.exit_code = log, exit_code
        self.argv = None

    def exec(self, cmd, *, env=None):
        self.log.append(("sandbox.exec",))
        self.argv = cmd
        return ExecResult(exit_code=self.exit_code, stdout="isolated", stderr="")


def _policy(mode="allowlist", allow=("example.com",)):
    return resolve_policy("mock", EgressConfig(allow=_entries(*allow)), mode, app_port=3000)


def test_off_mode_makes_no_docker_call(tmp_path):
    client = _FakeClient()
    guard = EgressGuard(RUN_ID, client, _policy("off"), app_port=3000)
    guard.start()
    assert guard.sandbox_create_kwargs() == {}
    guard.isolate(_FakeSandbox(client.log))
    guard.teardown(tmp_path)
    assert guard.port_publisher is None
    assert client.log == []


def test_start_creates_networks_and_a_hardened_proxy(tmp_path):
    client = _FakeClient()
    guard = EgressGuard(RUN_ID, client, _policy(), app_port=3000)
    guard.start()
    names = [e for e in client.log if e[0] != "images.get"]
    assert names[:3] == [
        ("networks.create", egress.open_network_name(RUN_ID)),
        ("networks.create", egress.int_network_name(RUN_ID)),
        ("containers.create", egress.egress_container_name(RUN_ID)),
    ]
    assert names[3] == ("connect", egress.int_network_name(RUN_ID), {"aliases": ["vaultshield-egress"]})
    assert names[4] == ("start", egress.egress_container_name(RUN_ID))
    assert ("logs", egress.egress_container_name(RUN_ID)) in names
    assert not any(e[0] == "put_archive" for e in client.log)
    assert client.created[egress.int_network_name(RUN_ID)]["internal"] is True
    assert client.created[egress.open_network_name(RUN_ID)]["internal"] is False
    # The int bridge has no gateway address (no path to the Docker VM's listeners).
    assert client.created[egress.int_network_name(RUN_ID)]["options"] == {
        "com.docker.network.bridge.inhibit_ipv4": "true"}
    assert "options" not in client.created[egress.open_network_name(RUN_ID)] or \
        not client.created[egress.open_network_name(RUN_ID)]["options"]
    assert all(client.created[n]["enable_ipv6"] is False
               for n in (egress.int_network_name(RUN_ID), egress.open_network_name(RUN_ID)))
    kw = client.created[egress.egress_container_name(RUN_ID)]
    assert kw["image"] == egress.EGRESS_PROXY_IMAGE
    assert kw["read_only"] is True
    assert kw["cap_drop"] == ["ALL"] and kw["cap_add"] == ["NET_BIND_SERVICE"]
    assert kw["sysctls"]["net.ipv4.ip_forward"] == "0"
    assert kw["sysctls"]["net.ipv6.conf.all.forwarding"] == "0"
    assert kw["security_opt"] == ["no-new-privileges"]
    assert kw["ports"] == {"3000/tcp": None}
    [(stage, mount)] = kw["volumes"].items()
    assert mount == {"bind": "/opt/vaultshield", "mode": "ro"}
    assert sorted(p.name for p in Path(stage).iterdir()) == ["egress.json", "egress_proxy.py"]
    config = json.loads((Path(stage) / "egress.json").read_text())
    assert config["forward"] == {"port": 3000, "target": f"vaultshield-run-{RUN_ID}"}
    sandbox_kwargs = guard.sandbox_create_kwargs()
    assert sandbox_kwargs["cap_drop"] == ["NET_RAW"]
    assert sandbox_kwargs["network"] == egress.open_network_name(RUN_ID)
    assert sandbox_kwargs["extra_hosts"] == {"example.com": "10.9.0.2"}
    assert sandbox_kwargs["sysctls"] == {"net.ipv6.conf.all.disable_ipv6": "1"}
    guard.teardown(tmp_path)


def test_a_proxy_that_never_says_ready_is_an_egress_error(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "PROXY_READY_TIMEOUT_S", 0.2)
    client = _FakeClient(lines=["{}"])
    guard = EgressGuard(RUN_ID, client, _policy(), app_port=3000)
    with pytest.raises(EgressError, match="not ready"):
        guard.start()
    guard.teardown(tmp_path)


def test_a_fatal_proxy_line_is_an_egress_error(tmp_path):
    client = _FakeClient(lines=[json.dumps({"kind": "fatal", "detail": "forwarding enabled"})])
    guard = EgressGuard(RUN_ID, client, _policy(), app_port=3000)
    with pytest.raises(EgressError, match="refused to start"):
        guard.start()
    guard.teardown(tmp_path)


def test_isolate_connects_int_before_disconnecting_open_and_checks(tmp_path):
    client = _FakeClient()
    guard = EgressGuard(RUN_ID, client, _policy(), app_port=3000)
    guard.start()
    client.log.clear()
    sandbox = _FakeSandbox(client.log)
    guard.isolate(sandbox)
    assert [e[:2] for e in client.log] == [
        ("connect", egress.int_network_name(RUN_ID)),
        ("disconnect", egress.open_network_name(RUN_ID)),
        ("sandbox.exec",),
    ]
    # example.com is allowlisted, so the check probes example.net.
    assert sandbox.argv[3] == "example.net"
    assert sandbox.exec_env_defaults["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert sandbox.exec_env_defaults["no_proxy"] == "localhost,127.0.0.1,::1"
    assert sandbox.exec_env_defaults["HTTPS_PROXY"] == "http://vaultshield-egress:3128"
    guard.teardown(tmp_path)


def test_a_failing_isolation_check_is_an_egress_error(tmp_path):
    client = _FakeClient()
    guard = EgressGuard(RUN_ID, client, _policy(), app_port=3000)
    guard.start()
    with pytest.raises(EgressError, match="could not isolate sandbox network"):
        guard.isolate(_FakeSandbox(client.log, exit_code=1))
    guard.teardown(tmp_path)


def test_teardown_order_and_events(tmp_path):
    lines = [json.dumps({"kind": "ready"}), _line(raw_host="example.org")]
    client = _FakeClient(lines=lines)
    guard = EgressGuard(RUN_ID, client, _policy(), app_port=3000)
    guard.start()
    stage = Path(next(iter(client.created[egress.egress_container_name(RUN_ID)]["volumes"])))
    client.log.clear()
    guard.teardown(tmp_path)
    assert [e for e in client.log if e[0] in ("logs", "remove")] == [
        ("logs", egress.egress_container_name(RUN_ID)),
        ("remove", egress.egress_container_name(RUN_ID)),
        ("remove", egress.int_network_name(RUN_ID)),
        ("remove", egress.open_network_name(RUN_ID)),
    ]
    assert not stage.exists()
    assert (tmp_path / "egress.jsonl").read_text().splitlines() == lines
    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events == [{"event_type": "violation", "timestamp": _TS, "payload":
                       "egress violation: CONNECT example.org:443 denied (not_in_allowlist; agent mock, egress allowlist)"}]


def test_teardown_removal_failures_warn_once_each_and_never_raise(tmp_path, capsys):
    client = _FakeClient(raise_on_remove=docker.errors.APIError("refused"))
    guard = EgressGuard(RUN_ID, client, _policy(), app_port=3000)
    guard.start()
    guard.teardown(tmp_path)
    warnings = [l for l in capsys.readouterr().err.splitlines() if l.startswith("Warning:")]
    assert len(warnings) == 3
    assert all("APIError" in w and "refused" not in w for w in warnings)


def test_remove_egress_resources_by_name_and_staging_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(egress.tempfile, "gettempdir", lambda: str(tmp_path))
    mine = tmp_path / f"vaultshield-egress-{RUN_ID}-abc123"
    other = tmp_path / "vaultshield-egress-other-id-x"
    mine.mkdir()
    other.mkdir()
    client = _FakeClient()
    for name in (egress.egress_container_name(RUN_ID), egress.int_network_name(RUN_ID)):
        client.objects[name] = _Obj(client.log, name)
    assert remove_egress_resources(RUN_ID, client) is True
    assert [e for e in client.log if e[0] == "remove"] == [
        ("remove", egress.egress_container_name(RUN_ID)),
        ("remove", egress.int_network_name(RUN_ID)),
    ]
    assert not mine.exists() and other.exists()
    assert remove_egress_resources(RUN_ID, _FakeClient()) is False


def test_remove_egress_resources_reports_a_refusal():
    client = _FakeClient()
    name = egress.int_network_name(RUN_ID)
    client.objects[name] = _Obj(client.log, name, raise_on_remove=docker.errors.APIError("busy"))
    with pytest.raises(EgressError, match=name):
        remove_egress_resources(RUN_ID, client)


def test_a_bare_connect_that_sends_nothing_is_not_logged():
    out = io.StringIO()
    server = _server(ProxyConfig("mock", "allowlist", ()), out)
    try:
        socket.create_connection(("127.0.0.1", server.bound["connect"]), timeout=5).close()
        with socket.create_connection(("127.0.0.1", server.bound["connect"]), timeout=5) as c:
            c.sendall(b"CONNECT x.example.org:443 HTTP/1.1\r\n\r\n")
            _recv_head(c)
    finally:
        server.stop()
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l["host"] for l in lines] == ["x.example.org"]


def test_the_real_log_only_probe_log_maps_to_would_deny_events():
    """The developer's log-only proxy log (hostnames and decisions only)."""
    path = Path.home() / ".vaultshield/probes/20261009T083128Z-egress-log-only-proxy.jsonl"
    if not path.exists():
        pytest.skip("the developer's log-only probe output is not on this machine")
    events = proxy_lines_to_events(path.read_text().splitlines(), "antigravity", "log-only")
    payloads = [e.payload for e in events]
    assert ("egress violation (log-only, allowed): CONNECT example.com:443 not in "
            "allowlist for antigravity") in payloads
    assert any("CONNECT 1.1.1.1:443 denied (ip_literal" in p for p in payloads)


def test_the_antigravity_list_matches_the_probe_decision():
    hosts = egress.BUILTIN_ALLOWLISTS["antigravity"]
    assert hosts == ("oauth2.googleapis.com", "daily-cloudcode-pa.googleapis.com",
                     "cloudcode-pa.googleapis.com")
    for denied in ("www.googleapis.com", "storage.googleapis.com", "play.googleapis.com",
                   "antigravity-unleash.goog", "lh3.googleusercontent.com",
                   "antigravity-cli-auto-updater-974169037036.us-central1.run.app",
                   "playwright.azureedge.net", "playwright-akamai.azureedge.net",
                   "playwright-verizon.azureedge.net"):
        assert denied not in hosts
