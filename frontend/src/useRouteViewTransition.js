/**
 * `useRouteViewTransition()`: a 180ms cross-fade between routes, using the
 * browser's View Transitions API directly. The app keeps `BrowserRouter`;
 * react-router's own `viewTransition` option only works with its data routers.
 *
 * How it works:
 *  1. A capture-phase click listener on the document notices an in-app link
 *     click (same origin, a different path or query, plain left click, no
 *     target or download). It runs before react-router's `<Link>` handler.
 *  2. It cancels the click (`preventDefault`), which `<Link>` honours, and
 *     calls `document.startViewTransition()`. The browser snapshots the
 *     current page.
 *  3. Inside the update callback it performs the navigation itself with
 *     `navigate()`, and returns a promise that resolves once React has
 *     committed the new location. `BrowserRouter` applies navigations inside
 *     `startTransition`, so the new page is not on screen synchronously; the
 *     layout effect on `location` is the moment it is.
 *  4. The browser cross-fades from the snapshot to the new page (180ms, the
 *     `::view-transition-*` rules in index.css).
 *
 * A 1s cap resolves the promise regardless, so a transition can never hang
 * the page. Programmatic navigations (sign-in redirects, Delete project),
 * Back/Forward, and same-page `#anchor` links are left alone. It does nothing
 * when the API is missing or the visitor prefers reduced motion; then links
 * work exactly as before.
 */

import { useEffect, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

export const MAX_WAIT_MS = 1000;

/**
 * The in-app path to navigate to if `event` on `anchor` is a plain left-click
 * page change, otherwise `null`.
 *
 * @param {MouseEvent} event
 * @param {HTMLAnchorElement|null} anchor
 * @param {Location} current
 * @returns {string|null}
 */
export function inAppTarget(event, anchor, current = window.location) {
  if (!anchor || event.defaultPrevented || event.button !== 0) return null;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return null;
  if (anchor.target && anchor.target !== '_self') return null;
  if (anchor.hasAttribute('download')) return null;
  const href = anchor.getAttribute('href');
  if (!href || href.startsWith('#')) return null;
  let url;
  try {
    url = new URL(anchor.href, current.href);
  } catch {
    return null;
  }
  if (url.origin !== current.origin) return null;
  // Same path and query: a hash jump or a no-op, not a page change.
  if (url.pathname === current.pathname && url.search === current.search) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

function reducedMotion() {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export default function useRouteViewTransition() {
  const location = useLocation();
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const pending = useRef(null);

  // The new route has committed: let the pending transition finish.
  useLayoutEffect(() => {
    pending.current?.();
  }, [location]);

  useEffect(() => {
    if (typeof document === 'undefined' || typeof document.startViewTransition !== 'function') return undefined;

    const onClick = (event) => {
      if (pending.current || reducedMotion()) return;
      const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
      const to = inAppTarget(event, anchor);
      if (to === null) return;

      event.preventDefault();
      document.startViewTransition(
        () =>
          new Promise((resolve) => {
            let timer = 0;
            const finish = () => {
              clearTimeout(timer);
              pending.current = null;
              resolve();
            };
            pending.current = finish;
            timer = setTimeout(finish, MAX_WAIT_MS);
            navigateRef.current(to);
          }),
      );
    };

    // Capture phase, so this runs before react-router's own click handler.
    document.addEventListener('click', onClick, true);
    return () => {
      document.removeEventListener('click', onClick, true);
      pending.current?.();
    };
  }, []);
}
