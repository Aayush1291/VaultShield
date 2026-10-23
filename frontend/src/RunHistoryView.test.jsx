/**
 * Unit tests for the run history view (Requirements 19.2 and 19.3).
 *
 * Fixed-example checks only. The exhaustive "exactly one row per record, with
 * its status, for any record list" claim is Property 27 (Task 25.4), so what
 * is here is what a generated-input property does not pin down: the URL
 * actually requested (including the `project` filter, without which the view
 * would show other Projects' Runs), the empty state, re-fetching when the
 * selected Project changes, and the failure paths a Reviewer can hit.
 *
 * The transport is injected via `createApiClient({ fetchImpl })`, the same way
 * `src/api.test.js` and `ProjectListView.test.jsx` do it, so these render the
 * real component against the real client with nothing mocked out of the
 * middle.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { createApiClient, createMemoryTokenStore } from './api.js';
import RunHistoryView, {
  EMPTY_RUNS_MESSAGE,
  NO_PROJECT_SELECTED_MESSAGE,
  RUN_ROW_TESTID,
  RUN_STATUS_TESTID,
} from './RunHistoryView.jsx';

/** A stand-in for `Response`, which jsdom does not provide. */
function jsonResponse(body, { status = 200, statusText = 'OK' } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

/** A page of Runs as `RunViewSet`'s paginated list endpoint returns one. */
function runPage(runs) {
  return jsonResponse({ count: runs.length, next: null, previous: null, results: runs });
}

/** An API client whose transport answers with the queued responses in order. */
function clientReturning(...responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = jest.fn(async (url, init) => {
    calls.push({ url, init });
    return queue.length > 1 ? queue.shift() : queue[0];
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });
  return { client, calls };
}

// One Run per Run_Status other than `failed`, which has its own test below,
// in `Run.STATUS_CHOICES` (Requirement 11.2), newest first as
// `Run.Meta.ordering` returns them.
const RUNS = [
  { id: 5, run_id: 'run-20240507-aaaa', agent: 'codex', status: 'running', created_at: '2024-05-07T09:00:00Z' },
  { id: 4, run_id: 'run-20240506-bbbb', agent: 'kiro', status: 'awaiting_review', created_at: '2024-05-06T09:00:00Z' },
  { id: 3, run_id: 'run-20240505-cccc', agent: 'antigravity', status: 'approved', created_at: '2024-05-05T09:00:00Z' },
  { id: 2, run_id: 'run-20240504-dddd', agent: 'mock', status: 'rejected', created_at: '2024-05-04T09:00:00Z' },
  { id: 1, run_id: 'run-20240503-eeee', agent: 'codex', status: 'submitted', created_at: null },
];

it("requests only the Project's Runs and renders one row per Run with its status", async () => {
  const { client, calls } = clientReturning(runPage(RUNS));

  render(<RunHistoryView projectId={7} projectName="checkout-service" client={client} />);

  const rows = await screen.findAllByTestId(RUN_ROW_TESTID);
  expect(rows).toHaveLength(RUNS.length);
  expect(calls).toHaveLength(1);
  // The `project` filter is what makes this a Project's run history rather
  // than every Run on the instance.
  expect(calls[0].url).toBe('/api/runs/?project=7');

  for (const [index, run] of RUNS.entries()) {
    expect(rows[index]).toHaveTextContent(run.run_id);
    expect(rows[index]).toHaveTextContent(run.agent);
    // Requirement 19.2: the Run_Status is displayed, as the value the backend
    // stores rather than a relabelled one.
    expect(rows[index]).toHaveTextContent(run.status);
  }

  const badges = screen.getAllByTestId(RUN_STATUS_TESTID);
  expect(badges.map((badge) => badge.textContent)).toEqual(RUNS.map((run) => run.status));

  expect(rows[0]).toHaveTextContent('2024-05-07');
  // `created_at: null` on the last record renders no date rather than an
  // "Invalid Date" one.
  expect(rows[4]).not.toHaveTextContent('Invalid');

  expect(screen.getByRole('heading', { name: /checkout-service/ })).toBeInTheDocument();
});

it('renders a failed Run with its own neutral badge rather than the unknown-status fallback', async () => {
  const failedRun = {
    id: 6,
    run_id: 'run-20240508-ffff',
    agent: 'codex',
    status: 'failed',
    created_at: '2024-05-08T09:00:00Z',
  };
  const { client } = clientReturning(runPage([failedRun]));

  render(<RunHistoryView projectId={7} client={client} />);

  const rows = await screen.findAllByTestId(RUN_ROW_TESTID);
  expect(rows).toHaveLength(1);

  const badge = screen.getByTestId(RUN_STATUS_TESTID);
  expect(badge).toHaveTextContent('failed');
  expect(badge).toHaveAttribute('data-status', 'failed');
  // `gray` is the explicit `failed` entry; `slate` would mean the badge fell
  // through to the unknown-status style.
  expect(badge).toHaveClass('bg-gray-100');
  expect(badge).not.toHaveClass('bg-slate-100');
});

it('renders a protect Run like any other row: its agent name and status, no new badge', async () => {
  // `protect sync` uploads a Run with agent `protect`; the history list shows it
  // exactly as today, with no special status.
  const protectRun = {
    id: 9,
    run_id: 'run-20240509-pppp',
    agent: 'protect',
    status: 'awaiting_review',
    created_at: '2024-05-09T09:00:00Z',
  };
  const { client } = clientReturning(runPage([protectRun]));

  render(<RunHistoryView projectId={7} client={client} />);

  const [row] = await screen.findAllByTestId(RUN_ROW_TESTID);
  expect(row).toHaveTextContent('protect');
  expect(row).toHaveTextContent('awaiting_review');
  const badge = screen.getByTestId(RUN_STATUS_TESTID);
  expect(badge).toHaveAttribute('data-status', 'awaiting_review');
});

it('renders an empty state when the Project has no Runs', async () => {
  const { client } = clientReturning(runPage([]));

  render(<RunHistoryView projectId={7} client={client} />);

  expect(await screen.findByText(EMPTY_RUNS_MESSAGE)).toBeInTheDocument();
  expect(screen.queryAllByTestId(RUN_ROW_TESTID)).toHaveLength(0);
});

it('does not request anything until a Project is selected', async () => {
  const { client, calls } = clientReturning(runPage(RUNS));

  render(<RunHistoryView projectId={null} client={client} />);

  expect(screen.getByText(NO_PROJECT_SELECTED_MESSAGE)).toBeInTheDocument();
  expect(calls).toHaveLength(0);
  // An unfiltered request would answer with other Projects' Runs, so nothing
  // is rendered as a run history either.
  expect(screen.queryAllByTestId(RUN_ROW_TESTID)).toHaveLength(0);
  expect(screen.queryByText(EMPTY_RUNS_MESSAGE)).not.toBeInTheDocument();
});

it('re-fetches for the newly selected Project and drops the previous one\u2019s Runs', async () => {
  const { client, calls } = clientReturning(runPage(RUNS), runPage([RUNS[0]]));

  const { rerender } = render(<RunHistoryView projectId={7} client={client} />);
  await waitFor(() => expect(screen.getAllByTestId(RUN_ROW_TESTID)).toHaveLength(RUNS.length));

  rerender(<RunHistoryView projectId={8} client={client} />);

  await waitFor(() => expect(screen.getAllByTestId(RUN_ROW_TESTID)).toHaveLength(1));
  expect(calls.map((call) => call.url)).toEqual(['/api/runs/?project=7', '/api/runs/?project=8']);
});

it('tells the Reviewer to sign in when the request is rejected as unauthenticated', async () => {
  const { client } = clientReturning(
    jsonResponse({ detail: 'Authentication credentials were not provided.' }, {
      status: 401,
      statusText: 'Unauthorized',
    }),
  );

  render(<RunHistoryView projectId={7} client={client} />);

  expect(await screen.findByRole('alert')).toHaveTextContent(/sign in to load runs/i);
  expect(screen.queryAllByTestId(RUN_ROW_TESTID)).toHaveLength(0);
  // The failure must not read as "this Project has no Runs" (Requirement 19.3
  // is about an empty list, not an unanswered request).
  expect(screen.queryByText(EMPTY_RUNS_MESSAGE)).not.toBeInTheDocument();
});

it('re-fetches when the Reviewer retries after a transport failure', async () => {
  const calls = [];
  let attempt = 0;
  const fetchImpl = jest.fn(async (url, init) => {
    calls.push({ url, init });
    attempt += 1;
    if (attempt === 1) throw new TypeError('Failed to fetch');
    return runPage([RUNS[0]]);
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });

  render(<RunHistoryView projectId={7} client={client} />);

  expect(await screen.findByRole('alert')).toHaveTextContent(/could not be reached/i);

  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

  await waitFor(() => expect(screen.getAllByTestId(RUN_ROW_TESTID)).toHaveLength(1));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(calls).toHaveLength(2);
});
