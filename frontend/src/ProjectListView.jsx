/**
 * The dashboard's project list (Requirement 19.1, Task 25.2).
 *
 * design.md: "`ProjectListView`: fetches `GET /api/projects/`, renders one
 * card per Project." That is split here into two pieces on purpose:
 *
 * - `ProjectList` / `ProjectCard` are pure. Their output is a direct function
 *   of the records handed to them — no fetching, no state, no clock — so the
 *   fast-check property over exact list rendering (Property 27, Task 25.4)
 *   can drive them with generated record arrays and count what came out.
 * - `ProjectListView` owns the one impure concern: it asks the API client for
 *   the records and decides which of loading / failed / loaded to show.
 *
 * The card count is therefore never a judgement call. `ProjectList` maps the
 * record array once and renders no card that is not backed by a record, which
 * is the "exactly one, no more, no fewer" half of Requirement 19.1 holding by
 * construction rather than by a test passing.
 */

import { useEffect, useState } from 'react';

import api, { unwrapResults } from './api.js';
import { describeLoadFailure as describeFailure, formatDate } from './format.js';

/**
 * Marks one rendered Project card.
 *
 * Exported so tests count cards by the same string the component renders,
 * instead of both sides hard-coding it and drifting apart.
 */
export const PROJECT_CARD_TESTID = 'project-card';

/** Shown when the backend returned no Projects at all. */
export const EMPTY_PROJECTS_MESSAGE =
  'No projects yet. Run `vaultshield run` against a repository to create one.';

/**
 * Format a Project's `created_at` for display.
 *
 * `src/format.js` holds the implementation, because `RunHistoryView` (Task
 * 25.3) formats a Run's timestamp identically and neither list view should
 * have to import the other to do it. Kept exported under this name since it
 * is this view's use of it that the tests cover.
 */
export { formatDate as formatCreatedAt };

/**
 * Turn a failed project load into something worth showing a Reviewer.
 *
 * Wraps the shared describer with this view's subject, so the two authored
 * messages say "projects" rather than "data". See `src/format.js` for which
 * statuses get their own copy and why.
 *
 * @param {unknown} error typically an `ApiError`
 * @returns {string}
 */
export function describeLoadFailure(error) {
  return describeFailure(error, { subject: 'projects' });
}

/**
 * One Project, as a card.
 *
 * Renders the record's own fields verbatim — no defaulting a blank name to a
 * placeholder — so what is on screen is what the backend sent. `repo_url` is
 * monospaced and `break-all` because a remote is a long unbroken token that
 * would otherwise push the card wider than its grid column.
 *
 * `onSelect` is optional, and its absence is what a card looked like before
 * `RunHistoryView` existed: the name renders as plain heading text. When it is
 * supplied the name becomes a `<button>` rather than a click handler on the
 * card, so opening a Project's run history (Requirement 19.2) is reachable by
 * keyboard and announced as an action — a `<li onClick>` is neither.
 *
 * @param {{ project: { id?: number|string, name?: string, repo_url?: string,
 *                      created_at?: string },
 *           onSelect?: (project: object) => void }} props
 */
export function ProjectCard({ project, onSelect, selected = false }) {
  const created = formatDate(project?.created_at);
  const selectable = typeof onSelect === 'function';

  return (
    <li
      data-testid={PROJECT_CARD_TESTID}
      data-project-id={project?.id}
      aria-current={selected ? 'true' : undefined}
      className={`rounded-card px-3.5 py-3 transition-colors ${
        selected ? 'bg-accent-soft' : 'hover:bg-surface'
      }`}
    >
      <h3 className="text-sm font-semibold">
        {selectable ? (
          <button
            type="button"
            onClick={() => onSelect(project)}
            className="block w-full truncate text-left text-fg"
          >
            {project?.name}
          </button>
        ) : (
          <span className="text-fg">{project?.name}</span>
        )}
      </h3>
      <p className="mt-1 break-all font-mono text-[11px] leading-snug text-fg-muted">
        {project?.repo_url}
      </p>
      {created !== '' && <p className="mt-2 text-[11px] text-fg-subtle">Created {created}</p>}
    </li>
  );
}

/**
 * Exactly one card per Project, or the empty state.
 *
 * Pure and total: any non-array (including the `[]` `unwrapResults` returns
 * for an unusable payload) is treated as no records, so this cannot throw on
 * `.map` and cannot render a card the caller did not supply data for.
 *
 * `role="list"` is set explicitly because Safari drops list semantics from a
 * `<ul>` whose list-style is removed, which Tailwind's reset does.
 *
 * @param {{ projects?: unknown[], onSelectProject?: (project: object) => void,
 *           selectedProjectId?: number|string|null }} props
 */
export function ProjectList({ projects, onSelectProject, selectedProjectId = null }) {
  const records = Array.isArray(projects) ? projects : [];

  if (records.length === 0) {
    return <p className="px-2 text-sm leading-relaxed text-fg-muted">{EMPTY_PROJECTS_MESSAGE}</p>;
  }

  return (
    <ul role="list" className="space-y-1">
      {records.map((project, index) => (
        <ProjectCard
          key={project?.id ?? index}
          project={project}
          onSelect={onSelectProject}
          selected={selectedProjectId != null && String(project?.id) === String(selectedProjectId)}
        />
      ))}
    </ul>
  );
}

/**
 * Fetch `GET /api/projects/` and render the result.
 *
 * The API client is a prop defaulting to the shared singleton, matching how
 * `src/api.test.js` injects a transport: a test drives this against
 * `createApiClient({ fetchImpl })` and asserts on real rendering, with no
 * global patched and no module mocked.
 *
 * Only the first page is shown. The backend paginates at 50 Projects
 * (`PAGE_SIZE` in settings) and Requirement 19.1 is about displaying what was
 * retrieved; following `next` is a separate concern from rendering one card
 * per retrieved record, so it is deliberately not done here.
 *
 * @param {{ client?: typeof api, onSelectProject?: (project: object) => void,
 *           selectedProjectId?: number|string|null }} props
 *   `selectedProjectId` is presentational only — it highlights the open
 *   Project's card in the sidebar and does not change what is fetched.
 */
export default function ProjectListView({
  client = api,
  onSelectProject,
  selectedProjectId = null,
  onProjectsLoaded,
  onNewProject,
  newProjectButtonRef,
}) {
  // `null` distinguishes "not loaded yet" from the genuinely empty list, so
  // the first paint is a loading line rather than a flash of the empty state.
  const [projects, setProjects] = useState(null);
  const [error, setError] = useState(null);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    setProjects(null);
    setError(null);

    (async () => {
      try {
        const payload = await client.listProjects({ signal: controller.signal });
        const records = unwrapResults(payload);
        if (active) {
          setProjects(records);
          // Hand the loaded records up so a routed layout can resolve the
          // current Project for its breadcrumb and delete dialog without a
          // second request. `onProjectsLoaded` is a stable setter in practice.
          if (typeof onProjectsLoaded === 'function') onProjectsLoaded(records);
        }
      } catch (cause) {
        // The client wraps an aborted request as a status-0 `ApiError` like
        // any other transport failure, so the signal is what tells the two
        // apart. Without this check, StrictMode's second effect pass — which
        // aborts the first — would surface as "the backend could not be
        // reached" on a working backend.
        if (active && !controller.signal.aborted) setError(cause);
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, [client, reloadCount]);

  const loading = projects === null && error === null;

  return (
    <section aria-labelledby="project-list-heading">
      <div className="mb-3 flex items-baseline justify-between gap-2 px-2">
        <h2
          id="project-list-heading"
          className="text-xs font-semibold uppercase tracking-wide text-fg-subtle"
        >
          Projects
        </h2>
        {projects !== null && (
          <p className="text-[11px] text-fg-subtle">
            {projects.length} {projects.length === 1 ? 'project' : 'projects'}
          </p>
        )}
      </div>

      <button
        ref={newProjectButtonRef}
        type="button"
        onClick={onNewProject}
        className="mb-4 inline-flex w-full items-center justify-center gap-1 rounded-pill bg-accent px-4 py-2 text-sm font-semibold text-accent-fg shadow-soft transition-colors hover:bg-accent-hover hover:text-accent-hover-fg"
      >
        <span aria-hidden="true">+</span>New project
      </button>

      {loading && (
        <p role="status" className="text-sm text-fg-subtle">
          Loading projects…
        </p>
      )}

      {error !== null && (
        <div
          role="alert"
          className="rounded-card border border-danger/30 bg-danger-soft p-3 text-sm text-danger-fg"
        >
          <p>{describeLoadFailure(error)}</p>
          <button
            type="button"
            onClick={() => setReloadCount((count) => count + 1)}
            className="mt-3 rounded-pill border border-danger bg-surface px-3 py-1 text-xs font-medium text-danger-fg hover:bg-danger-soft"
          >
            Try again
          </button>
        </div>
      )}

      {projects !== null && (
        <ProjectList
          projects={projects}
          onSelectProject={onSelectProject}
          selectedProjectId={selectedProjectId}
        />
      )}
    </section>
  );
}
