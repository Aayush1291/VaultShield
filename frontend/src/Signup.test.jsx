/**
 * Tests for the /signup placeholder (step 3).
 *
 * There is no self-service registration yet, so the page must NOT render a
 * form — it explains how accounts are obtained today and links to sign in.
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import Signup from './Signup.jsx';

function renderSignup() {
  return render(
    <MemoryRouter>
      <Signup />
    </MemoryRouter>,
  );
}

it('explains how accounts are obtained and links to sign in, with no form', () => {
  renderSignup();

  expect(screen.getByRole('heading', { name: 'Sign up' })).toBeInTheDocument();
  expect(screen.getByText(/vaultshield signup/)).toBeInTheDocument();
  expect(screen.getByText(/account your administrator created/i)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login');

  // No form controls: accounts cannot be created here yet.
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
