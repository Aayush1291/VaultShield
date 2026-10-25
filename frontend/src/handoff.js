/**
 * The hero-to-demo handoff: where the hero's decorative pieces start, the
 * path each takes, and whether the page has room for it at all.
 *
 * The pieces (a .env card, a key chip, a shield) start in the hero's right
 * margin, beside the hero terminal, and settle onto the top edge of the See it
 * work window, where their docked copies then ride the pinned stage. The
 * route is chosen so that a piece never covers text, and never another piece:
 *
 *  1. Shared descent (0 to QUEUE_END of the scroll). All three drift down a
 *     clear column to the right of every hero and See it work heading line,
 *     lagging behind the scroll (that lag is the parallax). They queue in
 *     order, env lowest, each stopping just above the one below. They all use
 *     the same easing, and the upper pieces travel further, so the gaps
 *     between them only ever shrink toward GAP: they cannot touch.
 *  2. One at a time, lowest first: a short drop to the piece's own dock row,
 *     then a slide along the window's top edge into its docked spot. Pieces
 *     dock left to right, so a sliding piece moves away from the queue and
 *     never crosses a piece that has already docked.
 *
 * `lag` is how far a piece falls behind the scroll during the descent, as a
 * fraction of it (0 would scroll with the page, 1 would stand still on screen).
 * With smoothstep easing the steepest moment is 1.5 times the average, so a
 * lag below 2/3 never makes a piece move against the scroll.
 *
 * Everything is in the handoff layer's coordinates and measured on mount,
 * resize and font load — never during a scroll frame. CSS turns the result
 * plus the scroll progress `--p` into transforms.
 */

/** Where the shared descent ends, as a fraction of the handoff scroll. */
export const QUEUE_END = 0.74;

/**
 * In queue order: env leads (lowest, docks leftmost, first). `drop` and
 * `slide` are the stretches of the handoff scroll for phase 2; a piece hands
 * over to its docked copy at the end of its slide.
 */
export const HANDOFF_PIECES = [
  { key: 'env', lag: 0.55, drop: [0.74, 0.745], slide: [0.745, 0.82] },
  { key: 'key', lag: 0.6, drop: [0.82, 0.865], slide: [0.865, 0.905] },
  { key: 'shield', lag: 0.65, drop: [0.905, 0.95], slide: [0.95, 0.965] },
];

/** Space kept between queued pieces (px). */
export const GAP = 20;
/** Space between the column and the nearest line of text (px). */
export const CLEAR = 24;
/** Space a piece keeps from the page's right edge (px). */
export const EDGE = 8;
/** Breathing room around text a path must not enter (px). */
export const MARGIN = 8;

const hits = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
const grow = (b, by) => ({ left: b.left - by, top: b.top - by, right: b.right + by, bottom: b.bottom + by });
const boxAt = (left, top, { width, height }) => ({ left, top, right: left + width, bottom: top + height });

/**
 * Plan the handoff.
 *
 * @param {{
 *   span: number,          // scroll from the page top to the window's arrival
 *   layerWidth: number,
 *   beside: Array<{left:number, top:number, right:number, bottom:number}>,
 *                          // text and boxes the column runs to the right of
 *   avoid: Array<{left:number, top:number, right:number, bottom:number}>,
 *                          // more text no path may enter (the window's title)
 *   docks: Array<{left:number, top:number, width:number, height:number}>,
 *   sizes: Array<{width:number, height:number}>,
 * }} geometry, every array in HANDOFF_PIECES order
 * @returns {{ ok: boolean, reason: string|null,
 *             pieces: Array<{left:number, top:number, dx:number, dy1:number, dy2:number}> }}
 *   `left`/`top` is where a piece starts; `dy1` the shared descent, `dy2`
 *   its own drop, `dx` its slide. `ok` false means the page is too tight for
 *   a clean route, and the docked copies should simply be shown.
 */
export function planHandoff({ span, layerWidth, beside, avoid, docks, sizes }) {
  const column = Math.max(0, ...beside.map((b) => b.right)) + CLEAR;
  // Where each piece waits after the descent: env on its own dock row, the
  // others stacked above it.
  const waits = [];
  HANDOFF_PIECES.forEach((_, i) => {
    waits[i] = i === 0 ? docks[0].top : waits[i - 1] - GAP - sizes[i].height;
  });
  const pieces = HANDOFF_PIECES.map(({ lag }, i) => {
    const left = Math.max(column, docks[i].left);
    const dy1 = lag * QUEUE_END * span;
    return { left, top: waits[i] - dy1, dx: docks[i].left - left, dy1, dy2: docks[i].top - waits[i] };
  });
  const result = (reason) => ({ ok: reason === null, reason, pieces });

  if (!(span > 0)) return result('no scroll to run on');
  const blocked = [...beside, ...avoid].map((b) => grow(b, MARGIN));
  for (let i = 0; i < pieces.length; i += 1) {
    const { left, top } = pieces[i];
    const { width, height } = sizes[i];
    const dock = docks[i];
    if (left + width > layerWidth - EDGE) return result('no room beside the text');
    if (top < 0) return result('starts above the page');
    // The queue starts in order, with at least GAP between neighbours.
    if (i > 0 && top + height + GAP > pieces[i - 1].top + 0.5) return result('queue out of order');
    const descent = { left, right: left + width, top, bottom: waits[i] + height };
    const drop = { left, right: left + width, top: waits[i], bottom: dock.top + height };
    const slide = {
      left: Math.min(dock.left, left),
      right: Math.max(dock.left, left) + width,
      top: dock.top,
      bottom: dock.top + height,
    };
    for (const leg of [descent, drop, slide]) {
      if (blocked.some((b) => hits(leg, b))) return result('path crosses text');
    }
    // Phase 2 moves one piece at a time: it must miss the pieces already
    // docked and the ones still waiting above.
    for (const leg of [drop, slide]) {
      for (let j = 0; j < pieces.length; j += 1) {
        if (j === i) continue;
        const other = j < i ? boxAt(docks[j].left, docks[j].top, docks[j]) : boxAt(pieces[j].left, waits[j], sizes[j]);
        if (hits(leg, other)) return result('pieces would collide');
      }
    }
  }
  return result(null);
}
