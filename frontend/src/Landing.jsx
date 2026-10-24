/**
 * The `/` landing route.
 *
 * Step 1 stands up a minimal landing so routing has a real public home; step 2
 * ports the full marketing page (website/index.html) into this component. The
 * header links adapt to auth: a signed-in Reviewer sees "Open dashboard",
 * everyone else sees "Log in" / "Sign up".
 */

import { Link } from 'react-router-dom';

import { useAuth } from './auth.jsx';

export default function Landing() {
  const { signedIn } = useAuth();

  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-10 text-center">
      <h1 className="text-hero font-semibold tracking-tight text-fg">VaultShield</h1>
      <p className="mt-3 max-w-md text-fg-muted">
        Let AI agents read your code, not your secrets.
      </p>
      <nav className="mt-8 flex items-center gap-4" aria-label="Primary">
        {signedIn ? (
          <Link
            to="/dashboard"
            className="rounded-card bg-accent px-4 py-2 text-sm font-semibold text-accent-contrast hover:bg-accent-strong"
          >
            Open dashboard
          </Link>
        ) : (
          <>
            <Link
              to="/login"
              className="rounded-card border border-border px-4 py-2 text-sm font-medium text-fg hover:border-border-strong"
            >
              Log in
            </Link>
            <Link
              to="/signup"
              className="rounded-card bg-accent px-4 py-2 text-sm font-semibold text-accent-contrast hover:bg-accent-strong"
            >
              Sign up
            </Link>
          </>
        )}
      </nav>
    </main>
  );
}
