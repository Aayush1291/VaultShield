/**
 * The `/` landing route — the marketing page, ported from website/index.html.
 *
 * The content is the original page's, verbatim: the hero and its CSS-only
 * terminal mock, the three steps, before/after, the review loop, the honest
 * "what it does not" limits, the measured-so-far checklist, the honestly
 * labelled supported-agents grid (Antigravity proven live; the other three
 * built from docs, not live-tested), and the FAQ. Two things changed because
 * it is now a route in the dashboard app rather than a standalone file:
 *
 *  - The header carries real navigation: Log in / Sign up, or Open dashboard
 *    when already signed in.
 *  - The install command is built from the page's OWN origin
 *    (`<origin>/install.sh`) from one place, because the installer is now
 *    served by this same site (frontend/public/install.sh) instead of a
 *    separate `python -m http.server` on :8080.
 *
 * The page keeps its own self-contained stylesheet (no CDN, font, or
 * analytics) in a scoped `<style>` under `.vs-landing`, so dark mode
 * (`prefers-color-scheme`), the reduced-motion fallback, and the responsive
 * breakpoints travel with the component and unmount with the route. Step 4
 * widens the container and the hero within this same stylesheet.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { useAuth } from './auth.jsx';

/**
 * The install command, built from the page's own origin — the single place
 * the installer URL is spelled. Served by this site at `/install.sh`.
 *
 * @param {string} origin e.g. `window.location.origin`
 * @returns {string}
 */
export function buildInstallCommand(origin) {
  return `curl -fsSL ${origin}/install.sh | sh`;
}

/** Marks the measured-claims list items, so a test can enumerate them. */
export const MEASURED_CLAIM_TESTID = 'measured-claim';

const LANDING_CSS = `
.vs-landing {
  --bg:#fbfaf8; --fg:#1b1b1f; --muted:#5b5b66; --subtle:#83838f;
  --card:#ffffff; --card-2:#f4f2ee; --border:#e7e3dc; --border-2:#d7d2c9;
  --accent:#4f46e5; --accent-2:#7c3aed; --on-accent:#ffffff; --accent-soft:#eef0ff;
  --code-bg:#0d1117; --code-fg:#e6edf3; --ok:#1a7f37; --warn:#8a6d00;
  --radius:16px; --radius-lg:24px;
  --shadow:0 1px 2px rgba(15,23,42,.05), 0 12px 32px rgba(15,23,42,.08);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  color-scheme:light dark; color:var(--fg);
  font:16px/1.65 var(--sans); min-height:100vh; -webkit-font-smoothing:antialiased;
  /* Full-bleed gradient wash behind the whole page (spans the viewport
     because .vs-landing is the full-width root), over the base canvas. */
  background-color:var(--bg);
  background-image:
    radial-gradient(70% 45% at 50% -8%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 72%),
    radial-gradient(45% 35% at 92% 2%, color-mix(in srgb, var(--accent-2) 14%, transparent), transparent 70%);
  background-repeat:no-repeat;
}
@media (prefers-color-scheme: dark) {
  .vs-landing {
    --bg:#0d0e12; --fg:#ecedf1; --muted:#a6a8b3; --subtle:#7f818d;
    --card:#16181f; --card-2:#1d2029; --border:#2a2d38; --border-2:#3a3e4c;
    --accent:#818cf8; --accent-2:#a78bfa; --on-accent:#0d0e12; --accent-soft:#1e2030;
    --code-bg:#05070a; --code-fg:#e6edf3; --ok:#3fb950; --warn:#d4a72c;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 12px 32px rgba(0,0,0,.55);
  }
}
.vs-landing *, .vs-landing *::before, .vs-landing *::after { box-sizing:border-box; }
.vs-landing a { color:var(--accent); }
.vs-landing .page { max-width:1240px; margin:0 auto; padding:1.5rem 1.5rem 5rem; }
.vs-landing .topnav { display:flex; align-items:center; justify-content:space-between; gap:1rem; }
.vs-landing .topnav .links { display:flex; align-items:center; gap:.9rem; font-size:.92rem; }
.vs-landing .wordmark { display:flex; align-items:center; gap:.55rem; font-weight:600; letter-spacing:-.01em; }
.vs-landing .wordmark svg { width:1.6rem; height:1.6rem; color:var(--accent); }
.vs-landing .btn { border-radius:10px; padding:.4rem .85rem; font-weight:600; text-decoration:none; }
.vs-landing .btn.primary { background:var(--accent); color:var(--on-accent); }
.vs-landing .btn.ghost { border:1px solid var(--border); color:var(--fg); }
.vs-landing .hero { position:relative; padding:2.5rem 0 1rem; }
.vs-landing .glow {
  position:absolute; inset:-40% -20% auto -20%; height:420px; z-index:-1;
  background:
    radial-gradient(45% 60% at 20% 0%, color-mix(in srgb, var(--accent) 24%, transparent), transparent 70%),
    radial-gradient(45% 60% at 85% 10%, color-mix(in srgb, var(--accent-2) 22%, transparent), transparent 70%);
  filter:blur(6px); pointer-events:none;
}
.vs-landing h1 { font-size:clamp(2rem,5vw,3.1rem); line-height:1.05; letter-spacing:-.03em; margin:1.4rem 0 .6rem; }
.vs-landing h1 .grad { background:linear-gradient(90deg,var(--accent),var(--accent-2)); -webkit-background-clip:text; background-clip:text; color:transparent; }
.vs-landing .lead { font-size:1.15rem; color:var(--muted); max-width:42ch; }
.vs-landing h2 { font-size:1.45rem; letter-spacing:-.02em; margin:2.8rem 0 .4rem; }
.vs-landing h3 { font-size:1.05rem; margin:0 0 .3rem; }
.vs-landing p { margin:.6rem 0; }
.vs-landing .muted { color:var(--muted); }
.vs-landing .sub { color:var(--subtle); font-size:.92rem; }
.vs-landing .pill { display:inline-flex; align-items:center; gap:.4rem; border:1px solid var(--border); background:var(--card); border-radius:999px; padding:.25rem .7rem; font-size:.8rem; color:var(--muted); }
.vs-landing .cmdbox { display:flex; gap:.5rem; align-items:center; background:var(--code-bg); border-radius:var(--radius); padding:.8rem .9rem; margin:1.2rem 0 .5rem; box-shadow:var(--shadow); overflow-x:auto; }
.vs-landing .cmdbox code { color:var(--code-fg); font:.95rem/1.5 var(--mono); white-space:pre; flex:1; }
.vs-landing button.copy { flex:0 0 auto; background:var(--accent); color:var(--on-accent); border:none; border-radius:10px; padding:.4rem .8rem; font:600 .85rem var(--sans); cursor:pointer; }
.vs-landing button.copy:active { opacity:.85; }
.vs-landing .hero-grid { display:grid; gap:1.6rem; grid-template-columns:1fr; align-items:start; }
@media (min-width:1024px) { .vs-landing .hero-grid { grid-template-columns:1.1fr .9fr; gap:2.5rem; } }
.vs-landing .term { background:var(--code-bg); border-radius:var(--radius); box-shadow:var(--shadow); overflow:hidden; }
.vs-landing .term-bar { display:flex; gap:.4rem; padding:.6rem .8rem; border-bottom:1px solid rgba(255,255,255,.08); }
.vs-landing .term-bar i { width:.65rem; height:.65rem; border-radius:50%; background:#3a3f4b; }
.vs-landing .term-body { margin:0; padding:.9rem 1rem 1.1rem; font:.9rem/1.7 var(--mono); color:var(--code-fg); }
.vs-landing .term .prompt { color:#7ee787; }
.vs-landing .term .cmd { display:inline-block; overflow:hidden; white-space:nowrap; vertical-align:bottom; max-width:18ch; border-right:2px solid var(--accent-2); animation:vs-type 1.5s steps(18,end) .4s both, vs-caret .8s step-end 6; }
.vs-landing .term .line { display:block; opacity:0; animation:vs-fade .4s ease forwards; }
.vs-landing .term .l1 { animation-delay:2.1s; }
.vs-landing .term .l2 { animation-delay:2.6s; }
.vs-landing .term .dim { color:var(--subtle); }
.vs-landing .term .held { color:#e3b341; }
@keyframes vs-type { from { width:0; } to { width:18ch; } }
@keyframes vs-caret { 50% { border-color:transparent; } }
@keyframes vs-fade { to { opacity:1; } }
@media (prefers-reduced-motion: reduce) {
  .vs-landing .term .cmd { width:auto; max-width:none; animation:none; border-right:none; }
  .vs-landing .term .line { opacity:1; animation:none; }
}
.vs-landing .card { background:var(--card); border:1px solid var(--border); border-radius:var(--radius-lg); padding:1.2rem 1.3rem; box-shadow:var(--shadow); }
.vs-landing .grid { display:grid; gap:1rem; grid-template-columns:1fr; }
@media (min-width:680px) { .vs-landing .grid.cols-3 { grid-template-columns:repeat(3,1fr); } .vs-landing .grid.cols-2 { grid-template-columns:repeat(2,1fr); } }
.vs-landing ol.steps { counter-reset:step; list-style:none; padding:0; }
.vs-landing ol.steps > li { counter-increment:step; position:relative; padding-left:2.6rem; margin:1rem 0; }
.vs-landing ol.steps > li::before { content:counter(step); position:absolute; left:0; top:-.1rem; width:1.8rem; height:1.8rem; border-radius:50%; background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff; display:flex; align-items:center; justify-content:center; font-weight:600; font-size:.95rem; }
.vs-landing .ba { display:grid; gap:.8rem; grid-template-columns:1fr; }
@media (min-width:620px) { .vs-landing .ba { grid-template-columns:1fr 1fr; } }
.vs-landing .env { background:var(--code-bg); color:var(--code-fg); border-radius:var(--radius); padding:.85rem 1rem; font:.85rem/1.7 var(--mono); overflow-x:auto; }
.vs-landing .env .tag { display:block; color:var(--subtle); font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; margin-bottom:.4rem; }
.vs-landing .env .leak { color:#ff7b72; }
.vs-landing .env .safe { color:#7ee787; }
.vs-landing ul.plain { list-style:none; padding:0; }
.vs-landing ul.plain > li { position:relative; padding:.35rem 0 .35rem 1.7rem; }
.vs-landing ul.plain > li .mark { position:absolute; left:0; top:.4rem; font-weight:700; }
.vs-landing .yes .mark { color:var(--ok); }
.vs-landing .no .mark { color:var(--warn); }
.vs-landing .measured li.claim { position:relative; padding:.4rem 0 .4rem 1.7rem; }
.vs-landing .measured li.claim .mark { position:absolute; left:0; top:.4rem; font-weight:700; }
.vs-landing .measured li.yes .mark { color:var(--ok); }
.vs-landing .measured li.pending .mark { color:var(--warn); }
.vs-landing .agents { display:grid; gap:.7rem; grid-template-columns:1fr; }
@media (min-width:620px) { .vs-landing .agents { grid-template-columns:1fr 1fr; } }
.vs-landing .agent { display:flex; align-items:center; justify-content:space-between; gap:.8rem; border:1px solid var(--border); border-radius:var(--radius); padding:.7rem .9rem; background:var(--card); }
.vs-landing .agent .name { font-weight:600; }
.vs-landing .badge { font-size:.72rem; font-weight:600; border-radius:999px; padding:.2rem .6rem; white-space:nowrap; }
.vs-landing .badge.live { color:#0b6b2f; background:#e6f6ec; border:1px solid #b7e4c7; }
.vs-landing .badge.docs { color:#6b5600; background:#fbf3d6; border:1px solid #ecd9a0; }
@media (prefers-color-scheme: dark) {
  .vs-landing .badge.live { color:#7ee787; background:rgba(63,185,80,.14); border-color:rgba(63,185,80,.4); }
  .vs-landing .badge.docs { color:#e3b341; background:rgba(212,167,44,.14); border-color:rgba(212,167,44,.4); }
}
.vs-landing details.faq { border:1px solid var(--border); border-radius:var(--radius); padding:.2rem 1rem; margin:.6rem 0; background:var(--card); }
.vs-landing details.faq > summary { cursor:pointer; font-weight:600; padding:.7rem 0; list-style:none; }
.vs-landing details.faq > summary::-webkit-details-marker { display:none; }
.vs-landing details.faq > summary::after { content:"+"; float:right; color:var(--subtle); }
.vs-landing details.faq[open] > summary::after { content:"–"; }
.vs-landing details.faq > div { padding-bottom:.8rem; color:var(--muted); }
.vs-landing code.inline { background:var(--card-2); border:1px solid var(--border); border-radius:6px; padding:0 .3rem; font-family:var(--mono); font-size:.9em; }
.vs-landing footer { margin-top:3rem; border-top:1px solid var(--border); padding-top:1.2rem; color:var(--muted); font-size:.9rem; }
.vs-landing :where(a,button,summary):focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:6px; }
`;

export default function Landing() {
  const { signedIn } = useAuth();
  const origin = typeof window !== 'undefined' && window.location?.origin ? window.location.origin : '';
  const installCommand = buildInstallCommand(origin);
  const [copied, setCopied] = useState(null);

  async function copyInstall() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(installCommand);
        setCopied(true);
      } else {
        setCopied(false);
      }
    } catch {
      setCopied(false);
    }
    setTimeout(() => setCopied(null), 1500);
  }

  return (
    <div className="vs-landing">
      <style>{LANDING_CSS}</style>
      <div className="page">
        <div className="glow" aria-hidden="true" />

        <header className="topnav">
          <span className="wordmark" aria-label="VaultShield">
            <svg viewBox="0 0 24 24" aria-hidden="true" fill="none">
              <path d="M12 2.5 19 5.2v6.1c0 4.6-3 7.9-7 9.7-4-1.8-7-5.1-7-9.7V5.2L12 2.5Z" fill="currentColor" opacity=".16" />
              <path d="M12 2.5 19 5.2v6.1c0 4.6-3 7.9-7 9.7-4-1.8-7-5.1-7-9.7V5.2L12 2.5Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
              <path d="m9 11.7 2.2 2.2L15 9.8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span>VaultShield</span>
          </span>
          <nav className="links" aria-label="Primary">
            {signedIn ? (
              <Link className="btn primary" to="/dashboard">
                Open dashboard
              </Link>
            ) : (
              <>
                <Link className="btn ghost" to="/login">
                  Log in
                </Link>
                <Link className="btn primary" to="/signup">
                  Sign up
                </Link>
              </>
            )}
          </nav>
        </header>

        <section className="hero">
          <span className="pill">macOS only · for AI coding agents</span>
          <h1>
            Let AI agents read your code, <span className="grad">not your secrets</span>.
          </h1>
          <div className="hero-grid">
            <div>
              <p className="lead">
                AI coding tools read your project files to help you — including{' '}
                <code className="inline">.env</code> files full of API keys. VaultShield moves those
                values into the macOS Keychain and leaves a harmless placeholder, so the agent still
                sees that a key exists and what it is called, never the secret itself.
              </p>
              <div className="cmdbox">
                <code data-testid="install-cmd">{installCommand}</code>
                <button type="button" className="copy" onClick={copyInstall}>
                  {copied === true ? 'Copied' : copied === false ? 'Copy failed' : 'Copy'}
                </button>
              </div>
              <p className="sub">
                One line in Terminal. It installs a private, isolated copy and nothing else — no
                system software, no edits to your shell startup files.
              </p>
            </div>

            <div
              className="term"
              role="img"
              aria-label="Terminal: running vaultshield secure, after which the agent sees STRIPE_KEY=vs_protected instead of the real key."
            >
              <div className="term-bar" aria-hidden="true">
                <i />
                <i />
                <i />
              </div>
              <pre className="term-body">
                <span className="prompt">$</span> <span className="cmd">vaultshield secure</span>
                {'\n'}
                <span className="line l1">Moved 1 value to the Keychain, left a placeholder.</span>
                {'\n'}
                <span className="line l2">
                  <span className="dim"># what your agent now reads:</span>
                  {'\n'}STRIPE_KEY=<span className="held">vs_protected</span>
                </span>
              </pre>
            </div>
          </div>
        </section>

        <h2>Three steps</h2>
        <ol className="steps">
          <li>
            <h3>Install</h3>
            <p className="muted">
              Paste the one command above in Terminal. It sets up a private, isolated copy of
              VaultShield and nothing else on your system.
            </p>
          </li>
          <li>
            <h3>Secure a project</h3>
            <p className="muted">
              In your project folder, run <code className="inline">vaultshield secure</code>. It
              shows what it found, asks before changing anything, and tucks the values into the
              Keychain.
            </p>
          </li>
          <li>
            <h3>Ask your agent as usual</h3>
            <p className="muted">
              Point your AI coding tool at the project. It sees{' '}
              <code className="inline">API_KEY=vs_protected</code>, not your real key.
            </p>
          </li>
        </ol>

        <h2>Before and after</h2>
        <p className="muted">
          Your <code className="inline">.env</code> stays in place; only the values move.
        </p>
        <div className="ba">
          <div className="env">
            <span className="tag">before — .env on disk</span>STRIPE_KEY=
            <span className="leak">sk_live_51HxEXAMPLExxxNOTREAL</span>
            {'\n'}DATABASE_URL=<span className="leak">postgres://u:p@localhost/db</span>
          </div>
          <div className="env">
            <span className="tag">after — what the agent sees</span>STRIPE_KEY=
            <span className="safe">vs_protected</span>
            {'\n'}DATABASE_URL=<span className="safe">vs_protected</span>
          </div>
        </div>

        <h2>Review every AI change</h2>
        <div className="card">
          <p className="muted">
            For sandboxed runs, VaultShield records the diff, the terminal events, screenshots, and
            a branch bundle, then uploads them to a dashboard where a human reviews and approves —
            or rejects — before anything ships.
          </p>
          <div className="grid cols-3" style={{ marginTop: '1rem' }}>
            <div>
              <h3>See the diff</h3>
              <p className="sub">
                Every file the agent added, changed, or deleted, with its security findings
                alongside.
              </p>
            </div>
            <div>
              <h3>Approve or reject</h3>
              <p className="sub">Nothing reaches a real branch until a person says so.</p>
            </div>
            <div>
              <h3>Then submit</h3>
              <p className="sub">
                <code className="inline">vaultshield submit</code> pushes the approved branch and
                opens a pull request.
              </p>
            </div>
          </div>
        </div>

        <h2>What it protects, and what it does not</h2>
        <div className="card">
          <ul className="plain">
            <li className="yes">
              <span className="mark">✓</span> The <strong>value</strong> of every variable
              VaultShield moves out of a <code className="inline">.env</code> file. It lives in the
              Keychain; the file holds <code className="inline">vs_protected</code>.
            </li>
            <li className="yes">
              <span className="mark">✓</span> The agent can still read variable <strong>names</strong>,
              so it can reason about your project without seeing a single secret.
            </li>
            <li className="yes">
              <span className="mark">✓</span> You can undo it any time with{' '}
              <code className="inline">vaultshield unprotect</code>, which puts the real values back.
            </li>
          </ul>
          <p className="muted" style={{ marginBottom: '.3rem' }}>
            Honest about the limits:
          </p>
          <ul className="plain">
            <li className="no">
              <span className="mark">!</span> VaultShield protects the files it moves. A value you
              copied into a comment, another file, git history, or your shell history is not touched.
            </li>
            <li className="no">
              <span className="mark">!</span> A determined program running as <em>you</em> can still
              try indirect tricks (reading the Keychain through other tools, or encoding a value so a
              simple filter misses it). VaultShield raises the bar; it is not a sandbox.
            </li>
            <li className="no">
              <span className="mark">!</span> Only results that were actually measured are claimed
              below. Nothing here is a guarantee.
            </li>
          </ul>
        </div>

        <h2>Measured so far</h2>
        <div className="card measured">
          <ul className="plain">
            <li className="claim yes" data-testid={MEASURED_CLAIM_TESTID}>
              <span className="mark">✓</span> Placeholders held in the Antigravity CLI and App: an
              agent that got past the rules read the placeholder, not the secret.
            </li>
            <li className="claim yes" data-testid={MEASURED_CLAIM_TESTID}>
              <span className="mark">✓</span> The command hook blocked printenv live in Antigravity.
            </li>
            <li className="claim pending" data-testid={MEASURED_CLAIM_TESTID}>
              <span className="mark">○</span> Claude Code, Kiro and Codex backstops are built from
              docs, not yet live-tested.
            </li>
          </ul>
          <p className="muted">
            No invented numbers. These are the outcomes of the spike and the live tests that were
            run; the rest is honestly marked as not yet tested.
          </p>
        </div>

        <h2>Supported agents</h2>
        <div className="agents">
          <div className="agent">
            <span className="name">Antigravity</span>
            <span className="badge live">proven live</span>
          </div>
          <div className="agent">
            <span className="name">Claude Code</span>
            <span className="badge docs">built from docs, not live-tested</span>
          </div>
          <div className="agent">
            <span className="name">Kiro</span>
            <span className="badge docs">built from docs, not live-tested</span>
          </div>
          <div className="agent">
            <span className="name">Codex</span>
            <span className="badge docs">built from docs, not live-tested</span>
          </div>
        </div>

        <h2>FAQ</h2>
        <details className="faq">
          <summary>Does the AI agent ever see my real keys?</summary>
          <div>
            No — not for the values VaultShield moved. It reads the variable names and the
            placeholder <code className="inline">vs_protected</code>. The real value is in the macOS
            Keychain.
          </div>
        </details>
        <details className="faq">
          <summary>Is this a sandbox?</summary>
          <div>
            No. It is not a sandbox. It moves secret values out of the files an agent reads and
            raises the bar against casual exposure, but a determined program running as you can still
            try indirect tricks. Use the sandboxed <code className="inline">vaultshield run</code> for
            stronger isolation.
          </div>
        </details>
        <details className="faq">
          <summary>Which platforms are supported?</summary>
          <div>macOS only, for now — it relies on the macOS Keychain and needs Python 3.11+.</div>
        </details>
        <details className="faq">
          <summary>How do I remove it?</summary>
          <div>
            Re-run the install command with <code className="inline">-s -- --uninstall</code>. It
            removes the private environment and the <code className="inline">vaultshield</code>{' '}
            command and leaves your secrets in place.
          </div>
        </details>

        <footer>
          <p>macOS only. The install command above is served by this site — paste it into Terminal.</p>
        </footer>
      </div>
    </div>
  );
}
