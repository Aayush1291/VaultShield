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

import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import Landing, { AUTO_ADVANCE_MS, MEASURED_CLAIM_TESTID } from './Landing.jsx';
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

  // The hero pill's Copy button (the closing band repeats the pill).
  await userEvent.click(screen.getAllByRole('button', { name: 'Copy' })[0]);
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

it('carries dark-mode, reduced-motion, and responsive styling', () => {
  const { container } = renderLanding();
  const css = container.querySelector('style')?.textContent ?? '';

  // Dark mode now comes from the shared Paper/Ink tokens the page reads.
  const tokens = readFileSync(join(__dirname, 'index.css'), 'utf8');
  expect(tokens).toContain('prefers-color-scheme');
  expect(css).toContain('var(--color-canvas)');
  expect(css).toContain('prefers-reduced-motion');
  expect(css).toContain('@media (min-width:');
});

it('ships the viewport meta app-wide (index.html)', () => {
  // Viewport is a document-level concern and lives in the app shell, not the
  // component; assert it is present so the page stays mobile-friendly.
  const indexHtml = readFileSync(join(__dirname, '..', 'index.html'), 'utf8');
  expect(indexHtml).toContain('name="viewport"');
});

it('shows Log in / Get started when signed out, Open dashboard when signed in', () => {
  // Signed out (no token).
  const { unmount } = renderLanding();
  expect(screen.getByRole('link', { name: 'Log in' })).toHaveAttribute('href', '/login');
  expect(screen.getByRole('link', { name: 'Get started' })).toHaveAttribute('href', '#install');
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

// --- redesign: sections, hero toggle, FAQ ----------------------------------

describe('page structure', () => {
  it('renders the bands in order, each with its own heading', () => {
    renderLanding();
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual([
      'Measured so far',
      'Three steps, all in your terminal.',
      'Your agent sees the names. Never the values.',
      'Review every AI change before it ships.',
      'Honest about what we have tested.',
      'Questions, answered plainly.',
      'Keep the names. Hide the values.',
    ]);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'Let AI agents read your code, not your secrets.',
    );
  });

  it('links header sections and Get started to anchors that exist, and nowhere else', () => {
    const { container } = renderLanding();
    // Hidden below 900px by a media query jsdom does not evaluate, and
    // display:none drops the accessible name, so select it directly.
    const sections = container.querySelector('nav[aria-label="Sections"]');
    const hrefs = [...sections.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    expect(hrefs).toEqual(['#how', '#agents', '#faq']);

    // Every in-page anchor resolves; every route link is one that exists.
    for (const a of container.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href');
      if (href.startsWith('#')) {
        expect(container.querySelector(href)).not.toBeNull();
      } else {
        expect(['/login', '/dashboard']).toContain(href);
      }
    }
  });

  it('repeats the install pill in the closing band from the same constant', () => {
    renderLanding();
    expect(screen.getByTestId('install-cmd-cta')).toHaveTextContent(
      screen.getByTestId('install-cmd').textContent,
    );
  });

  it('marks the review mini-dashboard as a decorative illustration', () => {
    renderLanding();
    const caption = screen.getByText('Illustration');
    const figure = caption.closest('figure');
    expect(figure.querySelector('[aria-hidden="true"]')).not.toBeNull();
    // The fake Approve/Reject are not real controls.
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('ends with a minimal footer', () => {
    renderLanding();
    const footer = screen.getByRole('contentinfo');
    expect(footer).toHaveTextContent('macOS only');
    expect(footer).toHaveTextContent('Early software: only claims we measured');
  });
});

describe('hero terminal toggle', () => {
  const region = () => screen.getByRole('region', { name: /what an AI agent reads/i });
  const button = (name) => within(region()).getByRole('button', { name });

  afterEach(() => {
    delete window.matchMedia;
    jest.useRealTimers();
  });

  function mockMotion({ reduce }) {
    window.matchMedia = jest.fn().mockImplementation((query) => ({
      matches: query.includes('reduce') ? reduce : false,
      media: query,
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
    }));
  }

  it('starts on Without, showing the obviously fake key', () => {
    renderLanding();
    expect(button('Without VaultShield')).toHaveAttribute('aria-pressed', 'true');
    expect(button('With VaultShield')).toHaveAttribute('aria-pressed', 'false');
    expect(region()).toHaveTextContent('STRIPE_KEY=sk_live_51HxEXAMPLExxxNOTREAL');
    expect(region()).not.toHaveTextContent('vs_protected');
  });

  it('switches to With by click and back by keyboard', async () => {
    renderLanding();
    await userEvent.click(button('With VaultShield'));
    expect(button('With VaultShield')).toHaveAttribute('aria-pressed', 'true');
    expect(region()).toHaveTextContent('STRIPE_KEY=vs_protected');
    expect(region()).toHaveTextContent('Real value: macOS Keychain');
    expect(region()).not.toHaveTextContent('NOTREAL');

    button('Without VaultShield').focus();
    await userEvent.keyboard('{Enter}');
    expect(button('Without VaultShield')).toHaveAttribute('aria-pressed', 'true');
    expect(region()).toHaveTextContent('NOTREAL');
  });

  it('auto-advances once to With when motion is allowed and nobody interacted', () => {
    mockMotion({ reduce: false });
    jest.useFakeTimers();
    renderLanding();
    expect(button('Without VaultShield')).toHaveAttribute('aria-pressed', 'true');

    act(() => jest.advanceTimersByTime(AUTO_ADVANCE_MS + 10));
    expect(button('With VaultShield')).toHaveAttribute('aria-pressed', 'true');
  });

  it('does not auto-advance under prefers-reduced-motion', () => {
    mockMotion({ reduce: true });
    jest.useFakeTimers();
    renderLanding();
    act(() => jest.advanceTimersByTime(AUTO_ADVANCE_MS * 3));
    expect(button('Without VaultShield')).toHaveAttribute('aria-pressed', 'true');
  });

  it('does not auto-advance after the visitor used the toggle', () => {
    mockMotion({ reduce: false });
    jest.useFakeTimers();
    renderLanding();
    act(() => button('Without VaultShield').click());
    act(() => jest.advanceTimersByTime(AUTO_ADVANCE_MS * 3));
    expect(button('Without VaultShield')).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('FAQ', () => {
  it('is a set of closed details/summary accordions that open on click', async () => {
    const { container } = renderLanding();
    const faq = container.querySelector('#faq');
    const items = faq.querySelectorAll('details');
    expect(items.length).toBe(5);
    for (const item of items) {
      expect(item.querySelector('summary')).not.toBeNull();
      expect(item.open).toBe(false);
    }

    const limits = within(faq).getByText('Honest limits: what it does not protect');
    await userEvent.click(limits);
    expect(limits.closest('details').open).toBe(true);
    expect(limits.closest('details')).toHaveTextContent('indirect tricks');
  });
});
