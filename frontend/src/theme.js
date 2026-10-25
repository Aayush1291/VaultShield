/**
 * The color-theme preference: System, Light, or Dark.
 *
 * Stored in localStorage under ONE key, `THEME_STORAGE_KEY`, and applied as
 * `data-theme` on <html> — `"light"` or `"dark"`, or no attribute at all for
 * System, which lets `index.css` follow `prefers-color-scheme`.
 *
 * `index.html` carries a tiny inline script that reads the same key and sets
 * the attribute before the first paint, so a stored Dark preference never
 * flashes Paper. `theme.test.jsx` executes that script to keep the two in step.
 *
 * Every storage access is wrapped in try/catch: Safari private mode and some
 * embedded webviews expose localStorage but throw on use, and a theme toggle
 * must never take the page down with it.
 */

export const THEME_STORAGE_KEY = 'vaultshield.theme';

/** The three choices, in display order. */
export const THEME_OPTIONS = ['system', 'light', 'dark'];

const VALID = new Set(THEME_OPTIONS);

/** The stored preference, or `'system'` when unset, invalid, or unreadable. */
export function readStoredTheme() {
  try {
    const value = globalThis.localStorage?.getItem(THEME_STORAGE_KEY);
    return VALID.has(value) ? value : 'system';
  } catch {
    return 'system';
  }
}

/** Persist a preference; a storage failure is ignored (the choice still applies). */
export function storeTheme(preference) {
  try {
    globalThis.localStorage?.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    /* not persisted this session */
  }
}

/**
 * Reflect a preference onto <html>: `data-theme="light"|"dark"`, or no
 * attribute for System.
 *
 * @param {string} preference
 * @param {HTMLElement} [root]
 */
export function applyTheme(preference, root = globalThis.document?.documentElement) {
  if (!root) return;
  if (preference === 'light' || preference === 'dark') {
    root.setAttribute('data-theme', preference);
  } else {
    root.removeAttribute('data-theme');
  }
}
