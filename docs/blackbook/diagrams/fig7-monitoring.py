"""Section 7.5 images.

ev-13-missed-event.png  terminal capture of real commands run over the preserved
                        run directories (.vaultshield/runs); output saved verbatim
                        in docs/blackbook/evidence-captures/missed-event.txt.
fig7-5-monitoring-summary.png  bar chart of the benchmark figures recorded in the
                        safety-events table (32/40, 8/40, 40/40). It visualises
                        reported numbers; it is not a captured log.

Usage (repo root): cli/.venv/bin/python docs/blackbook/diagrams/fig7-monitoring.py
"""
import html
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CAP = ROOT / "evidence-captures" / "missed-event.txt"
OUT = ROOT / "images" / "evidence"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#fff}}
.win{{display:inline-block;width:1060px;border-radius:10px;overflow:hidden;box-shadow:0 0 0 1px #3a3f4b;
     background:#1e2430;font-family:Menlo,Monaco,monospace}}
.bar{{height:34px;background:#2b313d;display:flex;align-items:center;padding:0 14px;color:#c9ced8;
     font:600 14px -apple-system,Helvetica,Arial,sans-serif}}
.dot{{width:12px;height:12px;border-radius:50%;margin-right:8px}}
pre{{margin:0;padding:14px 18px 18px;color:#e6e9ef;font-size:16px;line-height:1.36;white-space:pre-wrap;word-break:break-all}}
.p{{color:#8fd18f}} .e{{color:#ff8a80;font-weight:bold}}
</style></head><body><div class="win" id="w"><div class="bar">
<span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span>
<span class="dot" style="background:#28c840"></span><span style="margin-left:14px">{title}</span></div>
<pre>{body}</pre></div></body></html>"""


def colour(line):
    s = html.escape(line)
    if line.startswith("$ "):
        return f'<span class="p">{s}</span>'
    if "file_change=0" in line or line.strip() == "0":
        return f'<span class="e">{s}</span>'
    return s


def terminal():
    lines = CAP.read_text().splitlines()
    body = "\n".join(colour(l) for l in lines)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        pg = b.new_page(device_scale_factor=2, viewport={"width": 1120, "height": 800})
        pg.set_content(PAGE.format(title="File-change events in the preserved run directories (.vaultshield/runs), 2 Nov 2026",
                                   body=body))
        pg.locator("#w").screenshot(path=str(OUT / "ev-13-missed-event.png"))
        b.close()


def chart():
    W, H = 450, 132
    rows = [("File-change event recorded", 32, "#5b8c5a"),
            ("File-change event missed", 8, "#c62828"),
            ("Agent's file present in diff.json", 40, "#4a6fa5")]
    x0, bw = 170, 200
    parts = [f'<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{W/2}" y="16" font-family="Helvetica, Arial, sans-serif" font-size="10.5" '
             f'font-weight="bold" text-anchor="middle">40 VaultShield benchmark runs, mock agent</text>']
    for k, (label, n, col) in enumerate(rows):
        y = 30 + k * 30
        parts.append(f'<text x="{x0 - 8}" y="{y + 12}" font-family="Helvetica, Arial, sans-serif" '
                     f'font-size="10" text-anchor="end">{html.escape(label)}</text>')
        parts.append(f'<rect x="{x0}" y="{y}" width="{bw}" height="17" fill="#eeeeee" stroke="#bbbbbb" stroke-width="0.6"/>')
        parts.append(f'<rect x="{x0}" y="{y}" width="{bw * n / 40:.1f}" height="17" fill="{col}"/>')
        parts.append(f'<text x="{x0 + bw + 6}" y="{y + 12}" font-family="Helvetica, Arial, sans-serif" '
                     f'font-size="10" font-weight="bold">{n}/40 ({n * 100 // 40}%)</text>')
    parts.append(f'<text x="{W/2}" y="{H - 8}" font-family="Helvetica, Arial, sans-serif" font-size="8.5" '
                 f'font-style="italic" text-anchor="middle">Values as recorded in the benchmark campaign; '
                 f'raw per-run logs were not preserved.</text>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}pt" height="{H}pt" viewBox="0 0 {W} {H}">'
           + "".join(parts) + "</svg>")
    sp = OUT / "fig7-5-monitoring-summary.svg"
    sp.write_text(svg)
    subprocess.run(["rsvg-convert", "-f", "png", "-d", "300", "-p", "300", "-o",
                    str(OUT / "fig7-5-monitoring-summary.png"), str(sp)], check=True)


if __name__ == "__main__":
    terminal()
    chart()
    print("ok")
