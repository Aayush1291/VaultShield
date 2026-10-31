"""Render every diagrams/*.mmd file to a print-quality PNG.

Usage (from the repository root):

    cli/.venv/bin/python docs/blackbook/diagrams/render.py <mermaid_dir> <out_dir>

<mermaid_dir> must contain node_modules/mermaid and node_modules/@mermaid-js/layout-elk
(npm install mermaid@11.17.2 @mermaid-js/layout-elk@0.2.3). Rendering uses the
locally installed Google Chrome through Playwright, at 3x device scale.

Per-diagram layout settings live in each .mmd file's front-matter config block.
The script prints each PNG's size and the effective font size it would have if
placed at the full text width of an A4 page with 1-inch margins.
"""

import functools
import http.server
import json
import os
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
MERMAID_DIR = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
ONLY = set(sys.argv[3:])

TEXT_WIDTH_PX = 602   # 6.27 in at 96 px/in
TEXT_HEIGHT_PX = 840  # usable height under a caption

BASE_CONFIG = {
    "startOnLoad": False,
    "theme": "neutral",
    "securityLevel": "loose",
    "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif", "fontSize": "26px"},
    "flowchart": {"htmlLabels": True, "curve": "basis", "padding": 14, "nodeSpacing": 45, "rankSpacing": 55, "wrappingWidth": 300},
    "sequence": {"useMaxWidth": False, "actorFontSize": 22, "noteFontSize": 20, "messageFontSize": 20, "actorMargin": 55, "messageMargin": 40, "boxMargin": 12, "height": 55, "width": 240},
    "classDiagram": {"useMaxWidth": False},
    "state": {"useMaxWidth": False},
    "gantt": {"useWidth": 1400, "fontSize": 15, "sectionFontSize": 15, "barHeight": 26},
    "xyChart": {"width": 900, "height": 520},
}

INDEX = """<!doctype html><html><body style="margin:0;background:#fff">
<div id="c" style="display:inline-block;padding:18px;background:#fff;width:1400px"></div>
<script type="module">
import mermaid from './node_modules/mermaid/dist/mermaid.esm.min.mjs';
import elk from './node_modules/@mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs';
mermaid.registerLayoutLoaders(elk);
window.mermaid = mermaid;
window.ready = true;
</script></body></html>"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass


def serve(directory: Path) -> int:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


(MERMAID_DIR / "vs_render_index.html").write_text(INDEX)
port = serve(MERMAID_DIR)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(device_scale_factor=3, viewport={"width": 1600, "height": 1200})
    for src in sorted(Path(os.environ.get("VS_SRC", HERE)).glob("*.mmd")):
        if ONLY and src.stem not in ONLY:
            continue
        # The ELK layout loads its chunks lazily; a fresh page load occasionally
        # races that import, so a render is retried a few times before failing.
        for attempt in range(4):
            try:
                page.goto(f"http://127.0.0.1:{port}/vs_render_index.html")
                page.wait_for_function("window.ready === true")
                page.evaluate(f"window.mermaid.initialize({json.dumps(BASE_CONFIG)})")
                svg = page.evaluate(
                    "async (s) => (await window.mermaid.render('d' + Math.floor(Math.random()*1e9), s)).svg",
                    src.read_text(),
                )
                break
            except Exception:
                if attempt == 3:
                    raise
        page.evaluate(
            """(svg) => {
                const c = document.getElementById('c');
                c.innerHTML = svg;
                const el = c.querySelector('svg');
                const vb = el.viewBox.baseVal;
                el.removeAttribute('style');
                el.setAttribute('width', vb.width);
                el.setAttribute('height', vb.height);
                c.style.width = 'auto';
            }""",
            svg,
        )
        box = page.query_selector("#c").bounding_box()
        page.query_selector("#c").screenshot(path=str(OUT / f"{src.stem}.png"))
        w, h = box["width"], box["height"]
        # Printed size when placed at full text width (capped at 85% of the text height).
        scale = min(TEXT_WIDTH_PX / w, 0.85 * TEXT_HEIGHT_PX / h)
        # Body font size: a per-file fontSize / messageFontSize override, else the base 26px.
        import re
        m = re.search(r"(?:messageFontSize|fontSize):\s*(\d+)", src.read_text())
        font_px = int(m.group(1)) if m else 26
        print(f"{src.stem:34s} {w:6.0f} x {h:6.0f} px   printed body text ≈ {font_px * scale * 0.75:4.1f} pt   height ≈ {h * scale / TEXT_HEIGHT_PX:4.0%} of page")
    browser.close()
