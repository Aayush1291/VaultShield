/**
 * Shared frame for the auth pages (/login, /signup).
 *
 * From 1024px: two columns — an editorial panel on the left (serif tagline,
 * a small dark terminal artifact, a warm gradient wash) and the form card on
 * the right. Below 1024px: one column, with a compact panel (wordmark and
 * tagline only) above the card so the form is reached without scrolling past
 * decoration.
 *
 * The panel holds no headings, buttons, or inputs, so the pages' own roles
 * and labels (which the tests use) stay unambiguous.
 */

import { Link } from 'react-router-dom';

function Shield() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" className="h-7 w-7 text-accent-text">
      <path d="M12 2.5 19 5.2v6.1c0 4.6-3 7.9-7 9.7-4-1.8-7-5.1-7-9.7V5.2L12 2.5Z" fill="currentColor" opacity=".16" />
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

/**
 * @param {{ children: React.ReactNode, footer?: React.ReactNode }} props
 *   `footer` renders under the card (e.g. the "Need an account?" line).
 */
export default function AuthLayout({ children, footer = null }) {
  return (
    <div className="min-h-screen bg-canvas lg:grid lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
      <aside className="vs-auth-wash relative isolate overflow-hidden border-b border-border bg-surface-2 px-6 py-6 lg:flex lg:flex-col lg:justify-between lg:border-r lg:border-b-0 lg:px-14 lg:py-12">
        <Link to="/" aria-label="VaultShield home" className="inline-flex items-center gap-2 font-semibold tracking-tight text-fg">
          <Shield />
          <span>VaultShield</span>
        </Link>

        <div className="mt-6 max-w-xl lg:mt-0">
          <p className="font-display text-[2rem] leading-[1.08] tracking-[-0.015em] text-fg sm:text-[2.5rem] lg:text-section">
            Your agent reads the names. <em className="text-accent-text">The values stay home.</em>
          </p>
          <p className="mt-5 hidden max-w-[52ch] text-body-lg text-fg-muted lg:block">
            Sign in to review what your agents changed, then approve or reject it before anything
            ships.
          </p>

          <div
            aria-hidden="true"
            className="mt-10 hidden max-w-md overflow-hidden rounded-card-lg border border-term-border bg-term-bg shadow-lift lg:block"
          >
            <div className="flex gap-1.5 border-b border-term-border px-4 py-3">
              <i className="h-2.5 w-2.5 rounded-full bg-term-muted/30" />
              <i className="h-2.5 w-2.5 rounded-full bg-term-muted/30" />
              <i className="h-2.5 w-2.5 rounded-full bg-term-muted/30" />
            </div>
            <pre className="m-0 px-5 py-4 font-mono text-[13px] leading-7 text-term-fg">
              <span className="text-term-accent">$</span> cat .env{'\n'}
              STRIPE_KEY=<span className="text-term-ok">vs_protected</span>
              {'\n'}
              <span className="text-term-muted"># real value: macOS Keychain</span>
            </pre>
          </div>
        </div>

        <p className="mt-10 hidden text-sm text-fg-muted lg:block">macOS only · Early software</p>
      </aside>

      <main className="flex flex-col items-center justify-center px-4 py-10 sm:px-6 lg:py-16">
        {children}
        {footer}
      </main>
    </div>
  );
}
