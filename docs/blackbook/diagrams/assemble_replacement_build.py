"""10-page replacement build: exact printed baseline + edits confined to
printed pages 7, 11, 13, 14, 37, 38, 41 and 42 (PDF 19, 23, 25, 26, 49, 50, 53, 54).
Captions of existing figures and all headings listed in the printed Contents,
List of Figures and List of Tables are left unchanged."""
import re, sys

s = open("/tmp/vs-baseline.tex").read()
GAP = open("/tmp/blk-gap.tex").read()


def r(old, new, cnt=1):
    global s
    assert s.count(old) == cnt, (s.count(old), old[:90])
    s = s.replace(old, new)


def between(t, a, b):
    i = t.index(a); j = t.index(b, i + len(a)); return t[i:j]


# ---- printed page 7: approved Research Gap. The printed bibliography has no
# entries 16-21, so the vendor sources go in a footnote on this page instead.
gap = GAP
for k in range(16, 22):
    gap = gap.replace(" \\cite{ref%d}" % k, "")
gap = gap.replace(
    "Agent vendors have since shipped security features of their own:",
    "Agent vendors have since shipped security features of their own\\footnote{Official documentation, accessed 1 November 2026: Claude Code, code.claude.com/docs/en/sandboxing; OpenAI Codex CLI, developers.openai.com/codex/sandbox; GitHub Copilot cloud agent, docs.github.com, ``Responsible use of GitHub Copilot cloud agent''; Docker Sandboxes, docs.docker.com/ai/sandboxes; OpenHands, docs.all-hands.dev.}:")
for o, n in [
    ("Official documentation, accessed 1 November 2026: Claude Code, code.claude.com/docs/en/sandboxing; OpenAI Codex CLI, developers.openai.com/codex/sandbox; GitHub Copilot cloud agent, docs.github.com, ``Responsible use of GitHub Copilot cloud agent''; Docker Sandboxes, docs.docker.com/ai/sandboxes; OpenHands, docs.all-hands.dev.",
     "Vendor documentation, accessed 1 Nov 2026: \\url{code.claude.com/docs/en/sandboxing}; \\url{developers.openai.com/codex/sandbox}; \\url{docs.github.com} (Copilot cloud agent); \\url{docs.docker.com/ai/sandboxes}; \\url{docs.all-hands.dev}."),
    (", and that governance research recommends human approval, legible logs and monitoring \\cite{ref9}, \\cite{ref10}.", ", and that governance research recommends human approval and legible logs \\cite{ref9}, \\cite{ref10}."),
    ("First, containment is tied to a vendor: each sandbox protects only its own agent, so a team using several agents has no single policy or evidence format.", "First, each sandbox protects only its own vendor's agent, with no common policy or evidence format."),
    ("Third, any activity record is produced beside the agent, so a hostile agent that tampers with Git state could misreport its change.", "Third, any activity record is produced beside the agent, which could tamper with Git state to misreport its change."),
    ("Fourth, approval is either per action or comes after the change has already been pushed to the repository host.", "Fourth, approval is per action or comes after the change is pushed."),
    ("dummy credentials and a one-shot clone token keep real secrets away, a separate trusted capture helper builds the change record, and an approval state machine holds the whole change before anything is pushed.", "dummy credentials and a one-shot clone token keep real secrets away, a separate capture helper builds the change record, and an approval gate holds the change until it is approved."),
    ("VaultShield's contribution is to close the four gaps together for any agent on the developer's own machine:", "VaultShield closes the four gaps together for any agent on the developer's machine:"),
    (" Existing products are ahead of VaultShield on isolation strength and default-deny networking.", " Existing products lead on isolation strength and default-deny networking."),
    ("Second, the project's own .env sits inside the workspace that local sandboxes allow,", "Second, the project's .env sits inside the workspace that local sandboxes allow,"),
]:
    assert gap.count(o) == 1, o[:60]
    gap = gap.replace(o, n)
assert "\\cite{ref1" not in re.sub(r"\\cite\{ref1[0-5]?\}", "", gap)
r(between(s, "\\section{Research Gap}", "\\clearpage\n\n\\chapter{Project Design}"), gap)

gap = gap.replace("\\captionof{table}{Comparison with existing AI coding-agent security solutions}", "\\captionof{table}[]{Comparison with existing AI coding-agent security solutions}")
assert "captionof{table}[]" in gap
r(between(s, "\\section{Research Gap}", "\\clearpage\n\n\\chapter{Project Design}"), gap)

# ---- printed page 13: 3.3.7, excluded from the Contents ----------------------
anchor = "after confirming approval with the backend.\n"
r(anchor, anchor + r"""
\subsection*{3.3.7\hspace{0.8em}Proposed Agentic AI Orchestration}

The Agentic AI Orchestrator of Objective 2 is planned for Semester VIII and has not been implemented in Semester VII; in the evaluated system the developer passes a literal prompt to vaultshield run. Its purpose is to replace a vague one-line prompt, which forces an agent to guess the framework, the files to change and the constraints, with a structured prompt, so that fewer changes are rejected at review and fewer files, APIs or packages are hallucinated. As the first row of Figure 3.2 and the context orchestrator in Figure 3.5 show, it will run on the host before any sandbox is created. A context scanner will collect a bounded, non-secret project summary: the directory listing to three levels, the start command and port detected by vaultshield init, dependency names, the start of the README and recent commit subjects. A sanitiser will replace protected values and secret-shaped strings with *** and block the request if anything secret-like remains; .env and key files are never opened. A language model will then write three to five clarifying questions in a strict JSON format, the developer will answer them at the terminal or from a file, and a prompt builder will combine the task, the project summary, the answers, a fixed Constraints section that the model cannot change, and a suggested approach. A final secret check will run before the prompt is saved as prompt.md in the run directory and passed unchanged into the existing sandboxed pipeline, where the reviewer sees it beside the diff and timeline. Because the Orchestrator sends project context to an external model and could be steered by hostile README text, it will gain no privileges: the agent still receives only dummy credentials, its change still passes trusted capture and human approval, and pattern-based sanitisation remains a stated residual risk. Its expected benefit, higher task success and approval rates with fewer hallucinated changes, will be measured on real agents in Semester VIII.
""")

# ---- printed pages 37-38: implementation status and genuine execution evidence
for f in ("detail-security-1280-light.png", "detail-approved-1280-light.png"):
    r("\\fbox{\\SafeIncludeGraphics[width=0.8\\textwidth,keepaspectratio]{images/screenshots/%s}}" % f,
      "\\fbox{\\SafeIncludeGraphics[width=0.64\\textwidth,keepaspectratio]{images/screenshots/%s}}" % f)
r("Producing a change and accepting it therefore remain separate steps.",
  r"""Producing a change and accepting it therefore remain separate steps. Everything in this chapter was implemented in Semester VII: the CLI and adapters, the Docker sandbox with a fresh clone, dummy and one-shot credentials, egress control, monitoring with the event log and off-limits paths, trusted capture and the structured diff, the MongoDB backend and dashboard, security scanning, and approval-gated submission. The Agentic AI Orchestrator, stronger sandbox isolation, improved monitoring and role-based approval are planned for Semester VIII.

Two further captures are genuine executions. Capture (i) shows submit refusing a run that was still awaiting review, and no sandbox, helper or proxy left after the run. Capture (ii) is from the isolation probe of 9 October 2026, run against a sandbox in which the Antigravity agent had been signed in: the agent's credential files are inside the sandbox, direct network and Docker-socket paths fail, and the proxy records each host the agent contacted. No Antigravity task response was preserved, so none is shown.

\begin{figure}[H]
\centering
\SafeIncludeGraphics[width=0.86\textwidth,keepaspectratio]{images/evidence/c-ev-2-submit-gate-cleanup.png}
\caption*{Execution capture (i): approval gate and cleanup check after a mock-agent run, 1 November 2026}
\end{figure}

\begin{figure}[H]
\centering
\SafeIncludeGraphics[width=0.86\textwidth,keepaspectratio]{images/evidence/c-ev-12-antigravity-sandbox.png}
\caption*{Execution capture (ii): probe and egress-proxy records from a sandbox with a signed-in Antigravity agent}
\end{figure}""")

# ---- printed page 41: Sample Test Cases table replaces Table 6.3 -------------
issues = between(s, "Table 6.3 lists the main issues found during development and testing.", "\\section{Sample Test Cases}")
sample_sec = between(s, "\\section{Sample Test Cases}", "\\section{Conclusion on Testing}")
tbl = sample_sec[sample_sec.index("\\begingroup\\small"):]
tbl = tbl.replace("\\begingroup\\small", "\\begingroup\\footnotesize\\setlength{\\extrarowheight}{0pt}\\renewcommand{\\arraystretch}{1.05}\\setcounter{table}{3}", 1)
tbl = tbl.replace("\\real{0.1000}} |\n  >{\\raggedright\\arraybackslash}p{(\\columnwidth - 9\\tabcolsep) * \\real{0.3000}} |\n  >{\\raggedright\\arraybackslash}p{(\\columnwidth - 9\\tabcolsep) * \\real{0.4600}} |\n  >{\\raggedright\\arraybackslash}p{(\\columnwidth - 9\\tabcolsep) * \\real{0.1400}}",
                  "\\real{0.0900}} |\n  >{\\raggedright\\arraybackslash}p{(\\columnwidth - 9\\tabcolsep) * \\real{0.2200}} |\n  >{\\raggedright\\arraybackslash}p{(\\columnwidth - 9\\tabcolsep) * \\real{0.6000}} |\n  >{\\raggedright\\arraybackslash}p{(\\columnwidth - 9\\tabcolsep) * \\real{0.0900}}")
assert "real{0.6000}" in tbl
s = s.replace(issues, "Table 6.4 presents representative test cases, each corresponding to an automated test or to the benchmark harness.\n\n" + tbl.rstrip() + "\n\n")

# ---- printed page 42: the two failed tests, then the conclusion --------------
r(sample_sec, r"""\section{Sample Test Cases}\label{sample-test-cases}

Two of the 2,704 tests failed. Both are Docker-backed tests of the optional interactive sign-in transport, used only when the antigravity agent runs with -\allowbreak{}-interactive-auth; the first is TC-10 in Table 6.4.

\textbf{Failure 1: interactive sign-in timeout.} The hanging sign-in test expects the CLI to give up within 60 s of a silent sign-in, but it took 600.33 s. The deadline is a socket timeout on the Docker exec stream, and on Docker Desktop 29.2.1 it does not interrupt the blocking read. This is a genuine availability defect in the optional authentication flow; it exposes no secret. The correction is an independent watchdog deadline that kills the exec.

\begin{figure}[H]
\centering
\SafeIncludeGraphics[width=0.82\textwidth,keepaspectratio]{images/evidence/c-ev-10-failure1.png}
\caption*{Failure 1: hanging sign-in test, excerpt of the pytest output (1 November 2026)}
\end{figure}

\textbf{Failure 2: redacted echo expectation.} The stdin test expects the raw sentinel value in the terminal output, which shows RECEIVED:[***]. Redaction replaces only an exact match of the sent value, so the output proves that the value was delivered and then masked as designed. The failure is an outdated assertion, not failed credential delivery; the assertion must expect the redacted output.

\begin{figure}[H]
\centering
\SafeIncludeGraphics[width=0.82\textwidth,keepaspectratio]{images/evidence/c-ev-11-failure2.png}
\caption*{Failure 2: stdin delivery test, excerpt of the pytest output (1 November 2026)}
\end{figure}

\textbf{Reliability impact.} The two failures represent about 0.07\% of the 2,704-test campaign. One is a genuine availability defect in an optional authentication flow; the other is an outdated test expectation. Neither affects the default non-interactive workflow, and the security-critical isolation, credential substitution, egress, trusted capture, token-hygiene and approval tests passed.

""")
concl_old = between(s, "\\section{Conclusion on Testing}", "\\clearpage\n\n\\chapter{Result and Discussions}")
r(concl_old, "\\section{Conclusion on Testing}\\label{conclusion-on-testing}\n\nOf 2,704 automated tests, 2,702 passed, and every security-critical behaviour was exercised against real Docker and MongoDB. The remaining gaps, the sign-in deadline and the file watcher's missed events, affect availability and supplementary evidence, not isolation or change capture.\n\n")

open(sys.argv[1], "w").write(s)
print("ok")
