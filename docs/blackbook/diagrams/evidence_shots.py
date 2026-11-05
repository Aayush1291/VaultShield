"""Render captured command output as terminal-style PNGs, and screenshot the
live dashboard for a real run.

Usage (from the repository root, with the CLI venv):
    cli/.venv/bin/python docs/blackbook/diagrams/evidence_shots.py <evidence_dir> <out_dir> [run_id]

<evidence_dir> holds plain-text captures produced by running the real commands
(for example `vaultshield run ... > run-egress.txt 2>&1`). Each capture is shown
verbatim, apart from the optional `keep` filter below, which only selects lines;
it never edits them. The dashboard shot logs in with the CLI's stored API token
(~/.vaultshield/token) and opens the given run on http://localhost:5173.
"""
import html
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

EVIDENCE = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
RUN_ID = sys.argv[3] if len(sys.argv) > 3 else None
OUT.mkdir(parents=True, exist_ok=True)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#fff}}
.win{{display:inline-block;width:1180px;border-radius:10px;overflow:hidden;
     box-shadow:0 0 0 1px #3a3f4b;background:#1e2430;font-family:Menlo,Monaco,monospace}}
.bar{{height:34px;background:#2b313d;display:flex;align-items:center;padding:0 14px;color:#c9ced8;
     font:600 14px -apple-system,Helvetica,Arial,sans-serif}}
.dot{{width:12px;height:12px;border-radius:50%;margin-right:8px}}
.t{{margin-left:14px}}
pre{{margin:0;padding:14px 18px 18px;color:#e6e9ef;font-size:15px;line-height:1.42;
     white-space:pre-wrap;word-break:break-all}}
.p{{color:#8fd18f}} .e{{color:#ff8a80}} .ok{{color:#8fd18f}} .w{{color:#ffd479}}
</style></head><body><div class="win" id="w"><div class="bar">
<span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span>
<span class="dot" style="background:#28c840"></span><span class="t">{title}</span></div>
<pre>{body}</pre></div></body></html>"""


def colour(line):
    s = html.escape(line)
    if line.startswith("$ "):
        return f'<span class="p">{s}</span>'
    if line.startswith(("Error", "FAILED", "E ")) or " FAILED" in line or "failed" in line.split(" in ")[0]:
        return f'<span class="e">{s}</span>'
    if " PASSED" in line or line.rstrip().endswith("passed") or " passed" in line:
        return f'<span class="ok">{s}</span>'
    return s


def terminal(page, name, title, lines):
    body = "\n".join(colour(l) for l in lines)
    page.set_content(PAGE.format(title=html.escape(title), body=body))
    page.locator("#w").screenshot(path=str(OUT / f"{name}.png"))
    print(OUT / f"{name}.png")


COMPACT_CSS = ".win{width:1060px !important} pre{font-size:17px !important;line-height:1.36 !important}"


def terminal_compact(page, name, title, lines):
    """Same capture, narrower window and larger type, for use at text width."""
    body = "\n".join(colour(l) for l in lines)
    html_ = PAGE.format(title=html.escape(title), body=body).replace("</style>", COMPACT_CSS + "</style>")
    page.set_content(html_)
    page.locator("#w").screenshot(path=str(OUT / f"{name}.png"))
    print(OUT / f"{name}.png")


def read(name):
    return (EVIDENCE / name).read_text(errors="replace").splitlines()


def keep(lines, pred):
    return [l for l in lines if pred(l)]


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(device_scale_factor=2, viewport={"width": 1240, "height": 800})

    terminal(page, "ev-1-run-egress", "vaultshield run with the egress allowlist (mock agent)",
             read("run-egress.txt"))

    terminal(page, "ev-2-submit-gate-cleanup", "Approval gate and cleanup check after the run",
             read("submit-refused.txt") + [""] + read("leftovers.txt"))

    probe = read("probe.txt")
    terminal(page, "ev-3-isolation-probe", "isolation_probe.py: hostile-agent probes in a real sandbox",
             ["$ python isolation_probe.py   # excerpt: probe headings and errors"] + keep(
                 probe, lambda l: l.startswith(("---", "[+]", "cat:", "ls:", "/workspace"))))

    terminal(page, "ev-4-run-artifacts", "Artifacts written by the run",
             ["$ cat .env.sandbox            # dummy values only"] + read("artifacts-env.txt")
             + ["", "$ cut -c1-150 events.jsonl     # lines cut at 150 characters"] + read("artifacts-events.txt")
             + ["", "# diff.json, pretty-printed with Python's json module, lines cut at 150 characters"] + read("artifacts-diff.txt"))

    eg = read("egress-tests.txt")
    terminal(page, "ev-5-security-docker-tests", "Docker-backed egress, token-hygiene and tamper tests",
             ["$ pytest -v tests/test_egress_docker.py   # excerpt: result lines"] + keep(
                 eg, lambda l: " PASSED" in l or " FAILED" in l or l.startswith("=") and "passed" in l))

    cli = read("cli-pytest.txt")
    tail = keep(cli, lambda l: l.startswith(("FAILED", "ERROR")) or ("passed" in l and " in " in l))
    terminal(page, "ev-6-test-suites", "Full automated test suites, re-run on 1 November 2026",
             ["$ cd cli && pytest -q -rfE   # excerpt: summary lines"] + tail
             + ["", "$ cd backend && pytest -q"] + read("backend-tests.txt")[-1:]
             + ["", "$ cd frontend && npm test"] + read("frontend-tests.txt")[-4:])

    two = read("two-failures.txt")
    terminal(page, "ev-7-two-failures", "The two failing tests, re-run individually",
             ["$ pytest --durations=2 <the two failing tests>   # excerpt"] + keep(
                 two, lambda l: l.startswith(("E ", "FAILED")) or "s call" in l
                 or ("failed" in l and " in " in l)))

    import os
    if os.path.exists(EVIDENCE / "agy-sandbox.txt"):
        terminal(page, "ev-12-antigravity-sandbox", "Signed-in Antigravity agent in a VaultShield sandbox: probe and egress records",
                 read("agy-sandbox.txt"))
    if os.path.exists(EVIDENCE / "fail1.txt"):
        terminal(page, "ev-10-failure1", "Failure 1, excerpt of pytest output",
                 ["$ pytest --durations=2 tests/test_sandbox_execution.py::<hanging sign-in test>"] + read("fail1.txt"))
        terminal(page, "ev-11-failure2", "Failure 2, excerpt of pytest output",
                 ["$ pytest --durations=2 tests/test_sandbox_execution.py::<stdin delivery test>"] + read("fail2.txt"))
    if os.path.exists(EVIDENCE / "fail1.txt"):
        terminal_compact(page, "c-ev-1-run-egress", "vaultshield run, mock agent, egress allowlist, 1 Nov 2026 (excerpt: path-only lines omitted)", read("run-egress-excerpt.txt"))
        terminal_compact(page, "c-ev-12-antigravity-sandbox", "Signed-in Antigravity agent in a VaultShield sandbox, 9 Oct 2026 (excerpt)", read("agy-sandbox-excerpt.txt"))
        terminal_compact(page, "c-ev-2-submit-gate-cleanup", "Approval gate and cleanup check after the run, 1 Nov 2026",
                         read("submit-refused.txt") + [""] + read("leftovers.txt"))
        terminal_compact(page, "c-ev-10-failure1", "Failure 1, excerpt of pytest output",
                         ["$ pytest --durations=2 tests/test_sandbox_execution.py::<hanging sign-in test>"] + read("fail1.txt"))
        terminal_compact(page, "c-ev-11-failure2", "Failure 2, excerpt of pytest output",
                         ["$ pytest --durations=2 tests/test_sandbox_execution.py::<stdin delivery test>"] + read("fail2.txt"))
    if RUN_ID:
        token = (Path.home() / ".vaultshield" / "token").read_text().strip()
        ctx = browser.new_context(device_scale_factor=2, viewport={"width": 1280, "height": 800})
        dash = ctx.new_page()
        dash.goto("http://localhost:5173/")
        dash.evaluate("t => localStorage.setItem('vaultshield.dashboard_auth_token', t)", token)
        import json, urllib.request
        req = urllib.request.Request(f"http://localhost:8000/api/runs/?run_id={RUN_ID}",
                                     headers={"Authorization": f"Token {token}"})
        data = json.load(urllib.request.urlopen(req))
        rows = data.get("results", data)
        run = next(r for r in rows if r.get("run_id") == RUN_ID)
        url = f"http://localhost:5173/dashboard/projects/{run['project']}/runs/{run['run_id']}"
        dash.goto(url)
        dash.wait_for_timeout(2500)
        dash.screenshot(path=str(OUT / "ev-8-live-run-detail.png"))
        print(OUT / "ev-8-live-run-detail.png")
        for tab in ("Timeline", "Diff"):
            loc = dash.get_by_role("tab", name=tab)
            if loc.count():
                loc.first.click()
                dash.wait_for_timeout(1200)
                dash.screenshot(path=str(OUT / f"ev-9-live-{tab.lower()}.png"))
                print(OUT / f"ev-9-live-{tab.lower()}.png")
    browser.close()
