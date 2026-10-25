/**
 * WCAG contrast check for the theme tokens in `src/index.css`.
 *
 * The tokens are parsed straight out of the stylesheet (no copy of the palette
 * lives here), then every foreground/background pair the app actually renders
 * is measured with the WCAG 2.x relative-luminance formula:
 *
 *  - text pairs must reach 4.5:1 (WCAG 1.4.3, normal text);
 *  - UI boundaries a user must perceive to operate a control (input and
 *    outline-button borders, the focus ring, the danger outline) must reach
 *    3:1 (WCAG 1.4.11).
 *
 * The decorative hairline `--color-border` is intentionally excluded: it only
 * separates rows and outlines cards, which 1.4.11 does not require.
 *
 * `LOWEST` at the bottom logs the weakest pair per theme, so the numbers in
 * the hand-off report come from this run rather than from a hand calculation.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const CSS = readFileSync(join(__dirname, 'index.css'), 'utf8');

/** The body of the first `{ ... }` block after `marker`, braces balanced. */
function blockAfter(marker) {
  const start = CSS.indexOf(marker);
  if (start === -1) throw new Error(`marker not found: ${marker}`);
  const open = CSS.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < CSS.length; i += 1) {
    if (CSS[i] === '{') depth += 1;
    if (CSS[i] === '}') {
      depth -= 1;
      if (depth === 0) return CSS.slice(open + 1, i);
    }
  }
  throw new Error(`unbalanced block after ${marker}`);
}

/** `--color-x: #rrggbb;` declarations in a block, as `{ x: '#rrggbb' }`. */
function hexTokens(block) {
  const out = {};
  for (const [, name, value] of block.matchAll(/--color-([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    out[name] = value.toLowerCase();
  }
  return out;
}

function luminance(hex) {
  const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r, g, b] = channels.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// Markers include the opening brace so prose in comments cannot match them.
const PAPER_BLOCK = '@theme static {';
const INK_BLOCK = ":root[data-theme='dark'] {";
const INK_SYSTEM_BLOCK = ":root:not([data-theme='light']) {";

const paper = hexTokens(blockAfter(PAPER_BLOCK));
const inkOverrides = hexTokens(blockAfter(INK_BLOCK));
const inkSystem = hexTokens(blockAfter(INK_SYSTEM_BLOCK));
// Ink only overrides what differs; anything it does not restate (the terminal
// text colors) is inherited from Paper.
const ink = { ...paper, ...inkOverrides };

const SURFACES = ['canvas', 'surface', 'surface-2'];
const TEXT_ON_SURFACES = [
  'fg',
  'fg-muted',
  'fg-subtle',
  'accent-text',
  'success-fg',
  'warn-fg',
  'danger-fg',
];
const STATUSES = ['running', 'awaiting', 'approved', 'rejected', 'submitted', 'failed', 'unknown'];

/** Every pair the app renders: [label, foreground token, background token, minimum]. */
function pairs() {
  const list = [];
  for (const bg of SURFACES) {
    for (const fg of TEXT_ON_SURFACES) list.push([`${fg} on ${bg}`, fg, bg, 4.5]);
    // UI boundaries.
    list.push([`border-strong on ${bg} (UI)`, 'border-strong', bg, 3]);
    list.push([`ring on ${bg} (UI)`, 'ring', bg, 3]);
    list.push([`danger outline on ${bg} (UI)`, 'danger', bg, 3]);
  }
  list.push(
    // Lavender pill buttons and their hover state.
    ['accent-fg on accent (pill)', 'accent-fg', 'accent', 4.5],
    ['accent-hover-fg on accent-hover (pill hover)', 'accent-hover-fg', 'accent-hover', 4.5],
    // Selected sidebar project: ink text on the soft lavender pill.
    ['fg on accent-soft (selected pill)', 'fg', 'accent-soft', 4.5],
    ['accent-text on accent-soft', 'accent-text', 'accent-soft', 4.5],
    // Feedback panels.
    ['danger-on on danger (danger button)', 'danger-on', 'danger', 4.5],
    ['danger-on on danger-hover', 'danger-on', 'danger-hover', 4.5],
    ['danger-fg on danger-soft (alert)', 'danger-fg', 'danger-soft', 4.5],
    ['success-fg on success-soft', 'success-fg', 'success-soft', 4.5],
    ['warn-fg on warn-soft', 'warn-fg', 'warn-soft', 4.5],
  );
  for (const s of STATUSES) {
    list.push([`status ${s} chip`, `status-${s}-fg`, `status-${s}-bg`, 4.5]);
  }
  // Terminal artifacts (landing hero, steps, auth panel).
  for (const fg of ['term-fg', 'term-muted', 'term-danger', 'term-ok', 'term-accent']) {
    list.push([`${fg} on term-bg`, fg, 'term-bg', 4.5]);
  }
  return list;
}

const LOWEST = {};

describe.each([
  ['paper', paper],
  ['ink', ink],
])('%s theme', (theme, tokens) => {
  const measured = pairs().map(([label, fg, bg, min]) => {
    if (!tokens[fg] || !tokens[bg]) throw new Error(`${theme}: missing token ${fg} or ${bg}`);
    return { label, ratio: contrastRatio(tokens[fg], tokens[bg]), min };
  });

  it.each(measured.map((m) => [m.label, m]))('%s meets its minimum', (_label, m) => {
    expect(m.ratio).toBeGreaterThanOrEqual(m.min);
  });

  afterAll(() => {
    const text = measured.filter((m) => m.min === 4.5).sort((a, b) => a.ratio - b.ratio)[0];
    const ui = measured.filter((m) => m.min === 3).sort((a, b) => a.ratio - b.ratio)[0];
    LOWEST[theme] = { text, ui };
  });
});

it('defines the dark theme identically for data-theme="dark" and for the system preference', () => {
  const declarations = (marker) =>
    blockAfter(marker)
      .split(';')
      .map((line) => line.trim())
      .filter(Boolean);
  expect(declarations(INK_SYSTEM_BLOCK)).toEqual(declarations(INK_BLOCK));
  expect(Object.keys(inkSystem).length).toBeGreaterThan(20);
});

it('uses a warm ink tint for shadows, never pure black', () => {
  const shadows = CSS.match(/--shadow-[a-z]+:[^;]+;/g) ?? [];
  expect(shadows.length).toBeGreaterThan(0);
  for (const shadow of shadows) {
    expect(shadow).not.toMatch(/rgba\(0,\s*0,\s*0/);
    expect(shadow).not.toMatch(/#000\b/);
  }
});

afterAll(() => {
  const fmt = (m) => `${m.label} = ${m.ratio.toFixed(2)}:1`;
  // eslint-disable-next-line no-console
  console.log(
    Object.entries(LOWEST)
      .map(([theme, { text, ui }]) => `${theme}: lowest text ${fmt(text)}; lowest UI ${fmt(ui)}`)
      .join('\n'),
  );
});
