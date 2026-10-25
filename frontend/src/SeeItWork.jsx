/**
 * "See it work": a scripted, four-scene illustration of what VaultShield does.
 *
 *   1. Without VaultShield   the agent reads .env and repeats the live key
 *   2. The transformation    the key lifts off, scrambles, flies into the
 *                            Keychain, and the line becomes vs_protected
 *   3. With VaultShield      read refused, cat shows the placeholder,
 *                            printenv blocked
 *   4. Your app still works  vaultshield exec prints ***
 *
 * Every line is one of the behaviours measured in the Antigravity test, with
 * invented values; nothing here claims more.
 *
 * ### How it plays
 *
 * The whole script is precomputed into frames (`DEMO_FRAMES`): each frame is
 * the full set of visible lines plus how long it stays. A tiny reducer
 * (`demoReducer`) walks the frames and one `setTimeout` per frame drives it,
 * so there is exactly one pending timer at a time and nothing to leak. It
 * starts when the stage enters the viewport, plays once through, then waits;
 * Replay starts over; the four step dots jump to a scene. It pauses while the
 * stage is hovered or focused, and while the tab is hidden.
 *
 * With reduced motion it never animates: it shows the final frame of scene 3,
 * and the dots switch between final frames instantly.
 *
 * The animated text is aria-hidden; a screen-reader paragraph describes the
 * four scenes, and the current scene is announced through the dots.
 */

import { useEffect, useReducer, useRef, useState } from 'react';

import { morphText, scramble } from './charMorph.js';
import { usePageVisible, useReducedMotion } from './motion.js';
import { FAKE_STRIPE_KEY } from './fakeValues.js';

/** The four scenes: label for the dots and copy for the side captions. */
export const DEMO_SCENES = [
  {
    title: 'Without VaultShield',
    caption: 'Ask an agent for the Stripe key: it reads .env and repeats the live value back to you.',
  },
  {
    title: 'The transformation',
    caption: 'VaultShield lifts the value out of the file, into the macOS Keychain, and leaves a placeholder.',
  },
  {
    title: 'With VaultShield',
    caption: 'Same request: the read is refused, cat shows the placeholder, and printenv is blocked.',
  },
  {
    title: 'Your app still works',
    caption: 'vaultshield exec gives your command the real value, and its output shows ***.',
  },
];

/** The scene a reduced-motion visitor sees, as a static final frame. */
export const REDUCED_MOTION_SCENE = 2;

/** The caption under the stage. */
export const DEMO_CAPTION = 'Illustration of a real test on Antigravity, with invented values.';

const PROMPT = 'You: Read my .env and tell me the Stripe key.';
const EXEC = `vaultshield exec -- sh -c 'echo "$STRIPE_KEY"'`;

// --- the script, as frames -------------------------------------------------

function cloneLines(lines) {
  return lines.map((line) => ({ ...line, segments: line.segments.map((seg) => ({ ...seg })) }));
}

/**
 * Build one scene's frames from a list of steps:
 *   { line, hold }                      append a whole line, then wait
 *   { type: [kind, prefix, text, cls], speed, hold }
 *                                       append a line and type `text` into it
 *   { set: (lines) => void, hold, extra } mutate the last lines (morph steps)
 */
function buildScene(steps) {
  const frames = [];
  let lines = [];
  let extra = {};
  const push = (delay) => frames.push({ lines: cloneLines(lines), delay, ...extra });

  for (const step of steps) {
    if (step.extra) extra = { ...extra, ...step.extra };
    if (step.line) {
      lines = [...lines, { id: lines.length, ...step.line }];
      push(step.hold ?? 300);
    } else if (step.type) {
      const [kind, prefix, text, cls = ''] = step.type;
      const line = { id: lines.length, kind, segments: [{ text: prefix }, { text: '', cls }] };
      lines = [...lines, line];
      for (const ch of text) {
        line.segments[1].text += ch;
        push(step.speed ?? 30);
      }
      frames[frames.length - 1].delay += step.hold ?? 300;
    } else if (step.set) {
      step.set(lines);
      push(step.hold ?? 300);
    }
  }
  return frames;
}

const seg = (text, cls = '') => ({ text, cls });

function sceneWithout() {
  return buildScene([
    { type: ['you', '', PROMPT], speed: 18, hold: 350 },
    { type: ['cmd', '$ ', 'cat .env'], speed: 55, hold: 250 },
    { type: ['out', 'STRIPE_KEY=', FAKE_STRIPE_KEY, 't-leak'], speed: 32, hold: 350 },
    { type: ['agent', 'Agent: Your key is ', FAKE_STRIPE_KEY, 't-leak'], speed: 16, hold: 1400 },
  ]);
}

function sceneTransformation() {
  const n = FAKE_STRIPE_KEY.length;
  const valueLine = (morph, vault) => ({
    id: 1,
    kind: 'out',
    segments: [seg('STRIPE_KEY='), { morph }],
    vault,
  });
  const steps = [
    { line: { kind: 'cmd', segments: [seg('$ cat .env')] }, hold: 200 },
    { set: (l) => l.push(valueLine({ text: FAKE_STRIPE_KEY, state: 'rest', n, cls: 't-leak' }, 'open')), hold: 450 },
    { set: (l) => { l[1] = valueLine({ text: FAKE_STRIPE_KEY, state: 'lift', n, cls: 't-leak' }, 'open'); }, hold: 380 },
  ];
  for (let f = 1; f <= 6; f += 1) {
    steps.push({ set: (l) => { l[1] = valueLine({ text: scramble(FAKE_STRIPE_KEY, f), state: 'lift', n, cls: 't-leak' }, 'open'); }, hold: 75 });
  }
  steps.push(
    { set: (l) => { l[1] = valueLine({ text: scramble(FAKE_STRIPE_KEY, 7), state: 'fly', n, cls: 't-leak' }, 'receiving'); }, hold: 1050 },
    { set: (l) => { l[1] = valueLine({ text: '', state: 'gone', n: 0, cls: '' }, 'locked'); }, hold: 300 },
  );
  for (let f = 1; f <= 8; f += 1) {
    steps.push({ set: (l) => { l[1] = valueLine({ text: morphText('', 'vs_protected', f, 8), state: 'in', n: 12, cls: 't-safe' }, 'locked'); }, hold: 55 });
  }
  steps.push({ line: { kind: 'dim', segments: [seg('# real value: macOS Keychain')] }, hold: 1600 });
  return buildScene(steps);
}

function sceneWith() {
  return buildScene([
    { line: { kind: 'you', segments: [seg(PROMPT)] }, hold: 600 },
    { line: { kind: 'agent', segments: [seg('Agent: read_file(.env)')] }, hold: 450 },
    { line: { kind: 'deny', segments: [seg('Permission denied for read_file(.env). Matches user-configured deny rule.')] }, hold: 900 },
    { type: ['cmd', '$ ', 'cat .env'], speed: 55, hold: 250 },
    { line: { kind: 'out', segments: [seg('STRIPE_KEY='), seg('vs_protected', 't-safe')] }, hold: 900 },
    { type: ['cmd', '$ ', 'printenv'], speed: 55, hold: 300 },
    { line: { kind: 'block', segments: [seg('Blocked: VaultShield: this command would print the environment, which can hold secret values.')] }, hold: 1000 },
    { type: ['agent', 'Agent: ', 'I can see STRIPE_KEY exists, but its value is protected.'], speed: 16, hold: 1600 },
  ]);
}

function sceneExec() {
  return buildScene([
    { type: ['cmd', '$ ', EXEC], speed: 30, hold: 400 },
    { line: { kind: 'out', segments: [seg('***', 't-safe')] }, hold: 2200 },
  ]);
}

/** Every scene's frames, built once. */
export const DEMO_FRAMES = [sceneWithout(), sceneTransformation(), sceneWith(), sceneExec()];

const lastFrame = (scene) => DEMO_FRAMES[scene].length - 1;

// --- the state machine -----------------------------------------------------

/** The starting state: idle at the top, or the reduced-motion final frame. */
export function initialDemoState(reduced) {
  return reduced
    ? { scene: REDUCED_MOTION_SCENE, frame: lastFrame(REDUCED_MOTION_SCENE), status: 'done' }
    : { scene: 0, frame: 0, status: 'idle' };
}

/**
 * idle --start--> playing --tick...--> done
 * jump(i): to scene i (its first frame and playing, or with reduced motion its
 * final frame, instantly). replay: from the top. reduce: to the static frame.
 */
export function demoReducer(state, action) {
  switch (action.type) {
    case 'start':
      return state.status === 'idle' ? { ...state, status: 'playing' } : state;
    case 'tick': {
      if (state.status !== 'playing') return state;
      if (state.frame < lastFrame(state.scene)) return { ...state, frame: state.frame + 1 };
      if (state.scene < DEMO_FRAMES.length - 1) return { scene: state.scene + 1, frame: 0, status: 'playing' };
      return { ...state, status: 'done' };
    }
    case 'jump': {
      const scene = Math.max(0, Math.min(DEMO_FRAMES.length - 1, action.scene));
      return action.reduced
        ? { scene, frame: lastFrame(scene), status: 'done' }
        : { scene, frame: 0, status: 'playing' };
    }
    case 'replay':
      return { scene: 0, frame: 0, status: 'playing' };
    case 'reduce':
      return initialDemoState(true);
    default:
      return state;
  }
}

/** How long the current frame stays on screen, in ms. */
export function frameDelay(state) {
  return DEMO_FRAMES[state.scene][state.frame].delay;
}

// --- rendering -------------------------------------------------------------

export function Vault({ state }) {
  return (
    <span className="d-vault" data-state={state}>
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
        <rect x="4.5" y="10" width="15" height="10" rx="2.5" stroke="currentColor" strokeWidth="1.6" />
        <path
          d={state === 'locked' ? 'M8 10V7.5a4 4 0 0 1 8 0V10' : 'M8 10V7.5a4 4 0 0 1 7.6-1.7'}
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
        />
        <circle cx="12" cy="15" r="1.4" fill="currentColor" />
      </svg>
      <span className="d-vault-label">Keychain</span>
    </span>
  );
}

export function Morph({ morph }) {
  return (
    <span className={`d-morph ${morph.cls}`} data-state={morph.state} style={{ '--n': morph.n }}>
      {Array.from(morph.text, (ch, i) => (
        // Index keys are right here: positions are what the animation tracks.
        // eslint-disable-next-line react/no-array-index-key
        <span key={i} className="d-ch" style={{ '--i': i }}>
          {ch}
        </span>
      ))}
    </span>
  );
}

function Line({ line }) {
  return (
    <div className={`d-line d-${line.kind}`}>
      {line.segments.map((s, i) =>
        s.morph ? (
          // eslint-disable-next-line react/no-array-index-key
          <Morph key={i} morph={s.morph} />
        ) : (
          // eslint-disable-next-line react/no-array-index-key
          <span key={i} className={s.cls || undefined}>
            {s.text}
          </span>
        ),
      )}
      {line.vault && <Vault state={line.vault} />}
    </div>
  );
}

/**
 * @param {{ scrollScene?: number|null }} props
 *   `scrollScene` is the scene the pinned scroll section points at. When it
 *   changes, the demo jumps there, so the captions and the stage follow the
 *   scroll; when it does not change, the demo plays on its own.
 */
export default function SeeItWork({ scrollScene = null, frameDecoration = null }) {
  const reduced = useReducedMotion();
  const visible = usePageVisible();
  const [state, dispatch] = useReducer(demoReducer, reduced, initialDemoState);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const stageRef = useRef(null);
  const paused = hovered || focused;

  // A preference change mid-visit drops straight to the static frame.
  useEffect(() => {
    if (reduced) dispatch({ type: 'reduce' });
  }, [reduced]);

  // Start once the stage is in view. Without IntersectionObserver, start now.
  useEffect(() => {
    if (reduced || state.status !== 'idle') return undefined;
    const stage = stageRef.current;
    if (typeof IntersectionObserver === 'undefined' || !stage) {
      dispatch({ type: 'start' });
      return undefined;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          dispatch({ type: 'start' });
          observer.disconnect();
        }
      },
      { threshold: 0.35 },
    );
    observer.observe(stage);
    return () => observer.disconnect();
  }, [reduced, state.status]);

  // The clock: one timeout for the current frame, only while playing,
  // unpaused, and visible. Cleared on every change and on unmount.
  useEffect(() => {
    if (state.status !== 'playing' || paused || !visible) return undefined;
    const timer = setTimeout(() => dispatch({ type: 'tick' }), frameDelay(state));
    return () => clearTimeout(timer);
  }, [state, paused, visible]);

  // Scroll-driven jumps from the pinned section (edge-triggered: only when the
  // scroll position moves to a different scene, and never on first report).
  const lastScroll = useRef(null);
  useEffect(() => {
    if (scrollScene === null || reduced) return;
    if (lastScroll.current !== null && lastScroll.current !== scrollScene) {
      dispatch({ type: 'jump', scene: scrollScene, reduced });
    }
    lastScroll.current = scrollScene;
  }, [scrollScene, reduced]);

  const frame = DEMO_FRAMES[state.scene][state.frame];

  return (
    <div className="lp-demo" data-scene={state.scene} data-status={state.status}>
      <ol className="lp-see-captions">
        {DEMO_SCENES.map((scene, i) => (
          <li key={scene.title} aria-current={i === state.scene ? 'step' : undefined}>
            <span className="lp-see-num" aria-hidden="true">
              {i + 1}
            </span>
            <span>
              <strong>{scene.title}</strong>
              <span className="lp-see-copy">{scene.caption}</span>
            </span>
          </li>
        ))}
      </ol>

      <figure className="lp-demo-figure">
        {/* Optional decoration on the window's frame (the landing page docks
            its hero pieces on the top edge). Outside the window's clipping,
            so it can sit across the edge. Decorative, aria-hidden by contract. */}
        {frameDecoration}
        <div
          ref={stageRef}
          className="lp-demo-window"
          role="group"
          aria-label="See it work demo. Hover or focus to pause."
          // Focusable so keyboard users can pause it the same way hovering does.
          tabIndex={0}
          onMouseEnter={() => setHovered(true)}
          onMouseLeave={() => setHovered(false)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
        >
          <div className="lp-demo-bar" aria-hidden="true">
            <span className="lp-dots">
              <i />
              <i />
              <i />
            </span>
            <span className="lp-demo-title">{DEMO_SCENES[state.scene].title}</span>
            {paused && state.status === 'playing' && <span className="lp-demo-paused">Paused</span>}
          </div>
          <div className="lp-demo-body" aria-hidden="true" data-testid="demo-stage">
            {frame.lines.map((line) => (
              <Line key={line.id} line={line} />
            ))}
          </div>
        </div>

        <div className="lp-demo-controls">
          <div className="lp-demo-dots" role="group" aria-label="Demo scenes">
            {DEMO_SCENES.map((scene, i) => (
              <button
                key={scene.title}
                type="button"
                className="lp-demo-dot"
                aria-label={`Scene ${i + 1}: ${scene.title}`}
                aria-pressed={i === state.scene}
                onClick={() => dispatch({ type: 'jump', scene: i, reduced })}
              />
            ))}
          </div>
          {!reduced && (
            <button type="button" className="lp-demo-replay" onClick={() => dispatch({ type: 'replay' })}>
              Replay
            </button>
          )}
        </div>
        <figcaption className="lp-demo-caption">{DEMO_CAPTION}</figcaption>
      </figure>

      <p className="sr-only">
        The illustration has four scenes. One, without VaultShield: you ask the agent for the Stripe
        key, it runs cat .env and repeats the key back to you. Two, the transformation: the key moves
        into the macOS Keychain and the line in .env becomes STRIPE_KEY=vs_protected. Three, with
        VaultShield: the same request; reading .env is refused by a deny rule, cat .env shows only
        vs_protected, and printenv is blocked; the agent replies that it can see STRIPE_KEY exists
        but its value is protected. Four, your app still works: vaultshield exec runs a command with
        the real value, and the output shows three asterisks.
      </p>
    </div>
  );
}
