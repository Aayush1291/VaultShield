/**
 * The `/` landing route.
 *
 * An editorial, terminal-first page in full-width bands, each with a centered
 * ~1240px container: sticky header, hero (serif headline, one install pill, a
 * Without/With terminal), See it work (the four-scene demo, closing with the
 * "Measured so far" claims), How it works, Protect, Review, FAQ, a closing
 * install band, and a minimal footer.
 *
 * Honesty constraints the tests pin (Landing.test.jsx):
 *  - In the FAQ's "Which AI tools are supported?" answer, Antigravity is the
 *    only tool labelled "proven live"; Claude Code, Kiro and Codex are
 *    "built from docs, not live-tested".
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

import SeeItWork, { Morph, Vault } from './SeeItWork.jsx';
import ThemeSwitch from './ThemeSwitch.jsx';
import { useAuth } from './auth.jsx';
import { morphText, scramble } from './charMorph.js';
import { FAKE_STRIPE_KEY } from './fakeValues.js';
import { usePageVisible } from './motion.js';

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
export { FAKE_STRIPE_KEY };

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
 * The hero morph, as frames: the key lifts, scrambles, flies into the
 * Keychain, and `vs_protected` resolves in its place. Each entry is
 * [morph, vault, delay ms]; `null` morph means the morph is finished.
 */
const N = FAKE_STRIPE_KEY.length;
export const HERO_MORPH_FRAMES = [
  [{ text: FAKE_STRIPE_KEY, state: 'lift', n: N, cls: 't-leak' }, 'open', 260],
  ...[1, 2, 3, 4, 5].map((f) => [{ text: scramble(FAKE_STRIPE_KEY, f), state: 'lift', n: N, cls: 't-leak' }, 'open', 70]),
  [{ text: scramble(FAKE_STRIPE_KEY, 6), state: 'fly', n: N, cls: 't-leak' }, 'receiving', 760],
  [{ text: '', state: 'gone', n: 0, cls: '' }, 'locked', 220],
  ...[1, 2, 3, 4, 5, 6, 7, 8].map((f) => [{ text: morphText('', 'vs_protected', f, 8), state: 'in', n: 12, cls: 't-safe' }, 'locked', 50]),
];

/**
 * The hero terminal: what an agent reads from `.env`, without and with
 * VaultShield. Starts on "Without". Once, after `AUTO_ADVANCE_MS`, it plays
 * the character morph into "With" — unless the visitor has used the toggle or
 * prefers reduced motion. The toggle (two `aria-pressed` buttons) switches
 * instantly, so it works by keyboard and needs no animation to make sense.
 * The morphing characters are aria-hidden; the live region only ever
 * announces the settled line.
 */
function HeroTerminal() {
  const [view, setView] = useState('without');
  const [morphFrame, setMorphFrame] = useState(null); // index into HERO_MORPH_FRAMES
  const interacted = useRef(false);
  const visible = usePageVisible();

  useEffect(() => {
    if (prefersReducedMotion()) return undefined;
    const timer = setTimeout(() => {
      if (interacted.current) return;
      setView('with');
      setMorphFrame(0);
    }, AUTO_ADVANCE_MS);
    return () => clearTimeout(timer);
  }, []);

  // Step the morph one frame per timeout; pause while the tab is hidden.
  useEffect(() => {
    if (morphFrame === null || !visible) return undefined;
    const timer = setTimeout(
      () => setMorphFrame((f) => (f === null || f + 1 >= HERO_MORPH_FRAMES.length ? null : f + 1)),
      HERO_MORPH_FRAMES[morphFrame][2],
    );
    return () => clearTimeout(timer);
  }, [morphFrame, visible]);

  function choose(next) {
    interacted.current = true;
    setMorphFrame(null);
    setView(next);
  }

  const protectedView = view === 'with';
  const morphing = protectedView && morphFrame !== null;
  const [morph, vault] = morphing ? HERO_MORPH_FRAMES[morphFrame] : [null, null];

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
        {morphing ? (
          <>
            STRIPE_KEY=
            <span aria-hidden="true">
              <Morph morph={morph} />
              <Vault state={vault} />
            </span>
            {'\n'}
            <span className="t-dim">&nbsp;</span>
          </>
        ) : protectedView ? (
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
/* Every section opens with a centered label and heading; the content below
   uses the full ~1240px container. */
.lp-head { text-align: center; max-width: 52rem; margin-inline: auto; }
.lp-kicker { font: 600 .78rem/1.4 var(--font-sans); letter-spacing: .14em; text-transform: uppercase; color: var(--color-fg-muted); margin: 0; }
.lp-h2 { font-family: var(--font-display); font-weight: 400; font-size: var(--text-section); line-height: 1.05; letter-spacing: -.015em; margin: .9rem auto 0; max-width: 20ch; text-wrap: balance; }
.lp-lede { font-size: var(--text-body-lg); line-height: 1.6; color: var(--color-fg-muted); max-width: 60ch; margin: 1.25rem auto 0; text-wrap: pretty; }
.lp-prose { font-size: var(--text-body-lg); line-height: 1.6; color: var(--color-fg-muted); max-width: var(--container-prose); margin: 1.5rem 0 0; text-wrap: pretty; }

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

.lp-slim-head { text-align: center; margin: 0 auto 2rem; max-width: 52rem; }
.lp-slim-head h3 { font-family: var(--font-display); font-weight: 400; font-size: clamp(1.75rem, 1.4rem + 1vw, 2.25rem); margin: 0; }
.lp-slim-head p { margin: .6rem auto 0; color: var(--color-fg-muted); font-size: .95rem; max-width: 60ch; }
/* See it work: captions beside a framed stage. */
.lp-demo { display: grid; gap: 2.5rem; grid-template-columns: minmax(0, 1fr); align-items: center; margin-top: clamp(2.5rem, 2rem + 2vw, 4rem); }
@media (min-width:900px) { .lp-demo { grid-template-columns: minmax(0, .82fr) minmax(0, 1.18fr); gap: 3.5rem; } }
.lp-see-captions { list-style: none; margin: 0; padding: 0; display: grid; gap: .4rem; }
.lp-see-captions li { display: flex; gap: 1rem; align-items: flex-start; padding: 1rem 1.1rem; border-radius: var(--radius-card); border: 1px solid transparent; transition: background-color .3s ease, border-color .3s ease; }
.lp-see-captions li[aria-current="step"] { background: var(--color-surface); border-color: var(--color-border); box-shadow: var(--shadow-soft); }
.lp-see-num { flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 2.1rem; height: 2.1rem; border-radius: 50%; border: 1px solid var(--color-border-strong); color: var(--color-fg-muted); font: 1.15rem/1 var(--font-display); transition: background-color .3s, color .3s; }
.lp-see-captions li[aria-current="step"] .lp-see-num { background: var(--color-accent); border-color: transparent; color: var(--color-accent-fg); }
.lp-see-captions strong { display: block; font-size: 1.08rem; font-weight: 600; color: var(--color-fg-muted); }
.lp-see-captions li[aria-current="step"] strong { color: var(--color-fg); }
.lp-see-copy { display: block; margin-top: .25rem; color: var(--color-fg-subtle); font-size: .97rem; line-height: 1.55; }
.lp-demo-figure { margin: 0; min-width: 0; }
.lp-demo-window { border-radius: 24px; background: var(--color-term-bg); color: var(--color-term-fg); border: 1px solid var(--color-term-border); box-shadow: var(--shadow-lift); overflow: hidden; }
.lp-demo-window:focus-visible { outline: 2px solid var(--color-ring); outline-offset: 3px; }
.lp-demo-bar { display: flex; align-items: center; gap: 1rem; padding: .85rem 1.1rem; border-bottom: 1px solid var(--color-term-border); }
.lp-demo-title { font: .8rem var(--font-mono); color: var(--color-term-muted); }
.lp-demo-paused { margin-left: auto; border: 1px solid var(--color-term-border); border-radius: var(--radius-pill); padding: .25rem .6rem; font: 600 .68rem/1 var(--font-sans); letter-spacing: .1em; text-transform: uppercase; color: var(--color-term-muted); }
.lp-demo-body { display: flex; flex-direction: column; gap: .3rem; height: 25rem; padding: 1.25rem 1.4rem; overflow: hidden; font: .86rem/1.7 var(--font-mono); }
@media (max-width: 599px) { .lp-demo-body { height: 31rem; padding: 1rem; font-size: .78rem; } }
.d-line { white-space: pre-wrap; overflow-wrap: anywhere; }
.d-you { color: var(--color-term-accent); }
.d-agent, .d-cmd, .d-out { color: var(--color-term-fg); }
.d-dim { color: var(--color-term-muted); }
.d-deny { color: var(--color-term-danger); }
.d-block { color: var(--color-term-danger); font-weight: 600; }
.d-line .t-leak { color: var(--color-term-danger); background: rgba(242, 160, 143, .12); border-radius: 4px; }
.lp-demo-controls { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-top: 1rem; }
.lp-demo-dots { display: flex; gap: .35rem; }
.lp-demo-dot { position: relative; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 50%; background: transparent; cursor: pointer; }
.lp-demo-dot::before { content: ""; position: absolute; inset: 9px; border-radius: 50%; background: var(--color-border-strong); transition: transform .2s ease, background-color .2s ease; }
.lp-demo-dot[aria-pressed="true"]::before { background: var(--color-accent-text); transform: scale(1.35); }
.lp-demo-replay { border-radius: var(--radius-pill); border: 1px solid var(--color-border-strong); background: var(--color-surface); color: var(--color-fg); font: 500 .9rem/1 var(--font-sans); padding: .6rem 1.1rem; cursor: pointer; }
.lp-demo-replay:hover { background: var(--color-surface-2); }
.lp-demo-caption { margin-top: .75rem; font-size: .85rem; color: var(--color-fg-subtle); }

/* The character morph: characters lift, scramble, fly into the Keychain
   (to the right, measured in ch, so no layout reads), and the placeholder
   resolves in. Transform and opacity only. */
.d-morph { display: inline-block; }
.d-ch { display: inline-block; white-space: pre; transition: transform .45s cubic-bezier(.2, .7, .2, 1), opacity .45s ease; transition-delay: calc(var(--i) * 14ms); }
.d-morph[data-state="lift"] .d-ch { transform: translateY(-.35em); }
.d-morph[data-state="fly"] .d-ch { transform: translate(calc((var(--n) - var(--i)) * 1ch + 2ch), -.15em) scale(.3); opacity: 0; transition-duration: .7s; }
.d-morph[data-state="in"] .d-ch { animation: d-in .28s ease both; }
@keyframes d-in { from { opacity: 0; transform: translateY(.35em); } to { opacity: 1; transform: none; } }
.d-vault { display: inline-flex; align-items: center; gap: .35rem; margin-left: 1ch; padding: .05rem .5rem; vertical-align: middle; border: 1px solid var(--color-term-border); border-radius: var(--radius-pill); color: var(--color-term-accent); font: 600 .68rem/1.4 var(--font-sans); transition: transform .3s ease, background-color .3s ease, color .3s ease; }
.d-vault svg { width: 1rem; height: 1rem; }
.d-vault[data-state="receiving"] { transform: scale(1.12); background: rgba(201, 188, 248, .14); }
.d-vault[data-state="locked"] { color: var(--color-term-ok); }

/* Measured so far, now the closing note of See it work. */
.lp-measured { margin-top: clamp(3rem, 2.5rem + 2vw, 5rem); padding-top: clamp(2.5rem, 2rem + 2vw, 3.5rem); border-top: 1px solid var(--color-border); }
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

.lp-split { display: grid; gap: 3rem; grid-template-columns: minmax(0, 1fr); align-items: center; margin-top: clamp(2.5rem, 2rem + 2vw, 4rem); }
.lp-split .lp-prose:first-child { margin-top: 0; }
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

/* The FAQ: heading and list centered as one block; the list is up to
   1040px wide (the container's full width below that). */
.lp-faq { max-width: 1040px; margin: clamp(2.5rem, 2rem + 2vw, 4rem) auto 0; border-top: 1px solid var(--color-border); text-align: left; }
.lp-faq details { border-bottom: 1px solid var(--color-border); }
.lp-faq summary { list-style: none; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 2rem; padding: 1.75rem .5rem; font-size: clamp(1.25rem, 1.1rem + .45vw, 1.5rem); line-height: 1.3; font-weight: 500; letter-spacing: -.01em; }
.lp-faq summary:hover { color: var(--color-accent-text); }
.lp-faq summary::-webkit-details-marker { display: none; }
.lp-faq summary::after { content: "+"; flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 2.5rem; height: 2.5rem; border-radius: 50%; border: 1px solid var(--color-border-strong); color: var(--color-fg-muted); font-size: 1.4rem; font-weight: 400; transition: transform .2s ease; }
.lp-faq details[open] summary::after { transform: rotate(45deg); }
.lp-faq .answer { padding: 0 .5rem 2rem; color: var(--color-fg-muted); font-size: 1.125rem; line-height: 1.6; max-width: var(--container-prose); }
.lp-faq .answer p { margin: 0; }
.lp-faq .answer p + p, .lp-faq .answer ul + p { margin-top: .75rem; }
.lp-faq .answer ul { margin: .75rem 0 0; padding-left: 1.2rem; display: grid; gap: .6rem; }
.lp-faq .answer ul.lp-tools { list-style: none; padding: 0; margin: 1rem 0; gap: .5rem; }
.lp-tools li { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: .5rem 1rem; padding: .6rem .9rem; border-radius: var(--radius-control); background: var(--color-surface); border: 1px solid var(--color-border); }
.lp-tool { color: var(--color-fg); font-weight: 600; }
.lp-tag { border-radius: var(--radius-pill); padding: .25rem .75rem; font: 600 .8rem/1.3 var(--font-sans); }
.lp-tag.live { background: var(--color-success-soft); color: var(--color-success-fg); }
.lp-tag.docs { background: var(--color-warn-soft); color: var(--color-warn-fg); }

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

        <section id="see" className="lp-band alt" aria-labelledby="see-title">
          <div className="lp-container lp-reveal">
            <div className="lp-head">
              <p className="lp-kicker">See it work</p>
              <h2 id="see-title" className="lp-h2">
                Watch a secret turn into a placeholder.
              </h2>
              <p className="lp-lede">
                The same request, before and after VaultShield, replayed from a real test.
              </p>
            </div>
            <SeeItWork />
            <div className="lp-measured">
              <div className="lp-slim-head">
                <h3 id="measured-title">Measured so far</h3>
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
          </div>
        </section>

        <section id="how" className="lp-band" aria-labelledby="how-title">
          <div className="lp-container lp-reveal">
            <div className="lp-head">
              <p className="lp-kicker">How it works</p>
              <h2 id="how-title" className="lp-h2">
                Three steps, all in your terminal.
              </h2>
            </div>
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
          <div className="lp-container lp-reveal">
            <div className="lp-head">
              <p className="lp-kicker">Protect</p>
              <h2 id="protect-title" className="lp-h2">
                Your agent sees the names. Never the values.
              </h2>
            </div>
            <div className="lp-split">
            <div>
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
          </div>
        </section>

        <section className="lp-band" aria-labelledby="review-title">
          <div className="lp-container lp-reveal">
            <div className="lp-head">
              <p className="lp-kicker">Review</p>
              <h2 id="review-title" className="lp-h2">
                Review every AI change before it ships.
              </h2>
            </div>
            <div className="lp-split flip">
            <div>
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
          </div>
        </section>


        <section id="faq" className="lp-band alt" aria-labelledby="faq-title">
          <div className="lp-container lp-reveal">
            <div className="lp-head">
              <p className="lp-kicker">FAQ</p>
              <h2 id="faq-title" className="lp-h2">
                Questions, answered plainly.
              </h2>
            </div>
            <div className="lp-faq">
              <details>
                <summary>Does the AI agent ever see my real keys?</summary>
                <div className="answer">
                  <p>No — not for the values VaultShield moved. It reads the variable names and the
                  placeholder <code className="lp-code">vs_protected</code>. The real value is in the
                  macOS Keychain.</p>
                </div>
              </details>
              <details>
                <summary>Which AI tools are supported?</summary>
                <div className="answer">
                  <p>
                    Antigravity is the one tool we have tested end to end. For the others, the
                    protection is written from each tool&rsquo;s documentation and has not been tested
                    live yet.
                  </p>
                  <ul className="lp-tools">
                    <li>
                      <span className="lp-tool">Antigravity</span>
                      <span className="lp-tag live">proven live</span>
                    </li>
                    <li>
                      <span className="lp-tool">Claude Code</span>
                      <span className="lp-tag docs">built from docs, not live-tested</span>
                    </li>
                    <li>
                      <span className="lp-tool">Kiro</span>
                      <span className="lp-tag docs">built from docs, not live-tested</span>
                    </li>
                    <li>
                      <span className="lp-tool">Codex</span>
                      <span className="lp-tag docs">built from docs, not live-tested</span>
                    </li>
                  </ul>
                  <p>All of them on macOS only.</p>
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
                  <p>No. It is not a sandbox. It moves secret values out of the files an agent reads and
                  raises the bar against casual exposure, but a determined program running as you can
                  still try indirect tricks. Use the sandboxed{' '}
                  <code className="lp-code">vaultshield run</code> for stronger isolation.</p>
                </div>
              </details>
              <details>
                <summary>Which platforms are supported?</summary>
                <div className="answer">
                  <p>macOS only, for now — it relies on the macOS Keychain and needs Python 3.11+.</p>
                </div>
              </details>
              <details>
                <summary>How do I remove it?</summary>
                <div className="answer">
                  <p>Re-run the install command with <code className="lp-code">-s -- --uninstall</code>. It
                  removes the private environment and the <code className="lp-code">vaultshield</code>{' '}
                  command and leaves your secrets in place.</p>
                </div>
              </details>
            </div>
          </div>
        </section>

        <section className="lp-band lp-cta" aria-labelledby="cta-title">
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
