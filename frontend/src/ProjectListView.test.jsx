/**
 * Unit tests for the project list view (Requirement 19.1).
 *
 * Fixed-example checks only. The exhaustive "exactly one card per record for
 * any record list" claim is Property 27 (Task 25.4), so what is here is the
 * things a generated-input property does not pin down: the URL actually
 * requested, the empty state, and the two failure paths a Reviewer can hit.
 *
 * The transport is injected via `createApiClient({ fetchImpl })`, the same way
 * `src/api.test.js` does it, so these render the real component against the
 * real client with nothing mocked out of the middle.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { createApiClient, createMemoryTokenStore } from './api.js';
import ProjectListView, {
  EMPTY_PROJECTS_MESSAGE,
  PROJECT_CARD_TESTID,
  formatCreatedAt,
} from './ProjectListView.jsx';

/** A stand-in for `Response`, which jsdom does not provide. */
function jsonResponse(body, { status = 200, statusText = 'OK' } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

/** An API client whose transport answers with the queued responses in order. */
function clientReturning(...responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = jest.fn(async (url, init) => {
    calls.push({ url, init });
    return queue.length > 1 ? queue.shift() : queue[0];
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });
  return { client, calls };
}

const PROJECTS = [
  { id: 1, name: 'checkout-service', repo_url: 'git@github.com:acme/checkout.git', created_at: '2024-05-01T10:00:00Z' },
  { id: 2, name: 'marketing-site', repo_url: 'https://github.com/acme/marketing.git', created_at: '2024-05-02T10:00:00Z' },
  { id: 3, name: 'internal-tools', repo_url: 'https://gitlab.com/acme/tools.git', created_at: null },
];

it('requests the project list endpoint once and renders one card per Project', async () => {
  const { client, calls } = clientReturning(
    jsonResponse({ count: 3, next: null, previous: null, results: PROJECTS }),
  );

  render(<ProjectListView client={client} />);

  const cards = await screen.findAllByTestId(PROJECT_CARD_TESTID);
  expect(cards).toHaveLength(PROJECTS.length);
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toBe('/api/projects/');

  for (const [index, project] of PROJECTS.entries()) {
    expect(cards[index]).toHaveTextContent(project.name);
    expect(cards[index]).toHaveTextContent(project.repo_url);
  }
  // `created_at: null` on the third record renders no creation line rather
  // than an "Invalid Date" one.
  expect(cards[0]).toHaveTextContent('Created 2024-05-01');
  expect(cards[2]).not.toHaveTextContent('Created');
});

it('renders an empty state when the backend returns no Projects', async () => {
  const { client } = clientReturning(jsonResponse({ count: 0, next: null, previous: null, results: [] }));

  render(<ProjectListView client={client} />);

  expect(await screen.findByText(EMPTY_PROJECTS_MESSAGE)).toBeInTheDocument();
  expect(screen.queryAllByTestId(PROJECT_CARD_TESTID)).toHaveLength(0);
});

it('tells the Reviewer to sign in when the request is rejected as unauthenticated', async () => {
  const { client } = clientReturning(
    jsonResponse({ detail: 'Authentication credentials were not provided.' }, {
      status: 401,
      statusText: 'Unauthorized',
    }),
  );

  render(<ProjectListView client={client} />);

  expect(await screen.findByRole('alert')).toHaveTextContent(/sign in to load projects/i);
  expect(screen.queryAllByTestId(PROJECT_CARD_TESTID)).toHaveLength(0);
});

it('re-fetches when the Reviewer retries after a transport failure', async () => {
  const calls = [];
  let attempt = 0;
  const fetchImpl = jest.fn(async (url, init) => {
    calls.push({ url, init });
    attempt += 1;
    if (attempt === 1) throw new TypeError('Failed to fetch');
    return jsonResponse({ count: 1, next: null, previous: null, results: [PROJECTS[0]] });
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });

  render(<ProjectListView client={client} />);

  expect(await screen.findByRole('alert')).toHaveTextContent(/could not be reached/i);

  await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

  await waitFor(() => expect(screen.getAllByTestId(PROJECT_CARD_TESTID)).toHaveLength(1));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(calls).toHaveLength(2);
});

describe('formatCreatedAt', () => {
  it('renders a locale-independent ISO date', () => {
    expect(formatCreatedAt('2024-05-01T23:30:00Z')).toBe('2024-05-01');
  });

  it('yields an empty string for anything unparseable', () => {
    for (const value of [undefined, null, '', 'not a date', {}, []]) {
      expect(formatCreatedAt(value)).toBe('');
    }
  });
});

// --- the New project form ---------------------------------------------------

const CREATED_PROJECT = {
  id: 7,
  name: 'new-service',
  repo_url: 'git@github.com:acme/new-service.git',
  created_at: '2024-06-01T10:00:00Z',
};

async function openNewProjectForm() {
  await userEvent.click(screen.getByRole('button', { name: 'New project' }));
}

it('creates a project, closes the form, reloads the list, and selects it', async () => {
  const { client, calls } = clientReturning(
    jsonResponse({ count: 0, next: null, previous: null, results: [] }), // initial load
    jsonResponse(CREATED_PROJECT, { status: 201, statusText: 'Created' }), // createProject
    jsonResponse({ count: 1, next: null, previous: null, results: [CREATED_PROJECT] }), // reload
  );
  const onSelectProject = jest.fn();

  render(<ProjectListView client={client} onSelectProject={onSelectProject} />);
  await screen.findByText(EMPTY_PROJECTS_MESSAGE);

  await openNewProjectForm();
  // Leading/trailing whitespace is trimmed before the request.
  await userEvent.type(screen.getByLabelText('Name'), '  new-service  ');
  await userEvent.type(
    screen.getByLabelText('Repository URL'),
    '  git@github.com:acme/new-service.git  ',
  );
  await userEvent.click(screen.getByRole('button', { name: 'Create project' }));

  await waitFor(() => expect(onSelectProject).toHaveBeenCalledWith(CREATED_PROJECT));

  const createCall = calls.find((call) => call.init?.method === 'POST');
  expect(createCall.url).toBe('/api/projects/');
  expect(JSON.parse(createCall.init.body)).toEqual({
    name: 'new-service',
    repo_url: 'git@github.com:acme/new-service.git',
  });

  // The form closed, and the list reloaded (a second GET) now showing the card.
  expect(screen.queryByLabelText('Name')).not.toBeInTheDocument();
  const listCalls = calls.filter((call) => (call.init?.method ?? 'GET') === 'GET');
  expect(listCalls).toHaveLength(2);
  expect(await screen.findByTestId(PROJECT_CARD_TESTID)).toHaveTextContent('new-service');
});

it('requires both fields and does not call the backend when either is empty', async () => {
  const { client, calls } = clientReturning(
    jsonResponse({ count: 0, next: null, previous: null, results: [] }),
  );

  render(<ProjectListView client={client} onSelectProject={jest.fn()} />);
  await screen.findByText(EMPTY_PROJECTS_MESSAGE);

  await openNewProjectForm();
  await userEvent.click(screen.getByRole('button', { name: 'Create project' }));

  expect(await screen.findByText('Name is required.')).toBeInTheDocument();
  expect(screen.getByText('Repository URL is required.')).toBeInTheDocument();
  // Nothing was posted: only the initial list GET was ever made.
  expect(calls.filter((call) => call.init?.method === 'POST')).toHaveLength(0);
  // The form stays open.
  expect(screen.getByLabelText('Name')).toBeInTheDocument();
});

it('shows a backend 400 next to the matching field (duplicate names are the backend\'s call)', async () => {
  const { client } = clientReturning(
    jsonResponse({ count: 0, next: null, previous: null, results: [] }),
    jsonResponse(
      {
        name: ['project with this name already exists.'],
        repo_url: ['Enter a valid repository URL.'],
      },
      { status: 400, statusText: 'Bad Request' },
    ),
  );

  render(<ProjectListView client={client} onSelectProject={jest.fn()} />);
  await screen.findByText(EMPTY_PROJECTS_MESSAGE);

  await openNewProjectForm();
  await userEvent.type(screen.getByLabelText('Name'), 'checkout-service');
  await userEvent.type(screen.getByLabelText('Repository URL'), 'git@github.com:acme/checkout.git');
  await userEvent.click(screen.getByRole('button', { name: 'Create project' }));

  // The request was made (no client-side duplicate check), and each field
  // error landed under its field.
  expect(await screen.findByText('project with this name already exists.')).toBeInTheDocument();
  expect(screen.getByText('Enter a valid repository URL.')).toBeInTheDocument();
  // The form stays open so the Reviewer can fix the input.
  expect(screen.getByLabelText('Name')).toBeInTheDocument();
});

it('prevents a double submit while the create is in flight', async () => {
  let resolveCreate;
  let postCount = 0;
  const fetchImpl = jest.fn(async (url, init) => {
    if ((init?.method ?? 'GET') === 'POST') {
      postCount += 1;
      return new Promise((resolve) => {
        resolveCreate = resolve;
      });
    }
    return jsonResponse({ count: 0, next: null, previous: null, results: [] });
  });
  const client = createApiClient({ fetchImpl, tokenStore: createMemoryTokenStore() });

  render(<ProjectListView client={client} onSelectProject={jest.fn()} />);
  await screen.findByText(EMPTY_PROJECTS_MESSAGE);

  await openNewProjectForm();
  await userEvent.type(screen.getByLabelText('Name'), 'svc');
  await userEvent.type(screen.getByLabelText('Repository URL'), 'git@github.com:acme/svc.git');

  const submit = screen.getByRole('button', { name: 'Create project' });
  await userEvent.click(submit); // first submit: create is now in flight
  expect(submit).toBeDisabled();
  await userEvent.click(submit); // ignored while pending
  expect(postCount).toBe(1);

  // Settle the in-flight request so no state updates land after the test.
  resolveCreate(jsonResponse(CREATED_PROJECT, { status: 201, statusText: 'Created' }));
  await waitFor(() => expect(screen.queryByLabelText('Name')).not.toBeInTheDocument());
});
