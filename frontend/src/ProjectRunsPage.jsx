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
    `rounded-pill border px-3.5 py-1.5 font-mono text-xs transition-colors ${
      active
        ? 'border-transparent bg-accent text-accent-fg'
        : 'border-border-strong bg-surface text-fg-muted hover:text-fg'
    }`;

  return (
    <section aria-labelledby="runs-heading" className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="runs-heading" className="font-display text-page font-normal text-fg">
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
          className="w-full max-w-sm rounded-control border border-border-strong bg-surface px-3.5 py-2.5 text-sm text-fg placeholder:text-fg-subtle focus:border-ring"
        />
      </div>

      {firstLoad && (
        <div role="status" aria-busy="true" className="space-y-2">
          <span className="sr-only">Loading runs…</span>
          {[0, 1, 2].map((row) => (
            <div key={row} aria-hidden="true" className="h-14 animate-pulse rounded-card bg-surface-2" />
          ))}
        </div>
      )}

      {error !== null && (
        <div role="alert" className="rounded-card-lg border border-danger/30 bg-danger-soft p-4 text-sm text-danger-fg">
          <p>{describeLoadFailure(error, { subject: 'runs' })}</p>
          <button
            type="button"
            onClick={() => setReloadKey((key) => key + 1)}
            className="mt-3 rounded-pill border border-danger bg-surface px-3 py-1 text-xs font-medium text-danger-fg hover:bg-danger-soft"
          >
            Try again
          </button>
        </div>
      )}

      {runs !== null && error === null && loaded.length === 0 && (
        <div className="rounded-card-xl border border-border bg-surface px-6 py-12 text-center shadow-soft">
          <h3 className="font-display text-[1.75rem] leading-tight font-normal text-fg">
            {status === '' ? 'No runs yet' : 'Nothing matches'}
          </h3>
          <p className="mx-auto mt-2 max-w-prose text-sm text-fg-muted">{status === '' ? EMPTY_RUNS_MESSAGE : FILTERED_EMPTY_MESSAGE}</p>
        </div>
      )}

      {runs !== null && error === null && loaded.length > 0 && visible.length === 0 && (
        <div className="rounded-card-xl border border-border bg-surface px-6 py-12 text-center shadow-soft">
          <h3 className="font-display text-[1.75rem] leading-tight font-normal text-fg">Nothing matches</h3>
          <p className="mx-auto mt-2 max-w-prose text-sm text-fg-muted">{FILTERED_EMPTY_MESSAGE}</p>
        </div>
      )}

      {visible.length > 0 && (
        <div className="overflow-x-auto rounded-card-lg border border-border bg-surface shadow-soft">
          <table className="w-full min-w-[40rem] border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-border text-[11px] uppercase tracking-[0.12em] text-fg-muted">
                <th scope="col" className="h-11 px-5 font-semibold">Run</th>
                <th scope="col" className="h-11 px-5 font-semibold">Agent</th>
                <th scope="col" className="h-11 px-5 font-semibold">Status</th>
                <th scope="col" className="h-11 px-5 font-semibold">Created</th>
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
                    className="h-14 border-b border-border transition-colors last:border-b-0 hover:bg-surface-2"
                  >
                    <td className="px-5 py-2">
                      <Link
                        to={`runs/${encodeURIComponent(run?.run_id ?? '')}`}
                        className="whitespace-nowrap font-mono text-sm font-medium text-accent-text underline-offset-4 hover:underline"
                      >
                        {run?.run_id}
                      </Link>
                    </td>
                    <td className="px-5 py-2">
                      <span className="rounded-pill bg-surface-2 px-2.5 py-0.5 font-mono text-xs text-fg-muted">
                        {run?.agent}
                      </span>
                    </td>
                    <td className="px-5 py-2">
                      <RunStatusBadge status={run?.status} />
                    </td>
                    <td className="px-5 py-2">
                      {relative !== '' && (
                        <time dateTime={run?.created_at} title={exact} className="text-sm text-fg-muted">
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
            className="rounded-pill border border-border-strong bg-surface px-4 py-2 text-sm font-medium text-fg transition-colors hover:bg-surface-2 disabled:opacity-60"
          >
            {loadingMore ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </section>
  );
}
