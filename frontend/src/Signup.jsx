/**
 * The `/signup` route — a placeholder, with no form yet.
 *
 * Account creation is not in this release: there is no self-service sign-up on
 * the backend (the REST surface has no registration endpoint), so rendering a
 * form here would be a control that cannot work. Instead this says plainly how
 * accounts are obtained today and points to `/login`.
 *
 * The reserved spot for provider buttons lives in `LoginView`; this page keeps
 * the message and the link, so wiring `vaultshield signup` later is a drop-in.
 */

import { Link } from 'react-router-dom';

/** Marks the sign-up placeholder card, so a test can find it unambiguously. */
export const SIGNUP_PLACEHOLDER_TESTID = 'signup-placeholder';

export default function Signup() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-10">
      <section
        data-testid={SIGNUP_PLACEHOLDER_TESTID}
        aria-labelledby="signup-heading"
        className="w-full max-w-sm rounded-card-xl border border-border bg-surface p-8 text-center shadow-card"
      >
        <h1 id="signup-heading" className="text-2xl font-semibold tracking-tight text-fg">
          Sign up
        </h1>
        <p className="mt-3 text-sm text-fg-muted">
          Account creation is coming in the next release, with{' '}
          <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-xs text-fg">
            vaultshield signup
          </code>
          .
        </p>
        <p className="mt-3 text-sm text-fg-muted">
          For now, sign in with an account your administrator created for you.
        </p>
        <p className="mt-6 text-sm">
          <Link to="/login" className="font-medium text-accent-text hover:underline">
            Back to sign in
          </Link>
        </p>
      </section>
    </div>
  );
}
