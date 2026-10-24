/**
 * Routing and auth-guard tests for the application.
 *
 * `App` is now the route table, so these mount it inside a `MemoryRouter` at a
 * chosen URL and assert which page renders and where the guards send an
 * un/authenticated Reviewer. The per-page behaviour (run history, run detail,
 * the project list, the delete flow) is covered by each page/component's own
 * test; what is here is the navigation contract.
 *
 * Every client is built on a memory token store with an injected transport, so
 * the signed-in state is the test's to control and nothing touches globals or
 * a real backend. Every token/credential is invented.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import App from './App.jsx';
import { createApiClient, createMemoryTokenStore } from './api.js';

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

/** Default transport: an empty paginated list for any GET, a token for login. */
function defaultFetch() {
  return jest.fn(async (url) => {
    if (url === '/api/auth/login/') return jsonResponse({ token: 'tok-after-login' });
    return jsonResponse({ count: 0, next: null, previous: null, results: [] });
  });
}

function renderApp(path, { token = null, fetchImpl } = {}) {
  const store = createMemoryTokenStore();
  if (token) store.set(token);
  const client = createApiClient({ fetchImpl: fetchImpl ?? defaultFetch(), tokenStore: store });
  render(
    <MemoryRouter initialEntries={[path]}>
      <App client={client} />
    </MemoryRouter>,
  );
  return { store, client };
}

it('renders the landing page at /', () => {
  renderApp('/');

  expect(screen.getByRole('link', { name: 'Log in' })).toHaveAttribute('href', '/login');
  expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup');
  expect(screen.getByTestId('install-cmd')).toBeInTheDocument();
});

it('shows a Not Found page with a link home for an unknown route', () => {
  renderApp('/no-such-page');

  expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Go home' })).toHaveAttribute('href', '/');
});

it('redirects a signed-out visitor from a protected route to /login', async () => {
  renderApp('/dashboard');

  expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
});

it('redirects a signed-in visitor away from /login to the dashboard', async () => {
  renderApp('/login', { token: 'active-token' });

  expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument();
});

it('signs in and returns to the page the Reviewer first asked for', async () => {
  // Deep-link to a protected page while signed out: the guard bounces to
  // /login remembering where we were headed, and sign-in returns us there.
  renderApp('/dashboard', { fetchImpl: defaultFetch() });

  await screen.findByRole('heading', { name: 'Sign in' });
  await userEvent.type(screen.getByLabelText('Username'), 'reviewer');
  await userEvent.type(screen.getByLabelText('Password'), 'correct horse');
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));

  // Back on the dashboard, not the login screen.
  expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument();
});

it('renders the dashboard for a signed-in Reviewer', async () => {
  renderApp('/dashboard', { token: 'active-token' });

  expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  // The overview heading is present (there are two "Projects" headings: the
  // sidebar list and the overview).
  expect(screen.getAllByRole('heading', { name: 'Projects' }).length).toBeGreaterThan(0);
});

it('signs out and returns to the login screen', async () => {
  const { store } = renderApp('/dashboard', { token: 'active-token' });

  await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }));

  expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  expect(store.get()).toBeNull();
});

it('returns to /login and clears the token when a dashboard request is refused (401)', async () => {
  const fetchImpl = jest.fn(async () =>
    jsonResponse(
      { detail: 'Authentication credentials were not provided.' },
      { status: 401, statusText: 'Unauthorized' },
    ),
  );
  const { store } = renderApp('/dashboard', { token: 'stale-token', fetchImpl });

  // The sidebar's project fetch is refused as unauthenticated, which signs the
  // Reviewer out and the guard sends them to /login.
  expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  expect(store.get()).toBeNull();
});
