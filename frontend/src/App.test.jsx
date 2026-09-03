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
import { EMPTY_PROJECTS_MESSAGE } from './ProjectListView.jsx';
import { DIFF_FILE_TESTID, NO_RUN_SELECTED_MESSAGE } from './RunDetailView.jsx';
import { NO_PROJECT_SELECTED_MESSAGE, RUN_ROW_TESTID } from './RunHistoryView.jsx';

beforeEach(() => {
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
