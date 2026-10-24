/**
 * API client for the VaultShield_Backend.
 *
 * One module per design.md's "REST API surface" table: every path in that
 * table has exactly one function here, so a view never assembles a URL or a
 * header itself. Requirement 19.1 (the project list view fetches Projects
 * from the backend) is served by `listProjects`, and Tasks 25.2-26 and 28.3
 * build on the rest.
 *
 * ### Every request except login carries the Dashboard_Auth_Token
 *
 * The backend sets DRF's `TokenAuthentication` + `IsAuthenticated` globally
 * (Task 15.2, Requirements 27.3-27.5), so every `/api/` path other than
 * `POST /api/auth/login/` is rejected before the view body runs unless the
 * request carries `Authorization: Token <token>`. That header is attached
 * here, once, rather than at each call site — a view that forgot it would
 * fail as a 401 with no other symptom.
 *
 * `login` is the single exemption and it says so explicitly (`auth: false`).
 * Sending a stale token to the login endpoint is not merely redundant: DRF
 * rejects an invalid token inside the authenticator, before the view runs, so
 * a user whose token had expired could never log back in.
 *
 * ### Why a factory plus a singleton
 *
 * `createApiClient` takes its base URL, transport, and token store as
 * arguments, so a test can drive it without patching globals and without a
 * live backend. `api` is the one instance the dashboard imports. Views should
 * use `api`; only tests and `src/main.jsx` should need the factory or
 * `api.configure`.
 */

import { DEFAULT_API_BASE_URL, stripTrailingSlashes } from './config.js';

/**
 * Key the Dashboard_Auth_Token is persisted under.
 *
 * Namespaced because `localStorage` is shared across everything served from
 * an origin, and in development that origin is a plain `localhost` port that
 * other local apps also use.
 */
export const TOKEN_STORAGE_KEY = 'vaultshield.dashboard_auth_token';

/**
 * A failed API call.
 *
 * Carries the HTTP status and the parsed response body so a view can tell
 * apart the cases it has to react to differently — 401 means the stored token
 * is gone or invalid and the Reviewer has to log in again, 400 carries DRF's
 * field errors, 409-style state-transition refusals (Task 28.1) carry a
 * message worth showing verbatim.
 */
export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {{ status?: number, payload?: unknown, cause?: unknown }} [details]
   */
  constructor(message, { status = 0, payload = null, cause = undefined } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
    if (cause !== undefined) {
      this.cause = cause;
    }
  }
}

/**
 * Token store backed by `window.localStorage`, surviving a page reload.
 *
 * Falls back to memory when storage is unavailable rather than throwing:
 * Safari's private mode and some embedded webviews expose `localStorage` but
 * make every write throw, and a dashboard that cannot persist a token should
 * still work for the length of one session.
 *
 * @returns {{ get: () => string|null, set: (token: string) => void, clear: () => void }}
 */
export function createBrowserTokenStore(storageKey = TOKEN_STORAGE_KEY) {
  let memoryToken = null;

  /** @returns {Storage|null} */
  function storage() {
    try {
      return globalThis.localStorage ?? null;
    } catch {
      return null;
    }
  }

  return {
    get() {
      try {
        return storage()?.getItem(storageKey) ?? memoryToken;
      } catch {
        return memoryToken;
      }
    },
    set(token) {
      memoryToken = token;
      try {
        storage()?.setItem(storageKey, token);
      } catch {
        /* memory-only for this session */
      }
    },
    clear() {
      memoryToken = null;
      try {
        storage()?.removeItem(storageKey);
      } catch {
        /* nothing persisted to remove */
      }
    },
  };
}

/**
 * Token store with no persistence, for tests and non-browser callers.
 *
 * @returns {{ get: () => string|null, set: (token: string) => void, clear: () => void }}
 */
export function createMemoryTokenStore() {
  let token = null;
  return {
    get: () => token,
    set: (value) => {
      token = value;
    },
    clear: () => {
      token = null;
    },
  };
}

/**
 * Serialize query parameters, dropping the ones that are not set.
 *
 * `null`/`undefined`/`''` are omitted rather than sent as empty strings: the
 * run list's `status` and `submitted` filters (Requirement 12.6) are absent
 * when a view is not filtering, and `?status=` is a different request from
 * no `status` at all. Booleans are written as `true`/`false` because that is
 * what `RunViewSet.get_queryset` parses for `submitted`.
 *
 * @param {Record<string, unknown>} [params]
 * @returns {string} a query string starting with `?`, or `''` when empty
 */
export function buildQueryString(params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    search.append(key, typeof value === 'boolean' ? String(value) : String(value));
  }
  const query = search.toString();
  return query === '' ? '' : `?${query}`;
}

/**
 * Read the records out of a list response.
 *
 * The backend paginates list endpoints by default
 * (`DEFAULT_PAGINATION_CLASS = PageNumberPagination`, `PAGE_SIZE = 50`), so
 * `GET /api/projects/` answers `{count, next, previous, results}` rather than
 * a bare array. Views want the records either way, and a plain array is still
 * accepted so a caller holding an already-unwrapped list is not a special
 * case. Anything else — including the `null` a failed-but-not-thrown call
 * could leave behind — yields an empty list, so a list view renders its
 * empty state instead of crashing on `.map`.
 *
 * @param {unknown} payload
 * @returns {unknown[]}
 */
export function unwrapResults(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.results)) return payload.results;
  return [];
}

/**
 * Build an API client.
 *
 * @param {object} [options]
 * @param {string} [options.baseUrl] prefix for every path; `''` for same-origin
 * @param {typeof fetch} [options.fetchImpl] transport, defaults to `globalThis.fetch`
 * @param {{ get: Function, set: Function, clear: Function }} [options.tokenStore]
 */
export function createApiClient({
  baseUrl = DEFAULT_API_BASE_URL,
  fetchImpl = undefined,
  tokenStore = createBrowserTokenStore(),
} = {}) {
  let currentBaseUrl = stripTrailingSlashes(baseUrl);
  let currentFetch = fetchImpl;

  /**
   * Resolve the transport at call time, not at construction time.
   *
   * jsdom does not provide `fetch`, and a test that installs one on
   * `globalThis` typically does so after importing this module. Reading it per
   * request means the singleton picks that up instead of having captured
   * `undefined`.
   */
  function resolveFetch() {
    const impl = currentFetch ?? globalThis.fetch;
    if (typeof impl !== 'function') {
      throw new ApiError('No fetch implementation available', { status: 0 });
    }
    return impl;
  }

  /**
   * Issue a request against the backend.
   *
   * @param {string} path an absolute API path, e.g. `/api/runs/`
   * @param {object} [options]
   * @param {string} [options.method] defaults to `GET`
   * @param {unknown} [options.body] JSON-serialized unless it is a `FormData`
   * @param {Record<string, unknown>} [options.params] query parameters
   * @param {boolean} [options.auth] attach the token; `true` everywhere but login
   * @param {AbortSignal} [options.signal]
   * @returns {Promise<unknown>} the parsed body, or `null` for an empty response
   * @throws {ApiError} on a transport failure or any non-2xx status
   */
  async function request(path, { method = 'GET', body, params, auth = true, signal } = {}) {
    const url = `${currentBaseUrl}${path}${buildQueryString(params)}`;
    const headers = { Accept: 'application/json' };

    if (auth) {
      const token = tokenStore.get();
      if (token) {
        // DRF's `TokenAuthentication` keyword is literally `Token`, not
        // `Bearer`; the latter is silently ignored and reads as anonymous.
        headers.Authorization = `Token ${token}`;
      }
    }

    const init = { method, headers, signal };

    // The `typeof` guard keeps this working outside a browser-like
    // environment, where `FormData` may not be defined at all and a bare
    // `instanceof` would throw a `ReferenceError` on every request.
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      // No `Content-Type`: the browser has to set it so it can append the
      // multipart boundary. Setting it by hand produces a body the server
      // cannot split apart.
      init.body = body;
    } else if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await resolveFetch()(url, init);
    } catch (cause) {
      // A network-level failure (backend down, DNS, aborted request) has no
      // status. Wrapping it keeps every caller's error handling to one type.
      throw new ApiError(`Request to ${url} failed`, { status: 0, cause });
    }

    const payload = await parseBody(response);

    if (!response.ok) {
      throw new ApiError(describeError(response, payload), {
        status: response.status,
        payload,
      });
    }

    return payload;
  }

  const client = {
    /** Current base URL, mostly useful in tests and diagnostics. */
    get baseUrl() {
      return currentBaseUrl;
    },

    /**
     * Override configuration after construction.
     *
     * `src/main.jsx` uses this to apply `VITE_API_BASE_URL`, which it can only
     * read from `import.meta.env` — see `src/config.js` for why that read does
     * not live in this module.
     *
     * @param {{ baseUrl?: string, fetchImpl?: typeof fetch }} options
     */
    configure({ baseUrl: nextBaseUrl, fetchImpl: nextFetch } = {}) {
      if (typeof nextBaseUrl === 'string') currentBaseUrl = stripTrailingSlashes(nextBaseUrl);
      if (nextFetch !== undefined) currentFetch = nextFetch;
      return client;
    },

    request,

    // --- Dashboard authentication (Requirement 27.1) ----------------------

    /** @returns {string|null} the stored Dashboard_Auth_Token, if any */
    getToken: () => tokenStore.get(),

    /** @returns {boolean} whether a token is stored at all */
    isAuthenticated: () => Boolean(tokenStore.get()),

    /**
     * Exchange a username/password credential for a Dashboard_Auth_Token and
     * store it.
     *
     * `POST /api/auth/login/`. Requirement 27.2: invalid credentials return no
     * token, which arrives here as a non-2xx and therefore an `ApiError` — the
     * previously stored token is left untouched in that case, so a failed
     * re-login does not log a working session out.
     *
     * @param {{ username: string, password: string }} credentials
     * @returns {Promise<string>} the issued token
     */
    async login({ username, password }) {
      const payload = await request('/api/auth/login/', {
        method: 'POST',
        body: { username, password },
        auth: false,
      });
      const token = payload && payload.token;
      if (typeof token !== 'string' || token === '') {
        throw new ApiError('Login response contained no token', { status: 200, payload });
      }
      tokenStore.set(token);
      return token;
    },

    /**
     * Forget the stored token.
     *
     * Local only, by design: DRF tokens have no revoke endpoint in the MVP, so
     * there is nothing to call.
     */
    logout() {
      tokenStore.clear();
    },

    // --- Projects ---------------------------------------------------------

    /**
     * `GET /api/projects/` — every Project the dashboard should list
     * (Requirement 19.1). Paginated; pass through `unwrapResults`.
     *
     * @param {{ signal?: AbortSignal }} [options]
     */
    listProjects: (options = {}) => request('/api/projects/', options),

    /**
     * `POST /api/projects/` — create a Project.
     *
     * @param {{ name: string, repo_url: string }} project
     */
    createProject: (project) => request('/api/projects/', { method: 'POST', body: project }),

    /**
     * `DELETE /api/projects/:id/` — delete a Project and everything under it
     * (its Runs and their events, diff, screenshots, approval, security
     * findings, and sandbox records). Answers `204 No Content`, which
     * `request` returns as `null`.
     *
     * The id is the Project's pk (a MongoDB ObjectId string), as returned by
     * `listProjects`.
     *
     * @param {string|number} id
     */
    deleteProject: (id) =>
      request(`/api/projects/${encodeURIComponent(id)}/`, { method: 'DELETE' }),

    // --- Runs -------------------------------------------------------------

    /**
     * `GET /api/runs/` — Runs, optionally filtered.
     *
     * `project` narrows the list to one Project's run history
     * (Requirement 19.2); `status` and `submitted` are the filters
     * Requirement 12.6 defines, where `submitted` distinguishes an empty
     * `pr_url` from a populated one.
     *
     * @param {{ project?: string|number, status?: string, submitted?: boolean,
     *           signal?: AbortSignal }} [options]
     */
    listRuns({ project, status, submitted, signal } = {}) {
      return request('/api/runs/', { params: { project, status, submitted }, signal });
    },

    /**
     * `POST /api/runs/` — create a Run for an existing Project.
     *
     * @param {object} run
     */
    createRun: (run) => request('/api/runs/', { method: 'POST', body: run }),

    /**
     * `GET /api/runs/:id/` — full Run detail: events, diff, screenshots, and
     * security findings in one response (Requirement 20.1).
     *
     * The id is the CLI-generated `run_id`, not the database pk:
     * `RunViewSet` sets `lookup_field = 'run_id'`.
     *
     * @param {string} runId
     * @param {{ signal?: AbortSignal }} [options]
     */
    getRun: (runId, options = {}) => request(`/api/runs/${encodeURIComponent(runId)}/`, options),

    /**
     * `PATCH /api/runs/:id/` — update named Run fields only (status
     * transitions, `pr_url`).
     *
     * @param {string} runId
     * @param {Record<string, unknown>} fields
     */
    updateRun: (runId, fields) =>
      request(`/api/runs/${encodeURIComponent(runId)}/`, { method: 'PATCH', body: fields }),

    /**
     * `POST /api/runs/:id/approve/` — approve a Run in `awaiting_review`.
     *
     * A Run in any other status is refused by the backend's state machine
     * (Task 28.1) and surfaces here as an `ApiError`.
     *
     * @param {string} runId
     */
    approveRun: (runId) =>
      request(`/api/runs/${encodeURIComponent(runId)}/approve/`, { method: 'POST' }),

    /**
     * `POST /api/runs/:id/reject/` — reject a Run in `awaiting_review`.
     *
     * @param {string} runId
     */
    rejectRun: (runId) =>
      request(`/api/runs/${encodeURIComponent(runId)}/reject/`, { method: 'POST' }),

    // --- Run artifact ingest ----------------------------------------------
    //
    // The three endpoints below are what the CLI's Uploader writes to
    // (Requirements 13.1-13.3). The dashboard only ever reads fully-uploaded
    // runs — Requirement 26 rules out any live-streaming path — so no view
    // calls these. They are here to keep this module a complete mirror of
    // design.md's REST API surface, which is also what makes it usable for
    // seeding a run by hand while building the views.

    /**
     * `POST /api/runs/:id/events/` — bulk Run_Event ingest, order preserved
     * via each event's `sequence`.
     *
     * @param {string} runId
     * @param {Array<object>} events
     */
    ingestEvents: (runId, events) =>
      request(`/api/runs/${encodeURIComponent(runId)}/events/`, { method: 'POST', body: events }),

    /**
     * `POST /api/runs/:id/diff/` — store the structured Run_Diff. The backend
     * enqueues the security scan off the back of this (Task 23.2).
     *
     * @param {string} runId
     * @param {{ files: Array<{path: string, additions: number, deletions: number}> }} diff
     */
    ingestDiff: (runId, diff) =>
      request(`/api/runs/${encodeURIComponent(runId)}/diff/`, { method: 'POST', body: diff }),

    /**
     * `POST /api/runs/:id/screenshots/` — upload one Screenshot as multipart.
     *
     * @param {string} runId
     * @param {{ route: string, image: Blob|File }} screenshot
     */
    uploadScreenshot(runId, { route, image }) {
      const form = new FormData();
      form.append('route', route);
      form.append('image', image);
      return request(`/api/runs/${encodeURIComponent(runId)}/screenshots/`, {
        method: 'POST',
        body: form,
      });
    },
  };

  return client;
}

/**
 * Parse a response body without assuming it has one.
 *
 * DRF answers `204 No Content` for some writes and returns HTML for an
 * unhandled server error, so neither "there is a body" nor "the body is JSON"
 * can be taken for granted. A body that fails to parse is returned as raw
 * text, which keeps the real server message available in `ApiError.payload`
 * instead of replacing it with a parse error.
 *
 * @param {Response} response
 * @returns {Promise<unknown>}
 */
async function parseBody(response) {
  if (response.status === 204) return null;

  const text = await response.text();
  if (text === '') return null;

  const contentType = response.headers?.get?.('content-type') ?? '';
  if (!contentType.includes('json')) return text;

  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * Turn an error response into a one-line message.
 *
 * Prefers whatever the backend said over a generic status line: DRF puts
 * `{"detail": "..."}` on authentication and permission failures and on its
 * own exceptions, and field errors arrive as `{field: ["..."]}`. Falling back
 * to the status text keeps the message useful when the body is HTML or empty.
 *
 * @param {Response} response
 * @param {unknown} payload
 * @returns {string}
 */
function describeError(response, payload) {
  const fallback = `${response.status} ${response.statusText || 'Request failed'}`.trim();

  if (typeof payload === 'string' && payload.trim() !== '') {
    return `${fallback}: ${payload.trim().slice(0, 200)}`;
  }
  if (payload && typeof payload === 'object') {
    if (typeof payload.detail === 'string') return `${fallback}: ${payload.detail}`;
    const fieldErrors = Object.entries(payload)
      .map(([field, messages]) => `${field}: ${[].concat(messages).join(', ')}`)
      .join('; ');
    if (fieldErrors !== '') return `${fallback}: ${fieldErrors}`;
  }
  return fallback;
}

/**
 * The client the dashboard uses.
 *
 * Same-origin by default; `src/main.jsx` reconfigures it when
 * `VITE_API_BASE_URL` is set.
 */
export const api = createApiClient();

export default api;
