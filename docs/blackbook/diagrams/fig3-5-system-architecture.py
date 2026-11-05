"""Figure 3.5: overall system architecture, hand placed at print size.
The proposed context orchestrator (Objective 2, Semester VIII) is drawn with
an asterisk, consistent with Figure 3.2; everything else is the Semester VII system.
"""
from svgkit import Canvas

S = 52                         # vertical shift below the CLI pipeline box
W, H = 450, 600 + S + 46
c = Canvas(W, H)
RED = "#c62828"


def mark(x, y, t):
    c.parts.append(f'<rect x="{x - 10}" y="{y - 7}" width="20" height="14" rx="4" fill="{RED}"/>')
    c.parts.append(f'<text x="{x}" y="{y + 3.5}" font-family="Helvetica, Arial, sans-serif" font-size="9" '
                   f'font-weight="bold" text-anchor="middle" fill="white">{t}</text>')
BW = 98                        # module box width
C = [58, 168, 280, 392]        # column centres
L = [x - BW / 2 for x in C]    # column left edges

# ---- actors and external services at the top ---------------------------------
c.box(220, 8, 100, 28, "Developer", 12, fill="white", bold=True)
c.box(L[3] - 4, 8, BW + 8, 34, "GitHub or GitLab", 11, fill="white", dashed=True)

# ---- developer host ------------------------------------------------------------
c.cluster(4, 58, W - 8, 146 + S, "Developer host: vaultshield CLI")
# proposed context orchestrator, between the developer and the CLI pipeline
c.box(128, 78, 140, 40, "Context orchestrator*\nscan, sanitise, clarify,\nenrich prompt", 9.5)
c.line([(245, 36), (245, 78)])
c.label(222, 56, "task", 9.5)
c.line([(198, 118), (198, 132)])
c.line([(300, 36), (300, 132)])
c.label(300, 104, "--prompt", 9.5)
c.box(C[1] - 40, 132, 190, 26, "CLI pipeline: run, submit, teardown", 11.5)
mods = ["Run setup\nconfig, dummy\n.env, adapter", "Evidence\nmonitor\nPTY, watcher",
        "Trusted\ncapture\nbranch, diff", "Delivery\nupload, submit,\nteardown"]
MY, MH = 140 + S, 52
c.line([(C[1] + 55, 106 + S), (C[1] + 55, 122 + S)], arrow=False)
c.line([(C[0], 122 + S), (C[3] - 15, 122 + S)], arrow=False)
for i, m in enumerate(mods):
    c.box(L[i], MY, BW, MH, m, 11.5)
    x = C[3] - 15 if i == 3 else C[i]
    c.line([(x, 122 + S), (x, MY)])
# delivery pushes the approved branch and opens the PR
c.line([(C[3] + 30, MY), (C[3] + 30, 42)])
c.label(C[3] + 30, 64, "push,\nopen PR", 10)

# ---- docker execution plane --------------------------------------------------
DY = 222 + S
c.cluster(4, DY, 336, 142, "Docker execution plane,\none per run", where="bottom-right")
sbx = c.box(L[0], DY + 26, C[1] + BW / 2 - L[0], 40, "Sandbox container: runs the agent\n(untrusted)", 11.5, fill="#fde8e8")
c.parts.append(f'<rect x="{L[0] - 5}" y="{DY + 21}" width="{C[1] + BW / 2 - L[0] + 10}" height="50" rx="9" fill="none" stroke="{RED}" stroke-width="1.6" stroke-dasharray="6,3"/>')
c.box(L[2], DY + 26, BW, 40, "Capture helper\nno network", 11.5)
c.box(L[0], DY + 90, BW + 10, 40, "Egress proxy\nallowlist check", 11.5)
c.line([(C[0], MY + MH), (C[0], DY + 26)])
c.label(C[0], DY - 6, "create", 10)
c.line([(C[1], MY + MH), (C[1], DY + 26)], both=True)
c.label(C[1], DY - 6, "exec, events", 10)
c.line([(C[2], MY + MH), (C[2], DY + 26)])
c.label(C[2], DY - 6, "work tree", 10)
c.line([(C[0], DY + 66), (C[0], DY + 90)])
c.label(C[0] + 34, DY + 78, "all traffic", 10)

# ---- review plane ---------------------------------------------------------------
RY = 392 + S
c.cluster(L[1] - 8, RY, W - 4 - (L[1] - 8), 150, "Review plane:\nDocker Compose", where="bottom-right")
c.box(L[1], RY + 28, BW, 40, "React\ndashboard", 11.5)
c.box(L[2], RY + 28, BW, 40, "Django\nREST API", 11.5)
c.box(L[3], RY + 22, BW, 52, "Celery, Redis\nsecurity scan,\nreaper", 11.5)
c.cylinder(L[2], RY + 92, BW, 46, "MongoDB\nreplica set", 11.5)
c.line([(L[1] + BW, RY + 48), (L[2], RY + 48)])
c.line([(L[2] + BW, RY + 48), (L[3], RY + 48)])
c.line([(C[2], RY + 68), (C[2], RY + 92)])
c.line([(C[3], RY + 74), (C[3], RY + 115), (L[2] + BW, RY + 115)])
# delivery uploads evidence to the API
c.line([(C[3], MY + MH), (C[3], RY - 12), (C[2], RY - 12), (C[2], RY + 28)])
c.label(C[3] + 8, 300 + S, "HTTPS\nwith token", 10)

# ---- bottom row: model APIs and reviewer ------------------------------------
c.box(C[0] - 56, 560 + S, 112, 32, "Agent model APIs", 11, fill="white", dashed=True)
c.line([(C[0], DY + 130), (C[0], 560 + S)])
c.label(C[0] - 4, 470 + S, "allowlisted\nhosts", 10)
c.box(C[1] - 45, 560 + S, 90, 30, "Reviewer", 12, fill="white", bold=True)
c.line([(C[1], 560 + S), (C[1], RY + 68)])
# ---- attack paths (threat model, Section 3.3) ------------------------------
mark(C[1] + BW / 2 - 16, DY + 57, "T1")
mark(C[0] - 22, DY + 12, "T2")
mark(C[0] - 18, DY + 78, "T3")
mark(C[2] + 22, DY + 12, "T4")
c.line([(C[1] + 30, DY + 21), (C[1] + 30, MY + MH)], dashed=True, color=RED, lw=1.3)
mark(C[1] + 30, DY + 13, "T5")
mark(C[2] + 26, RY + 80, "T6")
# ---- legend ------------------------------------------------------------------
LY = 600 + S + 2
c.parts.append(f'<rect x="4" y="{LY}" width="{W - 8}" height="42" rx="6" fill="white" stroke="#999999" stroke-width="0.8"/>')
c.parts.append(f'<line x1="12" y1="{LY + 10}" x2="28" y2="{LY + 10}" stroke="{RED}" stroke-width="1.6" stroke-dasharray="5,2"/>')
c.text(32, LY + 10, "trust boundary: the agent and its sandbox are untrusted; all other components are trusted", 9, anchor="start")
c.text(12, LY + 22, "Attack paths: T1 prompt injection, T2 secret theft, T3 network exfiltration, T4 Git tampering,", 9, anchor="start")
c.text(12, LY + 34, "T5 container escape, T6 approval bypass.   * Proposed (Objective 2), Semester VIII; not implemented.", 9, anchor="start")
c.save()
