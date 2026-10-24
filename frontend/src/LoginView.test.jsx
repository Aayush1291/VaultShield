/**
 * Unit tests for the sign-in screen (single-user MVP auth).
 *
 * The transport is injected via `createApiClient({ fetchImpl })`, the same way
 * the other view tests do it, so these drive the real component through the
 * real client with nothing mocked in the middle. No real credential is ever
 * used: the username, password, and token here are all invented.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { createApiClient, createMemoryTokenStore } from './api.js';
import LoginView, { describeLoginError } from './LoginView.jsx';

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

async function signIn({ username = 'reviewer', password = 'correct horse' } = {}) {
  await userEvent.type(screen.getByLabelText('Username'), username);
  await userEvent.type(screen.getByLabelText('Password'), password);
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

it('signs in on valid credentials, storing the token and reporting success', async () => {
  const { client, calls } = clientReturning(jsonResponse({ token: 'issued-token-xyz' }));
  const onSignedIn = jest.fn();

  render(<LoginView client={client} onSignedIn={onSignedIn} />);
  await signIn();

  await waitFor(() => expect(onSignedIn).toHaveBeenCalledTimes(1));
  // The token was stored by the client...
  expect(client.getToken()).toBe('issued-token-xyz');
  // ...from the login endpoint, which is the one request that carries no token.
  expect(calls[0].url).toBe('/api/auth/login/');
  expect(calls[0].init.headers.Authorization).toBeUndefined();
  // The token is never rendered.
  expect(screen.queryByText('issued-token-xyz')).not.toBeInTheDocument();
});

it('shows "wrong username or password" on a rejected credential and stores nothing', async () => {
  const { client } = clientReturning(
    jsonResponse(
      { detail: 'Invalid credentials.' },
      { status: 401, statusText: 'Unauthorized' },
    ),
  );
  const onSignedIn = jest.fn();

  render(<LoginView client={client} onSignedIn={onSignedIn} />);
  await signIn({ password: 'wrong' });

  expect(await screen.findByRole('alert')).toHaveTextContent('Wrong username or password');
  expect(client.getToken()).toBeNull();
  expect(onSignedIn).not.toHaveBeenCalled();
});

it('shows a backend-unreachable message when the request never completes', async () => {
  const fetchImpl = jest.fn(async () => {
    throw new TypeError('Failed to fetch');
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });

  render(<LoginView client={client} onSignedIn={jest.fn()} />);
  await signIn();

  expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach the VaultShield backend');
  expect(client.getToken()).toBeNull();
});

describe('describeLoginError', () => {
  it('maps the two actionable statuses and falls back to the client message', () => {
    expect(describeLoginError({ status: 401 })).toBe('Wrong username or password');
    expect(describeLoginError({ status: 403 })).toBe('Wrong username or password');
    expect(describeLoginError({ status: 0 })).toBe('Cannot reach the VaultShield backend');
    expect(describeLoginError({ status: 500, message: '500 Server Error' })).toBe('500 Server Error');
  });
});
