/**
 * Tests for the color-theme preference (step 3): the segmented switch, the
 * `data-theme` attribute, persistence under one localStorage key, tolerance of
 * a throwing storage, and the pre-paint inline script in index.html.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ThemeSwitch from './ThemeSwitch.jsx';
import { THEME_STORAGE_KEY, applyTheme, readStoredTheme } from './theme.js';

const root = document.documentElement;

afterEach(() => {
  globalThis.localStorage.clear();
  root.removeAttribute('data-theme');
  jest.restoreAllMocks();
});

const pressed = (name) => screen.getByRole('button', { name }).getAttribute('aria-pressed');

it('defaults to System: no data-theme attribute and System pressed', () => {
  render(<ThemeSwitch />);

  expect(screen.getByRole('group', { name: 'Color theme' })).toBeInTheDocument();
  expect(pressed('System')).toBe('true');
  expect(pressed('Light')).toBe('false');
  expect(pressed('Dark')).toBe('false');
  expect(root.hasAttribute('data-theme')).toBe(false);
});

it('applies and persists Dark, and restores it on the next mount', async () => {
  const { unmount } = render(<ThemeSwitch />);

  await userEvent.click(screen.getByRole('button', { name: 'Dark' }));

  expect(root.getAttribute('data-theme')).toBe('dark');
  expect(globalThis.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  expect(pressed('Dark')).toBe('true');

  unmount();
  root.removeAttribute('data-theme');
  render(<ThemeSwitch />);
  expect(pressed('Dark')).toBe('true');
  expect(root.getAttribute('data-theme')).toBe('dark');
});

it('is keyboard operable and returns to System by removing the attribute', async () => {
  render(<ThemeSwitch />);

  await userEvent.tab(); // System
  await userEvent.tab(); // Light
  expect(screen.getByRole('button', { name: 'Light' })).toHaveFocus();
  await userEvent.keyboard('{Enter}');
  expect(root.getAttribute('data-theme')).toBe('light');

  await userEvent.click(screen.getByRole('button', { name: 'System' }));
  expect(root.hasAttribute('data-theme')).toBe(false);
  expect(globalThis.localStorage.getItem(THEME_STORAGE_KEY)).toBe('system');
});

it('still switches when localStorage throws', async () => {
  jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new Error('SecurityError');
  });
  jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error('QuotaExceededError');
  });

  expect(readStoredTheme()).toBe('system');
  render(<ThemeSwitch />);
  await userEvent.click(screen.getByRole('button', { name: 'Dark' }));
  expect(root.getAttribute('data-theme')).toBe('dark');
});

it('ignores an invalid stored value', () => {
  globalThis.localStorage.setItem(THEME_STORAGE_KEY, 'neon');
  expect(readStoredTheme()).toBe('system');
  applyTheme(readStoredTheme());
  expect(root.hasAttribute('data-theme')).toBe(false);
});

describe('the pre-paint script in index.html', () => {
  const html = readFileSync(join(__dirname, '..', 'index.html'), 'utf8');
  // The one inline (src-less) script in the document head.
  const inline = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] ?? '';

  it('reads the same storage key as src/theme.js', () => {
    expect(inline).toContain(`'${THEME_STORAGE_KEY}'`);
  });

  it.each(['light', 'dark'])('sets data-theme="%s" from storage before React runs', (value) => {
    globalThis.localStorage.setItem(THEME_STORAGE_KEY, value);
    new Function(inline)();
    expect(root.getAttribute('data-theme')).toBe(value);
  });

  it('leaves the attribute off for System and survives a throwing storage', () => {
    globalThis.localStorage.setItem(THEME_STORAGE_KEY, 'system');
    new Function(inline)();
    expect(root.hasAttribute('data-theme')).toBe(false);

    jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(() => new Function(inline)()).not.toThrow();
  });
});
