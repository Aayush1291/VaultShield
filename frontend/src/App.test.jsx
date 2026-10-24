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
