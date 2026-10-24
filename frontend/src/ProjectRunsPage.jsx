/**
 * The `/dashboard/projects/:projectId` route: one Project's runs (step 6).
 *
 * A full-width table — run id (a link to the run's detail route), agent chip,
 * status chip, and a relative created time with the exact timestamp in its
 * `title`. Above it: status filter chips backed by the backend's `?status=`
 * filter, and a search box that narrows the loaded rows by run-id substring on
 * the client. Pagination follows DRF's `next` link: "Showing N of M" with a
 * Load more button that appends pages until `next` is null.
 *
 * The project's Delete control lives in the top bar (the layout), and a delete
 * navigates back to the overview — both handled there.
 */

import { useEffect, useState } from 'react';
import { Link, useOutletContext, useParams } from 'react-router-dom';

import { unwrapResults } from './api.js';
import { useAuth } from './auth.jsx';
import { describeLoadFailure, formatRelative, formatTimestamp } from './format.js';
import {
  EMPTY_RUNS_MESSAGE,
  RUN_ROW_TESTID,
  RunStatusBadge,
} from './RunHistoryView.jsx';

/** The six Run_Status values the filter offers, plus the implicit "All". */
export const RUN_STATUSES = [
  'running',
  'awaiting_review',
  'approved',
  'rejected',
  'submitted',
  'failed',
];

/** Shown when a status filter or search leaves no rows (vs. a project with no
 * runs at all, which gets `EMPTY_RUNS_MESSAGE`). */
export const FILTERED_EMPTY_MESSAGE = 'No runs match the current filter.';

export default function ProjectRunsPage() {
  const { projectId } = useParams();
  const { client } = useAuth();
  const { currentProject } = useOutletContext();

  const [status, setStatus] = useState(''); // '' = All
  const [search, setSearch] = useState('');
  const [runs, setRuns] = useState(null); // null = first load not finished
  const [count, setCount] = useState(0);
  const [next, setNext] = useState(null);
  const [error, setError] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  // First page: on project change, status change, or a retry. Resets the
  // accumulated rows so a filter switch cannot show the previous filter's runs.
  useEffect(() => {
    if (!projectId) return undefined;
    const controller = new AbortController();
    let active = true;
    setRuns(null);
    setError(null);
    setNext(null);
    setCount(0);
    (async () => {
      try {
        const payload = await client.listRuns({
          project: projectId,
          status: status || undefined,
          signal: controller.signal,
        });
        if (!active) return;
        setRuns(unwrapResults(payload));
        setCount(payload?.count ?? 0);
        setNext(payload?.next ?? null);
      } catch (cause) {
        if (active && !controller.signal.aborted) setError(cause);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [client, projectId, status, reloadKey]);

  async function loadMore() {
    if (!next || loadingMore) return;
    setLoadingMore(true);
    try {
      const payload = await client.followPage(next);
      setRuns((prev) => [...(prev ?? []), ...unwrapResults(payload)]);
      setCount((prev) => payload?.count ?? prev);
      setNext(payload?.next ?? null);
    } catch (cause) {
      setError(cause);
    } finally {
      setLoadingMore(false);
    }
  }

  const heading = currentProject?.name ? `Run history — ${currentProject.name}` : 'Run history';
  const loaded = runs ?? [];
  const term = search.trim().toLowerCase();
  const visible =
    term === ''
      ? loaded
      : loaded.filter((run) => String(run?.run_id ?? '').toLowerCase().includes(term));
  const firstLoad = runs === null && error === null;

  const chipClass = (active) =>
    `rounded-pill border px-3 py-1 text-xs font-medium ${
      active
        ? 'border-accent bg-accent-soft text-accent-strong'
        : 'border-border bg-surface text-fg-muted hover:border-border-strong'
    }`;

  return (
    <section aria-labelledby="runs-heading" className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="runs-heading" className="text-xl font-semibold tracking-tight text-fg">
          {heading}
        </h2>
        {runs !== null && (
          <p className="text-xs text-fg-subtle">
            Showing {loaded.length} of {count}
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by status">
        <button type="button" onClick={() => setStatus('')} aria-pressed={status === ''} className={chipClass(status === '')}>
          All
        </button>
        {RUN_STATUSES.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setStatus(value)}
            aria-pressed={status === value}
            className={chipClass(status === value)}
          >
            {value}
          </button>
        ))}
      </div>

      <div>
        <label htmlFor="run-search" className="sr-only">
          Search runs by id
        </label>
        <input
          id="run-search"
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search by run id…"
          className="w-full max-w-sm rounded-card border border-border bg-canvas px-3 py-2 text-sm text-fg placeholder:text-fg-subtle focus:border-accent"
        />
      </div>

      {firstLoad && (
        <div role="status" aria-busy="true" className="space-y-2">
          <span className="sr-only">Loading runs…</span>
          {[0, 1, 2].map((row) => (
            <div key={row} aria-hidden="true" className="h-12 animate-pulse rounded-card-lg border border-border bg-surface-muted" />
          ))}
        </div>
      )}

      {error !== null && (
        <div role="alert" className="rounded-card-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200">
          <p>{describeLoadFailure(error, { subject: 'runs' })}</p>
          <button
            type="button"
            onClick={() => setReloadKey((key) => key + 1)}
            className="mt-3 rounded border border-red-300 bg-surface px-3 py-1 text-xs font-medium text-red-800 hover:bg-red-100 dark:border-red-900/60 dark:text-red-200"
          >
            Try again
          </button>
        </div>
      )}

      {runs !== null && error === null && loaded.length === 0 && (
        <div className="rounded-card-lg border border-dashed border-border bg-surface p-6 text-center">
          <p className="text-sm text-fg-muted">{status === '' ? EMPTY_RUNS_MESSAGE : FILTERED_EMPTY_MESSAGE}</p>
        </div>
      )}

      {runs !== null && error === null && loaded.length > 0 && visible.length === 0 && (
        <div className="rounded-card-lg border border-dashed border-border bg-surface p-6 text-center">
          <p className="text-sm text-fg-muted">{FILTERED_EMPTY_MESSAGE}</p>
        </div>
      )}

      {visible.length > 0 && (
        <div className="overflow-hidden rounded-card-lg border border-border bg-surface shadow-soft">
          <table className="w-full border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-border text-xs uppercase tracking-wide text-fg-subtle">
                <th scope="col" className="px-4 py-2 font-medium">Run</th>
                <th scope="col" className="px-4 py-2 font-medium">Agent</th>
                <th scope="col" className="px-4 py-2 font-medium">Status</th>
                <th scope="col" className="px-4 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((run, index) => {
                const relative = formatRelative(run?.created_at);
                const exact = formatTimestamp(run?.created_at);
                return (
                  <tr
                    key={run?.run_id ?? run?.id ?? index}
                    data-testid={RUN_ROW_TESTID}
                    data-run-id={run?.run_id}
                    className="border-b border-border last:border-b-0 hover:bg-surface-muted"
                  >
                    <td className="px-4 py-3">
                      <Link
                        to={`runs/${encodeURIComponent(run?.run_id ?? '')}`}
                        className="break-all font-mono text-sm text-accent underline-offset-2 hover:underline"
                      >
                        {run?.run_id}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <span className="rounded-pill border border-border bg-surface-muted px-2 py-0.5 text-xs text-fg-muted">
                        {run?.agent}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <RunStatusBadge status={run?.status} />
                    </td>
                    <td className="px-4 py-3">
                      {relative !== '' && (
                        <time dateTime={run?.created_at} title={exact} className="text-xs text-fg-subtle">
                          {relative}
                        </time>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {next && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={loadMore}
            disabled={loadingMore}
            className="rounded-card border border-border bg-surface px-4 py-2 text-sm font-medium text-fg hover:border-border-strong disabled:opacity-60"
          >
            {loadingMore ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </section>
  );
}
