/**
 * Unit tests for API base URL resolution.
 *
 * These cover the distinction the dev-server proxy depends on: an unset or
 * blank `VITE_API_BASE_URL` has to mean same-origin, not a literal empty
 * host, or every request would be built against the wrong origin.
 */

import { DEFAULT_API_BASE_URL, resolveApiBaseUrl, stripTrailingSlashes } from './config.js';

describe('resolveApiBaseUrl', () => {
  it('falls back to same-origin when nothing is configured', () => {
    expect(resolveApiBaseUrl()).toBe(DEFAULT_API_BASE_URL);
    expect(resolveApiBaseUrl({})).toBe(DEFAULT_API_BASE_URL);
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: '   ' })).toBe(DEFAULT_API_BASE_URL);
  });

  it('uses a configured origin without its trailing slash', () => {
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: 'http://web:8000/' })).toBe('http://web:8000');
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: ' http://localhost:8000 ' })).toBe(
      'http://localhost:8000',
    );
  });
});

describe('stripTrailingSlashes', () => {
  it('removes every trailing slash and leaves the rest alone', () => {
    expect(stripTrailingSlashes('http://host//')).toBe('http://host');
    expect(stripTrailingSlashes('http://host/api')).toBe('http://host/api');
    expect(stripTrailingSlashes('')).toBe('');
  });
});
