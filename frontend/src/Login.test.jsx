/**
 * Tests for the /login route wrapper (step 3).
 *
 * The sign-in form itself is covered by `LoginView.test.jsx`; this checks the
 * route wrapper: it renders the form and offers a link to sign up. The
 * redirect-after-login behaviour is covered end to end in `App.test.jsx`.
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import Login from './Login.jsx';
import { AuthProvider } from './auth.jsx';
import { createApiClient, createMemoryTokenStore } from './api.js';

function renderLogin() {
  const client = createApiClient({ fetchImpl: jest.fn(), tokenStore: createMemoryTokenStore() });
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthProvider client={client}>
        <Login />
      </AuthProvider>
    </MemoryRouter>,
  );
}

it('renders the sign-in form and a link to sign up', () => {
  renderLogin();

  expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  expect(screen.getByLabelText('Username')).toBeInTheDocument();
  expect(screen.getByLabelText('Password')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup');
});
