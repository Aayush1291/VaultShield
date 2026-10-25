/**
 * The catch-all `*` route: a friendly Not Found page with a link home.
 *
 * Reached by any URL no other route matches. Because the dev server and
 * `vite preview` both fall back to `index.html` for unknown paths (Vite's SPA
 * default), a deep link to a mistyped URL lands here inside the app rather
 * than on a server 404.
 */

import { Link } from 'react-router-dom';

export default function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-10 text-center">
      <p className="font-mono text-sm font-semibold uppercase tracking-[0.2em] text-fg-subtle">
        404
      </p>
      <h1 className="mt-2 font-display text-section font-normal text-fg">Page not found</h1>
      <p className="mt-2 max-w-md text-sm text-fg-muted">
        That page does not exist. It may have moved, or the link may be wrong.
      </p>
      <Link
        to="/"
        className="mt-8 rounded-pill bg-accent px-4 py-2 text-sm font-semibold text-accent-fg shadow-soft transition-colors hover:bg-accent-hover hover:text-accent-hover-fg disabled:opacity-60"
      >
        Go home
      </Link>
    </main>
  );
}
