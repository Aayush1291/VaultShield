/**
 * Runtime configuration for the dashboard, resolved from Vite's env.
 *
 * ### Why this is a function taking `env`, not a module reading it
 *
 * `import.meta.env` is module-syntax-only. Jest transforms each test file to
 * CommonJS, where `import.meta` is a syntax error, so any module that reads it
 * directly becomes untestable. Keeping the read in `src/main.jsx` — the one
 * module Jest never loads — and passing the object in here leaves this file
 * plain, testable JavaScript.
 */

/**
 * Base URL the API client prefixes onto every request path.
 *
 * Empty string means same-origin relative requests (`/api/projects/`), which
 * is what the dev-server and preview proxies in `vite.config.js` are for:
 * the browser only ever talks to the dashboard's own origin, so the backend
 * needs no CORS middleware. Override it only when the API is genuinely on
 * another origin and CORS has been configured for it.
 */
export const DEFAULT_API_BASE_URL = '';

/**
 * Strip trailing slashes so joining with a leading-slash path cannot produce
 * a doubled separator (`http://host//api/`, which some routers 404).
 *
 * @param {string} value
 * @returns {string}
 */
export function stripTrailingSlashes(value) {
  return value.replace(/\/+$/, '');
}

/**
 * Resolve the API base URL from a Vite-style env object.
 *
 * A missing, non-string, or whitespace-only `VITE_API_BASE_URL` falls back to
 * same-origin rather than being treated as a configured value: an unset env
 * var in Vite is `undefined`, and an empty one in a `.env` file arrives as
 * `''`, and both mean "not configured".
 *
 * @param {Record<string, unknown>} [env] typically `import.meta.env`
 * @returns {string} a base URL with no trailing slash, or `''` for same-origin
 */
export function resolveApiBaseUrl(env = {}) {
  const configured = env.VITE_API_BASE_URL;
  if (typeof configured !== 'string' || configured.trim() === '') {
    return DEFAULT_API_BASE_URL;
  }
  return stripTrailingSlashes(configured.trim());
}
