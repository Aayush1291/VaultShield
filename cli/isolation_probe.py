"""Adversarial host-isolation probe for VaultShield's Sandbox_Module.

Creates a real Sandbox (the same class `vaultshield run` uses internally),
runs a battery of commands a hostile/compromised agent might try, prints
each result, then tears the Sandbox down.

This is NOT part of the VaultShield CLI — it is a standalone verification
script for live demo purposes, built directly on `vaultshield.sandbox.Sandbox`
so it exercises the real isolation boundary rather than a separate simulation.

Usage (from the `cli/` directory, with the venv activated):

    python isolation_probe.py

Batch 3 FR1 (threat check): `--agent`, `--interactive-auth`, `--egress` and
`--lan-gateway` build the Sandbox through the same `EgressGuard` +
`Sandbox.create` + `guard.isolate` path `run` uses, optionally sign the real
`agy` in (you open the URL and paste the code), then run
`isolation_probe_net.py` through Docker exec inside that signed-in container:

    python isolation_probe.py --agent antigravity --interactive-auth --egress log-only
    python isolation_probe.py --agent antigravity --interactive-auth --egress allowlist

Output: a table on stdout, plus `~/.vaultshield/probes/<ts>-egress-<mode>.json`
(probe results and the `/root/.gemini` listing: names, sizes, modes only) and,
in non-off modes, `<ts>-egress-<mode>-proxy.jsonl` (the hosts the agent
contacted). Everything is torn down in `finally`. The probe sends sentinel
payloads only; see `isolation_probe_net.py`.

Requires:
    - Docker daemon running and reachable.
    - Without `--agent`: `.env.sandbox` (or `--env-file`), as before. With
      `--agent`, a temporary dummy credential file is created.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import docker

# Import VaultShield's real Sandbox class directly.
from vaultshield.sandbox import Sandbox, SandboxError


# Each probe: a short label, and the command to run inside the container.
# All are read-only / non-destructive by design — we want to PROVE the
# boundary holds, not test what happens if it doesn't.
PROBES: list[tuple[str, list[str]]] = [
    ("Current working directory", ["pwd"]),
    ("List cwd contents", ["ls", "-la"]),
    ("Read host .env (should FAIL)", ["cat", "/host/.env"]),
    ("Read mounted .env.sandbox (should WORK)", ["cat", "/workspace/.env.sandbox"]),
    ("List host home directory (should FAIL)", ["ls", "-la", "/root"]),
    ("List /home (should be empty/absent)", ["ls", "-la", "/home"]),
    (
        "Access Docker socket (should FAIL)",
        ["ls", "-la", "/var/run/docker.sock"],
    ),
    (
        "Attempt Docker socket read via curl (should FAIL)",
        ["sh", "-c", "curl -s --unix-socket /var/run/docker.sock http://localhost/version || echo CURL_UNAVAILABLE_OR_BLOCKED"],
    ),
    ("Network identity / hostname", ["hostname"]),
    ("Write a test file into the repo", ["sh", "-c", "echo hostile-write-test > /workspace/repo/hostile_probe_marker.txt && cat /workspace/repo/hostile_probe_marker.txt"]),
]

PROBES_DIR = Path.home() / ".vaultshield" / "probes"
NET_PROBE_PATH = "/tmp/vs_probe_net.py"


def run_probes(repo_url: str, base_image: str, env_file: Path) -> int:
    client = docker.from_env()
    sandbox = Sandbox(run_id="isolation-probe-demo", client=client)

    print(f"[+] Creating Sandbox (base_image={base_image}, repo={repo_url}) ...")
    try:
        sandbox.create(repo_url=repo_url, base_image=base_image, env_file=env_file)
    except SandboxError as exc:
        print(f"[!] Sandbox creation FAILED: {exc}")
        return 1

    print("[+] Sandbox created. Running probes:\n")

    results = []
    for label, cmd in PROBES:
        result = sandbox.exec(cmd)
        status = "OK" if result.exit_code == 0 else f"exit={result.exit_code}"
        print(f"--- {label} [{status}] ---")
        print(f"cmd: {' '.join(cmd)}")
        if result.stdout.strip():
            print(f"stdout:\n{result.stdout.strip()}")
        if result.stderr.strip():
            print(f"stderr:\n{result.stderr.strip()}")
        print()
        results.append((label, result))

    print("[+] Tearing down Sandbox ...")
    sandbox.teardown()
    print("[+] Done.\n")

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for label, result in results:
        marker = "✅" if result.exit_code == 0 else "🚫"
        print(f"{marker} {label} (exit={result.exit_code})")

    return 0


def host_default_gateway() -> str | None:
    """The host's default route, for the LAN-gateway probe."""
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
            match = re.search(r"gateway:\s*(\S+)", out)
        else:
            out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
            match = re.search(r"default via (\S+)", out)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def run_agent_probe(agent: str, interactive_auth: bool, mode: str, repo_url: str,
                    base_image: str, lan_gateway: str | None) -> int:
    # Imported here so the original demo mode needs nothing new.
    from vaultshield import cli as cli_module
    from vaultshield.config import EgressConfig
    from vaultshield.egress import (
        EgressGuard,
        egress_container_name,
        int_network_name,
        open_network_name,
        resolve_policy,
    )
    from vaultshield.run_directory import generate_run_id
    from vaultshield.sandbox import container_name

    run_id = generate_run_id()
    policy = resolve_policy(agent, EgressConfig(), mode, app_port=None)
    adapter = cli_module._AGENT_ADAPTERS[agent]()
    client = docker.from_env()
    sandbox = Sandbox(run_id, client)
    guard = EgressGuard(run_id, client, policy, app_port=None)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    PROBES_DIR.mkdir(parents=True, exist_ok=True)
    out_json = PROBES_DIR / f"{ts}-egress-{mode}.json"
    out_proxy = PROBES_DIR / f"{ts}-egress-{mode}-proxy.jsonl"
    lan = lan_gateway or host_default_gateway()

    print(
        "[+] If this process is killed hard, clean up with:\n"
        f"    docker rm -f {container_name(run_id)} {egress_container_name(run_id)}; "
        f"docker network rm {int_network_name(run_id)} {open_network_name(run_id)}"
    )
    workdir = Path(tempfile.mkdtemp(prefix="vs-probe-"))
    env_file = workdir / ".env.sandbox"
    env_file.write_text("")
    code = 1
    try:
        guard.start()
        sandbox.create(repo_url, base_image, env_file, **guard.sandbox_create_kwargs())
        guard.isolate(sandbox)
        print(f"[+] Sandbox {container_name(run_id)} created (egress {mode}).")
        if interactive_auth:
            cli_module._authenticate_in_sandbox(sandbox, adapter, agent)
        source = (Path(__file__).parent / "isolation_probe_net.py").read_text()
        sandbox.write_file(NET_PROBE_PATH, source)
        print(f"[+] Manual equivalent: docker exec {container_name(run_id)} python3 {NET_PROBE_PATH}")
        argv = ["python3", NET_PROBE_PATH, "--json"] + (["--lan-gateway", lan] if lan else [])
        exit_code, (stdout, stderr) = sandbox.container.exec_run(
            argv, demux=True, environment=sandbox.exec_env_defaults or None
        )
        report = json.loads((stdout or b"{}").decode("utf-8", "replace"))
        report.update({"agent": agent, "egress": mode, "interactive_auth": interactive_auth,
                       "probe_exit_code": exit_code})
        out_json.write_text(json.dumps(report, indent=2))
        for r in report.get("results", []):
            print(f"  {r['probe']:<26} {r['target']:<40} {r['result']:<12} {r['detail']}")
        print(f"  /root/.gemini entries: {len(report.get('gemini_listing', []))}")
        for entry in report.get("gemini_listing", []):
            print(f"    {entry['type']:<4} {entry['mode']:<6} {entry['size']:>8}  {entry['path']}")
        code = 0
    except Exception as exc:  # noqa: BLE001 - report, then always tear down
        print(f"[!] probe failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            if guard.active:
                lines = guard.proxy_log_lines()
                out_proxy.write_text("".join(l + "\n" for l in lines))
                hosts = sorted({json.loads(l).get("raw_host") or "-" for l in lines
                                if l.startswith("{") and '"decision"' in l})
                print(f"[+] Hosts seen by the egress proxy: {', '.join(hosts) or '(none)'}")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] could not read the proxy log: {type(exc).__name__}")
        try:
            sandbox.teardown()
        finally:
            guard.teardown(workdir)
        print(f"[+] Torn down. Results: {out_json}" + (f" and {out_proxy}" if guard.active else ""))
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-url", default="https://github.com/octocat/Hello-World.git")
    parser.add_argument("--base-image", default=None)
    parser.add_argument("--env-file", default=".env.sandbox", type=Path)
    parser.add_argument("--agent", default=None, help="probe a signed-in agent's Sandbox (Batch 3)")
    parser.add_argument("--interactive-auth", action="store_true")
    parser.add_argument("--egress", choices=["off", "log-only", "allowlist"], default="off")
    parser.add_argument("--lan-gateway", default=None)
    args = parser.parse_args()

    if args.agent:
        return run_agent_probe(args.agent, args.interactive_auth, args.egress, args.repo_url,
                               args.base_image or "vaultshield-sandbox:local", args.lan_gateway)

    if not args.env_file.is_file():
        print(f"[!] --env-file not found at {args.env_file}. "
              f"Run `vaultshield run --agent=mock ...` once first to generate "
              f"one, or pass --env-file pointing at an existing .env.sandbox.")
        return 1

    return run_probes(args.repo_url, args.base_image or "python:3.11", args.env_file)


if __name__ == "__main__":
    sys.exit(main())
