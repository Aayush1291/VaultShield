"""Tiny SVG drawing kit for the report diagrams that need hand placement.

All coordinates are in points (1/72 in) at final print size, so a font size
of 11 here is 11 pt on paper when the figure is placed at its natural width.
Render a figure script with:  python3 <figure>.py <out.png>
which writes an SVG and converts it to a 300 dpi PNG with rsvg-convert.
"""

import subprocess
import sys
from html import escape

FONT = "Helvetica, Arial, sans-serif"
INK = "#333333"
FILL = "#f2f2f2"


def _text_width(s, size):
    # Average Helvetica advance is about 0.53 em for mixed lower-case text.
    return len(s) * size * 0.56


class Canvas:
    def __init__(self, width, height):
        self.w, self.h = width, height
        self.parts = []

    # ---- primitives -------------------------------------------------------
    def text(self, x, y, s, size=11, anchor="middle", weight="normal", bg=False,
             italic=False):
        """Draw one or more lines (s may contain \\n) centred vertically on y."""
        lines = s.split("\n")
        lh = size * 1.18
        top = y - lh * (len(lines) - 1) / 2
        if bg:
            wmax = max(_text_width(l, size) for l in lines)
            bx = {"middle": x - wmax / 2, "start": x, "end": x - wmax}[anchor]
            self.parts.append(
                f'<rect x="{bx - 2:.1f}" y="{top - size * 0.8:.1f}" width="{wmax + 4:.1f}" '
                f'height="{lh * len(lines) + size * 0.1:.1f}" fill="white"/>')
        style = ' font-style="italic"' if italic else ""
        for i, line in enumerate(lines):
            self.parts.append(
                f'<text x="{x:.1f}" y="{top + i * lh + size * 0.35:.1f}" font-family="{FONT}" '
                f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
                f'fill="#111111"{style}>{escape(line)}</text>')

    def box(self, x, y, w, h, label="", size=11.5, fill=FILL, dashed=False,
            bold=False, radius=7, stroke=INK, lw=1.2):
        dash = ' stroke-dasharray="5,3"' if dashed else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{radius}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{lw}"{dash}/>')
        if label:
            self.text(x + w / 2, y + h / 2, label, size,
                      weight="bold" if bold else "normal")
        return (x, y, w, h)

    def cluster(self, x, y, w, h, title, size=11.5, where="top-left"):
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="9" '
            f'fill="none" stroke="#666666" stroke-width="1.1"/>')
        n = title.count("\n")
        lh = size * 1.18
        ty = (y + 12 + n * lh / 2) if where.startswith("top") else (y + h - 7 - 0.35 * size - n * lh / 2)
        if where.endswith("left"):
            self.text(x + 8, ty, title, size, anchor="start", weight="bold")
        else:
            self.text(x + w - 8, ty, title, size, anchor="end", weight="bold")

    def cylinder(self, x, y, w, h, label, size=11.5):
        ry = 5
        self.parts.append(
            f'<path d="M{x},{y + ry} A{w / 2},{ry} 0 0 1 {x + w},{y + ry} V{y + h - ry} '
            f'A{w / 2},{ry} 0 0 1 {x},{y + h - ry} Z" fill="{FILL}" stroke="{INK}" stroke-width="1.2"/>')
        self.parts.append(
            f'<path d="M{x},{y + ry} A{w / 2},{ry} 0 0 0 {x + w},{y + ry}" fill="none" '
            f'stroke="{INK}" stroke-width="1.2"/>')
        self.text(x + w / 2, y + h / 2 + 3, label, size)

    def ellipse(self, cx, cy, rx, ry, label, size=10.5):
        self.parts.append(
            f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" fill="{FILL}" '
            f'stroke="{INK}" stroke-width="1.2"/>')
        self.text(cx, cy, label, size)

    def actor(self, cx, top, label, size=11):
        """UML stick figure; returns the y of the bottom of its label."""
        r = 6
        p = self.parts
        p.append(f'<circle cx="{cx}" cy="{top + r}" r="{r}" fill="white" stroke="{INK}" stroke-width="1.3"/>')
        p.append(f'<line x1="{cx}" y1="{top + 2 * r}" x2="{cx}" y2="{top + 30}" stroke="{INK}" stroke-width="1.3"/>')
        p.append(f'<line x1="{cx - 11}" y1="{top + 18}" x2="{cx + 11}" y2="{top + 18}" stroke="{INK}" stroke-width="1.3"/>')
        p.append(f'<line x1="{cx}" y1="{top + 30}" x2="{cx - 9}" y2="{top + 43}" stroke="{INK}" stroke-width="1.3"/>')
        p.append(f'<line x1="{cx}" y1="{top + 30}" x2="{cx + 9}" y2="{top + 43}" stroke="{INK}" stroke-width="1.3"/>')
        self.text(cx, top + 53 + (label.count("\n") * size * 0.6), label, size, bg=True)
        return top + 58 + label.count("\n") * size * 1.18

    def line(self, pts, arrow=True, dashed=False, both=False, lw=1.2, color="#444444",
             open_head=False):
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
        dash = ' stroke-dasharray="5,3"' if dashed else ""
        head = "url(#open)" if open_head else "url(#arrow)"
        me = f' marker-end="{head}"' if arrow else ""
        ms = f' marker-start="{head.replace(")", "s)")}"' if both else ""
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{lw}"{dash}{me}{ms}/>')

    def label(self, x, y, s, size=10, anchor="middle"):
        self.text(x, y, s, size, anchor=anchor, bg=True)

    # ---- output -------------------------------------------------------------
    def svg(self):
        defs = (
            '<defs>'
            '<marker id="arrow" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#444444"/></marker>'
            '<marker id="arrows" viewBox="0 0 10 10" refX="0.5" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="#444444"/></marker>'
            '<marker id="open" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="8" '
            'markerHeight="8" orient="auto"><path d="M0,0 L10,5 L0,10" fill="none" stroke="#444444" '
            'stroke-width="1.4"/></marker>'
            '</defs>')
        body = "\n".join(self.parts)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}pt" height="{self.h}pt" '
                f'viewBox="0 0 {self.w} {self.h}"><rect width="100%" height="100%" fill="white"/>'
                f'{defs}\n{body}\n</svg>')

    def save(self, png_path=None):
        png_path = png_path or sys.argv[1]
        svg_path = png_path[:-4] + ".svg"
        with open(svg_path, "w") as f:
            f.write(self.svg())
        subprocess.run(["rsvg-convert", "-f", "png", "-d", "300", "-p", "300",
                        "-o", png_path, svg_path], check=True)
        print(f"{png_path}: {self.w / 72:.2f} x {self.h / 72:.2f} in")
