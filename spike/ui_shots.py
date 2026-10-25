"""ui_shots.py: screenshot every dashboard surface, and check the layout.

Run with the Playwright that is already in cli/.venv (chromium):

    cli/.venv/bin/python spike/ui_shots.py              # build, preview, shoot
    cli/.venv/bin/python spike/ui_shots.py --skip-build # reuse frontend/dist
    cli/.venv/bin/python spike/ui_shots.py --base-url http://localhost:5173

It serves the built dashboard with `vite preview` on a free port (unless
--base-url points at a running dev server), and answers EVERY /api/** request
in the browser with small fixture JSON: 3 projects, 6 runs of mixed statuses,
and one run detail with a diff, a timeline and a finding. Nothing reaches a
backend. Dashboard pages get a FAKE token in localStorage
("fake-ui-shots-token"); no real credential is read or used.

Output goes to ui-shots/ (git-ignored):

  <page>-<width>-<theme>.png   for /, /login, /signup, /dashboard, a project's
                                runs, a run detail on each of its four tabs,
                                the New project modal and the delete dialog,
                                at 390, 1280 and 1920 wide, light and dark
  landing-full-1440-<theme>.png the landing page, full length, at 1440
  layout-report.json            the layout checks below

Layout checks (printed, and the exit code is 1 if any fails): at 390, 768,
1024, 1280, 1440, 1920 and 2560 wide, no page scrolls horizontally; landing
content never exceeds its 1240px container; the dashboard's main column
reaches the right edge of the viewport.

Themes are forced two ways at once, so both paths are exercised: the browser's
color_scheme, and the stored data-theme override (vaultshield.theme). Shots are
taken with reduced motion, so scroll fade-ups do not leave blank sections and
the hero terminal stays on its first ("Without") view.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, sync_playwright

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
OUT = REPO / "ui-shots"

SHOT_WIDTHS = (390, 1280, 1920)
CHECK_WIDTHS = (390, 768, 1024, 1280, 1440, 1920, 2560)
HEIGHTS = {390: 844, 768: 1024, 1024: 768, 1280: 800, 1440: 900, 1920: 1080, 2560: 1440}
THEMES = ("light", "dark")
LANDING_CONTAINER_MAX = 1240

FAKE_TOKEN = "fake-ui-shots-token"
TOKEN_KEY = "vaultshield.dashboard_auth_token"
THEME_KEY = "vaultshield.theme"

# --- fixtures ---------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat().replace("+00:00", "Z")


PROJECTS = [
    {"id": "6526a0000000000000000001", "name": "acme-shop", "repo_url": "git@github.com:acme/shop.git", "created_at": _ago(days=12)},
    {"id": "6526a0000000000000000002", "name": "billing-service", "repo_url": "https://github.com/acme/billing.git", "created_at": _ago(days=5)},
    {"id": "6526a0000000000000000003", "name": "docs-site", "repo_url": "https://gitlab.com/acme/docs.git", "created_at": _ago(hours=20)},
]
P1 = PROJECTS[0]["id"]


def _run(n: int, status: str, agent: str, minutes: int) -> dict:
    return {
        "id": f"6526b00000000000000000{n:02d}",
        "project": P1,
        "run_id": f"20261024-1{n}1500-a1b2c3d4e5f6a7b{n}",
        "agent": agent,
        "status": status,
        "pr_url": "https://github.com/acme/shop/pull/42" if status == "submitted" else "",
        "stuck": False,
        "created_at": _ago(minutes=minutes),
        "updated_at": _ago(minutes=minutes),
    }


RUNS = [
    _run(1, "awaiting_review", "antigravity", 6),
    _run(2, "running", "codex", 25),
    _run(3, "approved", "claude", 180),
    _run(4, "rejected", "kiro", 60 * 26),
    _run(5, "submitted", "antigravity", 60 * 50),
    _run(6, "failed", "mock", 60 * 24 * 4),
]
R1 = RUNS[0]["run_id"]

DETAIL = {
    **RUNS[0],
    "events": [
        {"id": "e1", "event_type": "output", "timestamp": _ago(minutes=7), "payload": "$ npm test\n> 42 passing (1.8s)", "sequence": 0},
        {"id": "e2", "event_type": "file_change", "timestamp": _ago(minutes=6, seconds=40), "payload": "modified src/billing/charge.ts", "sequence": 1},
        {"id": "e3", "event_type": "output", "timestamp": _ago(minutes=6, seconds=10), "payload": "Agent: replaced the hard-coded key with process.env.STRIPE_KEY", "sequence": 2},
    ],
    "diff": {
        "id": "d1",
        "files": [
            {"path": "src/billing/charge.ts", "additions": 12, "deletions": 3},
            {"path": "src/billing/charge.test.ts", "additions": 30, "deletions": 0},
            {"path": "README.md", "additions": 2, "deletions": 1},
        ],
    },
    "screenshots": [],
    "security_findings": [
        {"id": "f1", "rule_id": "hardcoded-secret", "file_path": "src/billing/legacy.ts", "line_number": 14, "description": "A string that looks like an API key is still present in a legacy file."},
    ],
}


def _page(results: list) -> dict:
    return {"count": len(results), "next": None, "previous": None, "results": results}


def handle_api(route: Route) -> None:
    """Answer every /api/** request from the fixtures above."""
    url = urlparse(route.request.url)
    path, query = url.path, parse_qs(url.query)

    def reply(body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    if path == "/api/projects/":
        return reply(_page(PROJECTS))
    if path == "/api/runs/":
        runs = RUNS if query.get("project", [None])[0] == P1 else []
        status = query.get("status", [None])[0]
        if status:
            runs = [r for r in runs if r["status"] == status]
        return reply(_page(runs))
    if path == f"/api/runs/{R1}/":
        return reply(DETAIL)
    if path.startswith("/api/runs/"):
        return reply({**RUNS[1], "events": [], "diff": None, "screenshots": [], "security_findings": []})
    return reply({"detail": "Not found."}, status=404)


# --- server -----------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def preview_server(skip_build: bool):
    if not skip_build:
        subprocess.run(["npm", "run", "build"], cwd=FRONTEND, check=True, stdout=subprocess.DEVNULL)
    port = free_port()
    proc = subprocess.Popen(
        ["npx", "vite", "preview", "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
        cwd=FRONTEND,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("vite preview did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


# --- browser helpers ----------------------------------------------------------


def new_context(browser, width: int, theme: str, signed_in: bool):
    context = browser.new_context(
        viewport={"width": width, "height": HEIGHTS[width]},
        color_scheme=theme,
        reduced_motion="reduce",
        device_scale_factor=1,
    )
    script = f"localStorage.setItem({json.dumps(THEME_KEY)}, {json.dumps(theme)});"
    if signed_in:
        script += f"localStorage.setItem({json.dumps(TOKEN_KEY)}, {json.dumps(FAKE_TOKEN)});"
    context.add_init_script(script)
    context.route("**/api/**", handle_api)
    return context


def settle(page) -> None:
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready.then(() => true)")
    page.wait_for_timeout(150)


PUBLIC = {"landing": "/", "login": "/login", "signup": "/signup"}
DASHBOARD = {
    "dashboard": "/dashboard",
    "runs": f"/dashboard/projects/{P1}",
    "detail-diff": f"/dashboard/projects/{P1}/runs/{R1}?tab=diff",
    "detail-timeline": f"/dashboard/projects/{P1}/runs/{R1}?tab=timeline",
    "detail-screenshots": f"/dashboard/projects/{P1}/runs/{R1}?tab=screenshots",
    "detail-security": f"/dashboard/projects/{P1}/runs/{R1}?tab=security",
}


def shoot(browser, base: str) -> list[str]:
    files: list[str] = []
    for theme in THEMES:
        for width in SHOT_WIDTHS:
            public = new_context(browser, width, theme, signed_in=False)
            page = public.new_page()
            for name, path in PUBLIC.items():
                page.goto(base + path)
                settle(page)
                files.append(save(page, f"{name}-{width}-{theme}.png"))
            public.close()

            ctx = new_context(browser, width, theme, signed_in=True)
            page = ctx.new_page()
            for name, path in DASHBOARD.items():
                page.goto(base + path)
                settle(page)
                files.append(save(page, f"{name}-{width}-{theme}.png"))

            page.goto(base + "/dashboard")
            settle(page)
            # The overview's own button: the sidebar copy is off-canvas at 390.
            page.get_by_role("button", name="New project").last.click()
            page.get_by_role("dialog").wait_for()
            files.append(save(page, f"new-project-modal-{width}-{theme}.png"))

            page.goto(base + DASHBOARD["runs"])
            settle(page)
            page.get_by_role("button", name="Delete project").click()
            page.get_by_role("dialog").wait_for()
            files.append(save(page, f"delete-dialog-{width}-{theme}.png"))
            ctx.close()

        full = new_context(browser, 1440, theme, signed_in=False)
        page = full.new_page()
        page.goto(base + "/")
        settle(page)
        files.append(save(page, f"landing-full-1440-{theme}.png", full_page=True))
        full.close()
    return files


def save(page, name: str, full_page: bool = False) -> str:
    target = OUT / name
    page.screenshot(path=str(target), full_page=full_page)
    return target.name


MEASURE = """() => {
  const doc = document.documentElement;
  // index.css clips body overflow as a safety net; lift it while measuring so
  // the check sees real overflow instead of the clipped result.
  const previous = document.body.style.overflowX;
  document.body.style.overflowX = 'visible';
  const overflow = Math.max(doc.scrollWidth, document.body.scrollWidth) - window.innerWidth;
  document.body.style.overflowX = previous;
  const containers = [...document.querySelectorAll('.lp-container')].map((el) => {
    const cs = getComputedStyle(el);
    return el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  });
  const main = document.querySelector('main');
  return {
    overflow,
    containerMax: containers.length ? Math.max(...containers) : null,
    mainRight: main ? main.getBoundingClientRect().right : null,
    innerWidth: window.innerWidth,
  };
}"""


STEP_WIDTHS = (1280, 1440, 1920)
STEP_TOLERANCE_PX = 1.0

MEASURE_STEPS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return { top: r.top + window.scrollY, height: r.height }; };
  const cards = [...document.querySelectorAll('.lp-step')].map(box);
  const terms = [...document.querySelectorAll('.lp-step .lp-snippet')].map((el) => ({
    ...box(el),
    scrolls: el.scrollHeight > el.clientHeight + 1 && getComputedStyle(el).overflowY !== 'hidden',
  }));
  return { cards, terms };
}"""


def check_steps(browser, base: str) -> list[dict]:
    """The three How-it-works cards, and their three terminal boxes, must be the
    same height (within 1px) and the boxes must start on the same line."""
    rows = []
    for width in STEP_WIDTHS:
        ctx = new_context(browser, width, "light", signed_in=False)
        page = ctx.new_page()
        page.goto(base + "/")
        settle(page)
        m = page.evaluate(MEASURE_STEPS)
        ctx.close()

        def spread(values):
            return max(values) - min(values) if values else 0.0

        card_h = [c["height"] for c in m["cards"]]
        term_h = [t["height"] for t in m["terms"]]
        term_top = [t["top"] for t in m["terms"]]
        problems = []
        if len(card_h) != 3 or len(term_h) != 3:
            problems.append(f"expected 3 cards and 3 terminals, found {len(card_h)} and {len(term_h)}")
        if spread(card_h) > STEP_TOLERANCE_PX:
            problems.append(f"card heights differ by {spread(card_h):.1f}px")
        if spread(term_h) > STEP_TOLERANCE_PX:
            problems.append(f"terminal heights differ by {spread(term_h):.1f}px")
        if spread(term_top) > STEP_TOLERANCE_PX:
            problems.append(f"terminal tops differ by {spread(term_top):.1f}px")
        if any(t["scrolls"] for t in m["terms"]):
            problems.append("a terminal box scrolls")
        rows.append({
            "width": width,
            "card_heights": [round(h, 2) for h in card_h],
            "terminal_heights": [round(h, 2) for h in term_h],
            "terminal_tops": [round(t, 2) for t in term_top],
            "problems": problems,
        })
    return rows


def check_layout(browser, base: str) -> list[dict]:
    rows = []
    for width in CHECK_WIDTHS:
        for signed_in, pages in ((False, PUBLIC), (True, DASHBOARD)):
            ctx = new_context(browser, width, "light", signed_in=signed_in)
            page = ctx.new_page()
            for name, path in pages.items():
                page.goto(base + path)
                settle(page)
                m = page.evaluate(MEASURE)
                problems = []
                if m["overflow"] > 0:
                    problems.append(f"scrolls horizontally by {m['overflow']}px")
                if name == "landing" and m["containerMax"] and m["containerMax"] > LANDING_CONTAINER_MAX + 0.5:
                    problems.append(f"container content {m['containerMax']:.0f}px > {LANDING_CONTAINER_MAX}px")
                if signed_in and m["mainRight"] is not None and m["mainRight"] < m["innerWidth"] - 1:
                    problems.append(f"main ends at {m['mainRight']:.0f}px of {m['innerWidth']}px")
                rows.append({"width": width, "page": name, **m, "problems": problems})
            ctx.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", help="use a running dev/preview server instead of starting one")
    parser.add_argument("--skip-build", action="store_true", help="reuse frontend/dist")
    parser.add_argument("--layout-only", action="store_true", help="run the layout checks, no screenshots")
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    server = contextlib.nullcontext(args.base_url) if args.base_url else preview_server(args.skip_build)
    with server as base, sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            files = [] if args.layout_only else shoot(browser, base)
            rows = check_layout(browser, base)
            steps = check_steps(browser, base)
        finally:
            browser.close()

    (OUT / "layout-report.json").write_text(json.dumps({"layout": rows, "steps": steps}, indent=2))
    failures = [r for r in rows if r["problems"]]
    step_failures = [r for r in steps if r["problems"]]
    print(f"{len(files)} screenshots in {OUT.relative_to(REPO)}/")
    for r in rows:
        status = "FAIL " + "; ".join(r["problems"]) if r["problems"] else "ok"
        extra = f" container={r['containerMax']:.0f}" if r["containerMax"] else ""
        print(f"  {r['width']:>4} {r['page']:<20} overflow={r['overflow']:>3}{extra}  {status}")
    print(f"layout: {len(rows) - len(failures)}/{len(rows)} checks passed")
    for r in steps:
        status = "FAIL " + "; ".join(r["problems"]) if r["problems"] else "ok"
        print(
            f"  steps {r['width']:>4} cards={r['card_heights']} terminals={r['terminal_heights']} "
            f"tops={r['terminal_tops']}  {status}"
        )
    print(f"steps: {len(steps) - len(step_failures)}/{len(steps)} equal-height checks passed")
    return 1 if failures or step_failures else 0


if __name__ == "__main__":
    sys.exit(main())
