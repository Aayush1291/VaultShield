/**
 * Authentication state and route guards for the dashboard (single-user MVP).
 *
 * The old shell carried `signedIn` in `App` and showed either `LoginView` or
 * the dashboard. With routing, that same state lives in a context so the route
 * guards (`RequireAuth`, `RedirectIfSignedIn`) and the layout (`Sign out`) can
 * read and change it without prop-drilling, and a 401/403 on any data call
 * still flips it — which, under routing, redirects to `/login` rather than
 * swapping a top-level branch.
 *
 * `client` exposed here is the GUARDED client: every view gets it, so an
 * unauthenticated response clears the token and signs out. `rawClient` is the
 * unguarded singleton, handed only to the sign-in form so a wrong-password
 * 401 is an ordinary form error rather than a sign-out.
 */

import { createContext, useContext, useMemo, useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import api from './api.js';

/**
 * Wrap an API client so an unauthenticated response signs the Reviewer out.
 *
 * Every method is forwarded unchanged; a rejected promise carrying a 401 or
 * 403 (`ApiError.status`) first calls `onUnauthorized`, then re-throws so the
 * caller's own error handling still runs. Non-function properties pass
 * through. (Moved here from `App` unchanged when routing landed.)
 *
 * @param {typeof api} client
 * @param {() => void} onUnauthorized
 * @returns {typeof api}
 */
export function guardClient(client, onUnauthorized) {
  return new Proxy(client, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver);
      if (typeof value !== 'function') return value;
      return (...args) => {
        const result = value.apply(target, args);
        if (result && typeof result.then === 'function') {
          return result.then(undefined, (error) => {
            if (error && (error.status === 401 || error.status === 403)) onUnauthorized();
            throw error;
          });
        }
        return result;
      };
    },
  });
}

const AuthContext = createContext(null);

/**
 * @param {{ client?: typeof api, children: React.ReactNode }} props
 *   `client` defaults to the shared singleton; a test injects one built with
 *   `createApiClient`.
 */
export function AuthProvider({ client = api, children }) {
  const [signedIn, setSignedIn] = useState(() => client.isAuthenticated());

  const guardedClient = useMemo(
    () =>
      guardClient(client, () => {
        client.logout();
        setSignedIn(false);
      }),
    [client],
  );

  const value = useMemo(
    () => ({
      signedIn,
      // The guarded client every data view uses.
      client: guardedClient,
      // The unguarded client the sign-in form uses (a login 401 is a form
      // error, not a sign-out).
      rawClient: client,
      signIn: () => setSignedIn(true),
      signOut: () => {
        client.logout();
        setSignedIn(false);
      },
    }),
    [signedIn, guardedClient, client],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** The auth context, or a thrown error when used outside its provider. */
export function useAuth() {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return ctx;
}

/**
 * Gate a route behind sign-in.
 *
 * With no token, redirect to `/login` and remember where the Reviewer was
 * headed (`state.from`) so the sign-in form can return them there. `replace`
 * so the guarded URL does not become a back-button trap.
 *
 * @param {{ children: React.ReactNode }} props
 */
export function RequireAuth({ children }) {
  const { signedIn } = useAuth();
  const location = useLocation();
  if (!signedIn) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return children;
}

/**
 * Keep a signed-in Reviewer out of `/login` and `/signup`.
 *
 * There is nothing to do on those pages once authenticated, so send them to
 * the dashboard instead.
 *
 * @param {{ children: React.ReactNode }} props
 */
export function RedirectIfSignedIn({ children }) {
  const { signedIn } = useAuth();
  if (signedIn) {
    return <Navigate to="/dashboard" replace />;
  }
  return children;
}
