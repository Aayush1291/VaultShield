"""Figure 3.2: proposed VaultShield pipeline, in the style of the original
Mermaid rendering. The proposed context-preparation steps (Objective 2) are
part of the first row and marked with an asterisk note.
Render: python3 fig3-2-proposed-pipeline.py ../images/diagrams/fig3-2-proposed-pipeline.png
"""
from svgkit import Canvas

W, H = 450, 352
c = Canvas(W, H)
F, S, LW, FS = "#ececec", "#999999", 0.8, 9


def node(x, y, w, h, s):
    c.box(x, y, w, h, s, FS, fill=F, stroke=S, radius=0, lw=LW)


def frame(y, h):
    c.parts.append(f'<rect x="4" y="{y}" width="{W - 8}" height="{h}" fill="none" '
                   f'stroke="#bbbbbb" stroke-width="0.8" stroke-dasharray="5,3"/>')


def arrow(pts):
    c.line(pts, lw=0.8, color="#555555")


NW, GAP, X0 = 101, 9, 12
X = [X0 + k * (NW + GAP) for k in range(4)]

# row 1: task and proposed context preparation
frame(6, 112)
r1 = ["Prompt or task\n+ repo URL\n+ agent", "Project-context\nscan +\nsanitisation*",
      "Clarifying\nquestions +\ndeveloper answers*", "Enriched prompt\n+ final secret\ncheck*"]
for x, s in zip(X, r1):
    node(x, 18, NW, 52, s)
for k in range(3):
    arrow([(X[k] + NW, 44), (X[k + 1], 44)])
c.text(W / 2, 86, "* Proposed context preparation (Objective 2), planned for Semester VIII.", 8.5, italic=True)
c.text(W / 2, 99, "In Semester VII the literal prompt goes directly to vaultshield run.", 8.5, italic=True)

# row 2: implemented sandboxed execution (right to left)
frame(128, 100)
r2 = ["Trusted capture\nhelper\nbranch + diff.json", "Monitored agent\nexecution: PTY +\nfile watch + egress log",
      "Disposable Docker\nsandbox: fresh clone\n+ dummy .env.sandbox", "vaultshield run\n(host CLI)"]
for x, s in zip(X, r2):
    node(x, 150, NW, 56, s)
arrow([(X[3] + NW / 2, 70), (X[3] + NW / 2, 150)])
for k in (3, 2, 1):
    arrow([(X[k], 178), (X[k - 1] + NW, 178)])

# row 3: storage, review and submission
frame(238, 108)
node(X[0], 266, NW, 44, "Backend + MongoDB\nstatus:\nawaiting_review")
arrow([(X[0] + NW / 2, 206), (X[0] + NW / 2, 266)])
cx, cy, rw, rh = 190, 288, 52, 40
c.parts.append(f'<path d="M{cx},{cy - rh} L{cx + rw},{cy} L{cx},{cy + rh} L{cx - rw},{cy} Z" '
               f'fill="{F}" stroke="{S}" stroke-width="{LW}"/>')
c.text(cx, cy, "Human\nreviewer\n(dashboard)", FS)
arrow([(X[0] + NW, 288), (cx - rw, 288)])
node(300, 250, 140, 32, "vaultshield submit\nopens PR / MR")
node(300, 296, 140, 32, "vaultshield teardown\ndeletes run branch")
arrow([(cx + rw / 2, cy - rh / 2), (300, 266)])
c.label(266, 262, "approve", 8.5)
arrow([(cx + rw / 2, cy + rh / 2), (300, 312)])
c.label(266, 318, "reject", 8.5)
c.save()
