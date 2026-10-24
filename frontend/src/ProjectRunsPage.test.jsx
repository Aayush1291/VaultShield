/**
 * Tests for the Project runs page (step 6): the table, the status filter, the
 * search box, and Load-more pagination.
 *
 * The page reads a route param and the layout's Outlet context, so it is
 * mounted inside a matching route tree that supplies both.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom';

import ProjectRunsPage, { FILTERED_EMPTY_MESSAGE } from './ProjectRunsPage.jsx';
import { EMPTY_RUNS_MESSAGE, RUN_ROW_TESTID } from './RunHistoryView.jsx';
import { AuthProvider } from './auth.jsx';
import { createApiClient, createMemoryTokenStore } from './api.js';

function resp(body) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: { get: () => 'application/json' },
    text: async () => JSON.stringify(body),
  };
}

const RUNS = [
  { id: 1, run_id: 'run-aaaa', agent: 'codex', status: 'awaiting_review', created_at: '2024-05-07T10:00:00Z' },
  { id: 2, run_id: 'run-bbbb', agent: 'claude', status: 'approved', created_at: '2024-05-07T11:00:00Z' },
  { id: 3, run_id: 'run-cccc', agent: 'codex', status: 'running', created_at: '2024-05-07T12:00:00Z' },
];

function renderRuns(fetchImpl) {
  const store = createMemoryTokenStore();
  store.set('active-token');
  const client = createApiClient({ fetchImpl, tokenStore: store });
  render(
    <MemoryRouter initialEntries={['/7']}>
      <AuthProvider client={client}>
        <Routes>
          <Route path=":projectId" element={<Outlet context={{ currentProject: { id: 7, name: 'checkout' } }} />}>
            <Route index element={<ProjectRunsPage />} />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const rowIds = () =>
  screen.queryAllByTestId(RUN_ROW_TESTID).map((row) => row.getAttribute('data-run-id'));

it('renders a table of runs, each id linking to its detail route, and Load more pages', async () => {
  const fetchImpl = jest.fn(async (url) => {
    if (url === '/api/runs/?project=7') {
      return resp({ count: 3, next: 'http://web:8000/api/runs/?project=7&page=2', results: [RUNS[0], RUNS[1]] });
    }
    if (url === '/api/runs/?project=7&page=2') {
      return resp({ count: 3, next: null, results: [RUNS[2]] });
    }
    return resp({ count: 0, next: null, results: [] });
  });

  renderRuns(fetchImpl);

  await waitFor(() => expect(rowIds()).toEqual(['run-aaaa', 'run-bbbb']));
  expect(screen.getByText(/Showing 2 of 3/)).toBeInTheDocument();
  // The run id is a link into the detail route.
  expect(screen.getByRole('link', { name: 'run-aaaa' })).toHaveAttribute('href', '/7/runs/run-aaaa');

  await userEvent.click(screen.getByRole('button', { name: 'Load more' }));

  await waitFor(() => expect(rowIds()).toEqual(['run-aaaa', 'run-bbbb', 'run-cccc']));
  expect(screen.getByText(/Showing 3 of 3/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
});

it('filters by status through the backend ?status= filter', async () => {
  const fetchImpl = jest.fn(async (url) => {
    if (url === '/api/runs/?project=7&status=approved') {
      return resp({ count: 1, next: null, results: [RUNS[1]] });
    }
    return resp({ count: 3, next: null, results: RUNS });
  });

  renderRuns(fetchImpl);
  await waitFor(() => expect(rowIds()).toHaveLength(3));

  await userEvent.click(screen.getByRole('button', { name: 'approved' }));

  await waitFor(() => expect(rowIds()).toEqual(['run-bbbb']));
  expect(fetchImpl).toHaveBeenCalledWith('/api/runs/?project=7&status=approved', expect.anything());
});

it('narrows the loaded rows by a run-id search, with a filtered-empty state', async () => {
  const fetchImpl = jest.fn(async () => resp({ count: 3, next: null, results: RUNS }));

  renderRuns(fetchImpl);
  await waitFor(() => expect(rowIds()).toHaveLength(3));

  await userEvent.type(screen.getByLabelText('Search runs by id'), 'bbbb');
  await waitFor(() => expect(rowIds()).toEqual(['run-bbbb']));

  await userEvent.clear(screen.getByLabelText('Search runs by id'));
  await userEvent.type(screen.getByLabelText('Search runs by id'), 'zzzz');
  expect(await screen.findByText(FILTERED_EMPTY_MESSAGE)).toBeInTheDocument();
  expect(rowIds()).toHaveLength(0);
});

it('shows the empty state when the project has no runs', async () => {
  const fetchImpl = jest.fn(async () => resp({ count: 0, next: null, results: [] }));

  renderRuns(fetchImpl);

  expect(await screen.findByText(EMPTY_RUNS_MESSAGE)).toBeInTheDocument();
});
