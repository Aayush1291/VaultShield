/**
 * Small hooks the landing page's motion decisions hang off.
 *
 * Everything degrades to "no motion": a browser that cannot answer a media
 * query (no `matchMedia`, as in jsdom) reports reduced motion, so the page is
 * complete and static by default and motion is opt-in.
 */

import { useEffect, useState } from 'react';

function query(media, fallback) {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return fallback;
  return window.matchMedia(media).matches;
}

/**
 * Whether `media` matches, kept current as it changes.
 *
 * @param {string} media e.g. '(min-width: 900px)'
 * @param {boolean} fallback the answer when matchMedia is unavailable
 */
export function useMediaQuery(media, fallback = false) {
  const [matches, setMatches] = useState(() => query(media, fallback));

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined;
    const list = window.matchMedia(media);
    const update = () => setMatches(list.matches);
    update();
    list.addEventListener?.('change', update);
    return () => list.removeEventListener?.('change', update);
  }, [media]);

  return matches;
}

/** True when motion should be avoided (also when the browser cannot say). */
export function useReducedMotion() {
  return useMediaQuery('(prefers-reduced-motion: reduce)', true);
}

/** False while the tab is hidden, so timed work can stop. */
export function usePageVisible() {
  const read = () => typeof document === 'undefined' || !document.hidden;
  const [visible, setVisible] = useState(read);

  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const update = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);

  return visible;
}
