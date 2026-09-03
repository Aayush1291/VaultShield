/**
 * Display text shared by the dashboard's views.
 *
 * The first two were written for `ProjectListView` (Task 25.2) and are needed
 * verbatim by `RunHistoryView` (Task 25.3): a Run row shows a timestamp the
 * same way a Project card does, and a failed run-history load has the same
 * three causes a failed project load has. They live in their own module rather
 * than being imported view-to-view, so neither list view depends on the other
 * and `RunDetailView` (Task 26) reuses both without pulling a list view in with
 * them — which it does, adding `formatTimestamp` for the event timeline.
 *
 * Nothing here touches the DOM, React, or the API client — these are pure
 * functions of their arguments, which is what lets a test (and Property 27)
 * pin the rendered text without rendering anything.
 */

/**
 * Format a backend timestamp for display.
 *
 * ISO date rather than `toLocaleString`: the rendered text is then identical
 * on every machine and in CI, so a test asserting on a card's or row's
 * contents is not asserting on the runner's locale or timezone. Anything
 * unparseable — a missing field, `null`, a non-date string — yields `''` and
 * the caller omits the line, because "Created Invalid Date" on screen is
 * worse than no creation date at all.
 *
 * @param {unknown} value typically an ISO 8601 timestamp from DRF
 * @returns {string} a `YYYY-MM-DD` date, or `''` when there is nothing to show
 */
export function formatDate(value) {
  if (typeof value !== 'string' && typeof value !== 'number') return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toISOString().slice(0, 10);
}

/**
 * Format a backend timestamp for display down to the second.
 *
 * `formatDate` is deliberately date-only, which is right for a Project card or
 * a Run row but useless in the event timeline (Requirement 20.3): a Run's
 * events are seconds apart, so every one of them would render the same day and
 * the order on screen would look arbitrary.
 *
 * UTC and ISO-shaped for the same reason `formatDate` is — the rendered text is
 * then identical on every machine and in CI, so a test asserting on an event's
 * contents is not asserting on the runner's timezone. The `T` becomes a space
 * because this is read as a timestamp rather than parsed. Anything unparseable
 * yields `''` and the caller omits the line.
 *
 * @param {unknown} value typically an ISO 8601 timestamp from DRF
 * @returns {string} a `YYYY-MM-DD HH:MM:SS` timestamp, or `''`
 */
export function formatTimestamp(value) {
  if (typeof value !== 'string' && typeof value !== 'number') return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toISOString().slice(0, 19).replace('T', ' ');
}

/**
 * Turn a failed load into something worth showing a Reviewer.
 *
 * The two statuses that get their own copy are the two with a specific cause
 * and a specific fix. 401/403 means no valid Dashboard_Auth_Token is stored:
 * the backend applies `TokenAuthentication` + `IsAuthenticated` to every
 * `/api/` path except login (Requirements 27.3-27.5), so it refuses the
 * request before the view body runs, and the fix is to sign in — not to
 * retry. Status `0` is `ApiError`'s "the request never reached the backend",
 * where retrying is exactly the right move. Everything else falls back to the
 * client's own message, which already carries DRF's `detail` when there is
 * one.
 *
 * `subject` is the plural lowercase noun for what failed to load
 * (`'projects'`, `'runs'`), so the two authored messages name the thing the
 * Reviewer was looking at instead of saying "data".
 *
 * @param {unknown} error typically an `ApiError`
 * @param {{ subject: string }} options
 * @returns {string}
 */
export function describeLoadFailure(error, { subject }) {
  const status = error?.status;
  if (status === 401 || status === 403) {
    return `Sign in to load ${subject}. Every API path except login requires a dashboard auth token.`;
  }
  if (status === 0) {
    return 'The backend could not be reached. Check that it is running, then try again.';
  }
  return error?.message || `${capitalize(subject)} could not be loaded.`;
}

/**
 * Upper-case the first character, for a `subject` used at the start of a
 * sentence. Not a general-purpose title-caser: the callers pass a single
 * lowercase word.
 *
 * @param {string} value
 * @returns {string}
 */
function capitalize(value) {
  return value === '' ? value : `${value[0].toUpperCase()}${value.slice(1)}`;
}
