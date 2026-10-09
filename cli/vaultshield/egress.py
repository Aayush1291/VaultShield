"""Egress_Module: the per-Run egress allowlist, enforced outside the agent's
control (Batch 3 FR2-FR4, FR6; design D1-D8, D10, D11).

### Mechanism

In `log-only` and `allowlist` modes, `EgressGuard` builds a per-Run Docker
topology before the Sandbox exists:

* `vaultshield-open-<id>` — an ordinary bridge network. The Sandbox is created
  on it, so `git clone` and the dependency install have network (D3).
* `vaultshield-int-<id>` — an `internal: true` bridge: no default route, no
  external DNS.
* `vaultshield-egress-<id>` — the filtering proxy (`egress_proxy.py`), on both
  networks, reachable from the int side as `vaultshield-egress`.

Right after `Sandbox.create` returns, and before any agent code (including the
`agy` sign-in) runs, `isolate` attaches the Sandbox to the int network and
detaches it from the open one, then asserts from inside that direct internet
fails, an unlisted name does not resolve, and the proxy answers.

### Why a root process in the Sandbox cannot undo it (D2)

After the switch the Sandbox's netns has one interface and no default route.
Changing routes, interfaces, iptables or sysctls needs `CAP_NET_ADMIN`, which
Docker does not grant. Attaching networks or changing capabilities is a Docker
API call, and there is no docker.sock in the Sandbox. `NET_RAW` is dropped, so
it cannot craft frames that use the proxy as a router, and the proxy runs with
`ip_forward=0` (and refuses to start otherwise; a container defaults to 1). The
proxy is a separate container holding no secrets; every byte the Sandbox sends
it passes the allowlist check. `HTTPS_PROXY` and the `/etc/hosts` entries are
conveniences: deleting them only means the client cannot connect at all.

### Off mode

`egress: off` (the default) makes every method here a no-op that makes no
Docker call, so `run`'s Docker calls are today's (NFR1).

`config.py` must not import this module (it would pull docker into config
loading); this module imports `config.EgressConfig` instead.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import click
import docker

from vaultshield import egress_proxy
from vaultshield.config import EgressConfig
from vaultshield.egress_proxy import AllowEntry, host_listed, parse_allow_entry
from vaultshield.monitor import EventLogRecord, _append_event_log, _timestamp_sort_key
from vaultshield.run_directory import event_log_path

# Pinned proxy runtime (NFR4): the official image, multi-arch index digest.
EGRESS_PROXY_IMAGE = (
    "python:3.11.16-slim@sha256:"
    "e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e"
)
_EGRESS_PROXY_PULL_REF = (
    "python@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e"
)
PROXY_ALIAS = "vaultshield-egress"
PROXY_PORT = egress_proxy.CONNECT_PORT
PROXY_SNI_PORT = egress_proxy.SNI_PORT
PROXY_MOUNT_DIR = "/opt/vaultshield"
PROXY_READY_TIMEOUT_S = 10.0
EGRESS_LOG_FILENAME = "egress.jsonl"
MAX_VIOLATION_EVENTS = 200

INT_NETWORK_INHIBIT_IPV4 = "com.docker.network.bridge.inhibit_ipv4"

LABEL_RUN_ID = "vaultshield.run_id"
LABEL_ROLE = "vaultshield.role"

# `NO_PROXY` keeps loopback off the proxy: an empty value would send the
# agent's own `http://localhost:<port>` traffic to the proxy, which refuses it
# as `plain_http` and floods the timeline (D11). Loopback never leaves the
# Sandbox's netns, so this does not weaken D2.
NO_PROXY_VALUE = "localhost,127.0.0.1,::1"

# Built-in per-agent lists (D7). Unioned with `egress.allow`.
BUILTIN_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    # From the developer's real-`agy` log-only probe (2026-10-09): the sign-in
    # uses `oauth2.googleapis.com` and the model calls go to
    # `daily-cloudcode-pa.googleapis.com` (11 connections). The production
    # `cloudcode-pa.googleapis.com` was not contacted by this build but is in
    # the binary's strings [inv §6] as the same API's prod endpoint, so it is
    # kept for other release channels. Seen and deliberately NOT listed:
    # `www.googleapis.com` and `storage.googleapis.com` (Cloud Storage upload
    # exfil paths), `play.googleapis.com`, `antigravity-unleash.goog` and the
    # `*.run.app` auto-updater (telemetry, feature flags, updates),
    # `lh3.googleusercontent.com` (avatars) and the `playwright*.azureedge.net`
    # CDNs (browser downloads).
    "antigravity": (
        "oauth2.googleapis.com",
        "daily-cloudcode-pa.googleapis.com",
        "cloudcode-pa.googleapis.com",
    ),
    # Unverified (API-key mode).
    "codex": ("api.openai.com",),
    # Unverified (AWS endpoint-change notice moving Kiro to *.kiro.dev).
    "kiro": ("runtime.us-east-1.kiro.dev", "management.us-east-1.kiro.dev"),
    # Verified: the mock agent makes no requests, so its policy is deny-all.
    "mock": (),
}
BUILTIN_STATUS: dict[str, str] = {
    "antigravity": "enforced; hosts confirmed by the log-only probe run",
    "codex": "unverified",
    "kiro": "unverified",
    "mock": "verified (deny-all)",
}
# Agents whose built-in policy is an explicit deny-all, so an empty effective
# list is accepted.
_DENY_ALL_AGENTS = frozenset({"mock"})

# Candidates for the post-switch "an unlisted name must not resolve" check.
_PROBE_NAME_CANDIDATES = (
    "example.com",
    "example.net",
    "example.org",
    "vs-isolation-check.example.com",
    "vs-isolation-check.invalid",
)

# Runs inside the Sandbox after the switch (§5.1 step 4). Exit 0 only if direct
# internet fails within 2 s, `argv[1]` does not resolve, and the proxy answers.
_ISOLATION_CHECK = r"""
import socket, sys
def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False
if tcp("1.1.1.1", 443):
    print("direct connection to 1.1.1.1:443 succeeded"); sys.exit(1)
try:
    socket.getaddrinfo(sys.argv[1], 443)
    print("resolved " + sys.argv[1]); sys.exit(1)
except socket.gaierror:
    pass
if not tcp(sys.argv[2], int(sys.argv[3])):
    print("the egress proxy is unreachable"); sys.exit(1)
print("isolated")
"""


class EgressError(Exception):
    """The egress topology could not be built, isolated or checked. Raised
    through `run`'s normal failed path (teardown, events upload, failed
    PATCH). Messages never carry proxy log lines, needles or env values."""


class EgressConfigError(EgressError):
    """The effective egress policy is invalid. Raised at `run` step 1, before
    anything exists on disk."""


@dataclass(frozen=True)
class EgressPolicy:
    """The effective policy for one Run: the agent, the mode, and the union of
    the built-in list with `egress.allow`."""

    agent: str
    mode: str
    entries: tuple[AllowEntry, ...]

    @property
    def active(self) -> bool:
        return self.mode != "off"

    @property
    def probe_name(self) -> str:
        return isolation_probe_name(self.entries)


def egress_container_name(run_id: str) -> str:
    return f"vaultshield-egress-{run_id}"


def open_network_name(run_id: str) -> str:
    return f"vaultshield-open-{run_id}"


def int_network_name(run_id: str) -> str:
    return f"vaultshield-int-{run_id}"


def _labels(run_id: str, role: str) -> dict[str, str]:
    return {LABEL_RUN_ID: run_id, LABEL_ROLE: role}


def isolation_probe_name(entries: Iterable[AllowEntry]) -> str:
    """The first candidate name the allowlist does NOT match (exact or
    wildcard, any port). `extra_hosts` maps every exact allowlisted name to
    the proxy, so an allowlisted probe name would resolve on a correctly
    isolated Sandbox."""
    entries = tuple(entries)
    for candidate in _PROBE_NAME_CANDIDATES:
        if not host_listed(entries, candidate):
            return candidate
    raise EgressConfigError(
        "egress: every isolation-check name ("
        + ", ".join(_PROBE_NAME_CANDIDATES)
        + ") is allowlisted, so the post-switch isolation check cannot run; "
        "remove one of them from `egress.allow`"
    )


def resolve_policy(
    agent: str,
    config_egress: EgressConfig,
    cli_mode: str | None,
    *,
    app_port: int | None,
) -> EgressPolicy:
    """The Run's effective egress policy (Batch 3 FR3, D7).

    Precedence: `--egress`, then `egress.mode`, then `off`. The entries are
    the agent's `BUILTIN_ALLOWLISTS` list unioned with `egress.allow`
    (de-duplicated, built-ins first).

    Raises `EgressConfigError` when, in a non-off mode: the effective list is
    empty for an agent without a deny-all built-in policy; every
    isolation-check name is allowlisted; or `app_port` is one of the proxy's
    own listeners (443, 3128), which would collide with the forwarder (§5.2).
    """
    mode = cli_mode if cli_mode is not None else config_egress.mode
    if mode not in egress_proxy.MODES:
        raise EgressConfigError(f"egress: unknown mode {mode!r}")
    builtin = tuple(parse_allow_entry(item) for item in BUILTIN_ALLOWLISTS.get(agent, ()))
    entries = tuple(dict.fromkeys(builtin + tuple(config_egress.allow)))
    policy = EgressPolicy(agent=agent, mode=mode, entries=entries)
    if not policy.active:
        return policy
    if not entries and agent not in _DENY_ALL_AGENTS:
        raise EgressConfigError(
            f"egress: mode {mode} needs at least one allowlisted host for agent "
            f"{agent}, and it has no built-in list; add hosts under `egress.allow` "
            "in .vaultshield.yml or use --egress=off"
        )
    if app_port in (PROXY_PORT, PROXY_SNI_PORT):
        raise EgressConfigError(
            f"egress: the app port {app_port} collides with the egress proxy's "
            f"port {app_port}; change `port` in .vaultshield.yml or use --egress=off"
        )
    isolation_probe_name(entries)
    return policy


def proxy_env() -> dict[str, str]:
    """The exec env that points clients at the proxy (D11). Not secret."""
    url = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
    return {
        "HTTPS_PROXY": url,
        "HTTP_PROXY": url,
        "https_proxy": url,
        "http_proxy": url,
        "NO_PROXY": NO_PROXY_VALUE,
        "no_proxy": NO_PROXY_VALUE,
    }


def _sanitise_host(raw: object) -> str:
    """A log host made safe for an event payload: printable ASCII only (other
    characters → `?`), at most 253 characters, `-` when missing."""
    if not isinstance(raw, str) or not raw:
        return "-"
    cleaned = "".join(ch if 0x20 <= ord(ch) <= 0x7E else "?" for ch in raw)
    return cleaned[:253]


def _render_port(raw: object) -> str:
    return str(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else "-"


def proxy_lines_to_events(
    lines: Iterable[str], agent: str, mode: str
) -> list[EventLogRecord]:
    """Maps the proxy's decision log to `violation` events (Batch 3 FR4, §5.3).

    One event per `deny` / `would_deny` line, in log order, at most
    `MAX_VIOLATION_EVENTS`, then one summary event for the rest. `allow`,
    `error`, `ready` and unparsable lines produce nothing. The payload names
    the host the client sent (`raw_host`, sanitised), not the lowercased
    match key, so `SecretSet.scrub` still finds a token an agent embedded in a
    hostname.
    """
    events: list[EventLogRecord] = []
    extra = 0
    last_ts = None
    for line in lines:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        decision = record.get("decision")
        if decision not in ("deny", "would_deny"):
            continue
        ts = record.get("ts")
        try:
            _timestamp_sort_key(EventLogRecord("violation", ts, "x"))
        except (TypeError, ValueError):
            continue
        host = _sanitise_host(record.get("raw_host"))
        port = _render_port(record.get("port"))
        verb = "TLS SNI" if record.get("kind") == "sni" else "CONNECT"
        if decision == "deny":
            payload = (
                f"egress violation: {verb} {host}:{port} denied "
                f"({record.get('reason')}; agent {agent}, egress {mode})"
            )
        else:
            payload = (
                f"egress violation (log-only, allowed): {verb} {host}:{port} "
                f"not in allowlist for {agent}"
            )
        last_ts = ts
        if len(events) < MAX_VIOLATION_EVENTS:
            events.append(EventLogRecord("violation", ts, payload))
        else:
            extra += 1
    if extra:
        events.append(
            EventLogRecord(
                "violation",
                last_ts,
                f"egress violation: {extra} further denials not listed (see "
                f"{EGRESS_LOG_FILENAME} in the Run_Directory)",
            )
        )
    return events


def _warn(what: str, run_id: str, exc: BaseException) -> None:
    """One `Warning:` line on stderr, exception type only (token-free)."""
    click.echo(
        f"Warning: could not remove {what} for run {run_id}: {type(exc).__name__}",
        err=True,
    )


class EgressGuard:
    """Owns one Run's egress objects (Batch 3 FR2, FR6; D3, D4, D10).

    `__init__` makes no Docker call, so `run` can bind it before its inner
    `try` and `teardown` is always safe. In off mode every method is a no-op.
    """

    def __init__(self, run_id: str, client, policy: EgressPolicy, *, app_port: int | None):
        self.run_id = run_id
        self._client = client
        self.policy = policy
        self._app_port = app_port
        self._open_net = None
        self._int_net = None
        self._proxy = None
        self._proxy_ip: str | None = None
        self._stage: Path | None = None
        self._start_attempted = False
        self._log_lines: list[str] | None = None

    @property
    def active(self) -> bool:
        return self.policy.active

    @property
    def port_publisher(self):
        """The container that publishes the app port to the host: the proxy in
        non-off modes (D4), `None` in off mode (the Sandbox publishes it)."""
        if not self.active or self._app_port is None:
            return None
        return self._proxy

    # --- start ---------------------------------------------------------------

    def _ensure_image(self) -> None:
        try:
            self._client.images.get(EGRESS_PROXY_IMAGE)
            return
        except docker.errors.ImageNotFound:
            pass
        except docker.errors.DockerException as exc:
            raise EgressError(
                f"egress: cannot inspect the egress proxy image {EGRESS_PROXY_IMAGE}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            self._client.images.pull(_EGRESS_PROXY_PULL_REF)
        except docker.errors.DockerException as exc:
            raise EgressError(
                f"egress: cannot pull the egress proxy image {EGRESS_PROXY_IMAGE}: "
                f"{exc}. Run `docker pull {EGRESS_PROXY_IMAGE}` while online, or use "
                "--egress=off."
            ) from exc

    def _write_stage(self) -> Path:
        """The read-only bind mount source: the proxy's own source and its
        config. World-readable, because the proxy runs with no
        CAP_DAC_OVERRIDE and the host owner is not the container's root."""
        stage = Path(tempfile.mkdtemp(prefix=f"vaultshield-egress-{self.run_id}-"))
        self._stage = stage
        os.chmod(stage, 0o755)
        source = Path(egress_proxy.__file__).read_bytes()
        (stage / "egress_proxy.py").write_bytes(source)
        forward = (
            {"port": self._app_port, "target": f"vaultshield-run-{self.run_id}"}
            if self._app_port is not None
            else None
        )
        config = {
            "agent": self.policy.agent,
            "mode": self.policy.mode,
            "allow": [entry.to_json() for entry in self.policy.entries],
            "forward": forward,
        }
        (stage / "egress.json").write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
        for name in ("egress_proxy.py", "egress.json"):
            os.chmod(stage / name, 0o644)
        return stage

    def start(self) -> None:
        """5a: open + int networks, the proxy (created, connected with its
        alias, started) and a wait for its `ready` line (≤ 10 s).

        Raises `EgressError`. A partial start is cleaned up by `teardown`.
        """
        if not self.active:
            return
        self._start_attempted = True
        self._ensure_image()
        try:
            self._open_net = self._client.networks.create(
                open_network_name(self.run_id),
                driver="bridge",
                internal=False,
                enable_ipv6=False,
                labels=_labels(self.run_id, "egress-open"),
            )
            # `inhibit_ipv4`: the bridge gets no gateway address, so the
            # Docker VM's own listeners (rpcbind :111, a Docker Desktop
            # daemon on :63289, measured open on a plain internal bridge's
            # gateway) are not reachable from the Sandbox (§5.6).
            # Container-to-container traffic (Sandbox → proxy) is unaffected.
            self._int_net = self._client.networks.create(
                int_network_name(self.run_id),
                driver="bridge",
                internal=True,
                enable_ipv6=False,
                options={INT_NETWORK_INHIBIT_IPV4: "true"},
                labels=_labels(self.run_id, "egress-int"),
            )
            stage = self._write_stage()
            self._proxy = self._client.containers.create(
                EGRESS_PROXY_IMAGE,
                command=[
                    "python3",
                    "-u",
                    f"{PROXY_MOUNT_DIR}/egress_proxy.py",
                    f"{PROXY_MOUNT_DIR}/egress.json",
                ],
                name=egress_container_name(self.run_id),
                network=open_network_name(self.run_id),
                ports=(
                    {f"{self._app_port}/tcp": None} if self._app_port is not None else None
                ),
                cap_drop=["ALL"],
                cap_add=["NET_BIND_SERVICE"],
                sysctls={
                    "net.ipv4.ip_forward": "0",
                    "net.ipv6.conf.all.forwarding": "0",
                    "net.ipv6.conf.all.disable_ipv6": "1",
                },
                read_only=True,
                tmpfs={"/tmp": ""},
                volumes={str(stage): {"bind": PROXY_MOUNT_DIR, "mode": "ro"}},
                security_opt=["no-new-privileges"],
                mem_limit="128m",
                pids_limit=128,
                labels=_labels(self.run_id, "egress-proxy"),
            )
            self._int_net.connect(self._proxy, aliases=[PROXY_ALIAS])
            self._proxy.start()
        except EgressError:
            raise
        except (docker.errors.DockerException, OSError) as exc:
            raise EgressError(
                f"egress: could not start the egress proxy: {type(exc).__name__}: {exc}"
            ) from exc
        self._wait_ready()
        try:
            self._proxy.reload()
            networks = self._proxy.attrs["NetworkSettings"]["Networks"]
            self._proxy_ip = networks[int_network_name(self.run_id)]["IPAddress"]
        except (docker.errors.DockerException, KeyError, TypeError) as exc:
            raise EgressError(
                f"egress: could not read the egress proxy's address: {type(exc).__name__}"
            ) from exc
        if not self._proxy_ip:
            raise EgressError("egress: the egress proxy has no address on the internal network")

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + PROXY_READY_TIMEOUT_S
        while True:
            try:
                raw = self._proxy.logs(stdout=True, stderr=False)
            except docker.errors.DockerException as exc:
                raise EgressError(
                    f"egress: could not read the egress proxy's log: {type(exc).__name__}"
                ) from exc
            for line in raw.decode("utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("kind") == "ready":
                    return
                if record.get("kind") == "fatal":
                    raise EgressError(
                        f"egress: the egress proxy refused to start: {record.get('detail')}"
                    )
            if time.monotonic() >= deadline:
                raise EgressError(
                    f"egress: the egress proxy was not ready within "
                    f"{PROXY_READY_TIMEOUT_S:.0f}s"
                )
            time.sleep(0.1)

    def sandbox_create_kwargs(self) -> dict:
        """The extra `Sandbox.create` kwargs for this mode (D11, §5.2). Empty
        in off mode, so the Sandbox's `containers.create` call is today's."""
        if not self.active:
            return {}
        extra_hosts = {
            entry.host: self._proxy_ip
            for entry in self.policy.entries
            if not entry.wildcard and not entry.is_ip
        }
        return {
            "network": open_network_name(self.run_id),
            "cap_drop": ["NET_RAW"],
            "sysctls": {"net.ipv6.conf.all.disable_ipv6": "1"},
            "extra_hosts": extra_hosts or None,
        }

    # --- isolate -------------------------------------------------------------

    def isolate(self, sandbox) -> None:
        """The host-side switch (D3, §5.1): connect int, then disconnect open,
        then set the proxy env and assert isolation from inside.

        Raises `EgressError("egress: could not isolate sandbox network: …")`.
        """
        if not self.active:
            return
        try:
            self._int_net.connect(sandbox.container)
            self._open_net.disconnect(sandbox.container, force=False)
        except docker.errors.DockerException as exc:
            raise EgressError(
                f"egress: could not isolate sandbox network: {type(exc).__name__}: {exc}"
            ) from exc
        sandbox.exec_env_defaults = proxy_env()
        try:
            result = sandbox.exec(
                [
                    "python3",
                    "-c",
                    _ISOLATION_CHECK,
                    self.policy.probe_name,
                    PROXY_ALIAS,
                    str(PROXY_PORT),
                ]
            )
        except Exception as exc:  # SandboxError and anything docker-py raises
            raise EgressError(
                f"egress: could not isolate sandbox network: the check could not run: "
                f"{type(exc).__name__}"
            ) from exc
        if result.exit_code != 0:
            detail = (result.stdout or result.stderr or "no output").strip().splitlines()
            raise EgressError(
                "egress: could not isolate sandbox network: "
                f"{(detail[-1] if detail else 'no output')[:200]} "
                f"(check exit code {result.exit_code})"
            )

    # --- teardown ------------------------------------------------------------

    def proxy_log_lines(self) -> list[str]:
        """The proxy's stdout lines (cached after the first read)."""
        if self._log_lines is None:
            if self._proxy is None:
                return []
            raw = self._proxy.logs(stdout=True, stderr=False)
            self._log_lines = raw.decode("utf-8", errors="replace").splitlines()
        return self._log_lines

    def _lookup(self, handle, getter, name: str):
        if handle is not None:
            return handle
        if not self._start_attempted:
            return None
        try:
            return getter(name)
        except docker.errors.NotFound:
            return None

    def teardown(self, run_dir: Path) -> None:
        """D10, in order, each best-effort with at most one warning and never
        raising: proxy logs → `egress.jsonl` + violation events; remove the
        proxy; remove int; remove open; remove the staging dir."""
        if not self.active:
            return
        run_id = self.run_id

        try:
            if self._proxy is not None:
                lines = self.proxy_log_lines()
                (run_dir / EGRESS_LOG_FILENAME).write_text(
                    "".join(line + "\n" for line in lines), encoding="utf-8"
                )
                events = sorted(
                    proxy_lines_to_events(lines, self.policy.agent, self.policy.mode),
                    key=_timestamp_sort_key,
                )
                if events:
                    _append_event_log(event_log_path(run_dir), events)
        except BaseException as exc:  # noqa: BLE001 - teardown never raises
            click.echo(
                f"Warning: could not record the egress log for run {run_id}: "
                f"{type(exc).__name__}",
                err=True,
            )

        steps = (
            (
                f"egress proxy {egress_container_name(run_id)}",
                lambda: self._lookup(
                    self._proxy, self._client.containers.get, egress_container_name(run_id)
                ),
                lambda obj: obj.remove(v=True, force=True),
            ),
            (
                f"network {int_network_name(run_id)}",
                lambda: self._lookup(
                    self._int_net, self._client.networks.get, int_network_name(run_id)
                ),
                lambda obj: obj.remove(),
            ),
            (
                f"network {open_network_name(run_id)}",
                lambda: self._lookup(
                    self._open_net, self._client.networks.get, open_network_name(run_id)
                ),
                lambda obj: obj.remove(),
            ),
        )
        for what, find, remove in steps:
            try:
                obj = find()
                if obj is not None:
                    try:
                        remove(obj)
                    except docker.errors.NotFound:
                        pass
            except BaseException as exc:  # noqa: BLE001
                _warn(what, run_id, exc)
        self._proxy = self._int_net = self._open_net = None

        if self._stage is not None:
            shutil.rmtree(self._stage, ignore_errors=True)
            self._stage = None


def remove_egress_resources(run_id: str, client) -> bool:
    """`vaultshield teardown`'s half (FR6): removes the proxy, the int and open
    networks by their derived names, then every `vaultshield-egress-<id>-*`
    staging dir in the system temp dir. Returns whether anything was removed.

    Idempotent: a missing object is skipped. Raises `EgressError` naming the
    object when Docker refuses to remove one.
    """
    removed = False
    targets = (
        (egress_container_name(run_id), client.containers.get, lambda o: o.remove(v=True, force=True)),
        (int_network_name(run_id), client.networks.get, lambda o: o.remove()),
        (open_network_name(run_id), client.networks.get, lambda o: o.remove()),
    )
    for name, getter, remove in targets:
        try:
            obj = getter(name)
        except docker.errors.NotFound:
            continue
        except docker.errors.DockerException as exc:
            raise EgressError(f"Cannot look up {name!r} for run {run_id}: {exc}") from exc
        try:
            remove(obj)
            removed = True
        except docker.errors.NotFound:
            continue
        except docker.errors.DockerException as exc:
            raise EgressError(f"Cannot remove {name!r} for run {run_id}: {exc}") from exc
    pattern = os.path.join(tempfile.gettempdir(), f"vaultshield-egress-{glob.escape(run_id)}-*")
    for stage in glob.glob(pattern):
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)
            removed = True
    return removed
