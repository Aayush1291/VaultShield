/**
 * Unit tests for the backend API client.
 *
 * The transport is injected rather than the global patched: `createApiClient`
 * takes a `fetchImpl`, so each test can assert on the exact URL, method,
 * headers, and body the client produced. That is the whole contract of an
 * HTTP client — there is no behaviour underneath it to exercise — and it is
 * checked against the real request objects the client builds, not around them.
 */

import {
  ApiError,
  buildQueryString,
  createApiClient,
  createMemoryTokenStore,
  unwrapResults,
} from './api.js';

/**
 * A stand-in for `Response`, which jsdom does not provide.
 *
 * Only the four members the client touches are implemented: `ok`, `status`,
 * `headers.get('content-type')`, and `text()`.
 */
function jsonResponse(body, { status = 200, statusText = 'OK' } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

/** Records every call and answers with the queued responses in order. */
function recordingFetch(...responses) {
  const calls = [];
  const queue = [...responses];
  const impl = jest.fn(async (url, init) => {
    calls.push({ url, init });
    return queue.length > 1 ? queue.shift() : queue[0];
  });
  impl.calls = calls;
  return impl;
}

function makeClient({ baseUrl = '', fetchImpl, token = null } = {}) {
  const tokenStore = createMemoryTokenStore();
  if (token) tokenStore.set(token);
  return { client: createApiClient({ baseUrl, fetchImpl, tokenStore }), tokenStore };
}

describe('request construction', () => {
  it('prefixes the configured base URL and keeps a single separator', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ results: [] }));
    const { client } = makeClient({ baseUrl: 'http://localhost:8000/', fetchImpl });

    await client.listProjects();

    expect(fetchImpl.calls[0].url).toBe('http://localhost:8000/api/projects/');
  });

  it('issues same-origin relative paths when no base URL is configured', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ results: [] }));
    const { client } = makeClient({ fetchImpl });

    await client.listProjects();

    expect(fetchImpl.calls[0].url).toBe('/api/projects/');
  });

  it('sends only the run filters that are set', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ results: [] }));
    const { client } = makeClient({ fetchImpl });

    await client.listRuns({ project: 7, submitted: false });

    expect(fetchImpl.calls[0].url).toBe('/api/runs/?project=7&submitted=false');
  });

  it('addresses run detail by run_id and patches with a JSON body', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ run_id: 'run 1' }));
    const { client } = makeClient({ fetchImpl });

    await client.getRun('run 1');
    await client.updateRun('run 1', { status: 'approved' });

    expect(fetchImpl.calls[0].url).toBe('/api/runs/run%201/');
    expect(fetchImpl.calls[1]).toMatchObject({
      url: '/api/runs/run%201/',
      init: {
        method: 'PATCH',
        body: JSON.stringify({ status: 'approved' }),
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      },
    });
  });

  it('follows a pagination link by path and query only, dropping the backend host', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ count: 0, next: null, previous: null, results: [] }));
    const { client } = makeClient({ fetchImpl });

    // DRF builds this with the proxied host; following it must stay same-origin.
    await client.followPage('http://web:8000/api/runs/?project=7&page=2');

    expect(fetchImpl.calls[0].url).toBe('/api/runs/?project=7&page=2');
  });

  it('deletes a project by id with a DELETE and resolves null on 204', async () => {
    const fetchImpl = recordingFetch(jsonResponse(undefined, { status: 204, statusText: '' }));
    const { client } = makeClient({ fetchImpl });

    const result = await client.deleteProject('65260f8a1c9d440000a1b2c3');

    expect(fetchImpl.calls[0]).toMatchObject({
      url: '/api/projects/65260f8a1c9d440000a1b2c3/',
      init: { method: 'DELETE' },
    });
    expect(result).toBeNull();
  });

  it('lets the browser set the multipart Content-Type for screenshot upload', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ id: 1 }));
    const { client } = makeClient({ fetchImpl });

    await client.uploadScreenshot('run-1', {
      route: '/dashboard',
      image: new Blob(['png'], { type: 'image/png' }),
    });

    const { init } = fetchImpl.calls[0];
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.body.get('route')).toBe('/dashboard');
    expect(init.headers['Content-Type']).toBeUndefined();
  });
});

describe('Dashboard_Auth_Token handling', () => {
  it('attaches the token to every authenticated request', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ results: [] }));
    const { client } = makeClient({ fetchImpl, token: 'abc123' });

    await client.listProjects();

    expect(fetchImpl.calls[0].init.headers.Authorization).toBe('Token abc123');
  });

  it('omits the header entirely when no token is stored', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ results: [] }));
    const { client } = makeClient({ fetchImpl });

    await client.listProjects();

    expect(fetchImpl.calls[0].init.headers.Authorization).toBeUndefined();
  });

  it('stores the issued token on login and never sends one to the login endpoint', async () => {
    const fetchImpl = recordingFetch(jsonResponse({ token: 'fresh-token' }));
    const { client } = makeClient({ fetchImpl, token: 'stale-token' });

    const token = await client.login({ username: 'reviewer', password: 'pw' });

    expect(token).toBe('fresh-token');
    expect(client.getToken()).toBe('fresh-token');
    expect(fetchImpl.calls[0].url).toBe('/api/auth/login/');
    expect(fetchImpl.calls[0].init.headers.Authorization).toBeUndefined();
    expect(fetchImpl.calls[0].init.body).toBe(
      JSON.stringify({ username: 'reviewer', password: 'pw' }),
    );
  });

  it('leaves an existing token in place when login is rejected', async () => {
    const fetchImpl = recordingFetch(
      jsonResponse({ non_field_errors: ['Unable to log in'] }, { status: 400 }),
    );
    const { client } = makeClient({ fetchImpl, token: 'still-valid' });

    await expect(client.login({ username: 'reviewer', password: 'wrong' })).rejects.toThrow(
      ApiError,
    );
    expect(client.getToken()).toBe('still-valid');
  });

  it('clears the stored token on logout', () => {
    const { client } = makeClient({ token: 'abc123' });

    expect(client.isAuthenticated()).toBe(true);
    client.logout();
    expect(client.isAuthenticated()).toBe(false);
  });
});

describe('error handling', () => {
  it("surfaces DRF's detail message and status", async () => {
    const fetchImpl = recordingFetch(
      jsonResponse({ detail: 'Invalid token.' }, { status: 401, statusText: 'Unauthorized' }),
    );
    const { client } = makeClient({ fetchImpl });

    await expect(client.listProjects()).rejects.toMatchObject({
      name: 'ApiError',
      status: 401,
      message: '401 Unauthorized: Invalid token.',
      payload: { detail: 'Invalid token.' },
    });
  });

  it('joins field errors from a rejected create', async () => {
    const fetchImpl = recordingFetch(
      jsonResponse({ repo_url: ['This field is required.'] }, { status: 400, statusText: 'Bad Request' }),
    );
    const { client } = makeClient({ fetchImpl });

    await expect(client.createProject({ name: 'demo' })).rejects.toMatchObject({
      status: 400,
      message: '400 Bad Request: repo_url: This field is required.',
    });
  });

  it('reports an unreachable backend as a status-less ApiError', async () => {
    const cause = new TypeError('Failed to fetch');
    const { client } = makeClient({
      fetchImpl: jest.fn(async () => {
        throw cause;
      }),
    });

    await expect(client.listProjects()).rejects.toMatchObject({ status: 0, cause });
  });

  it('returns null for an empty 204 response', async () => {
    const fetchImpl = recordingFetch(jsonResponse(undefined, { status: 204, statusText: '' }));
    const { client } = makeClient({ fetchImpl });

    await expect(client.approveRun('run-1')).resolves.toBeNull();
  });
});

describe('helpers', () => {
  it('drops unset query parameters and stringifies booleans', () => {
    expect(buildQueryString({})).toBe('');
    expect(buildQueryString({ status: undefined, submitted: null, project: '' })).toBe('');
    expect(buildQueryString({ status: 'approved', submitted: true })).toBe(
      '?status=approved&submitted=true',
    );
  });

  it('reads records out of paginated, plain, and unusable payloads', () => {
    const records = [{ id: 1 }, { id: 2 }];
    expect(unwrapResults({ count: 2, next: null, previous: null, results: records })).toEqual(
      records,
    );
    expect(unwrapResults(records)).toEqual(records);
    expect(unwrapResults(null)).toEqual([]);
    expect(unwrapResults({ detail: 'nope' })).toEqual([]);
  });
});
