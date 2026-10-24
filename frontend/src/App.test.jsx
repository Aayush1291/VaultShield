/**
 * Wiring tests for the dashboard shell.
 *
 * The shell holds one piece of logic — which Project the run history is for —
 * so this checks that it renders, that both views are mounted inside it
 * (a view nothing mounts is a view no Reviewer can reach), and that selecting
 * a Project is what opens that Project's run history (Requirement 19.2). Each
 * view's own behaviour is covered in `ProjectListView.test.jsx` and
 * `RunHistoryView.test.jsx`.
 *
 * `globalThis.fetch` is stubbed because the shell uses the shared `api`
 * singleton, which resolves its transport from the global — and jsdom
 * provides none. An empty page is enough for the first two tests: they assert
 * on wiring, not on what the lists do with records.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import App from './App.jsx';
import { TOKEN_STORAGE_KEY, createApiClient, createMemoryTokenStore } from './api.js';
import { EMPTY_PROJECTS_MESSAGE } from './ProjectListView.jsx';
import { DIFF_FILE_TESTID, NO_RUN_SELECTED_MESSAGE } from './RunDetailView.jsx';
import { NO_PROJECT_SELECTED_MESSAGE, RUN_ROW_TESTID } from './RunHistoryView.jsx';

beforeEach(() => {
  // The shell now shows the login screen when no Dashboard_Auth_Token is
  // stored. These tests assert on the signed-in dashboard, so a fake token is
  // seeded first; it is never a real credential and never sent anywhere (the
  // transport is the stub below). Cleared in afterEach.
  globalThis.localStorage?.setItem(TOKEN_STORAGE_KEY, 'fake-test-token');
  globalThis.fetch = jest.fn(async () => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: { get: () => 'application/json' },
    text: async () => JSON.stringify({ count: 0, next: null, previous: null, results: [] }),
  }));
});

afterEach(() => {
  delete globalThis.fetch;
  globalThis.localStorage?.clear();
});

it('renders the dashboard shell', async () => {
  render(<App />);

  expect(screen.getByRole('heading', { name: 'VaultShield' })).toBeInTheDocument();
  expect(await screen.findByText(EMPTY_PROJECTS_MESSAGE)).toBeInTheDocument();
});

it('mounts the project list view, which fetches the project list endpoint', async () => {
  render(<App />);

  expect(await screen.findByRole('heading', { name: 'Projects' })).toBeInTheDocument();
  expect(globalThis.fetch).toHaveBeenCalledWith('/api/projects/', expect.anything());
});

it('mounts the run history view, which waits for a Project to be selected', async () => {
  render(<App />);

  expect(await screen.findByRole('heading', { name: 'Run history' })).toBeInTheDocument();
  expect(screen.getByText(NO_PROJECT_SELECTED_MESSAGE)).toBeInTheDocument();
  // No Project selected yet, so nothing has asked for a filtered run list.
  expect(globalThis.fetch).not.toHaveBeenCalledWith(
    expect.stringContaining('/api/runs/'),
    expect.anything(),
  );
});

it('mounts the run detail view, which waits for a Run to be selected', async () => {
  render(<App />);

  expect(await screen.findByRole('heading', { name: 'Run detail' })).toBeInTheDocument();
  expect(screen.getByText(NO_RUN_SELECTED_MESSAGE)).toBeInTheDocument();
});

/** The project, run list, and run detail responses the two views below need. */
function dashboardFetch({ project, run, detail }) {
  return jest.fn(async (url) => {
    const body = url.startsWith('/api/projects/')
      ? { count: 1, next: null, previous: null, results: [project] }
      : url === `/api/runs/?project=${project.id}`
        ? { count: 1, next: null, previous: null, results: [run] }
        : detail;

    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: { get: () => 'application/json' },
      text: async () => JSON.stringify(body),
    };
  });
}

const PROJECT = { id: 42, name: 'checkout-service', repo_url: 'git@github.com:acme/checkout.git' };
const RUN = { id: 1, run_id: 'run-20240507-aaaa', agent: 'codex', status: 'awaiting_review' };
const RUN_DETAIL = {
  ...RUN,
  events: [],
  diff: { id: 1, files: [{ path: 'src/app.py', additions: 3, deletions: 1 }] },
  screenshots: [],
  security_findings: [],
};

it("opens the selected Project's run history (Requirement 19.2)", async () => {
  globalThis.fetch = dashboardFetch({ project: PROJECT, run: RUN, detail: RUN_DETAIL });

  render(<App />);

  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));

  await waitFor(() => expect(screen.getAllByTestId(RUN_ROW_TESTID)).toHaveLength(1));
  expect(screen.getByRole('heading', { name: `Run history — ${PROJECT.name}` })).toBeInTheDocument();
  expect(globalThis.fetch).toHaveBeenCalledWith(
    `/api/runs/?project=${PROJECT.id}`,
    expect.anything(),
  );
});

it('opens the selected Run in the detail view (Requirement 20.1)', async () => {
  globalThis.fetch = dashboardFetch({ project: PROJECT, run: RUN, detail: RUN_DETAIL });

  render(<App />);

  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  // The Run is addressed by its `run_id`, which is also what the row shows.
  await userEvent.click(await screen.findByRole('button', { name: RUN.run_id }));

  await waitFor(() => expect(screen.getAllByTestId(DIFF_FILE_TESTID)).toHaveLength(1));
  expect(globalThis.fetch).toHaveBeenCalledWith(`/api/runs/${RUN.run_id}/`, expect.anything());
  expect(screen.getAllByRole('tab')).toHaveLength(4);
});

it('closes the open Run when a different Project is selected', async () => {
  const other = { id: 43, name: 'billing-service', repo_url: 'git@github.com:acme/billing.git' };
  globalThis.fetch = jest.fn(async (url) => {
    const body = url.startsWith('/api/projects/')
      ? { count: 2, next: null, previous: null, results: [PROJECT, other] }
      : url.startsWith('/api/runs/?project=')
        ? {
            count: 1,
            next: null,
            previous: null,
            results: url.endsWith(String(PROJECT.id)) ? [RUN] : [],
          }
        : RUN_DETAIL;

    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: { get: () => 'application/json' },
      text: async () => JSON.stringify(body),
    };
  });

  render(<App />);

  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  await userEvent.click(await screen.findByRole('button', { name: RUN.run_id }));
  await waitFor(() => expect(screen.getAllByTestId(DIFF_FILE_TESTID)).toHaveLength(1));

  await userEvent.click(screen.getByRole('button', { name: other.name }));

  // The open Run belonged to the previous Project, so it cannot stay on screen
  // under the new Project's run list.
  expect(await screen.findByText(NO_RUN_SELECTED_MESSAGE)).toBeInTheDocument();
  expect(screen.queryAllByTestId(DIFF_FILE_TESTID)).toHaveLength(0);
});

it('shows only the selected Project\'s runs, never mixing two Projects (Requirement 19.2)', async () => {
  // Two Projects, each with its own Runs. The backend scopes the list by the
  // `?project=` query parameter (the step-1 fix), so switching Projects must
  // replace the run rows wholesale — a Run of the Project no longer selected
  // must never remain on screen.
  const other = { id: 43, name: 'billing-service', repo_url: 'git@github.com:acme/billing.git' };
  const runsA = [
    { id: 1, run_id: 'run-20240507-aaaa', agent: 'codex', status: 'awaiting_review' },
    { id: 2, run_id: 'run-20240507-aaab', agent: 'codex', status: 'approved' },
  ];
  const runsB = [{ id: 3, run_id: 'run-20240507-bbbb', agent: 'claude', status: 'running' }];

  globalThis.fetch = jest.fn(async (url) => {
    let body;
    if (url.startsWith('/api/projects/')) {
      body = { count: 2, next: null, previous: null, results: [PROJECT, other] };
    } else if (url === `/api/runs/?project=${PROJECT.id}`) {
      body = { count: runsA.length, next: null, previous: null, results: runsA };
    } else if (url === `/api/runs/?project=${other.id}`) {
      body = { count: runsB.length, next: null, previous: null, results: runsB };
    } else {
      // Any unscoped `/api/runs/` (every Project's Runs) would be the bug this
      // guards against, so answer it with a mix that the assertions reject.
      body = { count: 3, next: null, previous: null, results: [...runsA, ...runsB] };
    }
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: { get: () => 'application/json' },
      text: async () => JSON.stringify(body),
    };
  });

  const runIdsOnScreen = () =>
    screen.queryAllByTestId(RUN_ROW_TESTID).map((row) => row.getAttribute('data-run-id'));

  render(<App />);

  // Select Project A: exactly A's Runs, in order, and nothing of B's.
  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  await waitFor(() =>
    expect(runIdsOnScreen()).toEqual([runsA[0].run_id, runsA[1].run_id]),
  );

  // Switch to Project B: only B's Run remains; both of A's are gone.
  await userEvent.click(screen.getByRole('button', { name: other.name }));
  await waitFor(() => expect(runIdsOnScreen()).toEqual([runsB[0].run_id]));
  expect(screen.queryByText(runsA[0].run_id)).not.toBeInTheDocument();
  expect(screen.queryByText(runsA[1].run_id)).not.toBeInTheDocument();

  // Each list was fetched scoped to its own Project id, never unfiltered.
  expect(globalThis.fetch).toHaveBeenCalledWith(
    `/api/runs/?project=${PROJECT.id}`,
    expect.anything(),
  );
  expect(globalThis.fetch).toHaveBeenCalledWith(
    `/api/runs/?project=${other.id}`,
    expect.anything(),
  );
});

// --- authentication gate (single-user MVP auth) ----------------------------
//
// These inject a client built on a memory token store, so the signed-in state
// is controlled by the test and the beforeEach's seeded localStorage token is
// irrelevant to them. Every token/credential here is invented.

/** A stand-in for `Response`, which jsdom does not provide. */
function authJsonResponse(body, { status = 200, statusText = 'OK' } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

it('shows the login screen with no token, then the dashboard after signing in', async () => {
  const fetchImpl = jest.fn(async (url) => {
    if (url === '/api/auth/login/') return authJsonResponse({ token: 'tok-after-login' });
    return authJsonResponse({ count: 0, next: null, previous: null, results: [] });
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });

  render(<App client={client} />);

  // No token: the login screen, not the three views.
  expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Projects' })).not.toBeInTheDocument();

  await userEvent.type(screen.getByLabelText('Username'), 'reviewer');
  await userEvent.type(screen.getByLabelText('Password'), 'correct horse');
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));

  // The dashboard replaces the login screen, with a Sign out control.
  expect(await screen.findByRole('heading', { name: 'Projects' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument();
  expect(client.getToken()).toBe('tok-after-login');
});

it('returns to the login screen and clears the token when a view is refused (401)', async () => {
  const fetchImpl = jest.fn(async () =>
    authJsonResponse(
      { detail: 'Authentication credentials were not provided.' },
      { status: 401, statusText: 'Unauthorized' },
    ),
  );
  const store = createMemoryTokenStore();
  store.set('stale-token');
  const client = createApiClient({ fetchImpl, tokenStore: store });

  render(<App client={client} />);

  // A stored token means the dashboard renders first; the project fetch is
  // then refused as unauthenticated, which sends the Reviewer back to login.
  expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  expect(store.get()).toBeNull();
});

it('signs out, clearing the token and returning to the login screen', async () => {
  const fetchImpl = jest.fn(async () =>
    authJsonResponse({ count: 0, next: null, previous: null, results: [] }),
  );
  const store = createMemoryTokenStore();
  store.set('active-token');
  const client = createApiClient({ fetchImpl, tokenStore: store });

  render(<App client={client} />);

  await screen.findByRole('heading', { name: 'Projects' });
  await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));

  expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  expect(store.get()).toBeNull();
});

// --- redesigned shell: drawer and selected-project highlight ---------------

it('toggles the navigation drawer open and closed', async () => {
  globalThis.fetch = dashboardFetch({ project: PROJECT, run: RUN, detail: RUN_DETAIL });

  render(<App />);
  // Wait for the dashboard (sidebar project list) to load.
  await screen.findByRole('button', { name: PROJECT.name });

  const toggle = screen.getByRole('button', { name: 'Open navigation' });
  expect(toggle).toHaveAttribute('aria-controls', 'vs-sidebar');
  expect(toggle).toHaveAttribute('aria-expanded', 'false');

  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'true');

  // Opening reveals a backdrop that closes the drawer again.
  await userEvent.click(screen.getByRole('button', { name: 'Close navigation' }));
  expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveAttribute(
    'aria-expanded',
    'false',
  );
});

it('highlights the selected project in the sidebar', async () => {
  globalThis.fetch = dashboardFetch({ project: PROJECT, run: RUN, detail: RUN_DETAIL });

  render(<App />);
  const projectButton = await screen.findByRole('button', { name: PROJECT.name });

  // Nothing selected yet: the card is not marked current.
  expect(projectButton.closest('[data-testid="project-card"]')).not.toHaveAttribute(
    'aria-current',
  );

  await userEvent.click(projectButton);

  // Selecting it marks its card as the current one.
  expect(
    screen.getByRole('button', { name: PROJECT.name }).closest('[data-testid="project-card"]'),
  ).toHaveAttribute('aria-current', 'true');
});

// --- delete project: top-bar control, confirm dialog, and flow -------------

const OTHER_PROJECT = {
  id: 43,
  name: 'billing-service',
  repo_url: 'git@github.com:acme/billing.git',
};

/** A fetch mock for the delete flow. The project list answers `before` until
 *  a DELETE has landed, then `after`; runs are always empty here. */
function deleteFlowFetch({ before, after, deleteStatus = 204 } = {}) {
  let deleted = false;
  const respond = (body, status) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 204 ? '' : 'OK',
    headers: { get: () => 'application/json' },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  });

  return jest.fn(async (url, init = {}) => {
    const method = init.method ?? 'GET';
    if (url.startsWith('/api/projects/') && method === 'DELETE') {
      deleted = true;
      return respond(undefined, deleteStatus);
    }
    if (url.startsWith('/api/projects/')) {
      const list = deleted ? after ?? [] : before ?? [];
      return respond({ count: list.length, next: null, previous: null, results: list }, 200);
    }
    // Any /api/runs/ fetch: no runs for this flow.
    return respond({ count: 0, next: null, previous: null, results: [] }, 200);
  });
}

it('shows the Delete project control only once a Project is selected', async () => {
  globalThis.fetch = deleteFlowFetch({ before: [PROJECT] });

  render(<App />);
  const projectButton = await screen.findByRole('button', { name: PROJECT.name });

  // Nothing selected: no delete control in the top bar.
  expect(screen.queryByRole('button', { name: 'Delete project' })).not.toBeInTheDocument();

  await userEvent.click(projectButton);

  expect(screen.getByRole('button', { name: 'Delete project' })).toBeInTheDocument();
});

it('opens a confirm dialog that names the Project and its runs', async () => {
  globalThis.fetch = deleteFlowFetch({ before: [PROJECT] });

  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete project' }));

  const dialog = screen.getByRole('dialog');
  expect(dialog).toHaveAttribute('aria-modal', 'true');
  expect(dialog).toHaveTextContent(PROJECT.name);
  expect(dialog).toHaveTextContent(/runs/i);
  // Confirming has not happened, so nothing was deleted yet.
  expect(globalThis.fetch).not.toHaveBeenCalledWith(
    expect.stringMatching(/\/api\/projects\/.+\//),
    expect.objectContaining({ method: 'DELETE' }),
  );
});

it('moves focus into the dialog on open and back to the trigger on cancel', async () => {
  globalThis.fetch = deleteFlowFetch({ before: [PROJECT] });

  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  const trigger = screen.getByRole('button', { name: 'Delete project' });
  await userEvent.click(trigger);

  // Focus landed inside the dialog (its Cancel control).
  expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();

  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

  // Dialog gone, focus handed back to the control that opened it.
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Delete project' })).toHaveFocus();
});

it('cancels on Escape without deleting anything', async () => {
  globalThis.fetch = deleteFlowFetch({ before: [PROJECT] });

  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete project' }));

  await userEvent.keyboard('{Escape}');

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  // The Project is still selected (its run history heading is still shown).
  expect(screen.getByRole('heading', { name: `Run history — ${PROJECT.name}` })).toBeInTheDocument();
  expect(globalThis.fetch).not.toHaveBeenCalledWith(
    expect.stringMatching(/\/api\/projects\/.+\//),
    expect.objectContaining({ method: 'DELETE' }),
  );
});

it('deletes the Project, clears the selection, and reloads the sidebar without it', async () => {
  globalThis.fetch = deleteFlowFetch({ before: [PROJECT, OTHER_PROJECT], after: [OTHER_PROJECT] });

  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete project' }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

  // The DELETE hit the selected Project's detail URL.
  await waitFor(() =>
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `/api/projects/${PROJECT.id}/`,
      expect.objectContaining({ method: 'DELETE' }),
    ),
  );

  // Dialog closed, selection cleared (run history is back to its prompt), and
  // the reloaded sidebar no longer lists the deleted Project.
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByText(NO_PROJECT_SELECTED_MESSAGE)).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: PROJECT.name })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole('button', { name: OTHER_PROJECT.name })).toBeInTheDocument();
});

it('keeps the dialog open and shows an error when the delete fails', async () => {
  globalThis.fetch = deleteFlowFetch({ before: [PROJECT], deleteStatus: 500 });

  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: PROJECT.name }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete project' }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

  // The failure is announced and the dialog stays open so the Reviewer can
  // retry or cancel; the Project is still selected.
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: `Run history — ${PROJECT.name}` })).toBeInTheDocument();
});
