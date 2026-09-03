/**
 * Unit tests for the shared tab panel (Requirement 20.6).
 *
 * Fixed-example checks only. The exhaustive "exactly this record set, or the
 * empty state, for any record set" claim is Property 28 (Task 26.3), so what is
 * here is what a generated-input property does not pin down: the specific
 * shapes an absent record arrives in from the backend, that a tab's own
 * `emptyMessage` is the one shown, that the single-`Run_Diff` case works
 * without the caller wrapping it, and the panel wiring `RunDetailView` (Task
 * 26.2) builds its tabs on.
 *
 * Nothing is mocked. The component is pure, so each test renders it for real
 * and reads the DOM.
 */

import { render, screen } from '@testing-library/react';

import TabPanel, {
  DEFAULT_EMPTY_MESSAGE,
  TAB_PANEL_ITEM_TESTID,
  normalizeRecords,
} from './TabPanel.jsx';

/** Run_Event records as the timeline tab would receive them. */
const EVENTS = [
  { id: 1, event_type: 'command', payload: 'npm install' },
  { id: 2, event_type: 'file_write', payload: 'src/index.js' },
  { id: 3, event_type: 'network', payload: 'registry.npmjs.org' },
];

it('renders exactly one item per record, in order, through renderItem', () => {
  render(
    <TabPanel
      records={EVENTS}
      renderItem={(event, index) => `${index}: ${event.event_type} ${event.payload}`}
      emptyMessage="No events."
    />,
  );

  const items = screen.getAllByTestId(TAB_PANEL_ITEM_TESTID);
  expect(items).toHaveLength(EVENTS.length);
  // The index is passed through, and the caller's order is the rendered order
  // — which is what makes the timeline's chronological order (Requirement
  // 20.3) the caller's decision rather than something re-sorted here.
  expect(items.map((item) => item.textContent)).toEqual([
    '0: command npm install',
    '1: file_write src/index.js',
    '2: network registry.npmjs.org',
  ]);
  expect(screen.queryByText('No events.')).not.toBeInTheDocument();
});

it("renders the tab's own empty message, and no items, for every absent-record shape", () => {
  // The empty list a tab with no records gets, plus the three shapes an absent
  // single record arrives in: a nullable relation serialized as `null`, an
  // omitted field, and a blank diff body.
  for (const records of [[], null, undefined, '']) {
    const { unmount } = render(
      <TabPanel
        records={records}
        renderItem={() => 'should not be rendered'}
        emptyMessage="No screenshots were captured for this run."
      />,
    );

    // Requirement 20.6: the tab says so, rather than rendering a bare panel.
    expect(
      screen.getByText('No screenshots were captured for this run.'),
    ).toBeInTheDocument();
    expect(screen.queryAllByTestId(TAB_PANEL_ITEM_TESTID)).toHaveLength(0);
    expect(screen.queryByRole('list')).not.toBeInTheDocument();

    unmount();
  }
});

it('treats a lone record as a set of one, so the diff tab needs no array', () => {
  const diff = { id: 9, unified_diff: '--- a/app.py\n+++ b/app.py' };

  render(<TabPanel records={diff} renderItem={(record) => record.unified_diff} />);

  expect(screen.getAllByTestId(TAB_PANEL_ITEM_TESTID)).toHaveLength(1);
  expect(screen.getByText(/app\.py/)).toBeInTheDocument();
});

it('falls back to a generic empty message when a tab supplies none', () => {
  render(<TabPanel records={[]} renderItem={() => null} />);

  // A tab that forgets its own copy still satisfies Requirement 20.6.
  expect(screen.getByText(DEFAULT_EMPTY_MESSAGE)).toBeInTheDocument();
});

it('exposes the panel as a tabpanel a tab can own and tests can address', () => {
  render(
    <>
      <button type="button" role="tab" id="tab-security" aria-controls="panel-security">
        Security
      </button>
      <TabPanel
        id="panel-security"
        labelledBy="tab-security"
        itemTestId="security-finding"
        listClassName="grid gap-4"
        records={[{ id: 1, severity: 'high' }]}
        renderItem={(finding) => finding.severity}
      />
    </>,
  );

  const panel = screen.getByRole('tabpanel');
  expect(panel).toHaveAttribute('id', 'panel-security');
  // Named by its own tab, so the panel is not an unlabelled region.
  expect(panel).toHaveAccessibleName('Security');
  // A per-tab item id keeps one tab's items countable while the others are
  // mounted.
  expect(screen.getAllByTestId('security-finding')).toHaveLength(1);
  expect(screen.queryAllByTestId(TAB_PANEL_ITEM_TESTID)).toHaveLength(0);
  expect(screen.getByRole('list')).toHaveClass('grid');
});

it('keeps every array entry, including a nullish one, as its own record', () => {
  // Filtering here would render fewer items than the Run has records, which
  // would break Requirements 20.2-20.5 without breaking the empty state — the
  // kind of disagreement this component exists to prevent.
  expect(normalizeRecords([EVENTS[0], null, EVENTS[1]])).toHaveLength(3);
  expect(normalizeRecords([])).toEqual([]);
  expect(normalizeRecords(0)).toEqual([0]);
});
