"""Figure 3.8: use case diagram, hand placed at print size."""
from svgkit import Canvas

W, H = 450, 560
c = Canvas(W, H)
RX, RY = 47, 21
CL, CM, CR = 112, 228, 342          # column centres inside the system boundary
FS = 10.5

c.cluster(58, 14, 340, 458, "VaultShield")

def uc(x, y, label):
    c.ellipse(x, y, RX, RY, label, FS)
    return (x, y)

def dep(a, b, kind, lx=None, ly=None, side=None):
    """Dashed «include»/«extend» arrow from use case a to use case b."""
    (ax, ay), (bx, by) = a, b
    if ax == bx:                       # vertical
        s = 1 if by > ay else -1
        pts = [(ax, ay + s * RY), (bx, by - s * RY)]
    else:                              # horizontal-ish, left to right
        pts = [(ax + RX, ay), (bx - RX, by)]
    c.line(pts, dashed=True, open_head=True, lw=1.1)
    if lx is not None:
        c.label(lx, ly, f"\u00ab{kind}\u00bb", 9.5)

# ---- developer use cases (left column) ------------------------------------------
init   = uc(CL, 62,  "Initialise\nproject config")
egress = uc(CL, 132, "Enforce\negress policy")
run    = uc(CL, 214, "Execute\nsandboxed run")
shots  = uc(CL, 296, "Capture\nscreenshots")
submit = uc(CL, 372, "Submit\napproved run")
tear   = uc(CL, 438, "Tear down\nrejected run")

dep(egress, run, "extend", CL + 30, 173)
dep(shots, run, "extend", CL + 30, 255)

# ---- steps every run includes (middle column) ------------------------------------
creds  = uc(CM, 96,  "Generate dummy\ncredentials")
mon    = uc(CM, 160, "Monitor\nand log run")
cap    = uc(CM, 224, "Capture branch\nand diff")
up     = uc(CM, 288, "Upload run\nartifacts")
scan   = uc(CM, 368, "Scan diff for\nrisky patterns")
for t in (creds, mon, cap, up):
    c.line([(CL + RX, run[1]), (CM - RX, t[1])], dashed=True, open_head=True, lw=1.1)
c.label(CM, 46, "\u00abinclude\u00bb from\nExecute sandboxed run", 9.5)
dep(up, scan, "include", CM + 34, 328)

# ---- reviewer use cases (right column) -------------------------------------------
login   = uc(CR, 62,  "Log in,\nobtain token")
browse  = uc(CR, 128, "Browse projects\nand runs")
approve = uc(CR, 200, "Approve run")
inspect = uc(CR, 268, "Inspect run\nevidence")
reject  = uc(CR, 336, "Reject run")
reap    = uc(CR, 430, "Reap orphaned\nsandboxes")
dep(approve, inspect, "include", CR + 30, 234)
dep(reject, inspect, "include", CR + 30, 302)

# ---- actors -----------------------------------------------------------------------
for t in (init, run, submit, tear):
    c.line([(37, 178), (CL - RX, t[1])], arrow=False, lw=1.1)
c.actor(26, 160, "Developer", 10.5)

for t in (login, browse, approve, reject):
    c.line([(413, 188), (CR + RX, t[1])], arrow=False, lw=1.1)
c.actor(424, 170, "Reviewer", 10.5)

for t in (submit, tear):
    c.line([(41, 488), (CL - RX + 4, t[1] + 8)], arrow=False, lw=1.1)
c.actor(30, 470, "Repository\nhost", 10.5)

c.line([(290, 488), (CM + 10, scan[1] + RY)], arrow=False, lw=1.1)
c.line([(290, 488), (CR - 10, reap[1] + RY)], arrow=False, lw=1.1)
c.actor(290, 488, "System: Celery\nworker and beat", 10.5)

c.save()
