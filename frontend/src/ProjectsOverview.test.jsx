/**
 * Tests for the projects overview (step 8): linked cards, empty and loading
 * states, and the New project button.
 *
 * The overview reads the layout's Outlet context, so it is mounted inside a
 * route that supplies one.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom';

import ProjectsOverview, { OVERVIEW_CARD_TESTID } from './ProjectsOverview.jsx';
import { EMPTY_PROJECTS_MESSAGE } from './ProjectListView.jsx';

function renderOverview(context) {
  render(
    <MemoryRouter>
      <Routes>
        <Route path="/" element={<Outlet context={context} />}>
          <Route index element={<ProjectsOverview />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

const PROJECTS = [
  { id: 7, name: 'checkout', repo_url: 'git@github.com:acme/checkout.git', created_at: '2024-05-01T10:00:00Z' },
  { id: 8, name: 'billing', repo_url: 'https://github.com/acme/billing.git', created_at: null },
];

it('renders one card per project, each linking to its runs route', () => {
  renderOverview({ projects: PROJECTS, openNewProject: jest.fn() });

  const cards = screen.getAllByTestId(OVERVIEW_CARD_TESTID);
  expect(cards).toHaveLength(2);
  expect(screen.getByRole('link', { name: /checkout/ })).toHaveAttribute('href', '/dashboard/projects/7');
  expect(cards[0]).toHaveTextContent('git@github.com:acme/checkout.git');
  expect(cards[0]).toHaveTextContent('Created 2024-05-01');
  // `created_at: null` renders no creation line.
  expect(cards[1]).not.toHaveTextContent('Created');
});

it('shows an empty state when there are no projects', () => {
  renderOverview({ projects: [], openNewProject: jest.fn() });

  expect(screen.getByText(EMPTY_PROJECTS_MESSAGE)).toBeInTheDocument();
  expect(screen.queryAllByTestId(OVERVIEW_CARD_TESTID)).toHaveLength(0);
});

it('shows a loading state before the project list has loaded', () => {
  renderOverview({ projects: null, openNewProject: jest.fn() });

  expect(screen.getByRole('status')).toHaveTextContent(/loading projects/i);
});

it('opens the New project modal via the layout', async () => {
  const openNewProject = jest.fn();
  renderOverview({ projects: [], openNewProject });

  await userEvent.click(screen.getByRole('button', { name: 'New project' }));
  expect(openNewProject).toHaveBeenCalledTimes(1);
});
