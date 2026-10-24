/**
 * The dashboard sign-in screen (single-user MVP auth).
 *
 * Shown by `App` whenever there is no stored Dashboard_Auth_Token. It exchanges
 * a username/password for a token through the existing `api.login`, which
 * stores the token only on success (see `src/api.js`), and then tells `App` it
 * is signed in. It fetches nothing else and renders no dashboard chrome: its one
 * job is to turn a credential into a stored token.
 *
 * ### The token and the password never leave this component's inputs
 *
 * The password lives only in component state and in the body `api.login` sends;
 * it is never written to the DOM as text, never put in a message, never logged.
 * The issued token is handled entirely inside `api.login` (it stores it); this
 * component never reads it, so it cannot display or log it either. The error
 * messages below are derived from the HTTP status alone, never from the
 * response body, so a backend that echoed a credential could not surface it
 * here.
 *
 * ### Error copy is status-derived, not the client's raw message
 *
 * `api.login` throws `ApiError` with a `status`: a bad credential is a 401/403,
 * and a backend that was never reached is status `0` (see `ApiError`). Those
 * two are the cases a Reviewer can act on, so they get the plain copy this
 * component owns; anything else falls back to the client's own one-line
 * message. This mirrors `format.describeLoadFailure`'s status-based shape
 * without reaching into a load-failure helper whose wording ("Sign in to
 * load …") is wrong for the sign-in screen itself.
 */

import { useState } from 'react';

import api from './api.js';

/**
 * Turn a failed `api.login` into the message shown under the form.
 *
 * Status-only, so no part of the response body (which a hostile or misbehaving
 * backend could fill with a credential) is ever rendered. 401 and 403 both mean
 * the credential was refused; status `0` is `ApiError`'s "the request never
 * reached the backend"; everything else keeps the client's own message.
 *
 * @param {unknown} error typically an `ApiError`
 * @returns {string}
 */
export function describeLoginError(error) {
  const status = error?.status;
  if (status === 401 || status === 403) return 'Wrong username or password';
  if (status === 0) return 'Cannot reach the VaultShield backend';
  return error?.message || 'Sign in failed. Please try again.';
}

/**
 * The sign-in form.
 *
 * @param {{ client?: typeof api, onSignedIn?: () => void }} props
 *   `client` is the API client, defaulting to the shared singleton the same way
 *   every view defaults it, so a test injects one with `createApiClient`.
 *   `onSignedIn` is called once the token is stored, so `App` can show the
 *   dashboard.
 */
export default function LoginView({ client = api, onSignedIn }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);

  async function handleSubmit(event) {
    event.preventDefault();
    // One request at a time: a second submit while the first is in flight is
    // ignored, so a double click cannot fire two logins.
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await client.login({ username, password });
      // Only reached on success; `api.login` throws otherwise and the stored
      // token (if any) is left untouched.
      if (typeof onSignedIn === 'function') onSignedIn();
    } catch (cause) {
      setError(describeLoginError(cause));
    } finally {
      setPending(false);
    }
  }

  const fieldClass =
    'mt-1 w-full rounded-card border border-border bg-canvas px-3 py-2 text-sm text-fg placeholder:text-fg-subtle focus:border-accent';

  return (
    <section
      aria-labelledby="login-heading"
      className="w-full max-w-sm rounded-card-xl border border-border bg-surface p-8 shadow-card"
    >
      <div className="mb-6 flex flex-col items-center text-center">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" className="h-10 w-10 text-accent" fill="none">
          <path
            d="M12 2.5 19 5.2v6.1c0 4.6-3 7.9-7 9.7-4-1.8-7-5.1-7-9.7V5.2L12 2.5Z"
            fill="currentColor"
            opacity="0.16"
          />
          <path
            d="M12 2.5 19 5.2v6.1c0 4.6-3 7.9-7 9.7-4-1.8-7-5.1-7-9.7V5.2L12 2.5Z"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
          <path
            d="m9 11.7 2.2 2.2L15 9.8"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <p className="mt-3 text-[11px] font-semibold uppercase tracking-[0.2em] text-fg-subtle">
          VaultShield
        </p>
        <h2 id="login-heading" className="mt-2 text-2xl font-semibold tracking-tight text-fg">
          Sign in
        </h2>
        <p className="mt-1 text-sm text-fg-muted">
          Review your agent runs and approve what ships.
        </p>
      </div>

      <form onSubmit={handleSubmit} aria-busy={pending} className="space-y-4">
        <div>
          <label htmlFor="login-username" className="block text-sm font-medium text-fg-muted">
            Username
          </label>
          <input
            id="login-username"
            name="username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={pending}
            className={fieldClass}
          />
        </div>

        <div>
          <label htmlFor="login-password" className="block text-sm font-medium text-fg-muted">
            Password
          </label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={pending}
            className={fieldClass}
          />
        </div>

        {error !== null && (
          <p
            role="alert"
            className="rounded-card border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
          >
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={pending}
          className="w-full rounded-card bg-accent px-3 py-2.5 text-sm font-semibold text-accent-contrast transition-colors hover:bg-accent-strong disabled:opacity-60"
        >
          {pending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>

      {/*
        Reserved for a later phase: a divider and "Continue with GitHub" /
        "Continue with Google" buttons mount here, below the credential form.
        They are deliberately NOT rendered yet — this comment marks the slot so
        adding them is a drop-in, not a re-layout.
      */}
    </section>
  );
}
