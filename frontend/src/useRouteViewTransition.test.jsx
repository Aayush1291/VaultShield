/**
 * Tests for the route cross-fade: which clicks it takes over, that it waits
 * for the new route to commit, and that it stays out of the way where the
 * View Transitions API is missing or motion is reduced.
 */

import { act, render, screen } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';

import useRouteViewTransition, { MAX_WAIT_MS, inAppTarget } from './useRouteViewTransition.js';

function Shell() {
  useRouteViewTransition();
  const location = useLocation();
  return (
    <>
      <p data-testid="where">{location.pathname}</p>
      <Link to="/next">Next</Link>
      <a href="#faq">Anchor</a>
    </>
  );
}

function renderShell() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="*" element={<Shell />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  delete document.startViewTransition;
  delete window.matchMedia;
  jest.useRealTimers();
});

describe('inAppTarget', () => {
  const current = new URL('http://localhost/dashboard');
  const anchor = (href, attrs = {}) => {
    const a = document.createElement('a');
    a.setAttribute('href', href);
    for (const [k, v] of Object.entries(attrs)) a.setAttribute(k, v);
    return a;
  };
  const click = (extra = {}) => ({ button: 0, defaultPrevented: false, ...extra });

  it('returns the path for a plain in-app link to another page', () => {
    expect(inAppTarget(click(), anchor('/login'), current)).toBe('/login');
  });

  it('ignores anchors, the current page, other origins, modifiers, targets and downloads', () => {
    expect(inAppTarget(click(), anchor('#faq'), current)).toBeNull();
    expect(inAppTarget(click(), anchor('/dashboard'), current)).toBeNull();
    expect(inAppTarget(click(), anchor('https://example.com/x'), current)).toBeNull();
    expect(inAppTarget(click({ metaKey: true }), anchor('/login'), current)).toBeNull();
    expect(inAppTarget(click({ button: 1 }), anchor('/login'), current)).toBeNull();
    expect(inAppTarget(click(), anchor('/login', { target: '_blank' }), current)).toBeNull();
    expect(inAppTarget(click(), anchor('/f.zip', { download: '' }), current)).toBeNull();
    expect(inAppTarget(click({ defaultPrevented: true }), anchor('/login'), current)).toBeNull();
  });
});

it('navigates normally when the View Transitions API is missing', () => {
  renderShell();
  act(() => screen.getByRole('link', { name: 'Next' }).click());
  expect(screen.getByTestId('where')).toHaveTextContent('/next');
});

it('wraps an in-app link click in a view transition that ends when the route commits', async () => {
  let update = null;
  document.startViewTransition = jest.fn((cb) => {
    update = cb;
    return {};
  });
  renderShell();

  act(() => screen.getByRole('link', { name: 'Next' }).click());
  expect(document.startViewTransition).toHaveBeenCalledTimes(1);
  // The browser has not run the update yet: still on the old page.
  expect(screen.getByTestId('where')).toHaveTextContent('/');

  let settled = false;
  await act(async () => {
    await update().then(() => {
      settled = true;
    });
  });
  expect(screen.getByTestId('where')).toHaveTextContent('/next');
  expect(settled).toBe(true);
});

it('resolves anyway after the cap, so a transition can never hang', async () => {
  jest.useFakeTimers();
  let update = null;
  document.startViewTransition = jest.fn((cb) => {
    update = cb;
    return {};
  });
  renderShell();
  act(() => screen.getByRole('link', { name: 'Next' }).click());

  let settled = false;
  act(() => {
    update().then(() => {
      settled = true;
    });
  });
  await act(async () => {
    jest.advanceTimersByTime(MAX_WAIT_MS + 1);
  });
  expect(settled).toBe(true);
});

it('leaves same-page anchors and reduced-motion visitors alone', () => {
  document.startViewTransition = jest.fn();
  renderShell();
  act(() => screen.getByRole('link', { name: 'Anchor' }).click());
  expect(document.startViewTransition).not.toHaveBeenCalled();

  window.matchMedia = jest.fn(() => ({ matches: true, addEventListener: jest.fn(), removeEventListener: jest.fn() }));
  act(() => screen.getByRole('link', { name: 'Next' }).click());
  expect(document.startViewTransition).not.toHaveBeenCalled();
  expect(screen.getByTestId('where')).toHaveTextContent('/next');
});
