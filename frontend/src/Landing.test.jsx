/**
 * Tests for the landing page, ported from cli/tests/test_website_html.py.
 *
 * The original checks ran against the raw website/index.html; here they run
 * against the rendered `Landing` component. The categories are the same — only
 * external URL is the (now origin-built) install line, a working copy button,
 * the measured-claims checklist, honest limits, honest agent labels, macOS
 * only, and the dark / reduced-motion / responsive styling — adapted to the
 * routed component, whose install command is derived from the page origin.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import Landing, { MEASURED_CLAIM_TESTID } from './Landing.jsx';
import { AuthProvider } from './auth.jsx';
import { createApiClient, createMemoryTokenStore } from './api.js';

function renderLanding() {
  const client = createApiClient({ fetchImpl: jest.fn(), tokenStore: createMemoryTokenStore() });
  return render(
    <MemoryRouter>
      <AuthProvider client={client}>
        <Landing />
      </AuthProvider>
    </MemoryRouter>,
  );
}

/** The page origin jsdom runs under, which the install command is built from. */
const ORIGIN = window.location.origin; // http://localhost

it('builds the install command from the page origin and shows it with a Copy button', async () => {
  const writeText = jest.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

  renderLanding();

  const expected = `curl -fsSL ${ORIGIN}/install.sh | sh`;
  expect(screen.getByTestId('install-cmd')).toHaveTextContent(expected);

  await userEvent.click(screen.getByRole('button', { name: 'Copy' }));
  expect(writeText).toHaveBeenCalledWith(expected);
  expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument();
});

it('has no absolute URL other than the origin-built install line', () => {
  const { container } = renderLanding();

  // Every absolute http(s) URL in text and in href/src attributes.
  const urls = new Set();
  const matchAll = (text) => (text.match(/https?:\/\/[^\s"'<>)]+/g) ?? []).forEach((u) => urls.add(u));
  matchAll(container.textContent ?? '');
  container.querySelectorAll('[href],[src]').forEach((el) => {
    matchAll(el.getAttribute('href') ?? '');
    matchAll(el.getAttribute('src') ?? '');
  });

  expect(urls.size).toBeGreaterThan(0);
  for (const url of urls) {
    expect(url.startsWith(`${ORIGIN}/install.sh`)).toBe(true);
  }
});

it('pulls in no external stylesheet, script, font, or image', () => {
  const { container } = renderLanding();

  expect(container.querySelectorAll('link')).toHaveLength(0);
  expect(container.querySelectorAll('script')).toHaveLength(0);
  // No element loads a resource from an absolute URL.
  expect(container.querySelector('[src^="http"]')).toBeNull();
});

it('lists exactly the three measured claims, each marked', () => {
  renderLanding();

  const claims = screen.getAllByTestId(MEASURED_CLAIM_TESTID);
  expect(claims).toHaveLength(3);
  expect(claims[0]).toHaveTextContent(/Placeholders held in the Antigravity CLI and App/);
  expect(claims[1]).toHaveTextContent(/command hook blocked printenv live in Antigravity/);
  expect(claims[2]).toHaveTextContent(/Claude Code, Kiro and Codex backstops are built from docs/);
  for (const claim of claims) {
    expect(claim.querySelector('.mark')).not.toBeNull();
  }
});

it('states the honest limits', () => {
  const { container } = renderLanding();
  const text = container.textContent ?? '';

  expect(text.toLowerCase()).toContain('what it does not');
  expect(text).toContain('indirect tricks');
  expect(text).toContain('not a sandbox');
});

it('labels the supported agents honestly: one proven live, three built from docs', () => {
  const { container } = renderLanding();
  const text = container.textContent ?? '';
  const count = (needle) => text.split(needle).length - 1;

  expect(count('proven live')).toBe(1);
  expect(count('built from docs, not live-tested')).toBe(3);
  for (const agent of ['Antigravity', 'Claude Code', 'Kiro', 'Codex']) {
    expect(text).toContain(agent);
  }
  // Antigravity is the one proven-live agent.
  const antigravity = screen.getByText('Antigravity').closest('.agent');
  expect(within(antigravity).getByText('proven live')).toBeInTheDocument();
});

it('is macOS-only and shows the secure terminal mock', () => {
  const { container } = renderLanding();
  const text = container.textContent ?? '';

  expect(text).toContain('macOS only');
  expect(text).toContain('vaultshield secure');
  expect(text).toContain('vs_protected');
});

it('carries dark-mode, reduced-motion, and responsive styling in its own stylesheet', () => {
  const { container } = renderLanding();
  const css = container.querySelector('style')?.textContent ?? '';

  expect(css).toContain('prefers-color-scheme');
  expect(css).toContain('prefers-reduced-motion');
  expect(css).toContain('@media (min-width:');
});

it('ships the viewport meta app-wide (index.html)', () => {
  // Viewport is a document-level concern and lives in the app shell, not the
  // component; assert it is present so the page stays mobile-friendly.
  const indexHtml = readFileSync(join(__dirname, '..', 'index.html'), 'utf8');
  expect(indexHtml).toContain('name="viewport"');
});

it('shows Log in / Sign up when signed out, Open dashboard when signed in', () => {
  // Signed out (no token).
  const { unmount } = renderLanding();
  expect(screen.getByRole('link', { name: 'Log in' })).toHaveAttribute('href', '/login');
  expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup');
  unmount();

  // Signed in.
  const store = createMemoryTokenStore();
  store.set('active-token');
  const client = createApiClient({ fetchImpl: jest.fn(), tokenStore: store });
  render(
    <MemoryRouter>
      <AuthProvider client={client}>
        <Landing />
      </AuthProvider>
    </MemoryRouter>,
  );
  expect(screen.getByRole('link', { name: 'Open dashboard' })).toHaveAttribute('href', '/dashboard');
});
