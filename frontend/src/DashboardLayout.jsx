/**
 * The dashboard shell, now a routed layout.
 *
 * The frame every `/dashboard*` page mounts into: a left sidebar (wordmark,
 * the project list + New project, Sign out) and a content column with a top
 * bar (drawer toggle on narrow screens, Project > Run breadcrumb, and the
 * Delete project control) above an `<Outlet />`. It replaces the old `App`
 * shell: selection is now the URL, not local state, so a page is a route and
 * picking a project or run is a navigation.
 *
 * ### Resolving the current project without a second request
 *
 * The breadcrumb and the delete dialog need the open Project's *name*, which
 * only the project list carries (there is no `GET /api/projects/:id/`). Rather
 * than fetch the list twice, the sidebar's `ProjectListView` hands its loaded
 * records up through `onProjectsLoaded`, and the layout resolves the current
 * Project from the `:projectId` route param against them. On a deep link the
 * name simply fills in once the list loads.
 */

import { useEffect, useRef, useState } from 'react';
import { Link, Outlet, useNavigate, useParams } from 'react-router-dom';

import Modal from './Modal.jsx';
import NewProjectForm from './NewProjectForm.jsx';
import ProjectListView from './ProjectListView.jsx';
import { useAuth } from './auth.jsx';

/** An original shield glyph for the wordmark (decorative). */
function ShieldGlyph({ className = '' }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" className={className} fill="none">
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

export default function DashboardLayout() {
  const { client, signOut } = useAuth();
  const { projectId, runId } = useParams();
  const navigate = useNavigate();

  // Records the sidebar loaded, lifted here so the breadcrumb and delete
  // dialog can name the open Project without a second request. `null` until
  // the sidebar's first load reports in, so the overview can tell loading from
  // genuinely empty.
  const [projects, setProjects] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Bumped to remount the sidebar's `ProjectListView`, which re-runs its
  // fetch — how a create and a delete refresh the list.
  const [sidebarReloadKey, setSidebarReloadKey] = useState(0);

  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState(null);
  const deleteButtonRef = useRef(null);

  // New-project modal: whether it is open, whether a create is in flight (so
  // the modal refuses to close mid-request), and the button to restore focus
  // to on close.
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const newProjectButtonRef = useRef(null);

  const currentProject = (projects ?? []).find((p) => String(p?.id) === String(projectId)) ?? null;

  function selectProject(project) {
    setDrawerOpen(false);
    setConfirmingDelete(false);
    setDeleteError(null);
    navigate(`/dashboard/projects/${encodeURIComponent(project.id)}`);
  }

  function handleSignOut() {
    signOut();
    navigate('/login');
  }

  function reloadProjects() {
    setSidebarReloadKey((key) => key + 1);
  }

  /** After a create: close the modal, refresh the sidebar, open the new one. */
  function handleProjectCreated(project) {
    setNewProjectOpen(false);
    reloadProjects();
    navigate(`/dashboard/projects/${encodeURIComponent(project.id)}`);
  }

  function openDeleteDialog() {
    setDeleteError(null);
    setConfirmingDelete(true);
  }

  function cancelDelete() {
    setConfirmingDelete(false);
    setDeleteError(null);
  }

  /**
   * Delete the open Project, then refresh the sidebar and leave for the
   * overview — the Project (and whatever run was open under it) is gone.
   */
  async function confirmDeleteProject() {
    if (!projectId) return;
    setDeletePending(true);
    setDeleteError(null);
    try {
      await client.deleteProject(projectId);
      setConfirmingDelete(false);
      setDrawerOpen(false);
      reloadProjects();
      navigate('/dashboard');
    } catch (error) {
      setDeleteError(error);
    } finally {
      setDeletePending(false);
    }
  }

  return (
    <div className="min-h-screen bg-canvas text-fg">
      {drawerOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={() => setDrawerOpen(false)}
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
        />
      )}

      <div className="flex min-h-screen w-full">
        <aside
          id="vs-sidebar"
          className={`fixed inset-y-0 left-0 z-40 flex w-[272px] flex-col border-r border-border bg-surface transition-transform duration-200 md:static md:z-auto md:translate-x-0 ${
            drawerOpen ? 'translate-x-0' : '-translate-x-full'
          }`}
        >
          <Link to="/dashboard" className="flex items-center gap-2 px-5 py-5">
            <ShieldGlyph className="h-7 w-7 text-accent" />
            <span className="text-lg font-semibold tracking-tight text-fg">VaultShield</span>
          </Link>

          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
            <ProjectListView
              key={sidebarReloadKey}
              client={client}
              onSelectProject={selectProject}
              selectedProjectId={projectId ?? null}
              onProjectsLoaded={setProjects}
              onNewProject={() => setNewProjectOpen(true)}
              newProjectButtonRef={newProjectButtonRef}
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
                <li>
                  <Link to="/dashboard" className="text-fg-muted hover:text-fg">
                    Projects
                  </Link>
                </li>
                {currentProject && (
                  <>
                    <li aria-hidden="true" className="text-fg-subtle">
                      /
                    </li>
                    <li className="truncate font-medium text-fg" aria-current={runId ? undefined : 'page'}>
                      {runId ? (
                        <Link
                          to={`/dashboard/projects/${encodeURIComponent(currentProject.id)}`}
                          className="hover:underline"
                        >
                          {currentProject.name}
                        </Link>
                      ) : (
                        currentProject.name
                      )}
                    </li>
                  </>
                )}
                {runId && (
                  <>
                    <li aria-hidden="true" className="text-fg-subtle">
                      /
                    </li>
                    <li className="truncate font-mono text-xs text-fg" aria-current="page">
                      {runId}
                    </li>
                  </>
                )}
              </ol>
            </nav>

            {currentProject && (
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

          <main className="w-full flex-1 px-4 py-8 md:px-8">
            <Outlet
              context={{
                projects,
                currentProject,
                reloadProjects,
                openNewProject: () => setNewProjectOpen(true),
              }}
            />
          </main>
        </div>
      </div>

      {newProjectOpen && (
        <Modal
          title="New project"
          onClose={() => setNewProjectOpen(false)}
          closeDisabled={creating}
          returnFocusRef={newProjectButtonRef}
        >
          <NewProjectForm
            client={client}
            onCreated={handleProjectCreated}
            onCancel={() => setNewProjectOpen(false)}
            onPendingChange={setCreating}
          />
        </Modal>
      )}

      {confirmingDelete && currentProject && (
        <DeleteProjectDialog
          project={currentProject}
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

/**
 * Confirm-before-delete modal for a Project.
 *
 * Unchanged from the pre-routing shell: `role="dialog"` + `aria-modal`, focus
 * moved in on open and returned to the trigger on close, Escape cancels, Tab
 * trapped in the panel. It names the Project and warns its runs go with it.
 * (Step 5 may migrate this onto the generic `Modal` if its tests allow.)
 */
function DeleteProjectDialog({ project, pending, error, onConfirm, onCancel, returnFocusRef }) {
  const panelRef = useRef(null);
  const cancelRef = useRef(null);

  // Move focus into the dialog on open; return it to the trigger on close.
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
          This permanently deletes <span className="font-medium text-fg">{project.name}</span> and all
          of its runs. This can&rsquo;t be undone.
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
