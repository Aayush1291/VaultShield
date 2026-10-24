/**
 * Tests for the run detail page's tab-in-URL behaviour (step 7).
 *
 * Driven through the routed app at a run-detail deep link so the `?tab=`
 * param, the back link, and the detail view all work together. A location
 * probe lets the test read the URL after a tab switch.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';

import App from './App.jsx';
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

const PROJECT = { id: 7, name: 'checkout', repo_url: 'git@github.com:acme/checkout.git' };
const RUN_DETAIL = {
  id: 1,
  run_id: 'run-x',
  agent: 'codex',
  status: 'approved',
  events: [],
  diff: { id: 1, files: [{ path: 'src/app.py', additions: 3, deletions: 1 }] },
  screenshots: [],
  security_findings: [
    { id: 1, rule_id: 'no-secrets', file_path: 'app.py', line_number: 4, description: 'Found a secret' },
  ],
};

function detailFetch() {
  return jest.fn(async (url) => {
    if (url.startsWith('/api/projects/')) {
      return resp({ count: 1, next: null, previous: null, results: [PROJECT] });
    }
    if (url.startsWith('/api/runs/run-x/')) return resp(RUN_DETAIL);
    return resp({ count: 0, next: null, previous: null, results: [] });
  });
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="loc">{location.pathname + location.search}</div>;
}

function renderAt(url) {
  const store = createMemoryTokenStore();
  store.set('active-token');
  const client = createApiClient({ fetchImpl: detailFetch(), tokenStore: store });
  render(
    <MemoryRouter initialEntries={[url]}>
      <App client={client} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

it('opens the tab named in the URL on a deep link', async () => {
  renderAt('/dashboard/projects/7/runs/run-x?tab=security');

  // Only the selected tab's panel is mounted, so the finding shows and the
  // diff file does not.
  expect(await screen.findByText('Found a secret')).toBeInTheDocument();
  expect(screen.queryByText('src/app.py')).not.toBeInTheDocument();
});

it('defaults to the diff tab when no tab is in the URL', async () => {
  renderAt('/dashboard/projects/7/runs/run-x');

  expect(await screen.findByText('src/app.py')).toBeInTheDocument();
  expect(screen.queryByText('Found a secret')).not.toBeInTheDocument();
});

it('writes the selected tab into the URL when a tab is clicked', async () => {
  renderAt('/dashboard/projects/7/runs/run-x?tab=security');

  await screen.findByText('Found a secret');
  await userEvent.click(screen.getByRole('tab', { name: 'Diff' }));

  // The diff panel is now shown and the URL carries ?tab=diff.
  expect(await screen.findByText('src/app.py')).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByTestId('loc')).toHaveTextContent('/dashboard/projects/7/runs/run-x?tab=diff'),
  );
});

it('offers a back link to the project runs', async () => {
  renderAt('/dashboard/projects/7/runs/run-x');

  const back = await screen.findByRole('link', { name: /Back to runs/ });
  expect(back).toHaveAttribute('href', '/dashboard/projects/7');
});
