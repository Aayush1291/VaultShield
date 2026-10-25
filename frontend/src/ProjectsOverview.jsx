/**
 * The `/dashboard` index route: the projects overview (step 8).
 *
 * A grid of project cards (name, repo URL, created) each linking to that
 * project's runs route, a New project button that opens the same modal as the
 * sidebar, and an empty state when there are no projects. The project list and
 * the open-modal action come from the layout's Outlet context, so the overview
 * and the sidebar share one fetch and one New project flow.
 */

import { Link, useOutletContext } from 'react-router-dom';

import { EMPTY_PROJECTS_MESSAGE, formatCreatedAt } from './ProjectListView.jsx';

/** Marks one overview project card, so a test can count them. */
export const OVERVIEW_CARD_TESTID = 'overview-project-card';

export default function ProjectsOverview() {
  const { projects, openNewProject } = useOutletContext();

  return (
    <section aria-labelledby="overview-heading" className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="overview-heading" className="font-display text-page font-normal text-fg">
          Projects
        </h2>
        <button
          type="button"
          onClick={openNewProject}
          className="rounded-pill bg-accent px-4 py-2 text-sm font-semibold text-accent-fg shadow-soft transition-colors hover:bg-accent-hover hover:text-accent-hover-fg disabled:opacity-60"
        >
          New project
        </button>
      </div>

      {projects === null && (
        <p role="status" className="text-sm text-fg-subtle">
          Loading projects…
        </p>
      )}

      {projects !== null && projects.length === 0 && (
        <div className="rounded-card-xl border border-border bg-surface px-6 py-14 text-center shadow-soft">
          <h3 className="font-display text-[1.75rem] leading-tight font-normal text-fg">Nothing here yet</h3>
          <p className="mx-auto mt-2 max-w-prose text-sm text-fg-muted">{EMPTY_PROJECTS_MESSAGE}</p>
        </div>
      )}

      {projects !== null && projects.length > 0 && (
        <ul role="list" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {projects.map((project, index) => {
            const created = formatCreatedAt(project?.created_at);
            return (
              <li key={project?.id ?? index} data-testid={OVERVIEW_CARD_TESTID}>
                <Link
                  to={`/dashboard/projects/${encodeURIComponent(project?.id)}`}
                  className="block h-full rounded-card-lg border border-border bg-surface p-5 shadow-soft transition-[box-shadow,transform] hover:-translate-y-0.5 hover:shadow-card"
                >
                  <h3 className="truncate font-display text-[1.4rem] leading-tight font-normal text-fg">{project?.name}</h3>
                  <p className="mt-2 break-all font-mono text-xs leading-snug text-fg-muted">
                    {project?.repo_url}
                  </p>
                  {created !== '' && (
                    <p className="mt-4 text-xs text-fg-subtle">Created {created}</p>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
