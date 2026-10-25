/**
 * The `/` landing route.
 *
 * An editorial, terminal-first page in full-width bands, each with a centered
 * ~1240px container: sticky header, hero (serif headline, one install pill, a
 * Without/With terminal), a slim "Measured so far" strip, How it works,
 * Protect, Review, Agents, FAQ, a closing install band, and a minimal footer.
 *
 * Honesty constraints the tests pin (Landing.test.jsx):
 *  - Antigravity is the only agent labelled "proven live"; Claude Code, Kiro
 *    and Codex are "built from docs, not live-tested".
 *  - The measured-claims list holds exactly the three measured outcomes.
 *  - The honest limits (indirect tricks, not a sandbox) stay on the page.
 *  - The only absolute URL is the install line, built from the page's own
 *    origin by `buildInstallCommand` — the one place it is spelled.
 *
 * Styles are a scoped `<style>` under `.vs-landing` that reads the theme tokens
 * from index.css, so Paper/Ink switching needs nothing here. Motion (scroll
 * fade-up, the auto-advancing terminal, the typing caret) runs only when
 * `prefers-reduced-motion` is not set, and only via CSS plus an
 * IntersectionObserver: no animation library.
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import ThemeSwitch from './ThemeSwitch.jsx';
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

/** How long the hero terminal waits before showing "With VaultShield". */
export const AUTO_ADVANCE_MS = 3600;

/** The obviously fake key the "Without" view shows. */
export const FAKE_STRIPE_KEY = 'sk_live_51HxEXAMPLExxxNOTREAL';

/**
 * True when motion should be avoided. A browser that cannot answer (no
 * `matchMedia`) is treated as "reduce": the page is complete without motion.
 */
export function prefersReducedMotion() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return true;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function Shield({ className = '' }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" className={className}>
      <path
        d="M12 2.5 19 5.2v6.1c0 4.6-3 7.9-7 9.7-4-1.8-7-5.1-7-9.7V5.2L12 2.5Z"
        fill="currentColor"
        opacity=".16"
      />
      <path
        d="M12 2.5 19 5.2v6.1c0 4.6-3 7.9-7 9.7-4-1.8-7-5.1-7-9.7V5.2L12 2.5Z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="m9 11.7 2.2 2.2L15 9.8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The install command in a large pill with a Copy pill inside it. */
function InstallPill({ command, testId, id }) {
  const [copied, setCopied] = useState(null);
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    let ok = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(command);
        ok = true;
      }
    } catch {
      ok = false;
    }
    setCopied(ok);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(null), 1500);
  }

  return (
    <div className="lp-install" id={id}>
      <span className="lp-install-prompt" aria-hidden="true">
        $
      </span>
      <code data-testid={testId}>{command}</code>
      <button type="button" className="lp-pill" onClick={copy}>
        {copied === true ? 'Copied' : copied === false ? 'Copy failed' : 'Copy'}
      </button>
    </div>
  );
}

/**
 * The hero terminal: what an agent reads from `.env`, without and with
 * VaultShield. Starts on "Without"; advances once to "With" after
 * `AUTO_ADVANCE_MS` unless the visitor has used the toggle or prefers reduced
 * motion. The toggle is two `aria-pressed` buttons, so it works by keyboard and
 * needs no animation to make sense.
 */
function HeroTerminal() {
  const [view, setView] = useState('without');
  const interacted = useRef(false);

  useEffect(() => {
    if (prefersReducedMotion()) return undefined;
    const timer = setTimeout(() => {
      if (!interacted.current) setView('with');
    }, AUTO_ADVANCE_MS);
    return () => clearTimeout(timer);
  }, []);

  function choose(next) {
    interacted.current = true;
    setView(next);
  }

  const protectedView = view === 'with';

  return (
    <div className="lp-term" role="region" aria-label="Example: what an AI agent reads from .env">
      <div className="lp-term-bar">
        <span className="lp-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="lp-term-title">agent session · ~/acme-shop</span>
        <div className="lp-toggle" role="group" aria-label="Agent's view">
          <button type="button" aria-pressed={!protectedView} onClick={() => choose('without')}>
            Without VaultShield
          </button>
          <button type="button" aria-pressed={protectedView} onClick={() => choose('with')}>
            With VaultShield
          </button>
        </div>
      </div>
      <pre className="lp-term-body" aria-live="polite">
        <span className="t-prompt">$</span> <span className="lp-typed">cat .env</span>
        {'\n'}
        <span className="t-dim"># what your agent reads</span>
        {'\n'}
        {protectedView ? (
          <>
            STRIPE_KEY=<span className="t-safe">vs_protected</span>
            {'\n'}
            <span className="t-dim">Real value: macOS Keychain</span>
          </>
        ) : (
          <>
            STRIPE_KEY=<span className="t-leak">{FAKE_STRIPE_KEY}</span>
            {'\n'}
            <span className="t-dim">The live key is now in the agent&rsquo;s context.</span>
          </>
        )}
      </pre>
    </div>
  );
}

const LANDING_CSS = `
html:has(.vs-landing) { scroll-behavior: smooth; }
.vs-landing { background: var(--color-canvas); color: var(--color-fg); font-family: var(--font-sans); min-height: 100vh; }
.vs-landing *, .vs-landing *::before, .vs-landing *::after { box-sizing: border-box; }
.vs-landing section[id], .vs-landing #install { scroll-margin-top: 92px; }
.vs-landing a { color: var(--color-accent-text); }
.lp-container { width: 100%; max-width: calc(var(--container-page) + 2 * var(--spacing-gutter)); margin-inline: auto; padding-inline: var(--spacing-gutter); }

.lp-header { position: sticky; top: 0; z-index: 30; background: color-mix(in srgb, var(--color-canvas) 80%, transparent); -webkit-backdrop-filter: saturate(1.2) blur(14px); backdrop-filter: saturate(1.2) blur(14px); border-bottom: 1px solid var(--color-border); }
.lp-header-inner { display: flex; align-items: center; gap: .75rem; height: 68px; }
.lp-wordmark { display: inline-flex; align-items: center; gap: .55rem; font-weight: 600; letter-spacing: -.01em; font-size: 1.0625rem; color: var(--color-fg) !important; text-decoration: none; }
.lp-wordmark svg { width: 28px; height: 28px; color: var(--color-accent-text); }
.lp-sections { display: none; gap: 2rem; margin-inline: auto; }
.lp-sections a { color: var(--color-fg-muted); text-decoration: none; font-size: .95rem; }
.lp-sections a:hover { color: var(--color-fg); }
.lp-actions { display: flex; align-items: center; gap: .75rem; margin-left: auto; }
.lp-text-link { color: var(--color-fg) !important; font-weight: 500; font-size: .95rem; text-decoration: none; white-space: nowrap; }
.lp-text-link:hover { text-decoration: underline; text-underline-offset: 4px; }
@media (min-width:900px) { .lp-sections { display: flex; } .lp-actions { margin-left: 0; } }
@media (max-width: 479px) { .lp-wordmark-text { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; } .lp-actions { gap: .5rem; } }

.lp-pill { display: inline-flex; align-items: center; justify-content: center; gap: .4rem; border: 0; border-radius: var(--radius-pill); background: var(--color-accent); color: var(--color-accent-fg) !important; font: 600 .95rem/1 var(--font-sans); padding: .7rem 1.2rem; text-decoration: none; cursor: pointer; white-space: nowrap; box-shadow: var(--shadow-soft); transition: background-color .2s, color .2s; }
.lp-pill:hover { background: var(--color-accent-hover); color: var(--color-accent-hover-fg) !important; }
@media (max-width: 479px) { .lp-header .lp-pill { padding: .6rem .9rem; font-size: .9rem; } }

.lp-band { padding-block: var(--spacing-section); }
.lp-band.alt { background: var(--color-surface-2); }
.lp-kicker { font: 600 .78rem/1.4 var(--font-sans); letter-spacing: .14em; text-transform: uppercase; color: var(--color-fg-muted); margin: 0; }
.lp-h2 { font-family: var(--font-display); font-weight: 400; font-size: var(--text-section); line-height: 1.05; letter-spacing: -.015em; margin: .9rem 0 0; max-width: 18ch; text-wrap: balance; }
.lp-prose { font-size: var(--text-body-lg); line-height: 1.6; color: var(--color-fg-muted); max-width: var(--container-prose); margin: 1.5rem 0 0; text-wrap: pretty; }
.lp-split .lp-h2 { max-width: 16ch; }
.lp-code { font-family: var(--font-mono); font-size: .88em; color: var(--color-fg); background: color-mix(in srgb, var(--color-fg) 7%, transparent); border-radius: 6px; padding: .05rem .35rem; }

.lp-hero { position: relative; isolation: isolate; overflow: hidden; text-align: center; padding-block: clamp(4rem, 2rem + 6vw, 8.5rem) var(--spacing-section); }
.lp-hero::before { content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none; background: radial-gradient(60% 55% at 50% 0%, color-mix(in srgb, var(--color-accent) 38%, transparent), transparent 72%), radial-gradient(40% 40% at 88% 18%, color-mix(in srgb, #e8b98a 26%, transparent), transparent 70%), radial-gradient(35% 35% at 10% 30%, color-mix(in srgb, #e8b98a 16%, transparent), transparent 70%); }
.lp-eyebrow { display: inline-flex; align-items: center; gap: .5rem; border-radius: var(--radius-pill); border: 1px solid var(--color-border); background: color-mix(in srgb, var(--color-surface) 70%, transparent); padding: .35rem .9rem; font-size: .85rem; color: var(--color-fg-muted); }
.lp-display { font-family: var(--font-display); font-weight: 400; font-size: var(--text-display); line-height: 1; letter-spacing: -.02em; margin: 1.75rem auto 0; max-width: 14ch; text-wrap: balance; }
.lp-display em { font-style: italic; color: var(--color-accent-text); }
.lp-subline { font-size: var(--text-body-lg); line-height: 1.6; color: var(--color-fg-muted); max-width: 52ch; margin: 1.75rem auto 0; text-wrap: pretty; }
.lp-install { display: flex; align-items: center; gap: .75rem; width: 100%; max-width: 660px; margin: 2.75rem auto 0; padding: .5rem .5rem .5rem 1.4rem; border-radius: var(--radius-pill); background: var(--color-surface); border: 1px solid var(--color-border); box-shadow: var(--shadow-card); text-align: left; }
.lp-install-prompt { color: var(--color-fg-subtle); font-family: var(--font-mono); }
.lp-install code { flex: 1 1 auto; min-width: 0; overflow-x: auto; white-space: nowrap; scrollbar-width: none; font: 500 .95rem/1.5 var(--font-mono); color: var(--color-fg); }
.lp-install code::-webkit-scrollbar { display: none; }
.lp-install .lp-pill { flex: 0 0 auto; }
@media (max-width: 479px) { .lp-install { padding-left: 1rem; gap: .5rem; } .lp-install code { font-size: .78rem; } }
.lp-secondary { display: inline-block; margin-top: 1.25rem; font-size: .95rem; font-weight: 500; text-underline-offset: 4px; }

.lp-term { width: 100%; max-width: 900px; margin: clamp(3rem, 2rem + 4vw, 5rem) auto 0; border-radius: 24px; background: var(--color-term-bg); color: var(--color-term-fg); border: 1px solid var(--color-term-border); box-shadow: var(--shadow-lift); overflow: hidden; text-align: left; }
.lp-term-bar { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem 1rem; padding: .85rem 1rem .85rem 1.25rem; border-bottom: 1px solid var(--color-term-border); }
.lp-dots { display: inline-flex; gap: .45rem; }
.lp-dots i { width: 11px; height: 11px; border-radius: 50%; background: rgba(255, 250, 235, .18); }
.lp-term-title { font: .8rem var(--font-mono); color: var(--color-term-muted); }
.lp-toggle { margin-left: auto; display: inline-flex; gap: 2px; padding: 3px; border-radius: var(--radius-pill); border: 1px solid var(--color-term-border); }
.lp-toggle button { border: 0; background: transparent; color: var(--color-term-muted); font: 500 .82rem/1 var(--font-sans); padding: .5rem .85rem; border-radius: var(--radius-pill); cursor: pointer; white-space: nowrap; }
.lp-toggle button:hover { color: var(--color-term-fg); }
.lp-toggle button[aria-pressed="true"] { background: var(--color-term-accent); color: #1c1b14; }
@media (max-width: 559px) { .lp-term-title { display: none; } .lp-toggle { margin-left: 0; width: 100%; } .lp-toggle button { flex: 1 1 0; } }
.lp-term-body { margin: 0; padding: 1.5rem 1.6rem 1.8rem; font: .95rem/1.9 var(--font-mono); white-space: pre-wrap; overflow-wrap: anywhere; min-height: 11rem; }
@media (max-width: 479px) { .lp-term-body { font-size: .8rem; padding: 1.1rem; } }
.lp-typed { display: inline-block; overflow: hidden; white-space: nowrap; vertical-align: bottom; width: 8ch; border-right: 2px solid var(--color-term-accent); animation: lp-type 1s steps(8, end) .3s both, lp-caret .9s step-end 4 1.3s forwards; }
@keyframes lp-type { from { width: 0; } to { width: 8ch; } }
@keyframes lp-caret { 50% { border-color: transparent; } to { border-color: transparent; } }
.t-prompt { color: var(--color-term-accent); }
.t-dim { color: var(--color-term-muted); }
.t-leak { color: var(--color-term-danger); }
.t-safe { color: var(--color-term-ok); }
.lp-note { margin-top: 1rem; font-size: .85rem; color: var(--color-fg-subtle); }

.lp-slim { padding-block: var(--spacing-section-slim); background: var(--color-surface-2); border-block: 1px solid var(--color-border); }
.lp-slim-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: .5rem 2rem; margin-bottom: 1.75rem; }
.lp-slim-head h2 { font-family: var(--font-display); font-weight: 400; font-size: clamp(1.75rem, 1.4rem + 1vw, 2.25rem); margin: 0; }
.lp-slim-head p { margin: 0; color: var(--color-fg-muted); font-size: .95rem; max-width: 52ch; }
.lp-claims { display: grid; gap: 1rem; grid-template-columns: minmax(0, 1fr); list-style: none; margin: 0; padding: 0; }
@media (min-width:768px) { .lp-claims { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.lp-claim { display: flex; gap: .85rem; align-items: flex-start; padding: 1.3rem 1.4rem; border-radius: var(--radius-card); background: var(--color-surface); border: 1px solid var(--color-border); line-height: 1.55; font-size: .98rem; }
.lp-claim .mark { flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 1.65rem; height: 1.65rem; border-radius: 50%; font-weight: 700; font-size: .85rem; }
.lp-claim.yes .mark { background: var(--color-success-soft); color: var(--color-success-fg); }
.lp-claim.pending .mark { background: var(--color-warn-soft); color: var(--color-warn-fg); }

.lp-steps { display: grid; gap: 1.25rem; grid-template-columns: minmax(0, 1fr); align-items: stretch; list-style: none; margin: 3.5rem 0 0; padding: 0; }
.lp-step { display: flex; flex-direction: column; gap: 1rem; min-width: 0; padding: 1.75rem 1.5rem; border-radius: var(--radius-card-lg); background: var(--color-surface); border: 1px solid var(--color-border); box-shadow: var(--shadow-soft); }
.lp-numeral { display: block; font-family: var(--font-display); font-size: 6rem; line-height: .9; color: var(--color-accent-text); }
.lp-step h3 { margin: 0; font-size: 1.35rem; font-weight: 600; letter-spacing: -.01em; }
.lp-step p { margin: 0; color: var(--color-fg-muted); line-height: 1.6; font-size: 1.02rem; }
/* All three terminal boxes are one fixed height, text top-aligned, wrapped,
   never scrolled. */
.lp-snippet { margin: 0; height: 9rem; overflow: hidden; border-radius: var(--radius-card); background: var(--color-term-bg); color: var(--color-term-fg); font: .8rem/1.7 var(--font-mono); padding: .9rem 1.1rem; white-space: pre-wrap; overflow-wrap: anywhere; box-shadow: var(--shadow-soft); }
/* From 1024px: three equal cards. Grid rows are shared through subgrid, so the
   numerals, titles, copy and terminal boxes sit on the same lines across the
   row; without subgrid, the boxes are pushed to the bottom of equal-height
   cards, which puts them on the same line too. */
@media (min-width:1024px) {
  .lp-steps { grid-template-columns: repeat(3, minmax(0, 1fr)); grid-template-rows: auto auto 1fr auto; column-gap: 1.5rem; row-gap: 0; }
  .lp-step { padding: 2rem 1.75rem; }
  .lp-step .lp-snippet { margin-top: auto; }
}
@supports (grid-template-rows: subgrid) {
  @media (min-width:1024px) {
    .lp-step { display: grid; grid-row: span 4; grid-template-rows: subgrid; row-gap: 1rem; }
  }
}

.lp-split { display: grid; gap: 3rem; grid-template-columns: minmax(0, 1fr); align-items: center; }
@media (min-width:1024px) { .lp-split { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 5rem; } .lp-split.flip > :first-child { order: 2; } }
.lp-bullets { list-style: none; margin: 2rem 0 0; padding: 0; display: grid; gap: .95rem; max-width: var(--container-prose); }
.lp-bullets li { position: relative; padding-left: 2.1rem; line-height: 1.6; }
.lp-bullets li::before { content: "✓"; position: absolute; left: 0; top: .05rem; display: inline-flex; align-items: center; justify-content: center; width: 1.4rem; height: 1.4rem; border-radius: 50%; background: var(--color-success-soft); color: var(--color-success-fg); font-size: .75rem; font-weight: 700; }
.lp-env { display: grid; gap: 1rem; padding: 1.25rem; border-radius: var(--radius-card-xl); background: var(--color-surface); border: 1px solid var(--color-border); box-shadow: var(--shadow-card); min-width: 0; }
.lp-env-pane { border-radius: var(--radius-card); background: var(--color-term-bg); color: var(--color-term-fg); padding: 1.05rem 1.2rem; font: .85rem/1.85 var(--font-mono); overflow-x: auto; white-space: pre; }
.lp-env-tag { display: block; font: 600 .7rem/1.4 var(--font-sans); letter-spacing: .12em; text-transform: uppercase; color: var(--color-term-muted); margin-bottom: .4rem; }
.lp-points { display: grid; gap: 1.25rem; margin: 2rem 0 0; padding: 0; list-style: none; }
.lp-points h3 { margin: 0; font-size: 1.1rem; font-weight: 600; }
.lp-points p { margin: .3rem 0 0; color: var(--color-fg-muted); line-height: 1.6; }

.lp-illus { margin: 0; min-width: 0; }
.lp-mini { border-radius: var(--radius-card-xl); background: var(--color-surface); border: 1px solid var(--color-border); box-shadow: var(--shadow-lift); overflow: hidden; }
.lp-mini-bar { display: flex; align-items: center; gap: .45rem; padding: .8rem 1.1rem; background: var(--color-surface-2); border-bottom: 1px solid var(--color-border); }
.lp-mini-bar i { width: 9px; height: 9px; border-radius: 50%; background: color-mix(in srgb, var(--color-fg) 22%, transparent); }
.lp-mini-bar span { margin-left: .5rem; font-size: .78rem; color: var(--color-fg-muted); }
.lp-mini-row { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .75rem; padding: 1rem 1.25rem; border-bottom: 1px solid var(--color-border); }
.lp-mini-id { flex: 1 1 12rem; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: .85rem var(--font-mono); color: var(--color-fg); }
.lp-chip { border-radius: var(--radius-pill); padding: .2rem .65rem; font: 500 .75rem/1.4 var(--font-mono); }
.lp-chip.agent-chip { background: var(--color-surface-2); color: var(--color-fg-muted); }
.lp-chip.awaiting { background: var(--color-status-awaiting-bg); color: var(--color-status-awaiting-fg); }
.lp-mini-diff { margin: 1rem 1.25rem; border-radius: var(--radius-control); background: var(--color-term-bg); color: var(--color-term-fg); font: .8rem/1.85 var(--font-mono); padding: .8rem 1rem; white-space: pre; overflow: hidden; }
.lp-mini-actions { display: flex; flex-wrap: wrap; gap: .6rem; padding: 0 1.25rem 1.3rem; }
.lp-fake-btn { border-radius: var(--radius-pill); padding: .5rem 1.1rem; font: 600 .82rem/1 var(--font-sans); }
.lp-fake-btn.approve { background: var(--color-accent); color: var(--color-accent-fg); }
.lp-fake-btn.reject { border: 1px solid var(--color-danger); color: var(--color-danger-fg); }
.lp-illus figcaption { margin-top: .85rem; text-align: center; font-size: .8rem; color: var(--color-fg-subtle); }

.lp-agents { display: grid; gap: 1rem; grid-template-columns: minmax(0, 1fr); list-style: none; margin: 3rem 0 0; padding: 0; }
@media (min-width:768px) { .lp-agents { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (min-width:1280px) { .lp-agents { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
.vs-landing .agent { display: flex; flex-direction: column; justify-content: space-between; gap: 2.5rem; min-height: 11rem; padding: 1.6rem; border-radius: var(--radius-card-lg); background: var(--color-surface); border: 1px solid var(--color-border); box-shadow: var(--shadow-soft); }
.vs-landing .agent .name { font-family: var(--font-display); font-size: 2rem; line-height: 1.1; }
.vs-landing .badge { align-self: flex-start; border-radius: var(--radius-pill); padding: .35rem .8rem; font: 600 .78rem/1.3 var(--font-sans); }
.vs-landing .badge.live { background: var(--color-success-soft); color: var(--color-success-fg); }
.vs-landing .badge.docs { background: var(--color-warn-soft); color: var(--color-warn-fg); }

.lp-faq { max-width: 880px; margin: 3rem 0 0; border-top: 1px solid var(--color-border); }
.lp-faq details { border-bottom: 1px solid var(--color-border); }
.lp-faq summary { list-style: none; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 1.5rem; padding: 1.4rem .25rem; font-size: 1.15rem; font-weight: 500; }
.lp-faq summary::-webkit-details-marker { display: none; }
.lp-faq summary::after { content: "+"; flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 50%; border: 1px solid var(--color-border-strong); color: var(--color-fg-muted); font-size: 1.15rem; transition: transform .2s ease; }
.lp-faq details[open] summary::after { transform: rotate(45deg); }
.lp-faq .answer { padding: 0 .25rem 1.6rem; color: var(--color-fg-muted); line-height: 1.65; max-width: var(--container-prose); }
.lp-faq .answer ul { margin: .75rem 0 0; padding-left: 1.2rem; display: grid; gap: .6rem; }

.lp-cta { position: relative; isolation: isolate; overflow: hidden; text-align: center; }
.lp-cta::before { content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none; background: radial-gradient(55% 70% at 50% 100%, color-mix(in srgb, var(--color-accent) 30%, transparent), transparent 72%); }
.lp-cta .lp-h2 { margin-inline: auto; }

.lp-footer { border-top: 1px solid var(--color-border); padding-block: 2.5rem; }
.lp-footer-inner { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 1rem 2rem; color: var(--color-fg-muted); font-size: .92rem; }
.lp-footer-inner p { margin: 0; }

.lp-motion .lp-reveal { opacity: 0; transform: translateY(18px); transition: opacity .7s ease, transform .7s ease; }
.lp-motion .lp-reveal.is-in { opacity: 1; transform: none; }
@media (prefers-reduced-motion: reduce) {
  html:has(.vs-landing) { scroll-behavior: auto; }
  .lp-typed { animation: none; width: auto; border-right: 0; }
  .lp-motion .lp-reveal { opacity: 1; transform: none; transition: none; }
}
`;

export default function Landing() {
  const { signedIn } = useAuth();
  const rootRef = useRef(null);
  const origin = typeof window !== 'undefined' && window.location?.origin ? window.location.origin : '';
  const installCommand = buildInstallCommand(origin);

  // Gentle fade-up on scroll, only when motion is welcome and the browser can
  // observe intersections. Without either, every section is simply visible.
  useEffect(() => {
    const root = rootRef.current;
    if (!root || prefersReducedMotion() || typeof IntersectionObserver === 'undefined') return undefined;
    const targets = root.querySelectorAll('.lp-reveal');
    root.classList.add('lp-motion');
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-in');
            observer.unobserve(entry.target);
          }
        }
      },
      { rootMargin: '0px 0px -8% 0px', threshold: 0.08 },
    );
    targets.forEach((target) => observer.observe(target));
    return () => {
      observer.disconnect();
      root.classList.remove('lp-motion');
    };
  }, []);

  return (
    <div className="vs-landing" ref={rootRef} id="top">
      <style>{LANDING_CSS}</style>

      <header className="lp-header">
        <div className="lp-container lp-header-inner">
          <a href="#top" className="lp-wordmark" aria-label="VaultShield">
            <Shield />
            <span className="lp-wordmark-text">VaultShield</span>
          </a>
          <nav className="lp-sections" aria-label="Sections">
            <a href="#how">How it works</a>
            <a href="#agents">Agents</a>
            <a href="#faq">FAQ</a>
          </nav>
          <div className="lp-actions">
            <ThemeSwitch />
            {signedIn ? (
              <Link className="lp-pill" to="/dashboard">
                Open dashboard
              </Link>
            ) : (
              <>
                <Link className="lp-text-link" to="/login">
                  Log in
                </Link>
                <a className="lp-pill" href="#install">
                  Get started
                </a>
              </>
            )}
          </div>
        </div>
      </header>

      <main>
        <section className="lp-hero" aria-labelledby="hero-title">
          <div className="lp-container">
            <span className="lp-eyebrow">macOS only · for AI coding agents</span>
            <h1 id="hero-title" className="lp-display">
              Let AI agents read your code, <em>not your secrets.</em>
            </h1>
            <p className="lp-subline">
              Your agent keeps every variable name in your .env files. The values move to the macOS
              Keychain, and the files hold a harmless placeholder.
            </p>
            <InstallPill command={installCommand} testId="install-cmd" id="install" />
            <p className="lp-note">
              One line in Terminal. It installs a private, isolated copy and nothing else — no
              system software, no edits to your shell startup files.
            </p>
            <a className="lp-secondary" href="#how">
              See how it works
            </a>
            <HeroTerminal />
            <p className="lp-note">Example values only. The key shown is fake.</p>
          </div>
        </section>

        <section className="lp-slim" aria-labelledby="measured-title">
          <div className="lp-container lp-reveal">
            <div className="lp-slim-head">
              <h2 id="measured-title">Measured so far</h2>
              <p>
                No invented numbers. These are the outcomes of the spike and the live tests that
                were run; the rest is honestly marked as not yet tested.
              </p>
            </div>
            <ul className="lp-claims">
              <li className="lp-claim claim yes" data-testid={MEASURED_CLAIM_TESTID}>
                <span className="mark">✓</span>
                <span>
                  Placeholders held in the Antigravity CLI and App: an agent that got past the rules
                  read the placeholder, not the secret.
                </span>
              </li>
              <li className="lp-claim claim yes" data-testid={MEASURED_CLAIM_TESTID}>
                <span className="mark">✓</span>
                <span>The command hook blocked printenv live in Antigravity.</span>
              </li>
              <li className="lp-claim claim pending" data-testid={MEASURED_CLAIM_TESTID}>
                <span className="mark">○</span>
                <span>Claude Code, Kiro and Codex backstops are built from docs, not yet live-tested.</span>
              </li>
            </ul>
          </div>
        </section>

        <section id="how" className="lp-band" aria-labelledby="how-title">
          <div className="lp-container lp-reveal">
            <p className="lp-kicker">How it works</p>
            <h2 id="how-title" className="lp-h2">
              Three steps, all in your terminal.
            </h2>
            <ol className="lp-steps">
              <li className="lp-step">
                <span className="lp-numeral" aria-hidden="true">
                  1
                </span>
                <h3>Install</h3>
                <p>
                  Paste the one command above in Terminal. It sets up a private, isolated copy of
                  VaultShield and nothing else on your system.
                </p>
                <pre className="lp-snippet">
                  <span className="t-prompt">$</span> {installCommand}
                  {'\n'}
                  <span className="t-dim">Installed: ~/.local/bin/vaultshield</span>
                </pre>
              </li>
              <li className="lp-step">
                <span className="lp-numeral" aria-hidden="true">
                  2
                </span>
                <h3>Secure a project</h3>
                <p>
                  In your project folder, run <code className="lp-code">vaultshield secure</code>. It
                  shows what it found, asks before changing anything, and tucks the values into the
                  Keychain.
                </p>
                <pre className="lp-snippet">
                  <span className="t-prompt">$</span> vaultshield secure
                  {'\n'}
                  <span className="t-dim">Moved 1 value to the Keychain, left a placeholder.</span>
                </pre>
              </li>
              <li className="lp-step">
                <span className="lp-numeral" aria-hidden="true">
                  3
                </span>
                <h3>Ask your agent as usual</h3>
                <p>
                  Point your AI coding tool at the project. It sees{' '}
                  <code className="lp-code">API_KEY=vs_protected</code>, not your real key.
                </p>
                <pre className="lp-snippet">
                  <span className="t-dim"># what your agent now reads</span>
                  {'\n'}API_KEY=<span className="t-safe">vs_protected</span>
                </pre>
              </li>
            </ol>
          </div>
        </section>

        <section className="lp-band alt" aria-labelledby="protect-title">
          <div className="lp-container lp-split lp-reveal">
            <div>
              <p className="lp-kicker">Protect</p>
              <h2 id="protect-title" className="lp-h2">
                Your agent sees the names. Never the values.
              </h2>
              <p className="lp-prose">
                AI coding tools read your project files to help you — including{' '}
                <code className="lp-code">.env</code> files full of API keys. VaultShield moves those
                values into the macOS Keychain and leaves a harmless placeholder, so the agent still
                sees that a key exists and what it is called, never the secret itself.
              </p>
              <ul className="lp-bullets">
                <li>
                  The <strong>value</strong> of every variable VaultShield moves out of a{' '}
                  <code className="lp-code">.env</code> file. It lives in the Keychain; the file holds{' '}
                  <code className="lp-code">vs_protected</code>.
                </li>
                <li>
                  The agent can still read variable <strong>names</strong>, so it can reason about your
                  project without seeing a single secret.
                </li>
                <li>
                  You can undo it any time with <code className="lp-code">vaultshield unprotect</code>,
                  which puts the real values back.
                </li>
              </ul>
            </div>
            <div className="lp-env">
              <div className="lp-env-pane">
                <span className="lp-env-tag">before — .env on disk</span>
                STRIPE_KEY=<span className="t-leak">{FAKE_STRIPE_KEY}</span>
                {'\n'}DATABASE_URL=<span className="t-leak">postgres://u:p@localhost/db</span>
              </div>
              <div className="lp-env-pane">
                <span className="lp-env-tag">after — what the agent sees</span>
                STRIPE_KEY=<span className="t-safe">vs_protected</span>
                {'\n'}DATABASE_URL=<span className="t-safe">vs_protected</span>
              </div>
              <p className="lp-note" style={{ margin: 0 }}>
                Your .env stays in place; only the values move.
              </p>
            </div>
          </div>
        </section>

        <section className="lp-band" aria-labelledby="review-title">
          <div className="lp-container lp-split flip lp-reveal">
            <div>
              <p className="lp-kicker">Review</p>
              <h2 id="review-title" className="lp-h2">
                Review every AI change before it ships.
              </h2>
              <p className="lp-prose">
                For sandboxed runs, VaultShield records the diff, the terminal events, screenshots,
                and a branch bundle, then uploads them to a dashboard where a human reviews and
                approves — or rejects — before anything ships.
              </p>
              <ul className="lp-points">
                <li>
                  <h3>See the diff</h3>
                  <p>Every file the agent added, changed, or deleted, with its security findings alongside.</p>
                </li>
                <li>
                  <h3>Approve or reject</h3>
                  <p>Nothing reaches a real branch until a person says so.</p>
                </li>
                <li>
                  <h3>Then submit</h3>
                  <p>
                    <code className="lp-code">vaultshield submit</code> pushes the approved branch and
                    opens a pull request.
                  </p>
                </li>
              </ul>
            </div>
            <figure className="lp-illus">
              <div className="lp-mini" aria-hidden="true">
                <div className="lp-mini-bar">
                  <i />
                  <i />
                  <i />
                  <span>Runs · acme-shop</span>
                </div>
                <div className="lp-mini-row">
                  <span className="lp-mini-id">20261024-101500-a1b2c3d4</span>
                  <span className="lp-chip agent-chip">codex</span>
                  <span className="lp-chip awaiting">awaiting_review</span>
                </div>
                <div className="lp-mini-diff">
                  <span className="t-dim">src/billing.py</span>
                  {'\n'}
                  <span className="t-safe">+ key = os.environ[&quot;STRIPE_KEY&quot;]</span>
                  {'\n'}
                  <span className="t-leak">- key = &quot;sk_test_…&quot;</span>
                </div>
                <div className="lp-mini-actions">
                  <span className="lp-fake-btn approve">Approve</span>
                  <span className="lp-fake-btn reject">Reject</span>
                </div>
              </div>
              <figcaption>Illustration</figcaption>
            </figure>
          </div>
        </section>

        <section id="agents" className="lp-band alt" aria-labelledby="agents-title">
          <div className="lp-container lp-reveal">
            <p className="lp-kicker">Agents</p>
            <h2 id="agents-title" className="lp-h2">
              Honest about what we have tested.
            </h2>
            <p className="lp-prose">
              Antigravity is the one agent we have run end to end. The others follow each tool&rsquo;s
              documentation and say so until a live test says otherwise.
            </p>
            <ul className="lp-agents">
              <li className="agent">
                <span className="name">Antigravity</span>
                <span className="badge live">proven live</span>
              </li>
              <li className="agent">
                <span className="name">Claude Code</span>
                <span className="badge docs">built from docs, not live-tested</span>
              </li>
              <li className="agent">
                <span className="name">Kiro</span>
                <span className="badge docs">built from docs, not live-tested</span>
              </li>
              <li className="agent">
                <span className="name">Codex</span>
                <span className="badge docs">built from docs, not live-tested</span>
              </li>
            </ul>
          </div>
        </section>

        <section id="faq" className="lp-band" aria-labelledby="faq-title">
          <div className="lp-container lp-reveal">
            <p className="lp-kicker">FAQ</p>
            <h2 id="faq-title" className="lp-h2">
              Questions, answered plainly.
            </h2>
            <div className="lp-faq">
              <details>
                <summary>Does the AI agent ever see my real keys?</summary>
                <div className="answer">
                  No — not for the values VaultShield moved. It reads the variable names and the
                  placeholder <code className="lp-code">vs_protected</code>. The real value is in the
                  macOS Keychain.
                </div>
              </details>
              <details>
                <summary>Honest limits: what it does not protect</summary>
                <div className="answer">
                  <ul>
                    <li>
                      VaultShield protects the files it moves. A value you copied into a comment,
                      another file, git history, or your shell history is not touched.
                    </li>
                    <li>
                      A determined program running as <em>you</em> can still try indirect tricks
                      (reading the Keychain through other tools, or encoding a value so a simple filter
                      misses it). VaultShield raises the bar; it is not a sandbox.
                    </li>
                    <li>
                      Only results that were actually measured are claimed on this page. Nothing here is
                      a guarantee.
                    </li>
                  </ul>
                </div>
              </details>
              <details>
                <summary>Is this a sandbox?</summary>
                <div className="answer">
                  No. It is not a sandbox. It moves secret values out of the files an agent reads and
                  raises the bar against casual exposure, but a determined program running as you can
                  still try indirect tricks. Use the sandboxed{' '}
                  <code className="lp-code">vaultshield run</code> for stronger isolation.
                </div>
              </details>
              <details>
                <summary>Which platforms are supported?</summary>
                <div className="answer">
                  macOS only, for now — it relies on the macOS Keychain and needs Python 3.11+.
                </div>
              </details>
              <details>
                <summary>How do I remove it?</summary>
                <div className="answer">
                  Re-run the install command with <code className="lp-code">-s -- --uninstall</code>. It
                  removes the private environment and the <code className="lp-code">vaultshield</code>{' '}
                  command and leaves your secrets in place.
                </div>
              </details>
            </div>
          </div>
        </section>

        <section className="lp-band alt lp-cta" aria-labelledby="cta-title">
          <div className="lp-container lp-reveal">
            <h2 id="cta-title" className="lp-h2">
              Keep the names. Hide the values.
            </h2>
            <p className="lp-subline">One line in Terminal, on your Mac. Python 3.11 or newer.</p>
            <InstallPill command={installCommand} testId="install-cmd-cta" />
          </div>
        </section>
      </main>

      <footer className="lp-footer">
        <div className="lp-container lp-footer-inner">
          <span className="lp-wordmark">
            <Shield />
            <span>VaultShield</span>
          </span>
          <p>macOS only</p>
          <p>Early software: only claims we measured</p>
        </div>
      </footer>
    </div>
  );
}
