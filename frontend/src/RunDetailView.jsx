/**
 * The dashboard's run detail view (Requirement 20, Task 26.2).
 *
 * design.md: "`RunDetailView`: fetches `GET /api/runs/:id/`, renders four tabs
 * (`DiffTab`, `TimelineTab`, `ScreenshotsTab`, `SecurityTab`), each driven by a
 * shared `<TabPanel records={...} renderItem={...} emptyMessage={...} />` so
 * the empty-state behavior is implemented once and reused across all four
 * tabs."
 *
 * One request serves all four tabs, because `RunDetailSerializer` nests
 * everything: `events`, `diff`, `screenshots`, and `security_findings` all
 * arrive with the Run (Requirement 12.3). So switching tabs is local state, not
 * a fetch — a Reviewer comparing a finding against the diff that produced it
 * pays no round trip for it, and there is no per-tab loading state to get
 * wrong.
 *
 * Split the same way the two list views are, and for the same reason:
 *
 * - The four tab components are pure. Each takes its own record set and hands
 *   it to `TabPanel`, so its output is a direct function of its props — no
 *   fetching, no state, no clock. That is what lets the fast-check property
 *   over per-tab rendering (Property 28, Task 26.3) drive each one with
 *   generated record sets and count what came out, without simulating clicks.
 * - `RunDetailView` owns the impure concerns: which Run to ask about, the
 *   request, which of prompt / loading / failed / loaded to show, and which
 *   tab is selected.
 *
 * ### Only the selected tab's panel is mounted
 *
 * Requirements 20.2-20.5 are each phrased "WHEN a Reviewer selects the X tab",
 * so one visible panel at a time is what they describe, and rendering only that
 * one keeps the Screenshots tab from fetching every route's PNG on a Reviewer
 * who came to read the diff. Because the tab components are exported and pure,
 * nothing about testing them needs the other three mounted.
 *
 * ### The Run is addressed by `run_id`
 *
 * `RunViewSet` sets `lookup_field = "run_id"`, so `GET /api/runs/:id/` takes
 * the CLI-generated identifier rather than the database pk — the same value
 * `vaultshield submit <run-id>` uses. `api.getRun` encodes it.
 *
 * ### The decision controls (Requirement 21.4, Task 28.3)
 *
 * design.md: "Approve/Reject buttons call the corresponding backend action and,
 * on success, update the visible status copy to `"approved — awaiting
 * submission"` plus the literal text `vaultshield submit <run-id>` (with the
 * real run id interpolated) for approved Runs."
 *
 * They live in this view rather than in the run history list because a decision
 * is what a Reviewer makes *after* reading the four tabs — approving from a list
 * row would be approving a diff nobody opened.
 *
 * The backend's `apply_decision` (Task 28.1) accepts a decision only from
 * `awaiting_review`, so that is the only status the buttons are offered for;
 * see `DecisionControls` for why the alternative — always rendering them,
 * disabled — is not what happens here. The response is the decided Run as the
 * flat `RunSerializer` renders it, so it is merged into the loaded detail
 * payload rather than replacing it: replacing would drop the events, diff,
 * screenshots, and findings the four tabs are rendering from and blank all of
 * them on a successful approve.
 */

import { useEffect, useRef, useState } from 'react';

import api from './api.js';
import { describeLoadFailure, formatDate, formatTimestamp } from './format.js';
// A Run's status looks the same wherever it is shown, and `RunStatusBadge` is
// exported for exactly that. Re-styling the six Run_Status values here would
// give one Run two appearances across two views — the thing that component's
// own docstring argues against.
import { RunStatusBadge } from './RunHistoryView.jsx';
import TabPanel from './TabPanel.jsx';

/** Marks one changed file in the diff tab. */
export const DIFF_FILE_TESTID = 'diff-file';

/** Marks one Run_Event in the timeline tab. */
export const TIMELINE_EVENT_TESTID = 'timeline-event';

/** Marks one Screenshot in the gallery tab. */
export const SCREENSHOT_TESTID = 'screenshot';

/** Marks one Security_Finding in the security tab. */
export const SECURITY_FINDING_TESTID = 'security-finding';

// Requirement 20.6's empty states, one per tab. Each names what is missing
// rather than saying "no data", because the four causes are different and only
// one of them ("no findings") is good news.
export const EMPTY_DIFF_MESSAGE = 'No diff has been uploaded for this run yet.';
export const EMPTY_TIMELINE_MESSAGE = 'No events were recorded for this run.';
export const EMPTY_SCREENSHOTS_MESSAGE = 'No screenshots were captured for this run.';
export const EMPTY_SECURITY_MESSAGE = 'No security findings for this run.';

/**
 * Shown before a Run has been picked.
 *
 * Not an error and not a tab's empty state: with no Run chosen there is nothing
 * to ask the backend about, and showing four empty tabs would claim some Run
 * has no artifacts when none was selected.
 */
export const NO_RUN_SELECTED_MESSAGE =
  'Select a run to see its diff, timeline, screenshots, and security findings.';

/**
 * The only Run_Status a decision can be recorded from.
 *
 * The dashboard's copy of the backend's `ALLOWED_DECISIONS_FROM` gate
 * (Requirement 21.3). Duplicated deliberately and in one place: the buttons
 * have to know when a decision is possible before sending one, and the backend
 * remains the authority — a Run decided in another tab still refuses here, with
 * a 409 this view renders.
 */
export const DECIDABLE_STATUS = 'awaiting_review';

/**
 * Requirement 21.4's status copy for an approved Run, verbatim.
 *
 * The em dash and the wording are the requirement's, not a paraphrase, which is
 * why this is a constant rather than a string typed into the markup: it is
 * asserted on by Task 28.4's test and read back from the requirement.
 *
 * "awaiting submission" is the part that carries information a bare `approved`
 * does not — an approved Run has no pull request yet, and nothing on the
 * backend will open one. That only happens when a developer runs the CLI
 * command below on the machine holding the repository credentials
 * (Requirement 22), which is why the two are shown together.
 */
export const APPROVED_STATUS_COPY = 'approved — awaiting submission';

/** Marks the `vaultshield submit <run-id>` command text for an approved Run. */
export const SUBMIT_COMMAND_TESTID = 'submit-command';

/** The agent name of a Run that is a protection log, not a sandbox run that a
 * Reviewer approves or rejects. `protect sync` uploads these. */
export const PROTECT_AGENT = 'protect';

/** Marks the note shown in place of the decision controls for a protect-log Run. */
export const PROTECT_NOTE_TESTID = 'protect-note';

/** Shown in place of Approve/Reject for a protect-log Run: there is nothing to
 * decide, only a record to read. */
export const PROTECT_LOG_MESSAGE = 'Protection log: nothing to review';

/**
 * The CLI command that submits a given Run, with the real run id interpolated.
 *
 * `run_id` and not the database pk, because that is what `vaultshield submit`
 * takes — the same identifier `RunViewSet.lookup_field` resolves a Run by. A
 * nullish id yields `''` so the caller can omit the line rather than print a
 * command containing `undefined`, which a Reviewer could paste and run.
 *
 * @param {unknown} runId
 * @returns {string}
 */
export function formatSubmitCommand(runId) {
  if (typeof runId !== 'string' || runId.trim() === '') return '';
  return `vaultshield submit ${runId}`;
}

/**
 * The status text to show for a Run: Requirement 21.4's copy, or the stored
 * Run_Status.
 *
 * One function so the substitution happens for `approved` and only for
 * `approved`. Every other status renders as the backend stores it, matching the
 * badge in the run history list — see `RunStatusBadge` on why the dashboard
 * does not invent a second vocabulary for the other five.
 *
 * @param {unknown} status
 * @returns {unknown} the copy for `approved`, otherwise `status` unchanged
 */
export function statusCopy(status) {
  return status === 'approved' ? APPROVED_STATUS_COPY : status;
}

/**
 * The four tabs, in the order Requirement 20.1 lists them.
 *
 * A table rather than four hand-written pairs of button and panel, so a tab
 * cannot exist as a button with no panel or as a panel no button reaches, and
 * so the id wiring (`aria-controls` / `aria-labelledby`) is derived once
 * instead of typed eight times.
 */
export const TABS = [
  {
    id: 'diff',
    label: 'Diff',
    Panel: DiffTab,
    // Each tab names its own record type rather than taking a generic
    // `records`, so `propsOf` is where the one nested field it reads off the
    // Run detail payload is pinned. One entry per tab, and nothing else in this
    // module needs to know that the security tab's records live under
    // `security_findings`.
    propsOf: (run) => ({ diff: run?.diff }),
  },
  { id: 'timeline', label: 'Timeline', Panel: TimelineTab, propsOf: (run) => ({ events: run?.events }) },
  {
    id: 'screenshots',
    label: 'Screenshots',
    Panel: ScreenshotsTab,
    propsOf: (run) => ({ screenshots: run?.screenshots }),
  },
  {
    id: 'security',
    label: 'Security',
    Panel: SecurityTab,
    propsOf: (run) => ({ findings: run?.security_findings }),
  },
];

/** The tab a freshly opened Run detail view shows. */
export const DEFAULT_TAB_ID = TABS[0].id;

/**
 * The Run_Diff for the selected Run (Requirement 20.2).
 *
 * The record set is the diff's per-file entries, not the `Run_Diff` row itself.
 * `RunDiff.files` is the whole change set (`[{path, additions, deletions},
 * ...]`) and there is nothing else on the row, so one item per changed file is
 * what "display the Run_Diff" amounts to — and it makes both ways a diff can be
 * empty land on Requirement 20.6's empty state through the same branch: no diff
 * ingested yet (`diff` is `null`, which is what the serializer sends for a Run
 * still in progress) and a diff of nothing (`files: []`, which
 * `build_structured_diff` writes for a Run that changed no files). Passing the
 * row through as a single record would render an item containing an empty file
 * list for the second case, which is the bare panel Requirement 20.6 exists to
 * prevent.
 *
 * The counts are labelled for assistive tech because `+7` and `-2` on their own
 * are not readable as additions and deletions.
 *
 * @param {{ diff?: { files?: Array<{path?: string, additions?: number,
 *                                   deletions?: number}> }|null,
 *           id?: string, labelledBy?: string }} props
 */
export function DiffTab({ diff, id, labelledBy }) {
  // Guarded rather than passed straight through: `RunDiff.files` is a
  // `JSONField`, so the column itself promises nothing about the shape.
  const files = Array.isArray(diff?.files) ? diff.files : [];

  return (
    <TabPanel
      id={id}
      labelledBy={labelledBy}
      records={files}
      itemTestId={DIFF_FILE_TESTID}
      emptyMessage={EMPTY_DIFF_MESSAGE}
      listClassName="divide-y divide-border overflow-hidden rounded-card-lg border border-border bg-surface shadow-soft"
      renderItem={(file) => (
        <div className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3">
          <span className="min-w-0 grow break-all font-mono text-sm text-fg">
            {file?.path}
          </span>
          <span
            className="font-mono text-xs text-success-fg"
            aria-label={`${file?.additions ?? 0} additions`}
          >
            +{file?.additions ?? 0}
          </span>
          <span
            className="font-mono text-xs text-danger-fg"
            aria-label={`${file?.deletions ?? 0} deletions`}
          >
            -{file?.deletions ?? 0}
          </span>
        </div>
      )}
    />
  );
}

/**
 * The Run_Event records for the selected Run, oldest first (Requirement 20.3).
 *
 * The ordering is done here rather than trusted from the payload, because
 * "in chronological order" is a requirement on the dashboard: `RunHistoryView`
 * deliberately does not re-sort its rows since nothing specifies their order,
 * but this tab has an order specified for it, and a tab that merely echoes
 * whatever order it was handed satisfies Requirement 20.3 only for as long as
 * the backend's `ordering` happens to agree with it.
 *
 * In practice the two do agree — `RunEvent.Meta.ordering` is `["sequence"]`,
 * and `sequence` is assigned from the position of each event in a batch the
 * Monitor already timestamp-sorted (Requirement 6.3) — so this sort is normally
 * a no-op. What it buys is that the claim holds regardless.
 *
 * @param {{ events?: Array<object>, id?: string, labelledBy?: string }} props
 */
export function TimelineTab({ events, id, labelledBy }) {
  const ordered = sortEventsChronologically(events);

  return (
    <TabPanel
      id={id}
      labelledBy={labelledBy}
      records={ordered}
      itemTestId={TIMELINE_EVENT_TESTID}
      emptyMessage={EMPTY_TIMELINE_MESSAGE}
      listClassName="divide-y divide-border overflow-hidden rounded-card-lg border border-border bg-surface shadow-soft"
      renderItem={(event) => {
        const at = formatTimestamp(event?.timestamp);

        return (
          <div className="px-5 py-3">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="font-mono text-xs text-fg-subtle">{at}</span>
              <span className="rounded-pill bg-surface-2 px-2 py-0.5 font-mono text-xs text-fg-muted">
                {event?.event_type}
              </span>
            </div>
            {/* `whitespace-pre-wrap`: a `payload` is a captured line of
                terminal output, so its own line breaks and indentation are
                part of what the Reviewer needs to read. */}
            <p className="mt-1.5 whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-fg-muted">
              {event?.payload}
            </p>
          </div>
        );
      }}
    />
  );
}

/**
 * The Screenshot records for the selected Run (Requirement 20.4).
 *
 * A gallery, so the panel grids instead of stacking — `TabPanel`'s
 * `listClassName` exists for exactly this, the same way `ProjectList` grids its
 * cards.
 *
 * `image` is a `MEDIA_URL` path the backend renders absolute when its
 * serializer has the request in context, so it is used as the `src` unchanged.
 * The route is both the caption and the alt text: a screenshot's only
 * distinguishing feature is which route it captured, so "Screenshot of /login"
 * is the most a reader can be told without the image being described by hand.
 *
 * @param {{ screenshots?: Array<{route?: string, image?: string}>,
 *           id?: string, labelledBy?: string }} props
 */
export function ScreenshotsTab({ screenshots, id, labelledBy }) {
  return (
    <TabPanel
      id={id}
      labelledBy={labelledBy}
      records={screenshots}
      itemTestId={SCREENSHOT_TESTID}
      emptyMessage={EMPTY_SCREENSHOTS_MESSAGE}
      listClassName="grid gap-4 sm:grid-cols-2"
      renderItem={(screenshot) => (
        <figure className="overflow-hidden rounded-card-lg border border-border bg-surface shadow-soft">
          <img
            src={screenshot?.image}
            alt={`Screenshot of ${screenshot?.route ?? 'an unnamed route'}`}
            // Lazy because a Run can have a screenshot per configured route and
            // they are only worth fetching once this tab is on screen.
            loading="lazy"
            className="block w-full bg-surface-2 object-cover"
          />
          <figcaption className="break-all border-t border-border px-3 py-2 font-mono text-xs text-fg-muted">
            {screenshot?.route}
          </figcaption>
        </figure>
      )}
    />
  );
}

/**
 * The Security_Finding records for the selected Run (Requirement 20.5).
 *
 * All four stored fields are shown, because each one answers a different
 * question a Reviewer has: which rule matched (`rule_id`), where
 * (`file_path`/`line_number`), and why it matters (`description`).
 *
 * `line_number` is nullable in the schema — a file-level finding has no
 * meaningful line — so the location renders as the bare path in that case
 * rather than as `app.py:null` or a fabricated `:0`.
 *
 * @param {{ findings?: Array<{rule_id?: string, file_path?: string,
 *                             line_number?: number|null, description?: string}>,
 *           id?: string, labelledBy?: string }} props
 */
export function SecurityTab({ findings, id, labelledBy }) {
  return (
    <TabPanel
      id={id}
      labelledBy={labelledBy}
      records={findings}
      itemTestId={SECURITY_FINDING_TESTID}
      emptyMessage={EMPTY_SECURITY_MESSAGE}
      renderItem={(finding) => (
        <div className="rounded-card-lg border border-warn-fg/25 bg-warn-soft p-4">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="rounded-pill bg-surface px-2 py-0.5 font-mono text-xs text-warn-fg">
              {finding?.rule_id}
            </span>
            <span className="break-all font-mono text-xs text-fg-muted">
              {formatFindingLocation(finding)}
            </span>
          </div>
          <p className="mt-2 text-sm text-fg">{finding?.description}</p>
        </div>
      )}
    />
  );
}

/**
 * A finding's location as one string, or `''` when it has no path.
 *
 * @param {{ file_path?: string, line_number?: number|null }} [finding]
 * @returns {string}
 */
export function formatFindingLocation(finding) {
  const path = finding?.file_path ?? '';
  if (path === '') return '';
  return Number.isFinite(finding?.line_number) ? `${path}:${finding.line_number}` : path;
}

/**
 * The two decisions, as buttons (Requirements 21.1 and 21.2).
 *
 * One table rather than two hand-written buttons, so the disabled state, the
 * busy state, and the value posted are written once for both — a Reject button
 * that stayed clickable while an Approve was in flight would let a Reviewer
 * record two decisions on one Run, which the backend then refuses as a 409 for
 * reasons that have nothing to do with what they meant to do.
 */
const DECISIONS = [
  {
    value: 'approved',
    label: 'Approve',
    className:
      'border-accent bg-accent text-accent-fg hover:bg-accent-hover hover:text-accent-hover-fg disabled:opacity-60',
  },
  {
    value: 'rejected',
    label: 'Reject',
    className:
      'border-danger bg-surface text-danger-fg hover:bg-danger-soft disabled:opacity-60',
  },
];

/**
 * The decision area under the Run header: the two buttons, the approved Run's
 * submit command, and whatever a refused decision needs to say.
 *
 * Pure, like the four tab components, so its three states can be rendered from
 * props alone: a Run awaiting review, an approved Run, and a decision that
 * failed. `RunDetailView` owns the request and the state.
 *
 * ### The buttons appear only for a Run awaiting review
 *
 * Not rendered-but-disabled for the other four statuses, which is the other
 * defensible reading of "only actionable in `awaiting_review`". A disabled
 * Approve button on a rejected Run reads as "not yet" — as though finishing
 * something would enable it — when the truth is that this Run has been decided
 * and never will be again (`Approval.run` is a `OneToOneField`: a Run is decided
 * at most once). The status badge above already says which of the five states
 * the Run is in, so nothing is hidden by leaving the controls out; what is left
 * out is an affordance that could never work.
 *
 * Within `awaiting_review` the buttons *are* disabled rather than removed while
 * a decision is in flight, because there the affordance is real and the reason
 * it is briefly unavailable is temporary.
 *
 * ### A protect-log Run has nothing to decide
 *
 * A Run whose `agent` is `protect` is the protection log `vaultshield protect
 * sync` uploads, not a sandbox run a Reviewer acts on. `complete_run` still
 * leaves it in `awaiting_review`, which would otherwise show the buttons, so
 * this case is handled before the status is read: the Approve and Reject buttons
 * are left out entirely and a short note takes their place.
 *
 * @param {{ status?: string, agent?: string, runId?: string|null,
 *           pending?: string|null, error?: unknown,
 *           onDecide?: (decision: string) => void }} props
 */
export function DecisionControls({ status, agent, runId, pending = null, error = null, onDecide }) {
  if (agent === PROTECT_AGENT) {
    return (
      <div className="mb-4">
        <p data-testid={PROTECT_NOTE_TESTID} className="text-sm text-fg-muted">
          {PROTECT_LOG_MESSAGE}
        </p>
      </div>
    );
  }

  const decidable = status === DECIDABLE_STATUS;
  // Requirement 21.4's second half, and only for an approved Run: a `submitted`
  // Run already has its pull request, and running the command again on one would
  // open a second for the same change set.
  const submitCommand = status === 'approved' ? formatSubmitCommand(runId) : '';

  // Nothing to decide, nothing to submit, nothing that went wrong — so no
  // empty bordered strip between the header and the tabs.
  if (!decidable && submitCommand === '' && error === null) return null;

  return (
    <div className="mb-4 space-y-3">
      {decidable && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-card-lg bg-accent-soft px-5 py-4">
          <p className="text-sm font-medium text-fg">This run is awaiting your review.</p>
          <div className="flex flex-wrap items-center gap-3">
            {DECISIONS.map((decision) => (
              <button
                key={decision.value}
                type="button"
                // Both buttons go down together: see `DECISIONS`.
                disabled={pending !== null}
                // Announces the wait on the button the Reviewer actually pressed,
                // rather than only on the status line below.
                aria-busy={pending === decision.value}
                onClick={() => onDecide?.(decision.value)}
                className={`rounded-pill border px-5 py-2 text-sm font-semibold transition-colors disabled:cursor-not-allowed ${decision.className}`}
              >
                {decision.label}
              </button>
            ))}
            {pending !== null && (
              <p role="status" className="text-xs text-fg-muted">
                Recording your decision…
              </p>
            )}
          </div>
        </div>
      )}

      {submitCommand !== '' && (
        <div className="rounded-card-lg border border-success-fg/25 bg-success-soft p-4">
          <p className="text-sm text-success-fg">
            No pull request has been opened yet. Run this from the machine that holds the repository
            credentials:
          </p>
          {/* The literal command, in its own element, so a Reviewer can select
              the whole thing without picking the sentence up with it. */}
          <code
            data-testid={SUBMIT_COMMAND_TESTID}
            className="mt-2 block break-all font-mono text-sm text-success-fg"
          >
            {submitCommand}
          </code>
        </div>
      )}

      {error !== null && (
        <div
          role="alert"
          className="rounded-card-lg border border-danger/30 bg-danger-soft p-4 text-sm text-danger-fg"
        >
          {describeDecisionFailure(error)}
        </div>
      )}
    </div>
  );
}

/**
 * Turn a refused decision into something worth showing a Reviewer.
 *
 * A sibling of `describeLoadFailure` rather than a call to it, because a failed
 * decision has a case a failed load does not: Requirement 21.3's refusal
 * (Task 28.1's `StateTransitionError`) arrives as a 409 whose `detail` already
 * names the status the Run is actually in, and that sentence is more useful than
 * anything this function could write over the top of it. It is shown without the
 * client's `409 Conflict:` prefix, which tells a Reviewer nothing.
 *
 * The two statuses with authored copy are the two where the cause is not in the
 * body: 401/403 is a missing or expired Dashboard_Auth_Token, and status `0` is
 * `ApiError`'s "the request never reached the backend". Both say explicitly that
 * no decision was recorded, because a Reviewer who has just pressed Approve and
 * seen an error needs to know whether to press it again.
 *
 * @param {unknown} error typically an `ApiError`
 * @returns {string}
 */
export function describeDecisionFailure(error) {
  const status = error?.status;
  if (status === 401 || status === 403) {
    return 'Sign in to record a decision. No decision was recorded.';
  }
  if (status === 0) {
    return 'The backend could not be reached, so no decision was recorded. Check that it is running, then try again.';
  }

  const detail = error?.payload?.detail;
  if (typeof detail === 'string' && detail.trim() !== '') return detail;

  return error?.message || 'The decision could not be recorded.';
}

/**
 * Fold a decision response into the loaded Run detail payload.
 *
 * `POST /api/runs/:id/approve/` answers with the decided Run as the flat
 * `RunSerializer` renders it — no `events`, `diff`, `screenshots`, or
 * `security_findings`, since a decision changes one field and re-sending every
 * artifact to report it would be a large response saying very little. So the
 * response is merged over the loaded Run instead of replacing it: replacing
 * would leave the four tabs with nothing to render and turn a successful
 * approve into four empty states.
 *
 * `decision` is the fallback rather than the primary source, so the status shown
 * is the one the backend stored. They agree in practice — `apply_decision`
 * writes the decision to `Run.status` — and when the response is not a usable
 * object (a `204`, a proxy's HTML), the decision that was just accepted is still
 * a better answer than leaving `awaiting_review` on screen.
 *
 * @param {object|null} run the currently loaded Run detail payload
 * @param {string} decision `'approved'` or `'rejected'`
 * @param {unknown} payload the decision response body
 * @returns {object} a new Run object; neither argument is mutated
 */
export function mergeDecidedRun(run, decision, payload) {
  const usable = payload !== null && typeof payload === 'object' && !Array.isArray(payload);
  return { ...run, ...(usable ? payload : { status: decision }) };
}

/**
 * Order a Run's events oldest first, without dropping any of them.
 *
 * Pure, total, and a stable total order, which matters because a comparator
 * that is not transitive can put the same records in different orders on
 * different engines — and Property 28 asserts on the rendered order:
 *
 * 1. `timestamp`, the clock the Monitor recorded the event by, is the primary
 *    key. An event with no parseable timestamp cannot be placed
 *    chronologically, so it sorts after every event that can be.
 * 2. `sequence` breaks a tie, which is the common case rather than an edge one:
 *    two events written in the same second are ordinary, and `sequence` is the
 *    backend's own record of the order they were submitted in.
 * 3. Position in the input breaks a remaining tie, so events the first two keys
 *    cannot separate keep the order they arrived in instead of being shuffled.
 *
 * Every entry of the input appears in the output, including a nullish one:
 * rendering fewer events than the Run recorded would break Requirement 20.3
 * without breaking the empty state.
 *
 * @param {unknown} events
 * @returns {unknown[]} a new array; the input is not mutated
 */
export function sortEventsChronologically(events) {
  const records = Array.isArray(events) ? events : [];

  return records
    .map((event, index) => ({
      event,
      index,
      time: comparableTime(event?.timestamp),
      order: comparableNumber(event?.sequence),
    }))
    .sort(
      (a, b) =>
        // `Infinity - Infinity` is `NaN`, which is falsy, so two undated events
        // fall through to the next key rather than comparing as equal-and-done.
        a.time - b.time || a.order - b.order || a.index - b.index,
    )
    .map((entry) => entry.event);
}

/**
 * A timestamp as a sortable number, with unparseable values sorting last.
 *
 * @param {unknown} value
 * @returns {number}
 */
function comparableTime(value) {
  if (typeof value !== 'string' && typeof value !== 'number') return Number.POSITIVE_INFINITY;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed;
}

/**
 * A numeric field as a sortable number, with missing values sorting last.
 *
 * @param {unknown} value
 * @returns {number}
 */
function comparableNumber(value) {
  return Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
}

/**
 * Fetch `GET /api/runs/:id/` and render the Run across its four tabs.
 *
 * The API client is a prop defaulting to the shared singleton, matching how
 * `src/api.test.js` injects a transport: a test drives this against
 * `createApiClient({ fetchImpl })` and asserts on real rendering, with no
 * global patched and no module mocked.
 *
 * A nullish `runId` does not fetch — there is no Run to ask for — and renders
 * `NO_RUN_SELECTED_MESSAGE`.
 *
 * @param {{ runId?: string|null, client?: typeof api,
 *           tab?: string, onTabChange?: (id: string) => void }} props
 *   `tab`/`onTabChange` make the selected tab controllable (the run detail
 *   page drives it from the URL `?tab=`); omit both to use internal state.
 */
export default function RunDetailView({ runId = null, client = api, tab, onTabChange }) {
  // `null` distinguishes "not loaded yet" from a loaded Run, so the first paint
  // is a loading line rather than a flash of four empty tabs.
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [reloadCount, setReloadCount] = useState(0);
  // The selected tab is controllable: when `tab`/`onTabChange` are supplied
  // (the page drives them from the URL) they win; otherwise it is internal
  // state, which is the standalone behaviour this component's tests rely on.
  const tabControlled = tab !== undefined;
  const [internalTabId, setInternalTabId] = useState(DEFAULT_TAB_ID);
  const activeTabId = tabControlled ? tab || DEFAULT_TAB_ID : internalTabId;
  const changeTab = (id) => {
    if (tabControlled) onTabChange?.(id);
    else setInternalTabId(id);
  };
  // The decision currently being posted, or `null`. Both a "which button is
  // busy" flag and the guard that keeps a second decision from being sent while
  // the first is in flight.
  const [pendingDecision, setPendingDecision] = useState(null);
  const [decisionError, setDecisionError] = useState(null);
  // Focus has to follow arrow-key selection: the newly selected tab is the only
  // one in the tab order (see `tabIndex` below), so leaving focus on the old
  // one would strand the keyboard user on an element they have moved past.
  const tabRefs = useRef({});
  // Which Run is on screen right now, readable from an async continuation that
  // closed over an older render's `runId` — see `decide`.
  const selectedRunIdRef = useRef(runId);

  useEffect(() => {
    selectedRunIdRef.current = runId;
    // A decision belongs to the Run it was made on, so neither its in-flight
    // state nor its error survives a switch to another Run.
    setPendingDecision(null);
    setDecisionError(null);

    if (runId === null || runId === undefined || runId === '') {
      // Drop whatever the previously selected Run loaded, so its artifacts do
      // not stay on screen under a "select a run" prompt.
      setRun(null);
      setError(null);
      return undefined;
    }

    const controller = new AbortController();
    let active = true;

    setRun(null);
    setError(null);

    (async () => {
      try {
        const payload = await client.getRun(runId, { signal: controller.signal });
        if (active) setRun(payload);
      } catch (cause) {
        // The client wraps an aborted request as a status-0 `ApiError` like any
        // other transport failure, so the signal is what tells the two apart.
        // Without this check, StrictMode's second effect pass — which aborts
        // the first — would surface as "the backend could not be reached" on a
        // working backend.
        if (active && !controller.signal.aborted) setError(cause);
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, [client, runId, reloadCount]);

  // A newly selected Run opens on the diff — but only when the tab is internal;
  // a controlled tab is owned by the URL, so a deep link like `?tab=security`
  // is honoured rather than reset on mount.
  useEffect(() => {
    if (!tabControlled) setInternalTabId(DEFAULT_TAB_ID);
  }, [runId, tabControlled]);

  const selected = runId !== null && runId !== undefined && runId !== '';
  const loading = selected && run === null && error === null;
  const activeTab = TABS.find((tab) => tab.id === activeTabId) ?? TABS[0];

  /**
   * Record a decision for the Run on screen (Requirements 21.1, 21.2, 21.4).
   *
   * Posts to the action the decision names — the decision is the URL, not a
   * request body, which is how the backend routes it — and folds the response
   * into the loaded Run, so the status copy and the submit command appear without
   * a second `GET /api/runs/:id/`.
   *
   * A refusal is not only shown, it is applied: Requirement 21.3's 409 carries
   * the Run's stored `current_status` as a bare value for exactly this, so a Run
   * decided in another tab (or by another Reviewer) stops showing buttons that
   * cannot work, instead of leaving them there next to an error the Reviewer is
   * invited to retry into.
   *
   * Every state update is gated on the Run still being the selected one. A
   * decision is a click, so the Reviewer is free to select a different Run while
   * it is in flight — and without the guard, that Run would take on the previous
   * Run's status.
   *
   * @param {string} decision `'approved'` or `'rejected'`
   */
  async function decide(decision) {
    const decidedRunId = runId;
    if (decidedRunId === null || decidedRunId === undefined || decidedRunId === '') return;

    setPendingDecision(decision);
    setDecisionError(null);

    try {
      const payload =
        decision === 'approved'
          ? await client.approveRun(decidedRunId)
          : await client.rejectRun(decidedRunId);

      if (selectedRunIdRef.current !== decidedRunId) return;
      setRun((current) => mergeDecidedRun(current, decision, payload));
    } catch (cause) {
      if (selectedRunIdRef.current !== decidedRunId) return;

      setDecisionError(cause);

      const storedStatus = cause?.payload?.current_status;
      if (typeof storedStatus === 'string' && storedStatus !== '') {
        setRun((current) => (current === null ? current : { ...current, status: storedStatus }));
      }
    } finally {
      if (selectedRunIdRef.current === decidedRunId) setPendingDecision(null);
    }
  }

  /**
   * Select a tab and move focus to it, for arrow-key navigation.
   *
   * @param {string} id
   */
  function selectAndFocus(id) {
    changeTab(id);
    tabRefs.current[id]?.focus();
  }

  /**
   * The tablist's keyboard contract.
   *
   * `role="tablist"` is a promise that the arrow keys move between tabs, so it
   * is honoured rather than left as decoration. Selection follows focus
   * (automatic activation), which is the recommended behaviour when switching
   * panels is cheap — and here it is, since every tab's records are already in
   * hand from the one request.
   *
   * @param {import('react').KeyboardEvent<HTMLDivElement>} event
   */
  function onTabListKeyDown(event) {
    const current = TABS.findIndex((tab) => tab.id === activeTabId);
    if (current === -1) return;

    let next = null;
    if (event.key === 'ArrowRight') next = (current + 1) % TABS.length;
    else if (event.key === 'ArrowLeft') next = (current - 1 + TABS.length) % TABS.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = TABS.length - 1;

    if (next === null) return;
    // Otherwise Left/Right would also scroll the panel and Home/End would jump
    // the page, on a keystroke the Reviewer aimed at the tabs.
    event.preventDefault();
    selectAndFocus(TABS[next].id);
  }

  const created = formatDate(run?.created_at);

  return (
    <section aria-labelledby="run-detail-heading">
      {/* Sticky under the top bar, so the Run's id and status stay in view
          while a long diff or timeline scrolls. */}
      <div className="sticky top-16 z-10 -mx-6 mb-6 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2 border-b border-border bg-canvas/80 px-6 py-4 backdrop-blur-md md:-mx-8 md:px-8">
        <h2 id="run-detail-heading" className="font-display text-page font-normal text-fg">
          Run detail
        </h2>
        {run !== null && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span className="break-all font-mono text-sm text-fg">{run.run_id}</span>
            <span className="rounded-pill bg-surface-2 px-2.5 py-0.5 font-mono text-xs text-fg-muted">
              {run.agent}
            </span>
            {/* Requirement 21.4's copy replaces the badge's text for an approved
                Run rather than sitting beside it: two status strings in one
                header, in two vocabularies, is one more than the Run has. */}
            <RunStatusBadge status={run.status} label={statusCopy(run.status)} />
            {created !== '' && <span className="text-xs text-fg-subtle">{created}</span>}
          </div>
        )}
      </div>

      {!selected && <p className="text-sm text-fg-subtle">{NO_RUN_SELECTED_MESSAGE}</p>}

      {loading && (
        <div role="status" aria-busy="true" className="space-y-3">
          <span className="sr-only">Loading run…</span>
          <div aria-hidden="true" className="h-8 w-40 animate-pulse rounded-card bg-surface-2" />
          <div
            aria-hidden="true"
            className="h-40 animate-pulse rounded-card-lg border border-border bg-surface-2"
          />
        </div>
      )}

      {error !== null && (
        <div
          role="alert"
          className="rounded-card-lg border border-danger/30 bg-danger-soft p-4 text-sm text-danger-fg"
        >
          <p>{describeLoadFailure(error, { subject: 'run details' })}</p>
          <button
            type="button"
            onClick={() => setReloadCount((count) => count + 1)}
            className="mt-3 rounded-pill border border-danger bg-surface px-3 py-1 text-xs font-medium text-danger-fg hover:bg-danger-soft"
          >
            Try again
          </button>
        </div>
      )}

      {run !== null && (
        <>
          {/* Above the tabs, because a decision is about the Run rather than
              about whichever tab is open, and it should not scroll away with a
              long diff. */}
          <DecisionControls
            status={run.status}
            agent={run.agent}
            runId={run.run_id ?? runId}
            pending={pendingDecision}
            error={decisionError}
            onDecide={decide}
          />

          {/* The key handler sits on the tablist rather than on each tab, which
              is where the ARIA pattern puts it: it needs to know the whole tab
              order to answer "the next one". */}
          <div
            role="tablist"
            aria-label="Run artifacts"
            onKeyDown={onTabListKeyDown}
            className="mb-6 inline-flex max-w-full flex-wrap gap-1 rounded-pill border border-border bg-surface-2 p-1"
          >
            {TABS.map((tab) => {
              const isActive = tab.id === activeTab.id;

              return (
                <button
                  key={tab.id}
                  type="button"
                  role="tab"
                  id={tabDomId(tab.id)}
                  aria-selected={isActive}
                  aria-controls={panelDomId(tab.id)}
                  // Roving tabindex: the tablist is one stop in the page's tab
                  // order, and the arrow keys move within it.
                  tabIndex={isActive ? 0 : -1}
                  ref={(node) => {
                    tabRefs.current[tab.id] = node;
                  }}
                  onClick={() => changeTab(tab.id)}
                  className={`rounded-pill px-4 py-1.5 text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-surface text-fg shadow-soft'
                      : 'text-fg-muted hover:text-fg'
                  }`}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>

          <activeTab.Panel
            {...activeTab.propsOf(run)}
            id={panelDomId(activeTab.id)}
            labelledBy={tabDomId(activeTab.id)}
          />
        </>
      )}
    </section>
  );
}

/** DOM id of a tab button, referenced by its panel's `aria-labelledby`. */
function tabDomId(id) {
  return `run-tab-${id}`;
}

/** DOM id of a tab's panel, referenced by its tab's `aria-controls`. */
function panelDomId(id) {
  return `run-tabpanel-${id}`;
}
