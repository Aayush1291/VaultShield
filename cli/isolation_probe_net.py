"""In-container network/isolation probe (Batch 3 FR1, design §3.2).

Copied into the Sandbox by `isolation_probe.py` and run through Docker exec,
standard library only. It reports what a hostile agent could reach from where
`agy` actually runs.

Probe safety rules (enforced here):
* Payloads are sentinels only: a TLS ClientHello, `HEAD / HTTP/1.1` with a
  `Host` header and nothing else, and a DNS query for a random sentinel name.
  Nothing is read from the repo, the environment or `/root/.gemini` and sent
  anywhere.
* `/root/.gemini` is walked with `os.lstat` only; no file there is opened.
  Contents and env values are never printed, except `HTTPS_PROXY`.
* 2 s TCP connect, 5 s TLS/HEAD, one attempt per target, no retries.
* LAN and private targets get a TCP connect only, no application bytes.
* Writes nothing (output is stdout only).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import socket
import ssl
import stat
import struct
import sys
import time

CONNECT_TIMEOUT_S = 2.0
TLS_TIMEOUT_S = 5.0
HOST_PORTS = (5050, 8000, 27017, 27018)


def _result(probe, target, result, detail="", started=None):
    ms = int((time.monotonic() - started) * 1000) if started else 0
    return {"probe": probe, "target": target, "result": result, "detail": detail, "ms": ms}


def _classify(exc):
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "unresolvable"
    code = getattr(exc, "errno", None)
    if code == errno.ECONNREFUSED:
        return "refused"
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
        return "unreachable"
    if isinstance(exc, PermissionError):
        return "denied"
    return "error"


def tcp(probe, host, port, family=socket.AF_UNSPEC):
    started = time.monotonic()
    target = f"{host}:{port}"
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except OSError as exc:
        return _result(probe, target, _classify(exc), type(exc).__name__, started)
    last = None
    for fam, typ, proto, _c, addr in infos[:1]:
        sock = socket.socket(fam, typ, proto)
        sock.settimeout(CONNECT_TIMEOUT_S)
        try:
            sock.connect(addr)
            return _result(probe, target, "open", str(addr[0]), started)
        except OSError as exc:
            last = exc
        finally:
            sock.close()
    return _result(probe, target, _classify(last), type(last).__name__, started)


def tls_head(probe, host, port=443):
    started = time.monotonic()
    target = f"{host}:{port}"
    try:
        raw = socket.create_connection((host, port), timeout=TLS_TIMEOUT_S)
    except OSError as exc:
        return _result(probe, target, _classify(exc), type(exc).__name__, started)
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            tls.settimeout(TLS_TIMEOUT_S)
            tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            line = tls.recv(64).split(b"\r\n", 1)[0].decode("latin-1")
            return _result(probe, target, "open", f"TLS ok; {line}", started)
    except OSError as exc:
        return _result(probe, target, _classify(exc), type(exc).__name__, started)
    finally:
        raw.close()


def via_proxy(probe, proxy, host, port, *, tls=False):
    started = time.monotonic()
    target = f"CONNECT {host}:{port}"
    phost, _, pport = proxy.rpartition(":")
    try:
        sock = socket.create_connection((phost, int(pport)), timeout=CONNECT_TIMEOUT_S)
    except OSError as exc:
        return _result(probe, target, _classify(exc), type(exc).__name__, started)
    try:
        sock.settimeout(TLS_TIMEOUT_S)
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                break
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 200" not in status:
            return _result(probe, target, "denied", status, started)
        if not tls:
            return _result(probe, target, "open", status, started)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=host) as t:
            t.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            line = t.recv(64).split(b"\r\n", 1)[0].decode("latin-1")
        return _result(probe, target, "open", f"{status}; TLS ok; {line}", started)
    except OSError as exc:
        return _result(probe, target, _classify(exc), type(exc).__name__, started)
    finally:
        sock.close()


def default_gateway():
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                fields = line.split()
                if fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except OSError:
        pass
    return None


def subnet_gateways():
    """The conventional gateway (first host) of every directly attached
    IPv4 subnet: the int network's gateway once the Sandbox has no default
    route."""
    out = []
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if f[1] == "00000000" or f[2] != "00000000":
                    continue
                net = struct.unpack("<L", struct.pack("<L", int(f[1], 16)))[0]
                ip = socket.inet_ntoa(struct.pack("<L", net))
                first = ip.rsplit(".", 1)[0] + "." + str(int(ip.rsplit(".", 1)[1]) + 1)
                out.append(first)
    except (OSError, ValueError):
        pass
    return out


def resolve(probe, name):
    started = time.monotonic()
    try:
        addrs = sorted({i[4][0] for i in socket.getaddrinfo(name, 443)})
        return _result(probe, name, "resolved", ",".join(addrs), started)
    except OSError as exc:
        return _result(probe, name, _classify(exc), type(exc).__name__, started)


def udp_dns(probe, server, name):
    started = time.monotonic()
    qid = secrets.randbits(16)
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        query += bytes([len(label)]) + label.encode()
    query += b"\x00" + struct.pack(">HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(CONNECT_TIMEOUT_S)
    try:
        sock.sendto(query, (server, 53))
        data, _ = sock.recvfrom(512)
        return _result(probe, f"{server}:53/udp", "open", f"answered ({len(data)} bytes)", started)
    except OSError as exc:
        return _result(probe, f"{server}:53/udp", _classify(exc), type(exc).__name__, started)
    finally:
        sock.close()


def capabilities():
    out = []
    try:
        with open("/proc/self/status") as fh:
            cap = next(l.split()[1] for l in fh if l.startswith("CapEff:"))
        out.append(_result("capabilities", "CapEff", "resolved", cap))
    except (OSError, StopIteration):
        out.append(_result("capabilities", "CapEff", "error"))
    for label, args in (
        ("raw ICMP socket (NET_RAW)", (socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)),
        ("AF_PACKET socket (NET_RAW)", (getattr(socket, "AF_PACKET", 17), socket.SOCK_RAW, 0)),
    ):
        try:
            socket.socket(*args).close()
            out.append(_result("capability", label, "open", "allowed"))
        except OSError as exc:
            out.append(_result("capability", label, _classify(exc), type(exc).__name__))
    try:
        import fcntl

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        req = struct.pack("16si", b"eth0", 1400)
        fcntl.ioctl(sock.fileno(), 0x8922, req)  # SIOCSIFMTU
        out.append(_result("capability", "SIOCSIFMTU (NET_ADMIN)", "open", "allowed"))
    except OSError as exc:
        out.append(_result("capability", "SIOCSIFMTU (NET_ADMIN)", _classify(exc), type(exc).__name__))
    return out


def files():
    out = [_result("file", "/var/run/docker.sock",
                   "open" if os.path.exists("/var/run/docker.sock") else "unreachable")]
    for path in ("/host", "/Users", "/home"):
        try:
            entries = os.listdir(path)
            out.append(_result("file", path, "open", f"{len(entries)} entries"))
        except OSError as exc:
            out.append(_result("file", path, _classify(exc), type(exc).__name__))
    return out


def gemini_listing(root="/root/.gemini"):
    """Relative names, sizes and modes only; nothing is opened."""
    listing = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            listing.append({
                "path": os.path.relpath(full, root),
                "size": st.st_size,
                "mode": oct(stat.S_IMODE(st.st_mode)),
                "type": "dir" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "file",
            })
    return sorted(listing, key=lambda e: e["path"])


def run(lan_gateway):
    results = []
    gw = default_gateway()
    for host in filter(None, (gw, "host.docker.internal")):
        for port in HOST_PORTS:
            results.append(tcp("host port", host, port))
    if gw is None:
        for host in subnet_gateways():
            for port in HOST_PORTS:
                results.append(tcp("int-network gateway", host, port))
    results.append(resolve("resolve", "gateway.docker.internal"))
    if lan_gateway:
        results.append(tcp("LAN gateway", lan_gateway, 80))
    else:
        results.append(_result("LAN gateway", "-", "skipped", "no --lan-gateway"))
    for host in ("10.0.0.1", "172.16.0.1", "169.254.169.254"):
        results.append(tcp("private range", host, 80))
    results.append(tls_head("internet TLS", "example.com"))
    results.append(tcp("internet TCP", "1.1.1.1", 443))
    proxy = os.environ.get("HTTPS_PROXY", "")
    if proxy:
        p = proxy.split("://", 1)[-1].rstrip("/")
        results.append(via_proxy("proxy", p, "example.com", 443))
        results.append(via_proxy("proxy", p, "oauth2.googleapis.com", 443, tls=True))
        results.append(via_proxy("proxy", p, "1.1.1.1", 443))
        results.append(via_proxy("proxy", p, "host.docker.internal", 8000))
        results.append(tls_head("SNI path", "oauth2.googleapis.com"))
    label = f"vs-probe-{secrets.token_hex(8)}.example.com"
    results.append(resolve("DNS exfil (resolver)", label))
    results.append(udp_dns("DNS exfil (direct UDP)", "8.8.8.8", label))
    results.append(tcp("IPv6", "2001:4860:4860::8888", 443, socket.AF_INET6))
    results.extend(capabilities())
    results.extend(files())
    return {"https_proxy": proxy or None, "results": results, "gemini_listing": gemini_listing()}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--lan-gateway", default=None)
    args = parser.parse_args(argv)
    report = run(args.lan_gateway)
    if args.json:
        print(json.dumps(report))
    else:
        for r in report["results"]:
            print(f"{r['probe']:<26} {r['target']:<40} {r['result']:<12} {r['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
