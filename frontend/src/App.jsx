/**
 * The dashboard shell.
 *
 * The frame the views mount into. `ProjectListView` (Task 25.2) is the landing
 * view, `RunHistoryView` (Task 25.3) sits below it, and `RunDetailView` (Task
 * 26.2) below that.
 *
 * ### Authentication gate (single-user MVP auth)
 *
 * Every `/api/` path except login requires a stored Dashboard_Auth_Token, so
 * with no token stored there is nothing the three views can load. This shell
 * therefore shows `LoginView` until a token exists, and swaps to the dashboard
 * once one does. Two things bring the Reviewer back to the login screen: the
 * "Sign out" control (which clears the token), and any view's request being
 * refused as unauthenticated — a 401/403 means the stored token is gone or
 * invalid, so it is cleared and the login screen returns. That second path is
 * wired without touching the views: they are handed a thin wrapper around the
 * API client (`guardClient`) that watches for a 401/403 on any call, clears the
 * token, and re-throws so the view still renders its own sign-in message.
 *
 * ### Selection lives here, and there is still no router
 *
 * Requirement 19.2 starts "WHEN a Reviewer opens a Project's run history
 * view" and Requirement 20.1 "WHEN a Reviewer opens a Run detail view", so
 * something has to carry which Project and which Run those are. That something
 * is two pieces of state in this component, not a route: nothing in the
 * requirements or in design.md's Frontend section calls for URL-addressable
 * views, and picking a routing library is a decision the first view that
 * genuinely needs URL parameters should drive.
 *
 * All three views stay mounted, each rendering its own "select a project" /
 * "select a run" prompt until something is picked. That keeps the shell free of
 * conditional layout and keeps each view in charge of every state it can be in.
 *
 * Keeping the shell separate from `main.jsx` is what lets it be rendered in a
 * test without pulling in `import.meta.env` or a real DOM root.
 */

import { useMemo, useState } from 'react';

import api from './api.js';
import LoginView from './LoginView.jsx';
import ProjectListView from './ProjectListView.jsx';
import RunDetailView from './RunDetailView.jsx';
import RunHistoryView from './RunHistoryView.jsx';

/**
 * Wrap an API client so an unauthenticated response brings the shell back to
 * the login screen, without the views having to know about it.
 *
 * Every method call is forwarded unchanged; the only addition is that a
 * rejected promise carrying a 401 or 403 (`ApiError.status`) first calls
 * `onUnauthorized`, then re-throws so the calling view's own error handling
 * (its "sign in to load …" path) runs exactly as before. Non-function
 * properties — the `baseUrl` getter, say — pass straight through.
 *
 * A `Proxy` rather than a hand-listed set of methods, so a call the views make
 * (or a later view makes) cannot slip past the guard by being forgotten here.
 *
 * @param {typeof api} client
 * @param {() => void} onUnauthorized
 * @returns {typeof api}
 */
function guardClient(client, onUnauthorized) {
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

/**
 * @param {{ client?: typeof api }} props `client` defaults to the shared
 *   singleton, matching the views; a test injects one built with
 *   `createApiClient`.
 */
export default function App({ client = api }) {
  // Seeded from the stored token: a page load with a token in hand goes
  // straight to the dashboard, without a flash of the login screen.
  const [signedIn, setSignedIn] = useState(() => client.isAuthenticated());

  // The whole Project record, not just its id: the run history heading names
  // the Project, and the list already has the record in hand.
  const [selectedProject, setSelectedProject] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);

  // Stable across renders (keyed only on `client`), so the views' fetch
  // effects, which depend on their `client` prop, do not re-run every render.
  const guardedClient = useMemo(
    () =>
      guardClient(client, () => {
        client.logout();
        setSignedIn(false);
      }),
    [client],
  );

  /**
   * Open a Project's run history, closing whatever Run was open.
   *
   * The Run has to be dropped: it belongs to the previously selected Project,
   * and leaving it on screen would show one Project's run list above another
   * Project's run detail.
   *
   * @param {object} project
   */
  function selectProject(project) {
    setSelectedProject(project);
    setSelectedRun(null);
  }

  function handleSignOut() {
    client.logout();
    setSignedIn(false);
    setSelectedProject(null);
    setSelectedRun(null);
  }

  if (!signedIn) {
    return (
      <div className="min-h-screen bg-slate-50 text-slate-900">
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
            <h1 className="text-lg font-semibold tracking-tight">VaultShield</h1>
            <p className="text-sm text-slate-500">Agent run review</p>
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-6 py-8">
          <LoginView client={client} onSignedIn={() => setSignedIn(true)} />
        </main>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <h1 className="text-lg font-semibold tracking-tight">VaultShield</h1>
          <div className="flex items-center gap-4">
            <p className="text-sm text-slate-500">Agent run review</p>
            <button
              type="button"
              onClick={handleSignOut}
              className="text-sm font-medium text-indigo-700 underline-offset-2 hover:underline"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-10 px-6 py-8">
        <ProjectListView client={guardedClient} onSelectProject={selectProject} />
        <RunHistoryView
          client={guardedClient}
          projectId={selectedProject?.id ?? null}
          projectName={selectedProject?.name ?? ''}
          onSelectRun={setSelectedRun}
        />
        {/* `run_id`, not the pk: `RunViewSet` looks a Run up by it
            (`lookup_field = "run_id"`). */}
        <RunDetailView client={guardedClient} runId={selectedRun?.run_id ?? null} />
      </main>
    </div>
  );
}
