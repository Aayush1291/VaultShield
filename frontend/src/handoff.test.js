/**
 * Tests for planHandoff: the hero pieces' route onto the See it work window.
 * The geometry is the measured 1440x900 landing page (layer coordinates),
 * rounded; the real page is checked frame by frame in a browser by
 * spike/ui_shots.py's tooling, which jsdom cannot replace.
 */

import { CLEAR, GAP, HANDOFF_PIECES, QUEUE_END, planHandoff } from './handoff.js';

const box = (left, top, right, bottom) => ({ left, top, right, bottom });

// The landing at 1440x900: hero text and terminal, See it work's head, the
// window's title bar, and the docked badges on the window's top edge.
const page = () => ({
  span: 1611,
  layerWidth: 1440,
  beside: [
    box(330, 180, 1110, 528), // headline
    box(408, 556, 1032, 647), // subline
    box(390, 691, 1050, 747), // install pill
    box(270, 906, 1170, 1149), // hero terminal
    box(417, 1510, 1023, 1643), // See it work heading
    box(372, 1663, 1068, 1693), // lede (its line, not its box)
  ],
  avoid: [box(660, 1770, 707, 1781), box(723, 1768, 870, 1783)], // window dots, title
  docks: [
    { left: 976, top: 1711, width: 122, height: 69 },
    { left: 1107, top: 1750, width: 155, height: 30 },
    { left: 1272, top: 1732, width: 48, height: 48 },
  ],
  sizes: [
    { width: 122, height: 69 },
    { width: 155, height: 30 },
    { width: 48, height: 48 },
  ],
});

describe('planHandoff', () => {
  it('routes all three pieces down a column clear of every line of text', () => {
    const plan = planHandoff(page());
    expect(plan).toMatchObject({ ok: true, reason: null });
    // Right of the hero terminal, the widest thing on the way down.
    plan.pieces.forEach((piece) => expect(piece.left).toBeGreaterThanOrEqual(1170 + CLEAR));
  });

  it('lands every piece exactly on its docked copy', () => {
    const geometry = page();
    const plan = planHandoff(geometry);
    plan.pieces.forEach((piece, i) => {
      expect(piece.left + piece.dx).toBe(geometry.docks[i].left);
      expect(piece.top + piece.dy1 + piece.dy2).toBeCloseTo(geometry.docks[i].top, 6);
    });
  });

  it('starts them queued in order, spread out, with the upper pieces travelling further', () => {
    const { pieces } = planHandoff(page());
    const { sizes } = page();
    for (let i = 1; i < pieces.length; i += 1) {
      // Above the one below, with more than GAP between them...
      expect(pieces[i].top + sizes[i].height + GAP).toBeLessThan(pieces[i - 1].top);
      // ...and closing on it, so the gap only shrinks toward GAP.
      expect(pieces[i].dy1).toBeGreaterThan(pieces[i - 1].dy1);
    }
  });

  it('starts them in the hero, between the install pill and the foot of its terminal', () => {
    const { pieces } = planHandoff(page());
    pieces.forEach((piece) => {
      expect(piece.top).toBeGreaterThan(747);
      expect(piece.top).toBeLessThan(1149);
    });
  });

  it('never makes a piece move against the scroll (smoothstep peaks at 1.5x)', () => {
    const { pieces } = planHandoff(page());
    pieces.forEach((piece) => {
      const steepest = (1.5 * piece.dy1) / (QUEUE_END * page().span);
      expect(steepest).toBeLessThan(1);
    });
  });

  it('runs one piece at a time after the queue, in order, each landing before the next moves', () => {
    let previousEnd = QUEUE_END;
    for (const { drop, slide } of HANDOFF_PIECES) {
      expect(drop[0]).toBeGreaterThanOrEqual(previousEnd);
      expect(drop[1]).toBeGreaterThan(drop[0]);
      expect(slide[0]).toBeGreaterThanOrEqual(drop[1]);
      expect(slide[1]).toBeGreaterThan(slide[0]);
      previousEnd = slide[1];
    }
    // The last handover (plus its fade) finishes before the window arrives.
    expect(previousEnd).toBeLessThan(0.98);
  });

  it('gives up (docked copies only) when there is no room beside the text', () => {
    const geometry = page();
    geometry.layerWidth = 1260;
    expect(planHandoff(geometry)).toMatchObject({ ok: false, reason: 'no room beside the text' });
  });

  it('gives up when a slide along the window edge would cross the heading lines', () => {
    const geometry = page();
    // A lede that reaches down into the docked row.
    geometry.beside[5] = box(372, 1663, 1068, 1720);
    expect(planHandoff(geometry)).toMatchObject({ ok: false, reason: 'path crosses text' });
  });

  it('gives up when a docked badge would cover the window title', () => {
    const geometry = page();
    geometry.avoid.push(box(950, 1740, 1000, 1760));
    expect(planHandoff(geometry)).toMatchObject({ ok: false, reason: 'path crosses text' });
  });

  it('gives up with nothing to scroll', () => {
    expect(planHandoff({ ...page(), span: 0 }).ok).toBe(false);
  });
});
