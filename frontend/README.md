# VaultShield Dashboard

The React + Tailwind frontend a Reviewer uses to inspect uploaded Runs and
approve or reject them. It only ever reads fully-uploaded runs — there is no
live-streaming path — and it talks to the Django backend over the REST API in
`design.md`'s "REST API surface" table.

## Layout

```
frontend/
  index.html            # Vite entry document
  vite.config.js        # React + Tailwind plugins, /api and /media dev proxy
  babel.config.cjs      # Jest-only transform (browser build does not use it)
  jest.config.cjs       # jsdom environment, co-located *.test.js(x)
  src/
    main.jsx            # browser entry; the only reader of import.meta.env
    App.jsx             # dashboard shell; mounts the views, holds the selected Project, owns the delete-project dialog
    ProjectListView.jsx # GET /api/projects/, one card per Project
    RunHistoryView.jsx  # GET /api/runs/?project=:id, one row per Run with its status
    api.js              # backend API client (one function per REST endpoint)
    config.js           # API base URL resolution
    format.js           # display text shared by the views (dates, load failures)
    index.css           # Tailwind v4 entry
```

## Views

Each view splits fetching from rendering: the view component owns the request
and the loading/failed/loaded decision, and a pure component underneath it
(`ProjectList`/`ProjectCard`, `RunList`/`RunRow`) turns records into markup.
The pure half takes records as a prop and holds no state, so it renders exactly
what it was given and can be driven directly by a property test.

Views take the API client as a prop defaulting to the shared singleton
(`<ProjectListView client={...} />`), which is how tests point one at an
injected transport instead of a live backend.

Selecting a Project in the list opens its run history. That selection is one
piece of state in `App.jsx`, not a route — nothing in the requirements calls
for URL-addressable views yet, so no routing library is pulled in. Both views
stay mounted, and `RunHistoryView` renders a "select a project" prompt until
one is picked rather than requesting an unfiltered run list.

## Signing in and creating projects

The dashboard uses **single-user MVP authentication**: one shared account and a
token, with no sign-up, no password reset, and no multi-user roles. It is
deliberately minimal and is not a substitute for real multi-user auth.

**Signing in.** With no token stored, the app shows a sign-in screen
(`LoginView`) instead of the three views. Enter the dashboard account's username
and password and submit; on success `POST /api/auth/login/` returns a token,
which the client stores. A wrong username or password shows "Wrong username or
password" and stores nothing; a backend that cannot be reached shows "Cannot
reach the VaultShield backend". The password and the token are never logged or
rendered. This replaces pasting a token into the browser console.

Once signed in, a **Sign out** link in the header clears the stored token and
returns to the sign-in screen. If any view's request is refused as
unauthenticated (401/403) — typically an expired or cleared token — the token is
dropped and the sign-in screen returns automatically.

**Creating a project.** The project list has a **New project** button that opens
a small form with Name and Repository URL. The URL field accepts both
`https://…` and `git@…:…` remotes; both fields are required and trimmed.
Submitting calls `POST /api/projects/`; on success the form closes, the list
reloads, and the new project is selected (opening its run history). This
replaces the old `curl` step. Duplicate names are allowed — the backend decides
— so a name clash comes back as a `400` shown next to the Name field, alongside
any other field errors.

**Deleting a project.** With a Project open, a **Delete project** control in the
top bar opens a confirmation dialog that names the Project and warns that all of
its runs are permanently deleted. The dialog is modal (`role="dialog"` +
`aria-modal`): focus moves into it on open and returns to the control on close,
Escape cancels, and Tab stays within it. Confirming calls
`DELETE /api/projects/:id/`, which removes the Project and its whole run history
on the backend; a failure is shown in the dialog so you can retry or cancel. On
success the selection clears, the run detail closes, and the sidebar reloads
without the deleted Project. The control is absent until a Project is selected.

**Where the token lives.** The Dashboard_Auth_Token is still kept in
`localStorage` (key `vaultshield.dashboard_auth_token`), falling back to memory
for the session where storage writes throw — signing in through the UI stores it
exactly the way pasting it into the console did. It is not an httpOnly cookie;
again, this is MVP single-user auth, not a hardened session.

## Development setup

```bash
npm install
npm run dev        # http://localhost:5173
```

The backend is expected at `http://localhost:8000`. Start it from `backend/`
(`python manage.py runserver`) before signing in.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_PROXY_TARGET` | `http://localhost:8000` | where the dev/preview server forwards `/api` and `/media`; `http://web:8000` inside the compose network |
| `VITE_API_BASE_URL` | unset (same-origin) | absolute API origin, bypassing the proxy; needs CORS on the backend |

Copy `.env.example` to `.env.local` to change either.

The dashboard requests relative paths and the dev server proxies them, so the
browser only ever makes same-origin requests and the backend needs no CORS
middleware. Set `VITE_API_BASE_URL` only when the API genuinely lives on
another origin.

## API client

`src/api.js` exports a configured singleton (`api`) plus the
`createApiClient` factory used by tests. Every method maps to one endpoint:

```js
import api, { unwrapResults } from './api.js';

await api.login({ username, password });        // stores the Dashboard_Auth_Token
const projects = unwrapResults(await api.listProjects());
const runs = unwrapResults(await api.listRuns({ project: projects[0].id }));
const run = await api.getRun(runs[0].run_id);

await api.createProject({ name, repo_url });    // POST /api/projects/
await api.deleteProject(projects[0].id);        // DELETE /api/projects/:id/, resolves null on 204
```

Two things it handles so no view has to:

- **Authentication.** The backend requires `Authorization: Token <token>` on
  every `/api/` path except `POST /api/auth/login/`, enforced before the view
  body runs. The client attaches the stored token to every request and
  deliberately omits it on login. The token is persisted in `localStorage`,
  falling back to memory where storage writes throw.
- **Pagination.** List endpoints answer `{count, next, previous, results}`, so
  `unwrapResults` reads the records out and yields `[]` for anything
  unusable — a list view renders its empty state rather than crashing.

Failures throw `ApiError`, carrying `status` and the parsed `payload`. Status
`0` means the request never reached the backend.

## Running tests

```bash
npm test              # Jest + React Testing Library
npm run test:watch
```

Tests sit next to the code they cover as `*.test.js` / `*.test.jsx`.
Property-based tests use fast-check and run through the same command.

The API client's tests inject the transport (`createApiClient({ fetchImpl })`)
rather than patching a global, so they assert on the exact URL, method,
headers, and body it produced without needing a backend.

## Building

```bash
npm run build      # -> dist/
npm run preview    # serves dist/ with the same API proxy
```
