/**
 * The shared body of a run detail tab (Requirement 20.6, Task 26.1).
 *
 * design.md: `RunDetailView` "renders four tabs (`DiffTab`, `TimelineTab`,
 * `ScreenshotsTab`, `SecurityTab`), each driven by a shared `<TabPanel
 * records={...} renderItem={...} emptyMessage={...} />` so the empty-state
 * behavior is implemented once and reused across all four tabs."
 *
 * That sentence is the whole point of this module. Requirement 20.6 is one
 * sentence about four tabs — "IF a tab has no associated data for the selected
 * Run, THEN THE VaultShield_Dashboard SHALL display an empty-state message for
 * that tab" — and four separate `records.length === 0` checks would be four
 * chances for one tab to render a bare panel, or a `0`, or nothing at all,
 * while the other three behave. Here the decision is made once: a panel either
 * has records and renders them, or it does not and renders its message. There
 * is no third branch to get wrong.
 *
 * What each tab still owns is what only it knows: which records belong to it
 * (Requirements 20.2-20.5), how one of them looks, and what its empty state
 * should say. So this component takes those as `records`, `renderItem`, and
 * `emptyMessage` and holds no data of its own — no fetching, no state, no
 * clock. Its output is a direct function of its props, which is what lets the
 * fast-check property over per-tab rendering (Property 28, Task 26.3) drive it
 * with generated record sets and count what came out.
 *
 * ### Why `records` is not required to be an array
 *
 * Three of the four tabs hold a list (`Run_Event`, `Screenshot`,
 * `Security_Finding`), but the diff tab holds a single `Run_Diff` that may be
 * absent — `Run.diff` is a nullable one-to-one, and `RunSerializer` sends
 * `null` when no Run_Diff was recorded. Property 28 treats all four uniformly,
 * as "any set of records belonging to a given tab", and so does this: a lone
 * record counts as a set of one, and `null` counts as the empty set. `DiffTab`
 * can therefore hand its `run.diff` straight through instead of wrapping it in
 * an array at the call site, which is one more place the empty check is not
 * re-implemented.
 */

/**
 * Marks one rendered item inside a panel.
 *
 * Exported so tests count items by the same string the component renders,
 * instead of both sides hard-coding it and drifting apart. A tab that wants
 * its own items countable separately passes `itemTestId`.
 */
export const TAB_PANEL_ITEM_TESTID = 'tab-panel-item';

/**
 * The empty state a tab gets when it supplies no `emptyMessage`.
 *
 * Deliberately generic and deliberately present. Each tab should name what is
 * missing ("No screenshots were captured for this run."), but a tab that
 * forgets still satisfies Requirement 20.6 rather than rendering an empty
 * panel that reads as a broken view.
 */
export const DEFAULT_EMPTY_MESSAGE = 'No data for this tab on this run.';

/**
 * Coerce whatever a tab passed as `records` into the set of records to render.
 *
 * Total by construction, because the input is backend data rather than a
 * literal written by a developer:
 *
 * - An array is that set, entry for entry and in order. Nothing is filtered
 *   out, not even a nullish entry, because dropping one would render fewer
 *   items than the Run has records and quietly break both Requirements
 *   20.2-20.5 and Property 28. `renderItem` sees exactly what the backend
 *   sent.
 * - `null`, `undefined`, and `''` are the empty set. Those are the shapes an
 *   absent record arrives in: a nullable relation serialized as `null`, a
 *   field the payload omitted, or a blank diff body. All three mean there is
 *   nothing to show, which is Requirement 20.6's condition.
 * - Anything else — an object, a non-empty string, a number — is a set of one.
 *   That is the `Run_Diff` case described above.
 *
 * @param {unknown} records
 * @returns {unknown[]}
 */
export function normalizeRecords(records) {
  if (Array.isArray(records)) return records;
  if (records === null || records === undefined || records === '') return [];
  return [records];
}

/**
 * A tab's records, or its empty-state message.
 *
 * `renderItem` is called once per record with `(record, index)` and returns
 * that record's content; this component wraps the result, so a tab writes what
 * one Run_Event or one Screenshot looks like and never writes a `.map`, a key,
 * or an empty check. It is the one prop with no fallback: unlike `records`,
 * it cannot arrive from the backend, so a missing one is a programming error
 * that should surface in the test suite rather than be papered over with
 * placeholder markup on a Reviewer's screen.
 *
 * `role="tabpanel"` sits here because this *is* the panel. `id` and
 * `labelledBy` are the wiring that makes the role true — the panel a tab
 * controls, named by that tab — and `RunDetailView` (Task 26.2) supplies them
 * from its tab buttons.
 *
 * `role="list"` is set explicitly on the `<ul>` because Safari drops list
 * semantics from a list whose list-style is removed, which Tailwind's reset
 * does. Items are marked up as a list even in the single-record diff case: the
 * count assistive tech announces is then honest for every tab, and one
 * container means keys and item test ids are handled in one place.
 *
 * @param {{ records?: unknown,
 *           renderItem: (record: unknown, index: number) => import('react').ReactNode,
 *           emptyMessage?: string,
 *           itemTestId?: string,
 *           listClassName?: string,
 *           id?: string,
 *           labelledBy?: string }} props
 */
export default function TabPanel({
  records,
  renderItem,
  emptyMessage = DEFAULT_EMPTY_MESSAGE,
  itemTestId = TAB_PANEL_ITEM_TESTID,
  // Vertical stack by default, since three of the four tabs are sequences.
  // The screenshot gallery (Requirement 20.4) overrides it with a grid, the
  // same way `ProjectList` grids its cards.
  listClassName = 'space-y-3',
  id,
  labelledBy,
}) {
  const items = normalizeRecords(records);

  return (
    <div id={id} role="tabpanel" aria-labelledby={labelledBy}>
      {items.length === 0 ? (
        <p className="text-sm text-fg-subtle">{emptyMessage}</p>
      ) : (
        <ul role="list" className={listClassName}>
          {items.map((record, index) => (
            <li key={record?.id ?? index} data-testid={itemTestId}>
              {renderItem(record, index)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
