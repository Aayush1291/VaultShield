"""Figure 3.9: sequence diagram of one approved run, hand placed at print size."""
from svgkit import Canvas

W = 450
X = {"dev": 34, "cli": 112, "sbx": 192, "be": 272, "rev": 340, "git": 408}
names = {"cli": "vaultshield\nCLI", "sbx": "Docker\nsandbox", "be": "Backend\nAPI",
         "git": "GitHub or\nGitLab"}
FS = 10.5          # message font size
ROW = 21           # vertical distance between messages

msgs = [
    ("phase", "Sandboxed run"),
    ("dev", "cli", "1. vaultshield run"),
    ("cli", "be", "2. register run: running"),
    ("cli", "sbx", "3. create sandbox, clone"),
    ("cli", "sbx", "4. run agent under PTY"),
    ("sbx", "cli", "5. output, file events", "reply"),
    ("cli", "sbx", "6. kill, read work tree"),
    ("cli", "cli", "7. build branch and diff\nin capture helper,\nscan for secrets"),
    ("cli", "be", "8. upload evidence"),
    ("be", "be", "9. security scan,\nawaiting_review"),
    ("phase", "Human review"),
    ("rev", "be", "10. inspect evidence"),
    ("rev", "be", "11. approve"),
    ("phase", "Submission"),
    ("dev", "cli", "12. vaultshield submit"),
    ("cli", "be", "13. status is approved?"),
    ("cli", "git", "14. push branch, open PR"),
    ("git", "cli", "15. PR URL", "reply"),
    ("cli", "be", "16. record pr_url"),
]

# work out the height first
y = 72
rows = []
for m in msgs:
    if m[0] == "phase":
        y += 8
        rows.append((m, y))
        y += 22
        continue
    lines = m[2].count("\n") + 1
    if m[0] == m[1]:
        rows.append((m, y))
        y += 13 * lines + 10
    else:
        rows.append((m, y))
        y += ROW
H = y + 10
c = Canvas(W, H)

# participants and lifelines
for k, x in X.items():
    if k in ("dev", "rev"):
        c.actor(x, 6, "Developer" if k == "dev" else "Reviewer", 10.5)
    else:
        c.box(x - 36, 12, 72, 34, names[k], 10.5)
    c.line([(x, 66 if k in ("dev", "rev") else 46), (x, H - 6)], arrow=False,
           dashed=True, color="#888888", lw=1)

for m, y in rows:
    if m[0] == "phase":
        c.line([(6, y), (W - 6, y)], arrow=False, dashed=True, color="#aaaaaa", lw=0.8)
        c.text(8, y + 11, m[1], 10.5, anchor="start", weight="bold", bg=True)
        continue
    a, b, text = m[0], m[1], m[2]
    reply = len(m) > 3
    xa, xb = X[a], X[b]
    if a == b:
        n = text.count("\n") + 1
        top = y
        c.line([(xa, top), (xa + 22, top), (xa + 22, top + 12 * n + 6), (xa + 1, top + 12 * n + 6)])
        c.text(xa + 28, top + (12 * n + 6) / 2, text, FS, anchor="start", bg=True)
        continue
    ay = y + 12
    c.line([(xa, ay), (xb, ay)], dashed=reply, open_head=reply)
    mid = (xa + xb) / 2
    c.label(mid, ay - 8, text, FS)

c.save()
