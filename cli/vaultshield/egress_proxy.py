"""Egress_Proxy: the per-Run filtering proxy that is a Sandbox's only way out
(Batch 3 FR2, D5, D6).

This file is copied ALONE into a `python:3.11-slim` sidecar container (through a
read-only bind mount, see `egress.EgressGuard.start`), so it imports nothing but
the standard library and nothing from `vaultshield`. Its pure functions are also
imported on the host: `config.py` validates `egress.allow` entries with
`parse_allow_entry`, and `egress.py` reuses `matches` and the entry type.

### What it enforces

The Sandbox's only network peer, after `EgressGuard.isolate`, is this proxy.
Three listeners:

* 3128 — `CONNECT host:port` (what `HTTPS_PROXY` makes clients send). Any other
  method or an absolute-URI request is refused as `plain_http`.
* 443 — a raw TLS connection, matched by the ClientHello's SNI. `extra_hosts`
  points every exact allowlisted name at this proxy, so a client that ignores
  `HTTPS_PROXY` still arrives here.
* the app port (optional) — a plain TCP forwarder from the proxy's published
  port to `vaultshield-run-<id>:<port>`, so the host can screenshot an app in a
  Sandbox that has no route out (D4). It accepts connections from the host side
  only in practice: the Sandbox reaching its own app through it gains nothing.

Decision order (D5), which is also the DNS-exfil fix: normalise the target;
an IP literal is denied unless exactly listed; the allowlist is matched BEFORE
any DNS lookup, so a non-allowlisted random label never reaches a resolver;
only then is the name resolved, and if ANY resolved address is not globally
routable the connection is denied (`private_address`, which closes DNS
rebinding and `host.docker.internal`).

The proxy only tunnels: it never terminates TLS, so it sees no plaintext and
holds no secrets. Every decision is one JSON line on stdout:
`{"ts","kind","host","raw_host","port","decision","reason"}`; the first line is
`{"kind":"ready"}`. `egress.proxy_lines_to_events` turns `deny`/`would_deny`
lines into `violation` events. A connection that was allowed but could not be
completed upstream is logged with `"decision":"error"` and produces no event,
because it is not a policy violation.
"""

from __future__ import annotations

import ipaddress
import json
import re
import select
import socket
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

CONNECT_PORT = 3128
SNI_PORT = 443
DEFAULT_ENTRY_PORT = 443

# Bounds (NFR3).
MAX_HEADER_BYTES = 8 * 1024
MAX_CLIENT_HELLO_BYTES = 16 * 1024
HANDSHAKE_TIMEOUT_S = 5.0
RESOLVE_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 5.0
IDLE_TIMEOUT_S = 300.0
MAX_TUNNELS = 64

MODES = ("off", "log-only", "allowlist")

REASONS = (
    "not_in_allowlist",
    "ip_literal",
    "private_address",
    "port_not_allowed",
    "plain_http",
    "bad_request",
    "no_sni",
)

_LABEL = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PORT = re.compile(r"\A[0-9]{1,5}\Z")
_MAX_NAME_LENGTH = 253


class AllowEntryError(ValueError):
    """An `egress.allow` entry that is not `host[:port]` in the D7 grammar."""


@dataclass(frozen=True)
class AllowEntry:
    """One allowlist entry. `host` is the normalised name, or the suffix after
    `*.` when `wildcard`, or a canonical IP literal."""

    host: str
    port: int = DEFAULT_ENTRY_PORT
    wildcard: bool = False

    @property
    def is_ip(self) -> bool:
        return _ip_or_none(self.host) is not None

    def to_json(self) -> dict:
        return {"host": self.host, "port": self.port, "wildcard": self.wildcard}

    @classmethod
    def from_json(cls, raw: dict) -> "AllowEntry":
        return cls(host=str(raw["host"]), port=int(raw["port"]), wildcard=bool(raw["wildcard"]))

    def __str__(self) -> str:
        host = f"*.{self.host}" if self.wildcard else self.host
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{self.port}"


@dataclass(frozen=True)
class Decision:
    """What the proxy does with one request. `addresses` is filled only for an
    allowed or would-deny request, in connect-preference order."""

    decision: str  # "allow" | "deny" | "would_deny"
    reason: str | None
    host: str
    port: int
    addresses: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow", "would_deny")


@dataclass(frozen=True)
class ParsedRequest:
    """A parsed CONNECT request: `host`/`port` on success, else `reason`."""

    host: str | None
    port: int | None
    reason: str | None = None


# --- pure functions ----------------------------------------------------------


def _ip_or_none(text: str):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _is_ldh_name(name: str, *, min_labels: int) -> bool:
    if not name or len(name) > _MAX_NAME_LENGTH:
        return False
    labels = name.split(".")
    if len(labels) < min_labels:
        return False
    if not all(_LABEL.match(label) for label in labels):
        return False
    # An all-digit last label is no TLD; it is a malformed IP literal.
    return not labels[-1].isdigit()


def is_public_address(address: str) -> bool:
    """True only for a globally routable address (D5 rule 4).

    `is_global` excludes 10/8, 172.16/12, 192.168/16 (incl. Docker Desktop's
    192.168.65/24), 169.254/16, 127/8, 100.64/10, `::1`, `fc00::/7`,
    `fe80::/10`. An IPv4-mapped IPv6 address is unwrapped first, so
    `::ffff:10.0.0.1` is judged as `10.0.0.1`. Anything unparsable is not
    public.
    """
    ip = _ip_or_none(address.split("%", 1)[0])
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global)


def _split_host_port(text: str) -> tuple[str, str | None]:
    """`host[:port]` / `[v6][:port]` → (host, port-text or None). Raises
    `AllowEntryError` on a malformed shape."""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise AllowEntryError("unterminated `[` in an IPv6 literal")
        host, rest = text[1:end], text[end + 1 :]
        if not rest:
            return host, None
        if not rest.startswith(":"):
            raise AllowEntryError("unexpected text after `]`")
        return host, rest[1:]
    if text.count(":") > 1:
        raise AllowEntryError("an IPv6 literal must be written in brackets, e.g. `[2001:db8::1]:443`")
    host, sep, port = text.partition(":")
    return host, (port if sep else None)


def _parse_port(text: str | None) -> int:
    if text is None:
        return DEFAULT_ENTRY_PORT
    if not _PORT.match(text):
        raise AllowEntryError(f"port {text!r} is not a number")
    port = int(text)
    if not 1 <= port <= 65535:
        raise AllowEntryError(f"port {port} is outside 1-65535")
    return port


def parse_allow_entry(text: object) -> AllowEntry:
    """Parses one `egress.allow` entry (D7 grammar) or raises `AllowEntryError`.

    Accepted: an LDH DNS name with ≥ 2 labels; `*.` + such a name (the wildcard
    only as the leftmost label, suffix ≥ 2 labels); a global IPv4 literal; a
    bracketed global IPv6 literal. Each optionally `:port` (default 443).
    Lowercased; one trailing dot stripped. Rejected: a scheme, a path, userinfo,
    whitespace, a bad port, a private/reserved IP literal.
    """
    if not isinstance(text, str):
        raise AllowEntryError(f"expected a string, got {type(text).__name__}")
    if not text:
        raise AllowEntryError("empty entry")
    if any(ch.isspace() for ch in text):
        raise AllowEntryError("whitespace is not allowed")
    if "://" in text:
        raise AllowEntryError("a URL scheme is not allowed; write the bare host")
    if "/" in text:
        raise AllowEntryError("a path is not allowed; write the bare host")
    if "@" in text:
        raise AllowEntryError("userinfo is not allowed")
    lowered = text.lower()
    bracketed = lowered.startswith("[")
    host, port_text = _split_host_port(lowered)
    port = _parse_port(port_text)
    if host.endswith(".") and not bracketed:
        host = host[:-1]
    if not host:
        raise AllowEntryError("empty host")

    if bracketed:
        ip = _ip_or_none(host)
        if not isinstance(ip, ipaddress.IPv6Address):
            raise AllowEntryError(f"{host!r} in brackets is not an IPv6 literal")
        if not is_public_address(str(ip)):
            raise AllowEntryError(f"{ip} is not a global address and could never be reached")
        return AllowEntry(host=str(ip), port=port)

    ip = _ip_or_none(host)
    if ip is not None:
        if not is_public_address(str(ip)):
            raise AllowEntryError(f"{ip} is not a global address and could never be reached")
        return AllowEntry(host=str(ip), port=port)

    if host.startswith("*."):
        suffix = host[2:]
        if "*" in suffix or not _is_ldh_name(suffix, min_labels=2):
            raise AllowEntryError(
                "a wildcard must be the leftmost label of a name with at least two "
                "more labels, e.g. `*.example.com`"
            )
        return AllowEntry(host=suffix, port=port, wildcard=True)
    if "*" in host:
        raise AllowEntryError("a wildcard is only allowed as a leading `*.`")
    if not _is_ldh_name(host, min_labels=2):
        raise AllowEntryError(
            f"{host!r} is not a DNS name with at least two labels (letters, digits, "
            "hyphens; labels of 1-63 characters; at most 253 characters)"
        )
    return AllowEntry(host=host, port=port)


def normalise_host(raw: str) -> str | None:
    """A request target's host, normalised for matching, or `None` if it is not
    a usable name (D5 rule 1).

    Lowercase, one trailing dot stripped, brackets removed from an IPv6
    literal, IP literals in canonical form, other names IDNA-encoded and then
    held to the LDH rules (≥ 1 label).
    """
    if not isinstance(raw, str) or not raw:
        return None
    host = raw.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.endswith("."):
        host = host[:-1]
    if not host:
        return None
    ip = _ip_or_none(host)
    if ip is not None:
        return str(ip)
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    if not _is_ldh_name(host, min_labels=1):
        return None
    return host


def _host_matches(entry: AllowEntry, host: str) -> bool:
    if entry.is_ip or _ip_or_none(host) is not None:
        # An IP target matches only an equal IP entry; a name entry never
        # matches an IP target.
        target = _ip_or_none(host)
        if target is not None and isinstance(target, ipaddress.IPv6Address) and target.ipv4_mapped:
            target = target.ipv4_mapped
        return entry.is_ip and target is not None and _ip_or_none(entry.host) == target
    if entry.wildcard:
        return host.endswith("." + entry.host)
    return host == entry.host


def host_listed(entries: Iterable[AllowEntry], host: str) -> bool:
    """True if any entry matches `host` (normalised), on any port."""
    return any(_host_matches(entry, host) for entry in entries)


def matches(entries: Iterable[AllowEntry], host: str, port: int) -> bool:
    """True if `host:port` (host already normalised) is on the allowlist.

    `*.example.com` matches `a.example.com` and `a.b.example.com`, never
    `example.com` or `badexample.com`. An IP target matches only an equal IP
    entry.
    """
    return any(_host_matches(entry, host) and entry.port == port for entry in entries)


Resolver = Callable[[str, int], Sequence[str]]


def default_resolver(host: str, port: int) -> list[str]:
    """`getaddrinfo` for `host`, returning the address strings in order."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _resolve_with_timeout(resolver: Resolver, host: str, port: int) -> list[str] | None:
    """Runs `resolver` in a worker thread, bounded by `RESOLVE_TIMEOUT_S`.
    `None` on failure or timeout."""
    result: dict[str, object] = {}

    def _work() -> None:
        try:
            result["value"] = list(resolver(host, port))
        except Exception as exc:  # noqa: BLE001 - any resolver failure is "no answer"
            result["error"] = exc

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(RESOLVE_TIMEOUT_S)
    if worker.is_alive() or "value" not in result:
        return None
    return result["value"]  # type: ignore[return-value]


def decide(
    entries: Sequence[AllowEntry],
    mode: str,
    host: str,
    port: int,
    resolver: Resolver = default_resolver,
    *,
    address_policy: Callable[[str], bool] = is_public_address,
) -> Decision:
    """The D5 decision for one request target.

    No resolver call happens before the allowlist check, so a non-allowlisted
    name (in `allowlist` mode) is never resolved. `log-only` relaxes only the
    allowlist rule: an IP literal, a port mismatch on a listed host, or a
    private resolved address is still denied there. An empty `addresses` on an
    allowed decision means the name did not resolve (the caller reports an
    upstream error, not a violation).
    """
    norm = normalise_host(host)
    if norm is None or not isinstance(port, int) or not 1 <= port <= 65535:
        return Decision("deny", "bad_request", host if norm is None else norm, port if isinstance(port, int) else 0)

    if _ip_or_none(norm) is not None:
        if not host_listed(entries, norm):
            return Decision("deny", "ip_literal", norm, port)
        if not matches(entries, norm, port):
            return Decision("deny", "port_not_allowed", norm, port)
        if not address_policy(norm):
            return Decision("deny", "private_address", norm, port)
        return Decision("allow", None, norm, port, (norm,))

    verdict = "allow"
    reason: str | None = None
    if not host_listed(entries, norm):
        if mode != "log-only":
            return Decision("deny", "not_in_allowlist", norm, port)
        verdict, reason = "would_deny", "not_in_allowlist"
    elif not matches(entries, norm, port):
        return Decision("deny", "port_not_allowed", norm, port)

    addresses = _resolve_with_timeout(resolver, norm, port)
    if not addresses:
        return Decision(verdict, reason, norm, port, ())
    if not all(address_policy(address) for address in addresses):
        return Decision("deny", "private_address", norm, port)
    ordered = sorted(addresses, key=lambda a: 0 if ":" not in a else 1)
    return Decision(verdict, reason, norm, port, tuple(dict.fromkeys(ordered)))


def parse_connect_request(data: bytes) -> ParsedRequest:
    """Parses the request head a client sent to the CONNECT listener.

    `data` is everything read so far; it must contain the `\\r\\n\\r\\n`
    terminator within `MAX_HEADER_BYTES`, else `bad_request`. A method other
    than CONNECT (and so any absolute-URI request) is `plain_http`. The target
    must be `host:port` (IPv6 in brackets).
    """
    end = data.find(b"\r\n\r\n")
    if end < 0 or end + 4 > MAX_HEADER_BYTES:
        return ParsedRequest(None, None, "bad_request")
    try:
        head = data[:end].decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes anything
        return ParsedRequest(None, None, "bad_request")
    request_line = head.split("\r\n", 1)[0]
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return ParsedRequest(None, None, "bad_request")
    method, target, _version = parts
    if method.upper() != "CONNECT":
        return ParsedRequest(None, None, "plain_http")
    if target.startswith("["):
        close = target.find("]")
        if close < 0 or target[close + 1 : close + 2] != ":":
            return ParsedRequest(None, None, "bad_request")
        host, port_text = target[: close + 1], target[close + 2 :]
    else:
        host, sep, port_text = target.rpartition(":")
        if not sep or ":" in host:
            return ParsedRequest(None, None, "bad_request")
    if not host or not _PORT.match(port_text or ""):
        return ParsedRequest(None, None, "bad_request")
    port = int(port_text)
    if not 1 <= port <= 65535:
        return ParsedRequest(None, None, "bad_request")
    return ParsedRequest(host, port)


def parse_client_hello_sni(data: bytes) -> str | None:
    """The SNI host name in a TLS ClientHello, or `None` (no SNI, truncated, or
    not a ClientHello). Only the first TLS record is inspected."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        record_len = int.from_bytes(data[3:5], "big")
        body = data[5 : 5 + record_len]
        if len(body) < record_len or len(body) < 4 or body[0] != 0x01:
            return None
        hs_len = int.from_bytes(body[1:4], "big")
        hello = body[4 : 4 + hs_len]
        if len(hello) < hs_len:
            return None
        pos = 2 + 32  # client_version + random
        sid_len = hello[pos]
        pos += 1 + sid_len
        cs_len = int.from_bytes(hello[pos : pos + 2], "big")
        pos += 2 + cs_len
        comp_len = hello[pos]
        pos += 1 + comp_len
        if pos + 2 > len(hello):
            return None
        ext_total = int.from_bytes(hello[pos : pos + 2], "big")
        pos += 2
        end = pos + ext_total
        if end > len(hello):
            return None
        while pos + 4 <= end:
            ext_type = int.from_bytes(hello[pos : pos + 2], "big")
            ext_len = int.from_bytes(hello[pos + 2 : pos + 4], "big")
            ext = hello[pos + 4 : pos + 4 + ext_len]
            if len(ext) < ext_len:
                return None
            pos += 4 + ext_len
            if ext_type != 0:
                continue
            list_len = int.from_bytes(ext[0:2], "big")
            names = ext[2 : 2 + list_len]
            npos = 0
            while npos + 3 <= len(names):
                name_type = names[npos]
                name_len = int.from_bytes(names[npos + 1 : npos + 3], "big")
                name = names[npos + 3 : npos + 3 + name_len]
                if len(name) < name_len:
                    return None
                if name_type == 0 and name:
                    return name.decode("ascii")
                npos += 3 + name_len
            return None
        return None
    except (IndexError, UnicodeDecodeError):
        return None


def now_iso8601() -> str:
    """UTC ISO-8601, the same format as `monitor._now_iso8601`."""
    return datetime.now(timezone.utc).isoformat()


# --- the server --------------------------------------------------------------


@dataclass(frozen=True)
class ProxyConfig:
    """The parsed `egress.json` (no secrets)."""

    agent: str
    mode: str
    allow: tuple[AllowEntry, ...]
    forward_port: int | None = None
    forward_target: str | None = None

    @classmethod
    def from_json(cls, raw: dict) -> "ProxyConfig":
        forward = raw.get("forward") or None
        return cls(
            agent=str(raw["agent"]),
            mode=str(raw["mode"]),
            allow=tuple(AllowEntry.from_json(item) for item in raw.get("allow", [])),
            forward_port=int(forward["port"]) if forward else None,
            forward_target=str(forward["target"]) if forward else None,
        )

    def to_json(self) -> dict:
        return {
            "agent": self.agent,
            "mode": self.mode,
            "allow": [entry.to_json() for entry in self.allow],
            "forward": (
                {"port": self.forward_port, "target": self.forward_target}
                if self.forward_port is not None
                else None
            ),
        }


def _relay(a: socket.socket, b: socket.socket, idle_timeout_s: float) -> None:
    """Copies bytes both ways until either side closes or `idle_timeout_s`
    passes with no traffic."""
    sockets = [a, b]
    try:
        while True:
            readable, _, errored = select.select(sockets, [], sockets, idle_timeout_s)
            if errored or not readable:
                return
            for source in readable:
                chunk = source.recv(65536)
                if not chunk:
                    return
                (b if source is a else a).sendall(chunk)
    except OSError:
        return


def _close(*socks: socket.socket | None) -> None:
    for sock in socks:
        if sock is None:
            continue
        try:
            sock.close()
        except OSError:
            pass


@dataclass
class ProxyServer:
    """The three listeners. `start()` binds and returns; `stop()` closes.

    Ports default to the production ones; tests pass `0` for ephemeral ports
    and read them back from `bound`.
    """

    config: ProxyConfig
    resolver: Resolver = default_resolver
    address_policy: Callable[[str], bool] = is_public_address
    bind_host: str = "0.0.0.0"
    connect_port: int = CONNECT_PORT
    sni_port: int | None = SNI_PORT
    forward_listen_port: int | None = None
    out: object = None
    bound: dict = field(default_factory=dict)
    _listeners: list = field(default_factory=list)
    _slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(MAX_TUNNELS)
    )
    _log_lock: threading.Lock = field(default_factory=threading.Lock)
    _stopping: threading.Event = field(default_factory=threading.Event)

    def log(self, record: dict) -> None:
        stream = self.out if self.out is not None else sys.stdout
        with self._log_lock:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()

    def _decision_line(self, kind: str, raw_host: str | None, decision: Decision) -> None:
        self.log(
            {
                "ts": now_iso8601(),
                "kind": kind,
                "host": decision.host,
                "raw_host": raw_host,
                "port": decision.port or None,
                "decision": decision.decision,
                "reason": decision.reason,
            }
        )

    def _listen(self, port: int, handler: Callable[[socket.socket], None], name: str) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.bind_host, port))
        listener.listen(128)
        self.bound[name] = listener.getsockname()[1]
        self._listeners.append(listener)
        threading.Thread(target=self._accept_loop, args=(listener, handler), daemon=True).start()

    def start(self) -> "ProxyServer":
        self._listen(self.connect_port, self._handle_connect, "connect")
        if self.sni_port is not None:
            self._listen(self.sni_port, self._handle_sni, "sni")
        if self.config.forward_port is not None:
            listen_port = (
                self.forward_listen_port
                if self.forward_listen_port is not None
                else self.config.forward_port
            )
            self._listen(listen_port, self._handle_forward, "forward")
        return self

    def stop(self) -> None:
        self._stopping.set()
        _close(*self._listeners)

    def _accept_loop(self, listener: socket.socket, handler: Callable[[socket.socket], None]) -> None:
        while not self._stopping.is_set():
            try:
                client, _addr = listener.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                _close(client)
                continue
            threading.Thread(target=self._run_handler, args=(handler, client), daemon=True).start()

    def _run_handler(self, handler: Callable[[socket.socket], None], client: socket.socket) -> None:
        try:
            handler(client)
        except Exception:  # noqa: BLE001 - one bad connection never kills the proxy
            _close(client)
        finally:
            self._slots.release()

    def _open_upstream(self, decision: Decision) -> socket.socket | None:
        for address in decision.addresses:
            try:
                return socket.create_connection((address, decision.port), timeout=CONNECT_TIMEOUT_S)
            except OSError:
                continue
        return None

    def _handle_connect(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(HANDSHAKE_TIMEOUT_S)
            data = b""
            while b"\r\n\r\n" not in data and len(data) <= MAX_HEADER_BYTES:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                # A bare connect that sent nothing (e.g. the post-switch
                # isolation check's reachability test) is not a request, so it
                # is neither a decision nor a violation.
                return
            parsed = parse_connect_request(data)
            if parsed.reason is not None:
                self._decision_line("connect", None, Decision("deny", parsed.reason, "-", 0))
                status = "405 Method Not Allowed" if parsed.reason == "plain_http" else "400 Bad Request"
                client.sendall(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
                return
            decision = decide(
                self.config.allow,
                self.config.mode,
                parsed.host or "",
                parsed.port or 0,
                self.resolver,
                address_policy=self.address_policy,
            )
            if not decision.allowed:
                self._decision_line("connect", parsed.host, decision)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            upstream = self._open_upstream(decision)
            if upstream is None:
                self._decision_line(
                    "connect",
                    parsed.host,
                    Decision("error", "upstream_unreachable", decision.host, decision.port),
                )
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return
            self._decision_line("connect", parsed.host, decision)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            # Anything the client pipelined after the head goes upstream first.
            rest = data[data.find(b"\r\n\r\n") + 4 :]
            if rest:
                upstream.sendall(rest)
            client.settimeout(None)
            upstream.settimeout(None)
            _relay(client, upstream, IDLE_TIMEOUT_S)
        except OSError:
            return
        finally:
            _close(client, upstream)

    def _handle_sni(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(HANDSHAKE_TIMEOUT_S)
            data = b""
            while len(data) < MAX_CLIENT_HELLO_BYTES:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) >= 5 and len(data) >= 5 + int.from_bytes(data[3:5], "big"):
                    break
            if not data:
                return
            sni = parse_client_hello_sni(data)
            if sni is None:
                self._decision_line("sni", None, Decision("deny", "no_sni", "-", SNI_PORT))
                return
            decision = decide(
                self.config.allow,
                self.config.mode,
                sni,
                SNI_PORT,
                self.resolver,
                address_policy=self.address_policy,
            )
            if not decision.allowed:
                self._decision_line("sni", sni, decision)
                return
            upstream = self._open_upstream(decision)
            if upstream is None:
                self._decision_line(
                    "sni", sni, Decision("error", "upstream_unreachable", decision.host, SNI_PORT)
                )
                return
            self._decision_line("sni", sni, decision)
            upstream.sendall(data)
            client.settimeout(None)
            upstream.settimeout(None)
            _relay(client, upstream, IDLE_TIMEOUT_S)
        except OSError:
            return
        finally:
            _close(client, upstream)

    def _handle_forward(self, client: socket.socket) -> None:
        upstream = None
        try:
            upstream = socket.create_connection(
                (self.config.forward_target, self.config.forward_port),
                timeout=CONNECT_TIMEOUT_S,
            )
            upstream.settimeout(None)
            _relay(client, upstream, IDLE_TIMEOUT_S)
        except OSError:
            return
        finally:
            _close(client, upstream)


def _read_sysctl(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return None


def forwarding_problems() -> list[str]:
    """The sysctls that must be 0 in the proxy's netns but are not (D2 item 3).
    A missing IPv4 file is a problem (fail closed); a missing IPv6 file means
    there is no IPv6 stack to forward with."""
    problems = []
    ipv4 = _read_sysctl("/proc/sys/net/ipv4/ip_forward")
    if ipv4 != "0":
        problems.append(f"net.ipv4.ip_forward={ipv4}")
    ipv6 = _read_sysctl("/proc/sys/net/ipv6/conf/all/forwarding")
    if ipv6 not in (None, "0"):
        problems.append(f"net.ipv6.conf.all.forwarding={ipv6}")
    return problems


def serve(
    config_path: str,
    *,
    resolver: Resolver = default_resolver,
    address_policy: Callable[[str], bool] = is_public_address,
) -> ProxyServer:
    """Loads `egress.json` and starts every listener. Returns the running
    server (the threads are daemons; `__main__` keeps the process alive)."""
    with open(config_path, encoding="utf-8") as handle:
        config = ProxyConfig.from_json(json.load(handle))
    return ProxyServer(config, resolver=resolver, address_policy=address_policy).start()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"kind": "fatal", "detail": "usage: egress_proxy.py <egress.json>"}), flush=True)
        return 2
    problems = forwarding_problems()
    if problems:
        print(json.dumps({"kind": "fatal", "detail": "forwarding enabled: " + ", ".join(problems)}), flush=True)
        return 2
    try:
        server = serve(argv[1])
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"kind": "fatal", "detail": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 2
    server.log({"kind": "ready", "ts": now_iso8601(), "ports": server.bound})
    try:
        threading.Event().wait()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - runs in the sidecar
    sys.exit(main(sys.argv))
