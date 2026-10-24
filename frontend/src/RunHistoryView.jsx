/**
 * A Project's run history (Requirements 19.2 and 19.3, Task 25.3).
 *
 * design.md: "`RunHistoryView`: fetches `GET /api/projects/:id/runs/` (or
 * filtered `/api/runs/?project=:id`), renders one row per Run with its status
 * badge; renders an empty-state message when the list is empty."
 *
 * The filtered collection is the one used, because it is the one the backend
 * actually exposes: `vaultshield_backend/urls.py` registers `RunViewSet` at
 * `/api/runs/` with no nested route under a Project, and `RunViewSet`'s
 * `get_queryset` narrows the list by query parameter. `api.listRuns` already
 * wraps it.
 *
 * Split the same way `ProjectListView` is, and for the same reason:
 *
 * - `RunList` / `RunRow` / `RunStatusBadge` are pure. Their output is a direct
 *   function of the records handed to them, so the fast-check property over
 *   exact list rendering (Property 27, Task 25.4) can drive them with
 *   generated record arrays and count what came out.
 * - `RunHistoryView` owns the impure concerns: which Project to ask about,
 *   the request, and which of prompt / loading / failed / loaded to show.
 *
 * `RunList` maps the record array once and renders no row that is not backed
 * by a record, so Requirement 19.2's "each Run associated with that Project"
 * and Requirement 19.3's empty state are one branch on the same array rather
 * than two independent behaviours that could disagree.
 */

import { useEffect, useState } from 'react';

import api, { unwrapResults } from './api.js';
import { describeLoadFailure, formatRelative, formatTimestamp } from './format.js';

/** A representative command for the empty state's copyable hint. */
export const RUN_HINT_COMMAND = 'vaultshield run --agent=mock --prompt="..." --repo-url=<repo>';

/**
 * Marks one rendered Run row.
 *
 * Exported so tests count rows by the same string the component renders,
 * instead of both sides hard-coding it and drifting apart.
 */
export const RUN_ROW_TESTID = 'run-row';

/** Marks the Run_Status badge inside a row. */
export const RUN_STATUS_TESTID = 'run-status';

/** Requirement 19.3: shown when the Project has no Runs at all. */
export const EMPTY_RUNS_MESSAGE =
  'No runs for this project yet. Run `vaultshield run` in the repository to create one.';

/**
 * Shown before a Project has been picked.
 *
 * Not an error and not the empty state: with no Project chosen there is
 * nothing to ask the backend about, and reusing `EMPTY_RUNS_MESSAGE` here
 * would claim some Project has no Runs when none was selected.
 */
export const NO_PROJECT_SELECTED_MESSAGE = 'Select a project to see its run history.';

/**
 * Badge styling per Run_Status.
 *
 * Keyed by the six literal values in `Run.STATUS_CHOICES` (Requirement
 * 11.2). `failed` is neutral grey rather than a warning colour because
 * nothing is pending on a reviewer: the Run ended before it could be
 * reviewed. It uses Tailwind's `gray` so it stays distinct from the `slate`
 * unknown-status fallback. A status outside that set — which only a backend
 * that grew a seventh value could produce — still renders, in the neutral
 * style, because dropping or blanking it would hide a Run's real state.
 */
const STATUS_STYLES = {
  running: 'border-sky-200 bg-sky-50 text-sky-700',
  awaiting_review: 'border-amber-200 bg-amber-50 text-amber-800',
  approved: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  rejected: 'border-red-200 bg-red-50 text-red-700',
  submitted: 'border-violet-200 bg-violet-50 text-violet-700',
  failed: 'border-gray-300 bg-gray-100 text-gray-600',
};

const UNKNOWN_STATUS_STYLE = 'border-slate-300 bg-slate-100 text-slate-700';

/**
 * A Run's status, as a badge.
 *
 * Renders the stored Run_Status string verbatim rather than a prettified
 * label. `Run.STATUS_CHOICES` deliberately repeats the stored value as its
 * display value, with the comment that the dashboard renders its own
 * human-facing copy where it needs one — and the one place the spec asks for
 * that is Requirement 21.4's `"approved — awaiting submission"` on the detail
 * view, after a decision. In a history list, the value the backend stores is
 * the value a Reviewer needs to see, and inventing a second vocabulary here
 * would give the same Run two names across two views.
 *
 * `label` is that one exception, and it exists so the exception does not
 * become a second badge: `RunDetailView` passes Requirement 21.4's copy for an
 * approved Run and gets the same styling, the same shape, and the same
 * `data-status` machine value as every other status. Omitting it — which every
 * caller but that one does — renders the stored value, so this stays the single
 * place a Run_Status is styled.
 *
 * @param {{ status?: string, label?: string }} props
 */
export function RunStatusBadge({ status, label }) {
  const style = STATUS_STYLES[status] ?? UNKNOWN_STATUS_STYLE;

  return (
    <span
      data-testid={RUN_STATUS_TESTID}
      // The stored Run_Status, regardless of what is rendered, so a test or a
      // stylesheet reads the machine value rather than the human copy.
      data-status={status}
      className={`inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 font-mono text-xs ${style}`}
    >
      {label ?? status}
    </span>
  );
}

/**
 * One Run, as a row.
 *
 * Shows the Run's own fields verbatim: `run_id` (the identifier every CLI
 * command addresses a Run by, so it is what a Reviewer copies into
 * `vaultshield submit`), the agent that produced it, its Run_Status, and the
 * creation date when there is a parseable one.
 *
 * `onSelect` is optional, and its absence is what a row looked like before
 * `RunDetailView` existed: the `run_id` renders as plain text. When it is
 * supplied the id becomes a `<button>` rather than a click handler on the row,
 * so opening a Run detail view (Requirement 20.1) is reachable by keyboard and
 * announced as an action — a `<li onClick>` is neither. Same shape as
 * `ProjectCard`'s `onSelect`, which opens this view.
 *
 * @param {{ run: { id?: number|string, run_id?: string, agent?: string,
 *                  status?: string, created_at?: string },
 *           onSelect?: (run: object) => void }} props
 */
export function RunRow({ run, onSelect }) {
  // Relative for the glance, the exact timestamp in the `title` for certainty.
  const relative = formatRelative(run?.created_at);
  const exact = formatTimestamp(run?.created_at);
  const selectable = typeof onSelect === 'function';

  return (
    <li
      data-testid={RUN_ROW_TESTID}
      data-run-id={run?.run_id}
      className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-4 py-3 transition-colors last:border-b-0 hover:bg-surface-muted"
    >
      {selectable ? (
        <button
          type="button"
          onClick={() => onSelect(run)}
          className="min-w-0 grow break-all text-left font-mono text-sm text-accent underline-offset-2 hover:underline"
        >
          {run?.run_id}
        </button>
      ) : (
        <span className="min-w-0 grow break-all font-mono text-sm text-fg">{run?.run_id}</span>
      )}
      <span className="rounded-pill border border-border bg-surface-muted px-2 py-0.5 text-xs text-fg-muted">
        {run?.agent}
      </span>
      <RunStatusBadge status={run?.status} />
      {relative !== '' && (
        <time dateTime={run?.created_at} title={exact} className="shrink-0 text-xs text-fg-subtle">
          {relative}
        </time>
      )}
    </li>
  );
}

/**
 * Exactly one row per Run, or the empty state (Requirements 19.2 and 19.3).
 *
 * Pure and total: any non-array (including the `[]` `unwrapResults` returns
 * for an unusable payload) is treated as no records, so this cannot throw on
 * `.map` and cannot render a row the caller supplied no data for.
 *
 * Rows keep the order they arrive in. `Run.Meta.ordering` is `-created_at`, so
 * that is newest first, and re-sorting here would only be a second, competing
 * definition of the history's order.
 *
 * `role="list"` is set explicitly because Safari drops list semantics from a
 * `<ul>` whose list-style is removed, which Tailwind's reset does.
 *
 * @param {{ runs?: unknown[], onSelectRun?: (run: object) => void }} props
 */
/** A one-line command box with a Copy button (best-effort clipboard). */
function CopyCommand({ command }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(command);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }
    } catch {
      /* clipboard unavailable; the command is still selectable */
    }
  }

  return (
    <div className="mt-4 flex items-stretch gap-2 text-left">
      <code className="min-w-0 grow overflow-x-auto rounded-card bg-surface-muted px-3 py-2 font-mono text-xs text-fg">
        {command}
      </code>
      <button
        type="button"
        onClick={copy}
        className="shrink-0 rounded-card border border-border bg-surface px-3 text-xs font-medium text-fg-muted hover:border-border-strong"
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

/** Requirement 19.3's empty state, made friendly: the message plus a copyable
 * `vaultshield run …` hint so a Reviewer knows how to produce the first Run. */
function EmptyRuns() {
  return (
    <div className="rounded-card-lg border border-dashed border-border bg-surface p-6 text-center">
      <p className="text-sm text-fg-muted">{EMPTY_RUNS_MESSAGE}</p>
      <CopyCommand command={RUN_HINT_COMMAND} />
    </div>
  );
}

export function RunList({ runs, onSelectRun }) {
  const records = Array.isArray(runs) ? runs : [];

  if (records.length === 0) {
    return <EmptyRuns />;
  }

  return (
    <ul
      role="list"
      className="overflow-hidden rounded-card-lg border border-border bg-surface shadow-soft"
    >
      {records.map((run, index) => (
        <RunRow key={run?.run_id ?? run?.id ?? index} run={run} onSelect={onSelectRun} />
      ))}
    </ul>
  );
}

/**
 * Fetch one Project's Runs and render them.
 *
 * The API client is a prop defaulting to the shared singleton, matching how
 * `src/api.test.js` injects a transport: a test drives this against
 * `createApiClient({ fetchImpl })` and asserts on real rendering, with no
 * global patched and no module mocked.
 *
 * `projectId` is what makes this a *Project's* run history rather than a list
 * of every Run on the instance, so a nullish one does not fetch at all — an
 * unfiltered `GET /api/runs/` would answer with other Projects' Runs, which
 * is a wrong answer to Requirement 19.2 rather than a slow one. That state
 * renders `NO_PROJECT_SELECTED_MESSAGE`.
 *
 * Only the first page is shown, as in `ProjectListView`: the backend
 * paginates at 50 (`PAGE_SIZE` in settings) and following `next` is a
 * separate concern from rendering one row per retrieved record.
 *
 * @param {{ projectId?: number|string|null, projectName?: string,
 *           client?: typeof api, onSelectRun?: (run: object) => void }} props
 */
export default function RunHistoryView({
  projectId = null,
  projectName = '',
  client = api,
  onSelectRun,
}) {
  // `null` distinguishes "not loaded yet" from the genuinely empty list, so
  // the first paint is a loading line rather than a flash of the empty state
  // — which would otherwise read as Requirement 19.3's "this Project has no
  // Runs" for every Project, briefly, on every load.
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    if (projectId === null || projectId === undefined || projectId === '') {
      // Clear whatever the previously selected Project loaded, so its Runs do
      // not stay on screen under a "select a project" prompt.
      setRuns(null);
      setError(null);
      return undefined;
    }

    const controller = new AbortController();
    let active = true;

    setRuns(null);
    setError(null);

    (async () => {
      try {
        const payload = await client.listRuns({ project: projectId, signal: controller.signal });
        if (active) setRuns(unwrapResults(payload));
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
  }, [client, projectId, reloadCount]);

  const selected = projectId !== null && projectId !== undefined && projectId !== '';
  const loading = selected && runs === null && error === null;
  const heading = projectName === '' ? 'Run history' : `Run history — ${projectName}`;

  return (
    <section aria-labelledby="run-history-heading">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h2 id="run-history-heading" className="text-xl font-semibold tracking-tight text-fg">
          {heading}
        </h2>
        {runs !== null && (
          <p className="shrink-0 text-xs text-fg-subtle">
            {runs.length} {runs.length === 1 ? 'run' : 'runs'}
          </p>
        )}
      </div>

      {!selected && <p className="text-sm text-fg-subtle">{NO_PROJECT_SELECTED_MESSAGE}</p>}

      {loading && (
        <div role="status" aria-busy="true" className="space-y-2">
          <span className="sr-only">Loading runs…</span>
          {[0, 1, 2].map((row) => (
            <div
              key={row}
              aria-hidden="true"
              className="h-12 animate-pulse rounded-card-lg border border-border bg-surface-muted"
            />
          ))}
        </div>
      )}

      {error !== null && (
        <div
          role="alert"
          className="rounded-card-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
        >
          <p>{describeLoadFailure(error, { subject: 'runs' })}</p>
          <button
            type="button"
            onClick={() => setReloadCount((count) => count + 1)}
            className="mt-3 rounded border border-red-300 bg-surface px-3 py-1 text-xs font-medium text-red-800 hover:bg-red-100 dark:border-red-900/60 dark:text-red-200"
          >
            Try again
          </button>
        </div>
      )}

      {runs !== null && <RunList runs={runs} onSelectRun={onSelectRun} />}
    </section>
  );
}
