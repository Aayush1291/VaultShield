"""Adversarial host-isolation probe for VaultShield's Sandbox_Module.

Creates a real Sandbox (the same class `vaultshield run` uses internally),
runs a battery of commands a hostile/compromised agent might try, prints
each result, then tears the Sandbox down.

This is NOT part of the VaultShield CLI — it is a standalone verification
script for live demo purposes, built directly on `vaultshield.sandbox.Sandbox`
so it exercises the real isolation boundary rather than a separate simulation.

Usage (from the `cli/` directory, with the venv activated):

    python isolation_probe.py

Requires:
    - Docker daemon running and reachable.
    - Run from a directory where `.env.sandbox` exists (or pass --env-file),
      since Sandbox.create() requires a real credential file to mount.
"""

from __future__ import annotations

import argparse
import sys
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default="https://github.com/octocat/Hello-World.git")
    parser.add_argument("--base-image", default="python:3.11")
    parser.add_argument("--env-file", default=".env.sandbox", type=Path)
    args = parser.parse_args()

    if not args.env_file.is_file():
        print(f"[!] --env-file not found at {args.env_file}. "
              f"Run `vaultshield run --agent=mock ...` once first to generate "
              f"one, or pass --env-file pointing at an existing .env.sandbox.")
        return 1

    return run_probes(args.repo_url, args.base_image, args.env_file)


if __name__ == "__main__":
    sys.exit(main())