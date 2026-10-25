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
 * It shares the two-column `AuthLayout` with /login.
 */

import { Link } from 'react-router-dom';

import AuthLayout from './AuthLayout.jsx';

/** Marks the sign-up placeholder card, so a test can find it unambiguously. */
export const SIGNUP_PLACEHOLDER_TESTID = 'signup-placeholder';

export default function Signup() {
  return (
    <AuthLayout>
      <section
        data-testid={SIGNUP_PLACEHOLDER_TESTID}
        aria-labelledby="signup-heading"
        className="w-full max-w-md rounded-card-xl border border-border bg-surface p-8 shadow-card sm:p-10"
      >
        <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-fg-subtle">
          VaultShield
        </p>
        <h1 id="signup-heading" className="mt-2 font-display text-page font-normal text-fg">
          Sign up
        </h1>
        <p className="mt-4 text-[15px] leading-relaxed text-fg-muted">
          Account creation is coming in the next release, with{' '}
          <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[13px] text-fg">
            vaultshield signup
          </code>
          .
        </p>
        <p className="mt-3 text-[15px] leading-relaxed text-fg-muted">
          For now, sign in with an account your administrator created for you.
        </p>
        <p className="mt-8 text-sm">
          <Link to="/login" className="font-medium text-accent-text underline-offset-4 hover:underline">
            Back to sign in
          </Link>
        </p>
      </section>
    </AuthLayout>
  );
}
