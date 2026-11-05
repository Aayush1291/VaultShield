"""Figure 3.5: overall system architecture, hand placed at print size."""
from svgkit import Canvas

W, H = 450, 600
c = Canvas(W, H)
BW = 98                        # module box width
C = [58, 168, 280, 392]        # column centres
L = [x - BW / 2 for x in C]    # column left edges

# ---- actors and external services at the top ---------------------------------
c.box(220, 8, 100, 28, "Developer", 12, fill="white", bold=True)
c.box(L[3] - 4, 8, BW + 8, 34, "GitHub or GitLab", 11, fill="white", dashed=True)

# ---- developer host ------------------------------------------------------------
c.cluster(4, 58, W - 8, 146, "Developer host: vaultshield CLI")
c.box(C[1] - 40, 80, 190, 26, "CLI pipeline: run, submit, teardown", 11.5)
c.line([(270, 36), (270, 80)])
mods = ["Run setup\nconfig, dummy\n.env, adapter", "Evidence\nmonitor\nPTY, watcher",
        "Trusted\ncapture\nbranch, diff", "Delivery\nupload, submit,\nteardown"]
MY, MH = 140, 52
c.line([(C[1] + 55, 106), (C[1] + 55, 122)], arrow=False)
c.line([(C[0], 122), (C[3] - 15, 122)], arrow=False)
for i, m in enumerate(mods):
    c.box(L[i], MY, BW, MH, m, 11.5)
    x = C[i] - 15 if i == 3 else C[i]
    c.line([(x, 122), (x, MY)])

# delivery pushes the approved branch and opens the PR
c.line([(C[3] + 30, MY), (C[3] + 30, 42)])
c.label(C[3] + 30, 64, "push,\nopen PR", 10)

# ---- docker execution plane --------------------------------------------------
DY = 222
c.cluster(4, DY, 336, 142, "Docker execution plane,\none per run", where="bottom-right")
sbx = c.box(L[0], DY + 26, C[1] + BW / 2 - L[0], 40, "Sandbox container: runs the agent", 11.5)
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
RY = 392
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
c.label(C[3] + 8, 300, "HTTPS\nwith token", 10)

# ---- bottom row: model APIs and reviewer ------------------------------------
c.box(C[0] - 56, 560, 112, 32, "Agent model APIs", 11, fill="white", dashed=True)
c.line([(C[0], DY + 130), (C[0], 560)])
c.label(C[0] - 4, 470, "allowlisted\nhosts", 10)
c.box(C[1] - 45, 560, 90, 30, "Reviewer", 12, fill="white", bold=True)
c.line([(C[1], 560), (C[1], RY + 68)])

c.save()
