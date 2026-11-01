# VaultShield: Sandboxed, Monitored Execution and Human Review for AI Coding Agents

## Title

- **Project:** VaultShield: Sandboxed, Monitored Execution and Human Review for AI Coding Agents
- **Degree:** Bachelor of Engineering in Computer Science and Engineering (Data Science), University of Mumbai
- **Institute:** A.P. Shah Institute of Technology, Thane
- **Academic Year:** 2026–2027
- **Team:**
  - Aayush Subhash Balip (24207010)
  - Krish Mahesh Patil (24207006)
  - Muhammad Anis Momin (24207008)
  - Sameer Hasan Saiyed (24207014)
- **Guides:** Prof. Rajashri Chaudhari, Prof. Hardiki Achrekar

---

## Introduction

- AI coding agents such as OpenAI Codex CLI, Google Antigravity, Amazon Kiro and Claude Code now read whole repositories, edit many files and run shell commands with little human intervention.
- Research agents such as SWE-agent and OpenHands already resolve a meaningful share of real GitHub issues on the SWE-bench benchmark.
- An agent runs with the privileges of the developer who launched it. It can read every secret in the shell and in .env files, write anywhere, contact any host and leave unreviewed changes in version control.
- VaultShield runs the agent inside a disposable Docker container, records what it does, captures its change on a dedicated Git branch and holds that change behind an explicit human approval step before it can become a pull or merge request.
- Running an agent and trusting its output become two separate steps.

---

## Problem Statement

Given a Git repository, a natural-language prompt and an AI coding agent, let the agent attempt the change while guaranteeing that:

- it never runs against the developer's real working tree, real application secrets or repository-host credentials;
- everything it does is recorded as reviewable evidence;
- its change is captured in a form the agent could not have tampered with;
- no change reaches a shared branch or pull request without an explicit human approval decision.

The agent must still be able to work: it needs a real repository, a working toolchain and configuration values that keep their structural shape. Every container, network and temporary branch must be removed on every exit path, and the overhead must stay small compared with typical agent run times.

---

## Motivation

- **Cost asymmetry:** an agent that deletes the wrong directory or commits a hard-coded key can cost hours of recovery, or far more if credentials leak. Running the same agent in a throwaway container costs about 1.2 seconds per run.
- **Credential exposure:** in a feasibility spike, a production coding agent asked to reveal values from .env read the file and printed both test secrets. Agent deny rules reduced the exposure but did not remove it.
- **Reviewability:** code review is the main quality-control step in software engineering, yet agent output usually lands directly in the working tree and skips the pull-request workflow.
- **Honest security engineering:** containers share the host kernel, so VaultShield states which controls it implements and which risks remain.

---

## Objectives

1. Develop a universal AI agent execution platform compatible with multiple AI coding agents and development environments through a common CLI interface.
2. Implement an intelligent Agentic AI Orchestrator that understands project context, asks clarification questions before execution, and provides enriched context to AI agents to improve accuracy and reduce hallucinations.
3. Ensure secure AI-assisted software development by executing AI agents inside isolated Docker-based sandbox environments while protecting production credentials and resources.
4. Provide comprehensive monitoring and automated security analysis by capturing AI activities, execution logs, code changes, and identifying security vulnerabilities or unsafe operations.
5. Enable transparent governance and human approval through a web-based dashboard that allows developers to review, verify, approve, or reject AI-generated changes before deployment.

---

## SDG

| SDG | How VaultShield contributes |
|---|---|
| SDG 9: Industry, Innovation and Infrastructure | Makes AI-assisted software development safer, so teams can adopt coding agents without exposing their infrastructure, credentials or codebases |
| SDG 8: Decent Work and Economic Growth | Lets developers delegate routine changes to agents while keeping control, which raises productivity without shifting risk onto the developer |
| SDG 16: Peace, Justice and Strong Institutions | Puts accountability, transparency and human oversight into AI-driven work through audit logs, attributable runs and an enforced approval step |

---

## Literature Survey

| Ref. | Work | Approach | Limitation relative to VaultShield |
|---|---|---|---|
| [1] | Chen et al., 2021 | Codex model evaluated by running generated programs in a sandbox | Internal harness for short functions; no repository workflow, credential model or review |
| [2] | Jimenez et al., 2024 | SWE-bench: 2,294 tasks from real GitHub issues | Measures capability; patches are applied for scoring, not held for approval |
| [3] | Yang et al., 2024 | SWE-agent with a purpose-built agent-computer interface in Docker | Container is a workspace, not a trust boundary; no secret substitution, egress control or review gate |
| [4] | Wang et al., 2025 | OpenHands platform with an event stream and sandboxed runtime | Changes are not captured on a reviewable branch behind an approval state machine |
| [5] | Ruan et al., 2024 | ToolEmu: LM-emulated sandbox for red-teaming agents; risky failures in 23.9% of cases | Emulation cannot contain a real agent acting on a real repository |
| [6] | Debenedetti et al., 2024 | AgentDojo benchmark for prompt-injection attacks and defences | Evaluates attacks; offers no containment or audit pipeline |
| [7] | Greshake et al., 2023 | Indirect prompt injection through retrieved data | Characterises the threat; mitigation is left open |
| [8] | Pearce et al., 2022 | Copilot study: about 40% of 1,689 programs vulnerable | Studies suggestions, not agents that also execute commands |
| [9] | Shavit et al., 2023 | Governance practices for agentic AI | Technology-neutral; no implementation for coding agents |
| [10] | Chan et al., 2024 | Visibility measures: agent identifiers, monitoring, activity logs | Defines what to log, not how to capture tamper-resistant evidence |
| [11] | Combe et al., 2016 | Security analysis of Docker | Does not address agent credentials or review |
| [12] | Agache et al., 2020 | Firecracker microVM isolation | Requires KVM; not integrated with a review workflow |
| [13] | Young et al., 2019 | Performance cost of gVisor | Quantifies isolation cost; is not an agent pipeline |
| [14] | Bacchelli and Bird, 2013 | Empirical study of modern code review | Concerns human changes; agent changes need behavioural context |

---

## Existing System

Developers currently run AI coding agents in one of three ways:

- **Raw local execution:** the agent runs in the developer's own checkout with the developer's environment, permissions and network access. Some products add permission prompts or deny rules, but these are product-specific and often relaxed by users.
- **Ad-hoc containerisation:** usually bind-mounts the real working tree, so every write still lands on the host and the real .env travels into the container.
- **Dev containers and cloud workspaces:** built for convenience, not for constraining an untrusted actor, and hold nothing for review.

Shortcomings:

- execution and acceptance are a single step;
- no separation between the agent and the developer's secrets;
- no structured record of what happened or which hosts were contacted;
- no enforced review before changes are committed;
- no automatic cleanup of processes the agent started.

---

## Research Gap

- Coding agents can make repository-scale changes [2], [3], [4].
- Their code and actions are measurably risky [5], [6], [7], [8].
- Container security depends on configuration [11], [12], [13].
- Governance research recommends human approval, legible logs and monitoring [9], [10].

No existing developer-facing pipeline combines these findings. None:

- substitutes dummy values for the developer's secrets;
- keeps the repository-host token out of the agent's reach;
- builds the diff in a component the agent cannot tamper with;
- holds the change behind an approval state machine before a pull request is opened.

---

## Proposed System

VaultShield splits execution, evidence collection, storage and review into separate stages:

1. The developer runs `vaultshield run` with an agent, a prompt and a repository URL.
2. The CLI registers the run with the backend and writes a dummy credential file that mirrors the shape of the real .env.
3. A Docker container is created with only the dummy credential file mounted. The repository is cloned inside the container and dependencies are installed.
4. Optional egress control moves the sandbox onto an internal network whose only peer is a filtering proxy.
5. The pre-agent commit is recorded as a trusted base. The agent runs under a pseudo-terminal while an in-container watcher records file changes into a timestamped event log. Configured routes are screenshotted with a headless browser.
6. The sandbox is killed. A separate hardened capture helper commits the change to a dedicated run branch and computes a structured diff.
7. Outputs are scanned for leaked credentials, the sandbox is torn down and the artifacts are uploaded. The run moves to awaiting review.
8. A reviewer approves or rejects the run in the dashboard. Only an approved run can be submitted as a pull or merge request.

| Concern | Existing practice | VaultShield |
|---|---|---|
| Working tree | Real checkout, modified in place | Fresh clone inside the container; host tree never mounted |
| Secrets | Real .env readable by the agent | Shape-preserving dummy values in a read-only .env.sandbox |
| Repository token | Often visible to the agent | Given only to the single git clone process |
| Network | Unrestricted, unlogged | Optional allowlist through a hardened proxy, every decision logged |
| Evidence | Terminal scroll-back | Event log, egress log, screenshots and structured diff |
| Change capture | Uncommitted edits | One commit on a run branch built by a separate helper |
| Review | Optional and informal | Enforced state machine; PR only from approved |
| Cleanup | Manual | Guaranteed teardown and a scheduled reaper |

---

## Requirement Analysis

### Functional Requirements

- Register a run and generate a unique run identifier before any container exists.
- Generate a dummy credential file that keeps the shape of every real .env value.
- Create one Docker sandbox per run with a fresh clone and no host working tree.
- Run any supported agent (mock, codex, kiro, antigravity) through a common adapter interface.
- Record terminal output, file changes and violations in a JSON Lines event log.
- Enforce off-limits paths and an optional egress allowlist.
- Capture screenshots of configured application routes.
- Capture the change as one commit on a dedicated run branch, with a structured diff.
- Scan outputs for leaked agent credentials and quarantine the diff on a hit.
- Upload evidence to the backend and run a static security scan on added lines.
- Let a reviewer approve or reject a run in the dashboard.
- Open a pull or merge request on GitHub or GitLab only for approved runs, and delete the branch of rejected runs.

### Non-Functional Requirements

- **Security:** real secrets and repository tokens never enter the sandbox or the artifacts; the Docker socket is never mounted.
- **Reliability:** teardown runs on success, failure, timeout and interruption; no containers or networks are left behind.
- **Integrity:** the diff is built where the agent cannot tamper with it; concurrent decisions produce exactly one winner.
- **Performance:** fixed overhead of a few seconds per run, small compared with agent sessions of several minutes.
- **Usability and accessibility:** text contrast of at least 4.5:1 and a responsive layout with no horizontal overflow.

### Hardware and Software Requirements

| Category | Requirement |
|---|---|
| Hardware | Laptop with 8 GB RAM or more, able to run Docker (evaluation used an Apple MacBook Air M4) |
| Operating system | macOS or Linux |
| Runtime | Python 3.11 or later, Node.js 18 or later, Git, Docker daemon |
| Database | MongoDB replica set or Atlas cluster, Redis |
| Browser | Playwright Chromium for screenshots |

---

## Methodology: Algorithms and Models

### Development Model

- Iterative and incremental development based on specification-driven development.
- Each requirement carried numbered acceptance criteria. The design defined the architecture, data model and numbered correctness properties.
- Each iteration delivered a slice of tasks with unit tests, property-based tests and Docker-backed integration tests.
- Phases:
  1. problem study and feasibility spike;
  2. requirements and design;
  3. CLI core and evidence capture;
  4. backend and dashboard;
  5. submission and hardening;
  6. MongoDB migration and the Protect module;
  7. evaluation and documentation.

### Core Algorithms

- **Shape-preserving credential substitution:** each real .env value is replaced with a dummy of the same structure, so the application can still start. In a connection string, for example, the host is replaced unless it is provably local.
- **One-shot clone credential:** the repository token is passed only to the single git clone process. It never appears in the container environment, the Git configuration or any later process, and it is sent only to hosts on a strict per-provider allowlist.
- **Event logging with off-limits enforcement:** every terminal line, file change and violation is written as one JSON line. A write to an off-limits path fails the run only after the log is written, so evidence always outlives the failure.
- **Egress allow/deny decision:** a hardened proxy checks every destination host against the allowlist and logs each decision. The sandbox sits on an internal network with no gateway.
- **Trusted capture:** the pre-agent commit is read through the Docker API before the agent runs. After the run, a helper container with no network, capabilities or environment rebuilds the repository from that base and commits the change to the run branch.
- **Token hygiene scan:** the diff, event log and changed files are scanned for the agent's own credential bytes. A hit fails the run, quarantines the diff and scrubs uploaded payloads.
- **Approval state machine:** the run moves from running to awaiting review or failed, and then from awaiting review to approved or rejected. The status check and status write are one conditional update inside a MongoDB transaction, so of two concurrent decisions exactly one succeeds and the other receives HTTP 409.

### Models

- **AI agents:** VaultShield does not train its own model. It runs external LLM coding agents (Codex, Kiro, Antigravity) through adapters, plus a deterministic mock agent for testing and benchmarking.
- **Basic COCOMO (organic):**
  - inputs: KLOC = 18.07, a = 2.4, b = 1.05, c = 2.5, d = 0.38;
  - effort E = 2.4 × 18.07^1.05 = 50.12 person-months;
  - time T = 2.5 × 50.12^0.38 = 11.06 months;
  - staffing P = 4.53 persons.
- **Diagrams:** UML class, activity, use case and sequence diagrams, the run state machine, the egress topology and the system architecture.

---

## Technology Stack

| Layer | Technology |
|---|---|
| CLI | Python 3.11+, Click, PyYAML, Docker SDK for Python, watchdog, httpx, Playwright, keyring |
| Execution plane | Docker containers, per-run internal networks, hardened egress proxy, capture helper container |
| Backend | Django 5.2, Django REST Framework, django-mongodb-backend, Pillow |
| Database | MongoDB replica set (transactions) |
| Async tasks | Celery with Redis (security scan, sandbox reaper, unsubmitted-run checks) |
| Frontend | React 19, React Router 7, Vite 7, Tailwind CSS 4 |
| Testing | pytest, Hypothesis, Jest, React Testing Library, fast-check |
| Deployment | Docker Compose |
| Integrations | GitHub and GitLab (pull and merge requests), macOS Keychain (Protect module) |

---

## Ethics and Social Impact

### Ethical Considerations

- **Human oversight:** no AI-generated change reaches a shared branch without an explicit human decision, so responsibility stays with a person.
- **Privacy and data protection:** real secrets never reach the agent or the stored artifacts, and leaked credentials are scrubbed before upload.
- **Accountability:** every run has an identifier that links its sandbox, branch, event log, evidence and decision, so actions can be attributed and audited.
- **Transparency about limits:** the report lists residual risks openly, such as container escape, egress being off by default and the single-developer trust model, instead of overstating safety.
- **Responsible testing:** all experiments used fake secrets, test repositories and a mock agent; no real user data was used.

### Social Impact

- Lowers the risk of adopting AI coding agents for students, startups and small teams that cannot afford dedicated security tooling.
- Reduces the chance of credential leaks and supply-chain incidents caused by agent mistakes or prompt injection.
- Keeps developers in control of AI output, supporting trust in AI-assisted work rather than blind automation.
- Built entirely on open-source tools at zero cash cost, so others can reproduce and extend it.

---

## Project Planning

### Team Roles

| Member | Role | Responsibilities |
|---|---|---|
| Aayush Subhash Balip | Backend and data engineering | Django REST API, MongoDB models, run state machine, artifact ingest, Celery tasks, Docker Compose stack |
| Krish Mahesh Patil | Frontend and user experience | React dashboard, run detail tabs, decision controls, accessibility and responsive layout |
| Muhammad Anis Momin | Security research and sandbox engineering | Sandbox lifecycle, credential shaping, egress proxy, trusted capture, token hygiene, threat model |
| Sameer Hasan Saiyed | Integration, testing and documentation | CLI pipeline, agent adapters, submit and teardown, test strategy, benchmark and report |

### Semester VII Schedule

| Sr. | Activity | Duration | Period |
|---|---|---|---|
| 1 | Group formation, topic, scope and objectives | 1 week | 10 Jul – 17 Jul 2026 |
| 2 | Topic discussion with a paper prototype | 1 week | 17 Jul – 24 Jul 2026 |
| 3 | Identifying functionalities per objective | 1 week | 24 Jul – 31 Jul 2026 |
| 4 | Literature review and research paper study | 1 week | 31 Jul – 7 Aug 2026 |
| 5 | Designing the graphical user interface | 3 weeks | 7 Aug – 28 Aug 2026 |
| 6 | Presentation I | 1 day | 4 Sep 2026 |
| 7 | Policy logic development | 1 week | 18 Sep – 25 Sep 2026 |
| 8 | Core model development and training | 1 week | 25 Sep – 1 Oct 2026 |
| 9 | Presentation II | 1 day | 9 Oct 2026 |
| 10 | Incorporating Presentation II suggestions, draft to guide | 1 week | 16 Oct – 23 Oct 2026 |
| 11 | Draft submission to the guide for review | 2 days | 23 Oct – 24 Oct 2026 |
| 12 | Final black book submission | 1 week | 26 Oct – 30 Oct 2026 |
| 13 | External presentation and viva voce | 1 day | 2 Nov 2026 |

### Cost

- Actual cash expenditure: ₹0. The project used existing laptops, open-source software, a local MongoDB replica set and free-plan repository hosting.
- COCOMO estimate: about 4.5 persons over about 11 months. This matches the actual four-member team working across two semesters.

### Key Risks and Mitigation

| Risk | Mitigation |
|---|---|
| Container escape | Docker socket never mounted; only dummy credentials mounted; gVisor or microVMs planned |
| Malicious dependency install hooks | Installs only inside the disposable sandbox; egress allowlist available |
| Network exfiltration with egress off | Log-only and allowlist modes; default-deny planned |
| Agent tampering with Git state | Trusted capture in a separate helper |
| Approval gate bypassed by a token holder | Single-developer trust domain documented; reviewer identities planned |
| Upstream agent CLI changes | Adapter interface isolates changes; mock adapter keeps the pipeline testable |

---

## Analytics Till Date

### Software Size

| Component | Product SLOC | Test SLOC |
|---|---|---|
| CLI package and tools | 12,404 | 16,697 |
| Backend | 1,150 | 2,552 |
| Frontend | 4,356 | 2,511 |
| Deployment configuration | 160 | – |
| **Total** | **18,070** | **21,760** |

### Automated Tests

| Suite | Tests | Passed | Failed | Pass rate |
|---|---|---|---|---|
| CLI | 2,080 | 2,078 | 2 | 99.90% |
| Backend | 341 | 341 | 0 | 100.00% |
| Frontend | 283 | 283 | 0 | 100.00% |
| **Total** | **2,704** | **2,702** | **2** | **99.93%** |

Lowest text contrast: 4.77:1 in the light theme and 5.80:1 in the dark theme. No horizontal overflow across 63 page and width combinations.

### Performance (40 benchmark runs, mock agent, MacBook Air M4)

| Configuration | Runs | Mean time (s) |
|---|---|---|
| Direct on host: clone and agent | 20 | 0.237 |
| VaultShield default | 20 | 1.403 |
| VaultShield with egress allowlist | 10 | 2.213 |
| VaultShield with one screenshot route | 10 | 3.294 |

- Overhead: +1.166 s in the default mode and +1.976 s with egress. These are about 0.39% and 0.66% of a five-minute agent session.
- Largest stage: trusted capture at 0.651 s (46.4%), the cost of a diff the agent cannot falsify.

### Safety Results

| Scenario | Result |
|---|---|
| Host checkout unchanged after the run | 40 of 40 |
| Agent's file present in diff.json | 40 of 40 |
| File-change event recorded for the agent's file | 32 of 40 |
| Non-allowlisted connections blocked and logged | 4 of 4 |
| Credential-leak vectors detected | 5 of 5 |
| Tampered in-container Git defeated | 2 of 2 |
| Dummy values in six real .env.sandbox files | 30 of 30 |
| Containers and networks left after the campaign | 0 |

### Secret Exposure Spike

| Config | Protection | Verdict |
|---|---|---|
| C0 | None | Both secrets printed |
| C1 | Agent deny rules for .env files | .env.local read into context |
| C2 | C1 plus a hook denying shell access to .env | .env.local read into context |
| C3 | C2 plus real values replaced by a placeholder on disk | Safe |

### Objective Status

| Objective | Status |
|---|---|
| 1. Universal agent execution platform | Achieved |
| 2. Agentic AI Orchestrator | Not achieved |
| 3. Secure sandboxed execution | Achieved |
| 4. Monitoring and security analysis | Achieved, with a file-watcher gap |
| 5. Governance and human approval | Achieved |

---

## Future Plan

### Objective 2: Agentic AI Orchestrator (not done)

- Objective 2 was not implemented in Semester VII. No part of the orchestrator is in the current system.
- Semester VIII plan: build a module that:
  - scans the project to understand its context;
  - asks the developer clarifying questions before execution;
  - passes an enriched prompt to the agent, checked to contain no secret.
- Then measure whether the enriched prompt improves agent accuracy and reduces hallucinations, completing Objective 2.

### Other Planned Work

- **Stronger isolation:** non-root sandbox user, dropped capabilities, no-new-privileges, resource limits, and gVisor or Firecracker runtimes.
- **Policy engine:** declarative rules such as required approvals for infrastructure files, plus default-deny egress with path-aware filtering.
- **Separation of duties:** reviewer accounts, role-based access control, multiple approvals, and every status change gated through the approval endpoint.
- **Cost and resource controls:** per-run token usage, per-project budgets and sandbox time limits.
- **Multi-repository and parallel runs:** several agents attempting the same task for comparison.
- **IDE integration and live review:** start runs from the editor, render patch text in the dashboard and stream events live.
- **Evidence quality:** an event-driven file watcher, event log cross-checks against the diff, and credential scanning of screenshots.
- **Broader evaluation:** real agents on representative repositories, including a SWE-bench subset, and a usability study with reviewers.

### Semester VIII Schedule

| Sr. | Activity | Duration | Period |
|---|---|---|---|
| 1 | Implementation of the Agentic AI Orchestrator (Objective 2) | 3 weeks | January 2027 |
| 2 | Evaluation with real agents on sample repositories | 3 weeks | January 2027 |
| 3 | Default sandbox hardening and fixes for issues found in testing | 3 weeks | January – February 2027 |
| 4 | Verification of the Codex and Kiro egress allowlists; reviewer-identity separation study | 2 weeks | February 2027 |
| 5 | Usability study with reviewers and dashboard refinements | 2 weeks | March 2027 |
| 6 | Research paper drafting and submission | 2 weeks | March 2027 |
| 7 | Final report, demonstration and viva preparation | 3 weeks | March – April 2027 |

---

## Bibliography

1. M. Chen et al., "[Evaluating Large Language Models Trained on Code](https://arxiv.org/abs/2107.03374)," arXiv:2107.03374, 2021.
2. C. E. Jimenez et al., "[SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://openreview.net/forum?id=VTF8yNQM66)," ICLR, 2024.
3. J. Yang et al., "[SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://proceedings.neurips.cc/paper_files/paper/2024/hash/5a7c947568c1b1328ccc5230172e1e7c-Abstract-Conference.html)," NeurIPS, 2024.
4. X. Wang et al., "[OpenHands: An Open Platform for AI Software Developers as Generalist Agents](https://arxiv.org/abs/2407.16741)," ICLR, 2025.
5. Y. Ruan et al., "[Identifying the Risks of LM Agents with an LM-Emulated Sandbox](https://openreview.net/forum?id=GEcwtMk1uA)," ICLR, 2024.
6. E. Debenedetti et al., "[AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents](https://proceedings.neurips.cc/paper_files/paper/2024/file/97091a5177d8dc64b1da8bf3e1f6fb54-Paper-Datasets_and_Benchmarks_Track.pdf)," NeurIPS Datasets and Benchmarks Track, 2024.
7. K. Greshake et al., "[Not What You've Signed Up For: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection](https://doi.org/10.1145/3605764.3623985)," ACM Workshop on Artificial Intelligence and Security, 2023.
8. H. Pearce et al., "[Asleep at the Keyboard? Assessing the Security of GitHub Copilot's Code Contributions](https://doi.org/10.1109/SP46214.2022.9833571)," IEEE Symposium on Security and Privacy, 2022.
9. Y. Shavit et al., "[Practices for Governing Agentic AI Systems](https://openai.com/index/practices-for-governing-agentic-ai-systems/)," OpenAI White Paper, 2023.
10. A. Chan et al., "[Visibility into AI Agents](https://doi.org/10.1145/3630106.3658948)," ACM FAccT, 2024.
11. T. Combe, A. Martin and R. Di Pietro, "[To Docker or Not to Docker: A Security Perspective](https://doi.org/10.1109/MCC.2016.100)," IEEE Cloud Computing, 2016.
12. A. Agache et al., "[Firecracker: Lightweight Virtualization for Serverless Applications](https://www.usenix.org/conference/nsdi20/presentation/agache)," USENIX NSDI, 2020.
13. E. G. Young et al., "[The True Cost of Containing: A gVisor Case Study](https://www.usenix.org/conference/hotcloud19/presentation/young)," USENIX HotCloud, 2019.
14. A. Bacchelli and C. Bird, "[Expectations, Outcomes, and Challenges of Modern Code Review](https://doi.org/10.1109/ICSE.2013.6606617)," ICSE, 2013.
15. B. W. Boehm, "[Software Engineering Economics](https://www.pearson.com/en-us/subject-catalog/p/software-engineering-economics/P200000000306)," Prentice-Hall, 1981.

---

## Conclusion

- VaultShield separates running an AI coding agent from trusting what it produces:
  - the agent gets a disposable Docker container, a fresh clone and only dummy credentials;
  - its output, file changes and network decisions are recorded in an event log;
  - its change is captured as one commit on a dedicated branch by a separate hardened container;
  - the change becomes a pull or merge request only after a human approves it.
- Built as a Python CLI, a Django REST Framework backend on MongoDB with Celery workers, and a React dashboard: about 18.1 thousand lines of product code and 2,704 automated tests at a 99.93% pass rate.
- The protection costs about 1.2 seconds per run. Every evaluated run left the host checkout untouched, kept real secrets away from the agent and left no containers behind.
- Objectives 1, 3, 4 and 5 were achieved. Objective 2, the Agentic AI Orchestrator, was not achieved in Semester VII and is planned for Semester VIII.
- Remaining gaps:
  - in the single-developer MVP, a token holder can bypass the dashboard through an unrestricted status update;
  - the polling file watcher missed some writes in very short runs, although the trusted diff captured every change.
