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

import { useEffect, useMemo, useRef, useState } from 'react';

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
 * Confirm-before-delete modal for a Project (Task: delete project).
 *
 * A genuine modal: `role="dialog"` + `aria-modal`, focus moved inside on open
 * and returned to the trigger on close, Escape cancels, and Tab is trapped
 * within the panel so keyboard focus cannot wander behind it. It names the
 * Project and spells out that its runs go with it, because the delete cascades
 * every Run and artifact under the Project and cannot be undone.
 *
 * It owns no delete state of its own — pending, error, and the request all
 * live in `App`, which is where the success path (clear selection, reload the
 * sidebar) has to happen. This component only renders that state and reports
 * the two intents: confirm and cancel.
 *
 * @param {{ project: { id?: string|number, name?: string }, pending: boolean,
 *           error: unknown, onConfirm: () => void, onCancel: () => void,
 *           returnFocusRef: { current: HTMLElement|null } }} props
 */
function DeleteProjectDialog({ project, pending, error, onConfirm, onCancel, returnFocusRef }) {
  const panelRef = useRef(null);
  const cancelRef = useRef(null);

  // Move focus into the dialog on open; hand it back to the trigger on close.
  // The trigger is captured now because by cleanup time a successful delete
  // has already unmounted it (focusing a detached node is then a harmless
  // no-op); on cancel it is still mounted and focus returns to it.
  useEffect(() => {
    const trigger = returnFocusRef?.current ?? null;
    cancelRef.current?.focus();
    return () => trigger?.focus?.();
  }, [returnFocusRef]);

  function handleKeyDown(event) {
    if (event.key === 'Escape') {
      if (!pending) onCancel();
      return;
    }
    if (event.key !== 'Tab') return;

    // Trap Tab within the panel so focus cannot leave an aria-modal dialog.
    const focusable = panelRef.current?.querySelectorAll(
      'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable || focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Visual overlay; clicking it cancels. Kept out of the tab order
          (tabIndex -1) so the trap below only cycles the panel's controls. */}
      <button
        type="button"
        aria-label="Close dialog"
        tabIndex={-1}
        onClick={() => {
          if (!pending) onCancel();
        }}
        className="absolute inset-0 bg-black/50"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-project-title"
        aria-describedby="delete-project-description"
        onKeyDown={handleKeyDown}
        className="relative z-10 w-full max-w-md rounded-card-lg border border-border bg-surface p-6 shadow-soft"
      >
        <h2 id="delete-project-title" className="text-lg font-semibold text-fg">
          Delete {project.name}?
        </h2>
        <p id="delete-project-description" className="mt-2 text-sm text-fg-muted">
          This permanently deletes{' '}
          <span className="font-medium text-fg">{project.name}</span> and all of its runs. This
          can&rsquo;t be undone.
        </p>

        {error !== null && error !== undefined && (
          <p
            role="alert"
            className="mt-4 rounded-card border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
          >
            Could not delete the project. Please try again.
          </p>
        )}

        <div className="mt-6 flex justify-end gap-3">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            disabled={pending}
            className="rounded-card border border-border bg-surface px-4 py-2 text-sm font-medium text-fg-muted hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            className="rounded-card border border-red-600 bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {pending ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
  );
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
  // Delete-project flow (the modal below). `confirming` is whether the dialog
  // is open; `pending`/`error` carry the in-flight DELETE's state.
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState(null);
  // Bumped after a successful delete to remount `ProjectListView` (via its
  // `key`), which re-runs its fetch-on-mount and drops the gone Project — the
  // same reload the list does for itself after a create, driven from here.
  const [sidebarReloadKey, setSidebarReloadKey] = useState(0);
  // So the dialog can return focus to the control that opened it on close.
  const deleteButtonRef = useRef(null);

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
    // A dialog opened for the previous Project must not linger over the new one.
    setConfirmingDelete(false);
    setDeleteError(null);
  }

  function handleSignOut() {
    client.logout();
    setSignedIn(false);
    setSelectedProject(null);
    setSelectedRun(null);
    setDrawerOpen(false);
    setConfirmingDelete(false);
    setDeleteError(null);
  }

  /** Open the confirm dialog, clearing any error left from a prior attempt. */
  function openDeleteDialog() {
    setDeleteError(null);
    setConfirmingDelete(true);
  }

  /** Close the dialog without deleting (Cancel, Escape, or the overlay). */
  function cancelDelete() {
    setConfirmingDelete(false);
    setDeleteError(null);
  }

  /**
   * Delete the selected Project, then drop it everywhere and reload the list.
   *
   * On success the Project and whatever Run was open are cleared (both belong
   * to a Project that no longer exists), the mobile drawer is closed, and the
   * sidebar is remounted to re-fetch without the gone Project. A failure keeps
   * the dialog open with its error shown so the Reviewer can retry or cancel.
   */
  async function confirmDeleteProject() {
    if (!selectedProject) return;
    setDeletePending(true);
    setDeleteError(null);
    try {
      await guardedClient.deleteProject(selectedProject.id);
      setConfirmingDelete(false);
      setSelectedProject(null);
      setSelectedRun(null);
      setDrawerOpen(false);
      setSidebarReloadKey((key) => key + 1);
    } catch (error) {
      setDeleteError(error);
    } finally {
      setDeletePending(false);
    }
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
              key={sidebarReloadKey}
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

            {/* Delete the open Project. Only meaningful once one is selected,
                so it is absent otherwise rather than disabled. */}
            {selectedProject && (
              <button
                ref={deleteButtonRef}
                type="button"
                onClick={openDeleteDialog}
                className="ml-auto inline-flex shrink-0 items-center rounded-card border border-red-300 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 dark:border-red-900/60 dark:text-red-300 dark:hover:bg-red-950/40"
              >
                Delete project
              </button>
            )}
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

      {confirmingDelete && selectedProject && (
        <DeleteProjectDialog
          project={selectedProject}
          pending={deletePending}
          error={deleteError}
          onConfirm={confirmDeleteProject}
          onCancel={cancelDelete}
          returnFocusRef={deleteButtonRef}
        />
      )}
    </div>
  );
}
