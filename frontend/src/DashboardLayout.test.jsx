/**
 * Tests for the dashboard shell's New project modal (step 5).
 *
 * Driven through the whole routed app at /dashboard so the modal, the sidebar
 * reload, and the navigate-to-new-project all run together the way they do in
 * the browser. The generic `Modal`'s own mechanics (focus, Escape, backdrop,
 * scroll lock) are exercised here through the one modal that uses it.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import App from './App.jsx';
import { createApiClient, createMemoryTokenStore } from './api.js';

function resp(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 201 ? 'Created' : 'OK',
    headers: { get: () => 'application/json' },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

const CREATED = {
  id: 7,
  name: 'new-service',
  repo_url: 'git@github.com:acme/new-service.git',
  created_at: '2024-06-01T10:00:00Z',
};

/** Projects list answers empty until a POST lands, then [CREATED]. */
function dashFetch({ createStatus = 201 } = {}) {
  let created = false;
  return jest.fn(async (url, init = {}) => {
    const method = init.method ?? 'GET';
    if (url === '/api/projects/' && method === 'POST') {
      created = true;
      return resp(createStatus === 201 ? CREATED : { name: ['already taken'] }, createStatus);
    }
    if (url.startsWith('/api/projects/')) {
      const list = created ? [CREATED] : [];
      return resp({ count: list.length, next: null, previous: null, results: list });
    }
    return resp({ count: 0, next: null, previous: null, results: [] });
  });
}

function renderDashboard(fetchImpl, path = '/dashboard') {
  const store = createMemoryTokenStore();
  store.set('active-token');
  const client = createApiClient({ fetchImpl, tokenStore: store });
  render(
    <MemoryRouter initialEntries={[path]}>
      <App client={client} />
    </MemoryRouter>,
  );
}

const PROJECT = { id: 7, name: 'checkout', repo_url: 'git@github.com:acme/checkout.git' };

/** Project list answers [PROJECT] until a DELETE lands, then empty. */
function deleteFetch() {
  let deleted = false;
  return jest.fn(async (url, init = {}) => {
    const method = init.method ?? 'GET';
    if (url.startsWith('/api/projects/') && method === 'DELETE') {
      deleted = true;
      return resp(undefined);
    }
    if (url.startsWith('/api/projects/')) {
      const list = deleted ? [] : [PROJECT];
      return resp({ count: list.length, next: null, results: list });
    }
    return resp({ count: 0, next: null, results: [] });
  });
}

it('opens an accessible New project modal from the sidebar and moves focus into it', async () => {
  renderDashboard(dashFetch());

  // The sidebar and the overview each have a New project button; use the
  // sidebar's (first in DOM order), which owns the focus-return ref.
  await screen.findByRole('button', { name: 'Sign out' });
  const trigger = screen.getAllByRole('button', { name: 'New project' })[0];
  await userEvent.click(trigger);

  const dialog = screen.getByRole('dialog', { name: 'New project' });
  expect(dialog).toHaveAttribute('aria-modal', 'true');
  // Focus moved into the modal (its first field).
  expect(screen.getByLabelText('Name')).toHaveFocus();
});

it('closes the modal on Escape and returns focus to the opener, without creating', async () => {
  const fetchImpl = dashFetch();
  renderDashboard(fetchImpl);

  await screen.findByRole('button', { name: 'Sign out' });
  const trigger = screen.getAllByRole('button', { name: 'New project' })[0];
  await userEvent.click(trigger);
  await userEvent.keyboard('{Escape}');

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(fetchImpl).not.toHaveBeenCalledWith('/api/projects/', expect.objectContaining({ method: 'POST' }));
});

it('creates a project, closes the modal, reloads the sidebar, and opens the new project', async () => {
  const fetchImpl = dashFetch();
  renderDashboard(fetchImpl);

  await screen.findByRole('button', { name: 'Sign out' });
  await userEvent.click(screen.getAllByRole('button', { name: 'New project' })[0]);
  await userEvent.type(screen.getByLabelText('Name'), 'new-service');
  await userEvent.type(screen.getByLabelText('Repository URL'), 'git@github.com:acme/new-service.git');
  await userEvent.click(screen.getByRole('button', { name: 'Create project' }));

  // The POST was made.
  await waitFor(() =>
    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/projects/',
      expect.objectContaining({ method: 'POST' }),
    ),
  );

  // The modal closed and we navigated to the new project's run history.
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(
    await screen.findByRole('heading', { name: `Run history — ${CREATED.name}` }),
  ).toBeInTheDocument();
});

it('surfaces a backend 400 inside the still-open modal', async () => {
  renderDashboard(dashFetch({ createStatus: 400 }));

  await screen.findByRole('button', { name: 'Sign out' });
  await userEvent.click(screen.getAllByRole('button', { name: 'New project' })[0]);
  await userEvent.type(screen.getByLabelText('Name'), 'dupe');
  await userEvent.type(screen.getByLabelText('Repository URL'), 'git@github.com:acme/dupe.git');
  await userEvent.click(screen.getByRole('button', { name: 'Create project' }));

  // The field error is shown and the modal stays open.
  expect(await screen.findByText('already taken')).toBeInTheDocument();
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});

// --- delete project (top-bar control on a project route) -------------------

it('shows the Delete project control on a project route and confirms before deleting', async () => {
  renderDashboard(deleteFetch(), '/dashboard/projects/7');

  // The control appears once the project is resolved from the loaded list.
  const trigger = await screen.findByRole('button', { name: 'Delete project' });
  await userEvent.click(trigger);

  const dialog = screen.getByRole('dialog');
  expect(dialog).toHaveAttribute('aria-modal', 'true');
  expect(dialog).toHaveTextContent(PROJECT.name);
  expect(dialog).toHaveTextContent(/runs/i);
});

it('deletes the project and navigates back to the overview', async () => {
  const fetchImpl = deleteFetch();
  renderDashboard(fetchImpl, '/dashboard/projects/7');

  await userEvent.click(await screen.findByRole('button', { name: 'Delete project' }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

  await waitFor(() =>
    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/projects/7/',
      expect.objectContaining({ method: 'DELETE' }),
    ),
  );
  // Back on the overview, and the control is gone. The empty message shows in
  // both the sidebar and the overview once the project list is empty.
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect((await screen.findAllByText(/No projects yet/i)).length).toBeGreaterThan(0);
  expect(screen.queryByRole('button', { name: 'Delete project' })).not.toBeInTheDocument();
});

it('cancels the delete on Escape without deleting', async () => {
  const fetchImpl = deleteFetch();
  renderDashboard(fetchImpl, '/dashboard/projects/7');

  const trigger = await screen.findByRole('button', { name: 'Delete project' });
  await userEvent.click(trigger);
  await userEvent.keyboard('{Escape}');

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(fetchImpl).not.toHaveBeenCalledWith('/api/projects/7/', expect.objectContaining({ method: 'DELETE' }));
});
