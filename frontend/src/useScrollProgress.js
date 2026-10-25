/**
 * `useScrollProgress(ref)`: scroll-linked progress as a CSS variable.
 *
 * Sets `--p` on the element to a number from 0 to 1 describing where it is in
 * its scroll journey: 0 when its top reaches the bottom of the viewport, 1 when
 * its bottom leaves the top (`mode: 'travel'`, the default); or, for a tall
 * section with a sticky child, 0 when its top hits the top of the viewport and
 * 1 when its bottom reaches the bottom (`mode: 'pin'`). CSS turns `--p` into
 * transforms and opacity, so motion never touches layout properties.
 *
 * The cost model, deliberately small:
 *  - one passive `scroll` listener and one `resize` listener;
 *  - each event at most schedules ONE requestAnimationFrame; the frame does a
 *    single `getBoundingClientRect()` read, then a single style write — no
 *    reads after writes, no reads in a loop;
 *  - nothing runs while the tab is hidden (the frame is skipped, and the
 *    listeners re-measure on `visibilitychange`);
 *  - everything is removed on unmount.
 *
 * `enabled: false` (reduced motion, narrow screens) attaches nothing and
 * leaves `--p` at `restValue`, so CSS shows the final, static state.
 *
 * `onProgress(p)` is optional, for a component that needs the number in JS
 * (the pinned demo picks a scene from it); it is called from the same frame.
 */

import { useEffect, useRef } from 'react';

/** Clamp to [0, 1]. */
export const clamp01 = (value) => Math.min(1, Math.max(0, value));

/**
 * Where a handoff target counts as "arrived": its top this far below the
 * top of the viewport (the sticky header is 68px; leave room above it).
 */
export const HANDOFF_REST_TOP = 120;

/**
 * Progress for an element rect against a viewport height.
 *
 *  - travel:  0 when the element's top reaches the viewport bottom, 1 when
 *             its bottom leaves the viewport top.
 *  - pin:     0 when the element's top reaches the viewport top, 1 when its
 *             bottom reaches the viewport bottom (for a sticky child).
 *  - handoff: 0 at the top of the page, 1 when `targetTop` (the target's
 *             top in document coordinates) has scrolled up to `restTop`
 *             pixels below the viewport top. `rect` is unused; `scrollY`
 *             drives it.
 *
 * @param {{ top: number, height: number }} rect
 * @param {number} viewport window.innerHeight
 * @param {'travel'|'pin'|'handoff'} mode
 * @param {{ scrollY?: number, targetTop?: number, restTop?: number }} [extra]
 */
export function progressFor(
  rect,
  viewport,
  mode = 'travel',
  { scrollY = 0, targetTop = 0, restTop = HANDOFF_REST_TOP } = {},
) {
  if (mode === 'handoff') {
    const span = targetTop - restTop;
    if (span <= 0) return 1;
    return clamp01(scrollY / span);
  }
  if (mode === 'pin') {
    const span = rect.height - viewport;
    if (span <= 0) return rect.top <= 0 ? 1 : 0;
    return clamp01(-rect.top / span);
  }
  const span = viewport + rect.height;
  if (span <= 0) return 0;
  return clamp01((viewport - rect.top) / span);
}

/**
 * @param {{ current: HTMLElement|null }} ref
 * @param {{ enabled?: boolean, mode?: 'travel'|'pin'|'handoff',
 *           restValue?: number, target?: string,
 *           onProgress?: (p: number) => void }} [options]
 *   `target` (handoff mode) is a selector for the element whose arrival
 *   means 1. Its document position is measured on attach and on resize only,
 *   so the scroll frame itself reads nothing but scrollY.
 *   `restSelector` (handoff mode, optional) names a sticky element whose
 *   computed `top` is where the target comes to rest (e.g. the pinned
 *   stage); arrival is then the moment it pins. Defaults to
 *   HANDOFF_REST_TOP.
 */
export default function useScrollProgress(
  ref,
  { enabled = true, mode = 'travel', restValue = 1, target = null, restSelector = null, onProgress } = {},
) {
  // Kept in a ref so a new callback each render does not re-attach listeners.
  const callback = useRef(onProgress);
  callback.current = onProgress;

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;

    if (!enabled || typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
      el.style.setProperty('--p', String(restValue));
      return undefined;
    }

    let frame = 0;
    let last = null;
    // Handoff mode: the target's document top, measured on attach and on
    // resize, never during a scroll frame.
    let targetTop = 0;
    let restTop = HANDOFF_REST_TOP;
    const measureTarget = () => {
      const node = target ? document.querySelector(target) : null;
      const rest = restSelector ? document.querySelector(restSelector) : null;
      // The sticky element's resolved `top` is a px value once laid out.
      const sticky = rest ? parseFloat(getComputedStyle(rest).top) : NaN;
      restTop = Number.isFinite(sticky) ? sticky : HANDOFF_REST_TOP;
      // While pinned, the target's live rect is not its document position;
      // take it from its sticky ancestor's static place instead, via the
      // offset chain (layout, not the pinned paint position).
      let top = 0;
      for (let el = node; el; el = el.offsetParent) top += el.offsetTop;
      targetTop = node ? top : 0;
    };

    const measure = () => {
      frame = 0;
      if (typeof document !== 'undefined' && document.hidden) return;
      // One read...
      const p =
        mode === 'handoff'
          ? progressFor(null, window.innerHeight, mode, { scrollY: window.scrollY, targetTop, restTop })
          : progressFor(el.getBoundingClientRect(), window.innerHeight, mode);
      if (p === last) return;
      last = p;
      // ...then one write.
      el.style.setProperty('--p', p.toFixed(4));
      callback.current?.(p);
    };

    const schedule = () => {
      if (frame === 0) frame = window.requestAnimationFrame(measure);
    };
    const onResize = () => {
      if (mode === 'handoff') measureTarget();
      schedule();
    };
    const onVisibility = () => {
      if (!document.hidden) schedule();
    };

    if (mode === 'handoff') measureTarget();
    schedule();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', onResize, { passive: true });
    document.addEventListener('visibilitychange', onVisibility);
    // Webfonts change layout after first paint; re-measure once they load.
    document.fonts?.ready?.then(onResize);

    return () => {
      if (frame !== 0) window.cancelAnimationFrame(frame);
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [ref, enabled, mode, restValue, target, restSelector]);
}
