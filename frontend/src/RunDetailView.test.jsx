/**
 * Unit tests for the run detail view (Requirement 20).
 *
 * Fixed-example checks only. The exhaustive "exactly this record set, or the
 * empty state, for any record set" claim is Property 28 (Task 26.3), so what is
 * here is what a generated-input property does not pin down: the URL actually
 * requested, that all four tabs exist and each one shows its own record type,
 * the fields of each record that reach the screen, the chronological ordering of
 * the timeline, and the failure paths a Reviewer can hit.
 *
 * The transport is injected via `createApiClient({ fetchImpl })`, the same way
 * `src/api.test.js` and the other view tests do it, so these render the real
 * component against the real client with nothing mocked out of the middle.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { createApiClient, createMemoryTokenStore } from './api.js';
import RunDetailView, {
  DecisionControls,
  DIFF_FILE_TESTID,
  EMPTY_DIFF_MESSAGE,
  EMPTY_SCREENSHOTS_MESSAGE,
  EMPTY_SECURITY_MESSAGE,
  EMPTY_TIMELINE_MESSAGE,
  NO_RUN_SELECTED_MESSAGE,
  SCREENSHOT_TESTID,
  SECURITY_FINDING_TESTID,
  TIMELINE_EVENT_TESTID,
  sortEventsChronologically,
} from './RunDetailView.jsx';
import { RunStatusBadge } from './RunHistoryView.jsx';

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

/** A Run as `RunDetailSerializer` renders one, with all four artifact sets. */
const RUN = {
  id: 4,
  project: 7,
  run_id: 'run-20240506-bbbb',
  agent: 'codex',
  status: 'awaiting_review',
  pr_url: '',
  stuck: false,
  created_at: '2024-05-06T09:00:00Z',
  updated_at: '2024-05-06T09:30:00Z',
  events: [
    { id: 11, event_type: 'output', timestamp: '2024-05-06T09:00:01Z', payload: 'npm install', sequence: 0 },
    { id: 12, event_type: 'file_change', timestamp: '2024-05-06T09:00:02Z', payload: 'src/app.py', sequence: 1 },
    { id: 13, event_type: 'violation', timestamp: '2024-05-06T09:00:03Z', payload: 'wrote .env', sequence: 2 },
  ],
  diff: {
    id: 3,
    files: [
      { path: 'src/app.py', additions: 12, deletions: 4 },
      { path: 'README.md', additions: 1, deletions: 0 },
    ],
  },
  screenshots: [
    { id: 21, route: '/login', image: '/media/screenshots/login.png' },
    { id: 22, route: '/dashboard', image: '/media/screenshots/dashboard.png' },
  ],
  security_findings: [
    {
      id: 31,
      rule_id: 'hardcoded_secret',
      file_path: 'src/app.py',
      line_number: 42,
      description: 'A literal API key was added.',
    },
    {
      id: 32,
      rule_id: 'destructive_sql',
      // Nullable in the schema: a file-level finding has no single line.
      file_path: 'migrations/0002.sql',
      line_number: null,
      description: 'A DROP TABLE statement was added.',
    },
  ],
};

/** A Run with no artifacts at all, as one still in progress serializes. */
const BARE_RUN = {
  ...RUN,
  status: 'running',
  events: [],
  // `RunDiff` is a reverse one-to-one, so DRF renders `null` until the diff is
  // ingested.
  diff: null,
  screenshots: [],
  security_findings: [],
};

it('requests the Run by run_id and opens on the diff tab (Requirements 20.1, 20.2)', async () => {
  const { client, calls } = clientReturning(jsonResponse(RUN));

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  const files = await screen.findAllByTestId(DIFF_FILE_TESTID);
  expect(calls).toHaveLength(1);
  // `RunViewSet.lookup_field` is `run_id`, not the database pk.
  expect(calls[0].url).toBe(`/api/runs/${RUN.run_id}/`);

  // Requirement 20.1: four separate tabs, all four present at once.
  expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
    'Diff',
    'Timeline',
    'Screenshots',
    'Security',
  ]);
  expect(screen.getByRole('tab', { name: 'Diff' })).toHaveAttribute('aria-selected', 'true');

  // Requirement 20.2: the Run_Diff, one row per changed file with its counts.
  expect(files).toHaveLength(RUN.diff.files.length);
  expect(files[0]).toHaveTextContent('src/app.py');
  expect(files[0]).toHaveTextContent('+12');
  expect(files[0]).toHaveTextContent('-4');
  expect(files[1]).toHaveTextContent('README.md');

  // The identity of the Run being reviewed, so the tabs are attributable.
  expect(screen.getByText(RUN.run_id)).toBeInTheDocument();
  expect(screen.getByText(RUN.status)).toBeInTheDocument();

  // One request served every tab, so no other tab is fetching.
  expect(screen.queryAllByTestId(TIMELINE_EVENT_TESTID)).toHaveLength(0);
});

it('shows the Run_Event records in chronological order (Requirement 20.3)', async () => {
  // Delivered out of order on purpose: the tab's ordering claim has to hold on
  // what arrived, not on what the backend usually sends.
  const shuffled = [RUN.events[2], RUN.events[0], RUN.events[1]];
  const { client } = clientReturning(jsonResponse({ ...RUN, events: shuffled }));

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  await userEvent.click(await screen.findByRole('tab', { name: 'Timeline' }));

  const events = screen.getAllByTestId(TIMELINE_EVENT_TESTID);
  expect(events).toHaveLength(RUN.events.length);
  expect(events.map((event) => event.textContent)).toEqual([
    expect.stringContaining('npm install'),
    expect.stringContaining('src/app.py'),
    expect.stringContaining('wrote .env'),
  ]);
  expect(events[0]).toHaveTextContent('output');
  expect(events[0]).toHaveTextContent('2024-05-06 09:00:01');
  // Switching tabs replaces the panel rather than stacking it.
  expect(screen.queryAllByTestId(DIFF_FILE_TESTID)).toHaveLength(0);
});

it('shows each Screenshot with its route (Requirement 20.4)', async () => {
  const { client } = clientReturning(jsonResponse(RUN));

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  await userEvent.click(await screen.findByRole('tab', { name: 'Screenshots' }));

  expect(screen.getAllByTestId(SCREENSHOT_TESTID)).toHaveLength(RUN.screenshots.length);
  for (const shot of RUN.screenshots) {
    const image = screen.getByRole('img', { name: `Screenshot of ${shot.route}` });
    expect(image).toHaveAttribute('src', shot.image);
    expect(screen.getByText(shot.route)).toBeInTheDocument();
  }
});

it('shows each Security_Finding in full (Requirement 20.5)', async () => {
  const { client } = clientReturning(jsonResponse(RUN));

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  await userEvent.click(await screen.findByRole('tab', { name: 'Security' }));

  const findings = screen.getAllByTestId(SECURITY_FINDING_TESTID);
  expect(findings).toHaveLength(RUN.security_findings.length);
  expect(findings[0]).toHaveTextContent('hardcoded_secret');
  expect(findings[0]).toHaveTextContent('src/app.py:42');
  expect(findings[0]).toHaveTextContent('A literal API key was added.');
  // A null `line_number` renders the bare path, not `:null` or a made-up `:0`.
  expect(findings[1]).toHaveTextContent('migrations/0002.sql');
  expect(findings[1]).not.toHaveTextContent('null');
});

it("shows each tab's own empty state for a Run with no artifacts (Requirement 20.6)", async () => {
  const { client } = clientReturning(jsonResponse(BARE_RUN));

  render(<RunDetailView runId={BARE_RUN.run_id} client={client} />);

  // `diff: null` is the shape a Run whose diff has not been ingested arrives in.
  expect(await screen.findByText(EMPTY_DIFF_MESSAGE)).toBeInTheDocument();
  expect(screen.queryAllByTestId(DIFF_FILE_TESTID)).toHaveLength(0);

  for (const [label, message, itemTestId] of [
    ['Timeline', EMPTY_TIMELINE_MESSAGE, TIMELINE_EVENT_TESTID],
    ['Screenshots', EMPTY_SCREENSHOTS_MESSAGE, SCREENSHOT_TESTID],
    ['Security', EMPTY_SECURITY_MESSAGE, SECURITY_FINDING_TESTID],
  ]) {
    await userEvent.click(screen.getByRole('tab', { name: label }));
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryAllByTestId(itemTestId)).toHaveLength(0);
  }
});

it('renders an ingested diff of nothing as the empty state, not a bare panel', async () => {
  // `build_structured_diff` writes `{"files": []}` for a Run that changed no
  // files, so this is a real payload rather than a hypothetical one.
  const { client } = clientReturning(jsonResponse({ ...RUN, diff: { id: 3, files: [] } }));

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  expect(await screen.findByText(EMPTY_DIFF_MESSAGE)).toBeInTheDocument();
});

it('moves between tabs with the arrow keys', async () => {
  const { client } = clientReturning(jsonResponse(RUN));

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  const diffTab = await screen.findByRole('tab', { name: 'Diff' });
  diffTab.focus();

  await userEvent.keyboard('{ArrowRight}');
  expect(screen.getByRole('tab', { name: 'Timeline' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getAllByTestId(TIMELINE_EVENT_TESTID)).toHaveLength(RUN.events.length);

  await userEvent.keyboard('{End}');
  expect(screen.getByRole('tab', { name: 'Security' })).toHaveAttribute('aria-selected', 'true');
  // Wraps rather than stopping, so the tablist has no dead end.
  await userEvent.keyboard('{ArrowRight}');
  expect(diffTab).toHaveAttribute('aria-selected', 'true');
});

it('does not request anything until a Run is selected', async () => {
  const { client, calls } = clientReturning(jsonResponse(RUN));

  render(<RunDetailView runId={null} client={client} />);

  expect(screen.getByText(NO_RUN_SELECTED_MESSAGE)).toBeInTheDocument();
  expect(calls).toHaveLength(0);
  expect(screen.queryAllByRole('tab')).toHaveLength(0);
  // The prompt must not read as a tab's empty state (Requirement 20.6 is about
  // a selected Run with no data, not about no Run at all).
  expect(screen.queryByText(EMPTY_DIFF_MESSAGE)).not.toBeInTheDocument();
});

it('re-fetches for a newly selected Run and reopens on the diff tab', async () => {
  const other = { ...BARE_RUN, run_id: 'run-20240507-aaaa' };
  const { client, calls } = clientReturning(jsonResponse(RUN), jsonResponse(other));

  const { rerender } = render(<RunDetailView runId={RUN.run_id} client={client} />);
  await waitFor(() => expect(screen.getAllByTestId(DIFF_FILE_TESTID)).toHaveLength(2));

  await userEvent.click(screen.getByRole('tab', { name: 'Security' }));
  rerender(<RunDetailView runId={other.run_id} client={client} />);

  expect(await screen.findByText(EMPTY_DIFF_MESSAGE)).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: 'Diff' })).toHaveAttribute('aria-selected', 'true');
  expect(calls.map((call) => call.url)).toEqual([
    `/api/runs/${RUN.run_id}/`,
    `/api/runs/${other.run_id}/`,
  ]);
});

it('tells the Reviewer to sign in when the request is rejected as unauthenticated', async () => {
  const { client } = clientReturning(
    jsonResponse({ detail: 'Authentication credentials were not provided.' }, {
      status: 401,
      statusText: 'Unauthorized',
    }),
  );

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  expect(await screen.findByRole('alert')).toHaveTextContent(/sign in to load run details/i);
  expect(screen.queryAllByRole('tab')).toHaveLength(0);
  // A failed load must not read as "this Run has no diff".
  expect(screen.queryByText(EMPTY_DIFF_MESSAGE)).not.toBeInTheDocument();
});

it('re-fetches when the Reviewer retries after a transport failure', async () => {
  const calls = [];
  let attempt = 0;
  const fetchImpl = jest.fn(async (url, init) => {
    calls.push({ url, init });
    attempt += 1;
    if (attempt === 1) throw new TypeError('Failed to fetch');
    return jsonResponse(RUN);
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });

  render(<RunDetailView runId={RUN.run_id} client={client} />);

  expect(await screen.findByRole('alert')).toHaveTextContent(/could not be reached/i);

  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

  await waitFor(() => expect(screen.getAllByTestId(DIFF_FILE_TESTID)).toHaveLength(2));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(calls).toHaveLength(2);
});

it('offers no decision for a failed Run and shows its status as stored', () => {
  // A `failed` Run ended before it could be reviewed, and `apply_decision`
  // refuses anything but `awaiting_review`, so neither button is offered.
  render(<DecisionControls status="failed" runId="run-20240508-ffff" />);

  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull();

  render(<RunStatusBadge status="failed" />);

  const badge = screen.getByText('failed');
  expect(badge).toHaveAttribute('data-status', 'failed');
});

describe('sortEventsChronologically', () => {
  it('keeps every event, including ones it cannot place in time', () => {
    const undated = { id: 99, event_type: 'output', payload: 'no timestamp' };
    const sorted = sortEventsChronologically([undated, RUN.events[1], null, RUN.events[0]]);

    // Dropping an event would break Requirement 20.3 without breaking the
    // empty state — the disagreement the ordering is written to avoid.
    expect(sorted).toHaveLength(4);
    expect(sorted.slice(0, 2)).toEqual([RUN.events[0], RUN.events[1]]);
    // Unplaceable events sort last rather than being interleaved arbitrarily.
    expect(sorted.slice(2)).toEqual(expect.arrayContaining([undated, null]));
  });

  it('breaks a timestamp tie by the sequence the backend recorded', () => {
    const same = '2024-05-06T09:00:00Z';
    const second = { id: 2, timestamp: same, sequence: 1, payload: 'second' };
    const first = { id: 1, timestamp: same, sequence: 0, payload: 'first' };

    expect(sortEventsChronologically([second, first])).toEqual([first, second]);
  });

  it('is total on non-arrays and does not mutate its input', () => {
    const input = [RUN.events[1], RUN.events[0]];
    const sorted = sortEventsChronologically(input);

    expect(input).toEqual([RUN.events[1], RUN.events[0]]);
    expect(sorted).toEqual([RUN.events[0], RUN.events[1]]);
    expect(sortEventsChronologically(null)).toEqual([]);
    expect(sortEventsChronologically(undefined)).toEqual([]);
  });
});
