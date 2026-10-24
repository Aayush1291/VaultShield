/**
 * The dashboard shell.
 *
 * The frame the three views mount into: a left sidebar (VaultShield wordmark,
 * the project list + New project, and the account area with Sign out), a top
 * bar with Project > Run breadcrumbs, and the main content column holding the
 * run history and run detail. On narrow screens the sidebar is an off-canvas
 * drawer toggled from the top bar.
 *
 * The views keep their own logic and state; this file only arranges them and
 * carries the selection. `ProjectListView` IS the sidebar's project list (it
 * already owns the list, the New project form, and the empty/loading/error
 * states), so there is one project list, not two.
 *
 * ### Authentication gate (single-user MVP auth)
 *
 * Every `/api/` path except login requires a stored Dashboard_Auth_Token, so
 * with no token stored there is nothing the three views can load. The shell
 * shows `LoginView` until a token exists, and swaps to the dashboard once one
 * does. Two things return to the login screen: the "Sign out" control (which
 * clears the token), and any view's request being refused as unauthenticated —
 * a 401/403 means the stored token is gone or invalid, so it is cleared and the
 * login screen returns. That second path is wired without touching the views:
 * they are handed a thin wrapper around the API client (`guardClient`) that
 * watches for a 401/403 on any call, clears the token, and re-throws so the
 * view still renders its own sign-in message.
 *
 * ### Selection lives here, and there is still no router
 *
 * Two pieces of state carry which Project and which Run are open. Nothing in
 * the requirements calls for URL-addressable views, so there is no router;
 * picking one is a decision the first view that needs URL parameters should
 * drive.
 */

import { useMemo, useState } from 'react';

import api from './api.js';
import LoginView from './LoginView.jsx';
import ProjectListView from './ProjectListView.jsx';
import RunDetailView from './RunDetailView.jsx';
import RunHistoryView from './RunHistoryView.jsx';

/** An original shield glyph for the wordmark. Decorative, so aria-hidden keeps
 * the heading's accessible name exactly "VaultShield". */
function ShieldGlyph({ className = '' }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      className={className}
      fill="none"
    >
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
  );
}

/**
 * Wrap an API client so an unauthenticated response brings the shell back to
 * the login screen, without the views having to know about it.
 *
 * Every method call is forwarded unchanged; the only addition is that a
 * rejected promise carrying a 401 or 403 (`ApiError.status`) first calls
 * `onUnauthorized`, then re-throws so the calling view's own error handling
 * runs exactly as before. Non-function properties pass straight through.
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
 *   singleton, matching the views; a test injects one with `createApiClient`.
 */
export default function App({ client = api }) {
  const [signedIn, setSignedIn] = useState(() => client.isAuthenticated());
  const [selectedProject, setSelectedProject] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const guardedClient = useMemo(
    () =>
      guardClient(client, () => {
        client.logout();
        setSignedIn(false);
      }),
    [client],
  );

  /**
   * Open a Project's run history, closing whatever Run was open. The Run has to
   * be dropped: it belongs to the previous Project. Also closes the mobile
   * drawer, since picking a project is the drawer's whole purpose.
   */
  function selectProject(project) {
    setSelectedProject(project);
    setSelectedRun(null);
    setDrawerOpen(false);
  }

  function handleSignOut() {
    client.logout();
    setSignedIn(false);
    setSelectedProject(null);
    setSelectedRun(null);
    setDrawerOpen(false);
  }

  if (!signedIn) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-10">
        <LoginView client={client} onSignedIn={() => setSignedIn(true)} />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-canvas text-fg">
      {/* Drawer backdrop (narrow screens only). */}
      {drawerOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={() => setDrawerOpen(false)}
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
        />
      )}

      <div className="mx-auto flex min-h-screen w-full max-w-7xl">
        <aside
          id="vs-sidebar"
          className={`fixed inset-y-0 left-0 z-40 flex w-72 flex-col border-r border-border bg-surface transition-transform duration-200 md:static md:z-auto md:translate-x-0 ${
            drawerOpen ? 'translate-x-0' : '-translate-x-full'
          }`}
        >
          <div className="flex items-center gap-2 px-5 py-5">
            <ShieldGlyph className="h-7 w-7 text-accent" />
            <h1 className="text-lg font-semibold tracking-tight text-fg">VaultShield</h1>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
            <ProjectListView
              client={guardedClient}
              onSelectProject={selectProject}
              selectedProjectId={selectedProject?.id ?? null}
            />
          </div>

          <div className="border-t border-border px-5 py-4">
            <p className="truncate text-xs text-fg-subtle">Signed in</p>
            <button
              type="button"
              onClick={handleSignOut}
              className="mt-1 text-sm font-medium text-accent hover:text-accent-strong hover:underline"
            >
              Sign out
            </button>
          </div>
        </aside>

        <div className="flex min-h-screen min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-border bg-surface/85 px-4 py-3 backdrop-blur md:px-8">
            <button
              type="button"
              aria-label="Open navigation"
              aria-controls="vs-sidebar"
              aria-expanded={drawerOpen}
              onClick={() => setDrawerOpen(true)}
              className="inline-flex h-9 w-9 items-center justify-center rounded-card border border-border text-fg-muted md:hidden"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5" fill="none">
                <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
              </svg>
            </button>

            <nav aria-label="Breadcrumb" className="min-w-0">
              <ol className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                <li className="text-fg-muted">Projects</li>
                {selectedProject && (
                  <>
                    <li aria-hidden="true" className="text-fg-subtle">
                      /
                    </li>
                    <li
                      className="truncate font-medium text-fg"
                      aria-current={selectedRun ? undefined : 'page'}
                    >
                      {selectedProject.name}
                    </li>
                  </>
                )}
                {selectedRun && (
                  <>
                    <li aria-hidden="true" className="text-fg-subtle">
                      /
                    </li>
                    <li className="truncate font-mono text-xs text-fg" aria-current="page">
                      {selectedRun.run_id}
                    </li>
                  </>
                )}
              </ol>
            </nav>
          </header>

          <main className="mx-auto w-full max-w-4xl flex-1 space-y-10 px-4 py-8 md:px-8">
            <RunHistoryView
              client={guardedClient}
              projectId={selectedProject?.id ?? null}
              projectName={selectedProject?.name ?? ''}
              onSelectRun={setSelectedRun}
            />
            {/* `run_id`, not the pk: `RunViewSet` looks a Run up by it. */}
            <RunDetailView client={guardedClient} runId={selectedRun?.run_id ?? null} />
          </main>
        </div>
      </div>
    </div>
  );
}
