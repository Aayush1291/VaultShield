# VaultShield Dashboard

The React + Tailwind frontend a Reviewer uses to inspect uploaded Runs and
approve or reject them. It only ever reads fully-uploaded runs — there is no
live-streaming path — and it talks to the Django backend over the REST API in
`design.md`'s "REST API surface" table.

## Layout

```
frontend/
  index.html            # Vite entry document (viewport meta lives here)
  vite.config.js        # React + Tailwind plugins, /api and /media dev proxy
  babel.config.cjs      # Jest-only transform (browser build does not use it)
  jest.config.cjs       # jsdom environment, co-located *.test.js(x)
  public/
    install.sh          # the macOS installer, served same-origin at /install.sh
    dist/               # the built wheel (git-ignored), served at /dist/<wheel>
  src/
    main.jsx            # browser entry; wraps <App/> in BrowserRouter; reads import.meta.env
    App.jsx             # the route table, wrapped in AuthProvider
    auth.jsx            # AuthProvider/useAuth, guardClient, RequireAuth, RedirectIfSignedIn
    Landing.jsx         # / editorial landing (scoped styles on the tokens, origin-built install cmd)
    SeeItWork.jsx       # the landing's four-scene "See it work" demo and its state machine
    useScrollProgress.js      # scroll progress as a --p CSS variable; flowPosition()
    handoff.js          # plans the hero pieces' route onto the demo window
    useRouteViewTransition.js # 180ms cross-fade between routes (View Transitions API)
    motion.js           # useMediaQuery, useReducedMotion, usePageVisible
    charMorph.js        # the scramble-then-settle character morph
    fakeValues.js       # the one obviously fake key the landing shows
    Login.jsx / Signup.jsx    # /login (wraps LoginView) and the /signup placeholder
    AuthLayout.jsx      # two-column frame for the auth pages
    theme.js / ThemeSwitch.jsx  # System / Light / Dark preference and its control
    NotFound.jsx        # the * friendly 404
    DashboardLayout.jsx # /dashboard shell: sidebar + top bar + <Outlet/>, New project + delete modals
    ProjectsOverview.jsx      # /dashboard index: project cards + New project + empty state
    ProjectRunsPage.jsx       # /dashboard/projects/:projectId: the runs table
    RunDetailPage.jsx         # .../runs/:runId: run detail, active tab in ?tab=
    Modal.jsx           # generic accessible modal (role=dialog, focus trap, scroll lock)
    ProjectListView.jsx # the sidebar project list (GET /api/projects/)
    RunHistoryView.jsx  # RunStatusBadge + the tab components reused by the pages
    RunDetailView.jsx   # the four tabs + decision controls (tab controllable via props)
    NewProjectForm.jsx  # the create-project form (used inside the modal)
    api.js              # backend API client (one function per REST endpoint)
    config.js           # API base URL resolution
    format.js           # display text shared by the views (dates, load failures)
    index.css           # Tailwind v4 entry, fonts, and the Paper/Ink theme tokens
    contrast.test.js    # WCAG contrast check over the tokens in index.css
```

## Theme tokens

`src/index.css` defines two complete themes as Tailwind v4 `@theme static`
tokens, switched by `data-theme` on `<html>`:

- **Paper** (light, default): warm cream canvas `#fbf9ef`, a slightly whiter
  surface, warm near-black ink `#1c1b14`, muted `#66655a`.
- **Ink** (dark): warm dark canvas `#14130f`, surface `#1d1c16`, never blue-black.

`data-theme="light"` or `"dark"` forces a theme; no attribute follows
`prefers-color-scheme`. Components only use semantic utilities — `bg-canvas`,
`bg-surface`, `bg-surface-2`, `text-fg`, `text-fg-muted`, `text-fg-subtle`,
`border-border`, `border-border-strong`, `bg-accent`/`text-accent-fg`,
`hover:bg-accent-hover`, `text-accent-text`, `bg-accent-soft`, `ring`,
`success`/`warn`/`danger` soft and fg pairs, per-status chip pairs
(`bg-status-<name>-bg text-status-<name>-fg`), an always-dark `term-*`
palette, `bg-scrim`, and the warm `shadow-soft`/`shadow-card`/`shadow-lift`.
Radii: `rounded-pill`, `rounded-card` (20px) to `rounded-card-xl` (28px),
`rounded-control` (12px). No component uses Tailwind's `dark:` variant, which
would follow the OS and ignore a Light/Dark override.

`--color-border` is a decorative 12% ink hairline (row dividers, card edges).
Anything a user must see to operate a control — input and outline-button
borders — uses `--color-border-strong`, which meets 3:1.

`src/contrast.test.js` parses the hex values out of `index.css` and fails if
any text/background pair the app uses is under 4.5:1, or a UI boundary
(control borders, the focus ring, the danger outline) is under 3:1. It also
checks that the two dark blocks (explicit and system) are identical and that
shadows are never pure black. It logs the lowest ratios on each run.

**Theme switch.** The System / Light / Dark control sits in the dashboard's
account area and the landing header. The choice is stored under
`vaultshield.theme` (every access in try/catch) and applied before first paint
by a tiny inline script in `index.html`, so there is no flash.

## Fonts and licenses

All fonts are self-hosted from npm packages and bundled by Vite; the page makes
no external font request.

| Role | Font | Package (pinned) | License |
|---|---|---|---|
| Display serif (landing, auth and page titles) | EB Garamond 400, 500, 400 italic, latin | `@fontsource/eb-garamond` 5.3.0 | SIL OFL 1.1 |
| UI sans (everything else) | Inter, variable `wght` axis | `@fontsource-variable/inter` 5.3.0 | SIL OFL 1.1 |
| Commands and ids | system monospace stack | — | — |

Both use `font-display: swap`. Metric-compatible fallback faces (`EB Garamond
Fallback` over Georgia, `Inter Fallback` over Arial, with `size-adjust` and
ascent/descent overrides computed from Capsize's published metrics) keep text
from jumping when the webfonts arrive.

## Landing motion

No animation library: CSS, IntersectionObserver, `requestAnimationFrame` and a
few small hooks. Everything that moves is decoration on a page that is
complete without it.

| | Reduced motion | Below 900px | 900–1199px | 1200px and up |
|---|---|---|---|---|
| Section reveal (fade + 24px rise, cards 60ms apart) | off, shown | on | on | on |
| Route cross-fade (180ms) | off | on | on | on |
| Hero terminal morph into "With" | off, toggle only | on | on | on |
| See it work demo | scene 3, static | plays on its own | pinned, scroll walks the scenes | pinned |
| Hero pieces settling onto the demo window | off | off | off | on, if the route fits |

No dashboard screen moves beyond the route cross-fade.

**`useScrollProgress(ref, options)`** (`src/useScrollProgress.js`) sets `--p`,
0 to 1, on the element, and CSS turns it into `transform` and `opacity` only.
Modes: `travel` (0 as the element enters at the bottom, 1 as it leaves at the
top), `pin` (for a tall track with a sticky child: 0 when the track's top hits
the viewport top, 1 when its bottom reaches the bottom) and `handoff` (0 at
the page top, 1 when a `target` has scrolled up to where it pins). The cost is
kept small on purpose: one passive `scroll` and one `resize` listener; each
event schedules at most one animation frame; a frame does one read (a rect,
or in `handoff` mode just `scrollY`) and one style write, and skips the write
if nothing changed. Nothing runs while the tab is hidden; it catches up on
`visibilitychange`. Listeners and any pending frame go on unmount.
`enabled: false` attaches nothing and leaves `--p` at `restValue`, the final
state. `onProgress(p)` gives the number to JS from the same frame.
`flowPosition(el)` is the measure-time helper: an element's place in the flow
from the offset chain, ignoring a reveal's transform and a stuck sticky
ancestor (Chromium reports that one where it is stuck).

**See it work** (`src/SeeItWork.jsx`) shows four scenes, with invented values:
Without (the agent repeats the fake live key), The transformation (the key
scrambles into `vs_protected` and a Keychain badge), With (the read is
refused, `cat` shows the placeholder, `printenv` is blocked) and
`vaultshield exec` (the command gets the real value; its output shows `***`).
`demoReducer` is a small state machine (`start`, `tick`, `jump`, `replay`,
`reduce`) over the frames in `DEMO_FRAMES`; one timeout at a time drives it,
cleared on every change and on unmount. It starts when the stage enters view,
plays once, pauses while hovered, focused or the tab is hidden, and has
Replay and four focusable scene buttons (`aria-pressed`). The animated stage
is `aria-hidden`; an `sr-only` paragraph describes all four scenes, and the
caption reads "Illustration of a real test on Antigravity, with invented
values." Under reduced motion it shows scene 3's final frame and the scene
buttons switch final frames instantly. From 900px with motion, the section is
a 260vh track with a sticky stage: each quarter of the scroll picks a scene
and its caption (`scrollScene`), and between scroll changes the clock plays.

**The hero pieces** (a `.env` card, a key chip with the fake key, a shield)
start in the hero's right margin beside its terminal and settle onto the top
edge of the See it work window as it scrolls into place, where docked copies
then ride the pinned stage. `planHandoff` (`src/handoff.js`) plans the route
from measured layout, on mount, resize and font load, never per scroll frame:
a shared descent down a column clear of every hero and See it work line,
lagging the scroll (the parallax; never against it), queued so the gaps only
shrink; then one piece at a time drops and slides into its spot. It checks
the route against the text and the other pieces, and where none fits (today
below about 1275px) the docked badges just sit on the window. CSS runs the
legs from `--p` with smoothstep easing.

**Route cross-fade.** `useRouteViewTransition` (mounted by `App.jsx`) catches
plain left clicks on same-origin links to another path and wraps the
navigation in `document.startViewTransition()`; `index.css` gives it a 180ms
cross-fade. It is a no-op where the API is missing and under reduced motion.
Programmatic navigations (sign-in redirects) and back/forward are not wrapped.
`BrowserRouter` and the test mounting are unchanged.

## Routes

The dashboard is a single-page app with client-side routing
(`react-router-dom`). `src/main.jsx` provides a `BrowserRouter`; tests mount
`<App/>` in a `MemoryRouter`.

| Path | Page |
|---|---|
| `/` | Landing (public) |
| `/login` | Sign in — redirects to `/dashboard` if already signed in |
| `/signup` | Placeholder (no form yet) — redirects to `/dashboard` if signed in |
| `/dashboard` | Projects overview |
| `/dashboard/projects/:projectId` | That project's runs |
| `/dashboard/projects/:projectId/runs/:runId` | Run detail (active tab in `?tab=`) |
| anything else | Not Found, with a link home |

**Guards.** `/dashboard*` requires a stored Dashboard_Auth_Token: with none, the
Reviewer is sent to `/login` and returned to the page they wanted after signing
in. A signed-in Reviewer visiting `/login` or `/signup` is sent to `/dashboard`.
Any request refused as unauthenticated (401/403) clears the token and returns
to `/login`, and so does Sign out.

**Deep links and refresh** work for every route: the dev server and
`vite preview` fall back to `index.html` for unknown paths (Vite's SPA default),
so the Dockerfile (`npm run dev`) and compose need no extra config. Run detail
reads its tab from `?tab=diff|timeline|screenshots|security` (default `diff`),
so a deep link or refresh opens on the right tab.

## Views and pages

Each view splits fetching from rendering: the view owns the request and the
loading/failed/loaded decision, and a pure component underneath it
(`ProjectList`/`ProjectCard`, the tab components) turns records into markup, so
it can be driven directly by a property test. Views and pages take the API
client as a prop defaulting to the shared singleton, which is how tests point
one at an injected transport instead of a live backend.

The runs page is a full-width table — run id (a link to the run's detail
route), agent, status, and a relative created time — with status filter chips
(the backend's `?status=`), a client-side run-id search, and a "Load more"
button that follows DRF's `next` link page by page.

## Signing in and creating projects

The dashboard uses **single-user MVP authentication**: one shared account and a
token, with no sign-up, no password reset, and no multi-user roles. It is
deliberately minimal and is not a substitute for real multi-user auth.

**Signing in.** With no token stored, `/dashboard*` sends the Reviewer to
`/login`. Enter the dashboard account's username and password and submit; on
success `POST /api/auth/login/` returns a token, which the client stores and the
Reviewer is returned to the page they first asked for (default `/dashboard`). A
wrong username or password shows "Wrong username or password" and stores
nothing; a backend that cannot be reached shows "Cannot reach the VaultShield
backend". The password and the token are never logged or rendered.

Once signed in, a **Sign out** control clears the token and returns to `/login`.
Any request refused as unauthenticated (401/403) — typically an expired token —
clears the token and returns to `/login` automatically.

**Signing up.** `/signup` is a placeholder with no form: account creation is a
later release (`vaultshield signup`); for now you sign in with an account the
administrator created.

**Creating a project.** The sidebar and the overview each have a **New project**
button that opens a modal with Name and Repository URL. The URL field accepts
both `https://…` and `git@…:…` remotes; both fields are required and trimmed.
Submitting calls `POST /api/projects/`; on success the modal closes, the sidebar
reloads, and you land on the new project's runs route. Duplicate names are the
backend's call, so a clash comes back as a `400` shown next to the Name field.

**Deleting a project.** On a project route, a **Delete project** control in the
top bar opens a confirm modal that names the Project and warns its runs are
permanently deleted (`role="dialog"` + `aria-modal`, focus trapped and returned
to the control, Escape/backdrop cancel). Confirming calls
`DELETE /api/projects/:id/`; on success the sidebar reloads and you return to the
overview. A failure is shown in the modal so you can retry or cancel.

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

## One origin for the installer

The landing page's one-line installer and the wheel it downloads are served by
the dashboard itself, from `public/`:

- `public/install.sh` → served at `/install.sh`. The Landing component builds
  the displayed command from the page's own origin
  (`curl -fsSL <origin>/install.sh | sh`), so it always points at wherever the
  page is served. `install.sh` defaults `VAULTSHIELD_BASE` to
  `http://localhost:5173` (the dashboard origin).
- `public/dist/vaultshield-<version>.whl` → served at `/dist/<wheel>`. Build it
  with `./website/build.sh`, which writes the wheel into `public/dist/` (that
  directory is git-ignored). `install.sh` downloads `$VAULTSHIELD_BASE/dist/…`.

Vite serves everything under `public/` at the site root in both `npm run dev`
and `vite preview`, so no build step is needed for `install.sh` to be live. In
compose, the frontend service bind-mounts `frontend/public` read-only so the
host-built wheel (created after the image) is reachable in the container.

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

## Screenshots and layout checks

`spike/ui_shots.py` photographs every surface for visual review, using the
Playwright already installed in `cli/.venv` (chromium; if the browser is
missing, run `cli/.venv/bin/python -m playwright install chromium` once):

```bash
cli/.venv/bin/python spike/ui_shots.py               # build, serve, shoot, check
cli/.venv/bin/python spike/ui_shots.py --skip-build  # reuse frontend/dist
cli/.venv/bin/python spike/ui_shots.py --base-url http://localhost:5173
cli/.venv/bin/python spike/ui_shots.py --layout-only # checks only, no PNGs
```

It serves the build with `vite preview` on a free port, answers every
`/api/**` request with fixture JSON (no backend is contacted), and gives
dashboard pages a fake token. PNGs land in `ui-shots/` (git-ignored): the
landing, login and signup pages, the overview, a project's runs, a run detail
on each tab, the New project modal and the delete dialog, at 390, 1280 and
1920 wide in light and dark, plus the full landing page at 1440 and the FAQ
(tools item open) at 1440 and 1920. Those use reduced motion, so scroll
fade-ups never leave blank sections. A second set runs with motion on: the
landing at 0/25/50/75/100% scroll at 1440, and the See it work demo about
0.5, 3, 6, 9 and 12s after it enters view. Screenshots are single frames:
they cannot show whether the motion feels smooth.

It also checks layout at 390, 768, 1024, 1280, 1440, 1920 and 2560 wide (no
horizontal scroll, landing content within its 1240px container, the dashboard
filling the viewport) and that the How it works cards and their terminal
boxes are equal height (within 1px) at 1280, 1440 and 1920, writes
`ui-shots/layout-report.json`, and exits 1 if a check fails.

## Building

```bash
npm run build      # -> dist/
npm run preview    # serves dist/ with the same API proxy
```
