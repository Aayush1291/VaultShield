/**
 * The application: one URL space, one React app.
 *
 * `App` no longer *is* the dashboard shell — it is the route table. It wraps
 * everything in `AuthProvider` (which owns the signed-in state and the guarded
 * API client) and declares the routes:
 *
 *   /                                             Landing (public)
 *   /login                                        sign in (→ dashboard if already in)
 *   /signup                                       placeholder (→ dashboard if already in)
 *   /dashboard                                    projects overview      } behind RequireAuth,
 *   /dashboard/projects/:projectId                that project's runs     } inside DashboardLayout
 *   /dashboard/projects/:projectId/runs/:runId    run detail             }
 *   *                                             Not Found
 *
 * The `<Router>` itself is provided by the caller — `src/main.jsx` uses
 * `BrowserRouter`, tests use `MemoryRouter` — so `App` stays router-agnostic
 * and a test can mount it at any initial URL. `client` is injected for the
 * same reason the views accept one: a test drives the whole app against
 * `createApiClient({ fetchImpl })` with no global patched.
 */

import { Route, Routes } from 'react-router-dom';

import DashboardLayout from './DashboardLayout.jsx';
import Landing from './Landing.jsx';
import Login from './Login.jsx';
import NotFound from './NotFound.jsx';
import ProjectRunsPage from './ProjectRunsPage.jsx';
import ProjectsOverview from './ProjectsOverview.jsx';
import RunDetailPage from './RunDetailPage.jsx';
import Signup from './Signup.jsx';
import api from './api.js';
import { AuthProvider, RedirectIfSignedIn, RequireAuth } from './auth.jsx';

/**
 * @param {{ client?: typeof api }} props `client` defaults to the shared
 *   singleton; a test injects one built with `createApiClient`.
 */
export default function App({ client = api }) {
  return (
    <AuthProvider client={client}>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route
          path="/login"
          element={
            <RedirectIfSignedIn>
              <Login />
            </RedirectIfSignedIn>
          }
        />
        <Route
          path="/signup"
          element={
            <RedirectIfSignedIn>
              <Signup />
            </RedirectIfSignedIn>
          }
        />
        <Route
          path="/dashboard"
          element={
            <RequireAuth>
              <DashboardLayout />
            </RequireAuth>
          }
        >
          <Route index element={<ProjectsOverview />} />
          <Route path="projects/:projectId" element={<ProjectRunsPage />} />
          <Route path="projects/:projectId/runs/:runId" element={<RunDetailPage />} />
        </Route>
        <Route path="*" element={<NotFound />} />
      </Routes>
    </AuthProvider>
  );
}
