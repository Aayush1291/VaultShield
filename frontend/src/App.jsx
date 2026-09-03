/**
 * The dashboard shell.
 *
 * The frame the views mount into. `ProjectListView` (Task 25.2) is the landing
 * view, `RunHistoryView` (Task 25.3) sits below it, and `RunDetailView` (Task
 * 26.2) below that.
 *
 * ### Selection lives here, and there is still no router
 *
 * Requirement 19.2 starts "WHEN a Reviewer opens a Project's run history
 * view" and Requirement 20.1 "WHEN a Reviewer opens a Run detail view", so
 * something has to carry which Project and which Run those are. That something
 * is two pieces of state in this component, not a route: nothing in the
 * requirements or in design.md's Frontend section calls for URL-addressable
 * views, and picking a routing library is a decision the first view that
 * genuinely needs URL parameters should drive.
 *
 * All three views stay mounted, each rendering its own "select a project" /
 * "select a run" prompt until something is picked. That keeps the shell free of
 * conditional layout and keeps each view in charge of every state it can be in.
 *
 * Keeping the shell separate from `main.jsx` is what lets it be rendered in a
 * test without pulling in `import.meta.env` or a real DOM root.
 */

import { useState } from 'react';

import ProjectListView from './ProjectListView.jsx';
import RunDetailView from './RunDetailView.jsx';
import RunHistoryView from './RunHistoryView.jsx';

export default function App() {
  // The whole Project record, not just its id: the run history heading names
  // the Project, and the list already has the record in hand.
  const [selectedProject, setSelectedProject] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);

  /**
   * Open a Project's run history, closing whatever Run was open.
   *
   * The Run has to be dropped: it belongs to the previously selected Project,
   * and leaving it on screen would show one Project's run list above another
   * Project's run detail.
   *
   * @param {object} project
   */
  function selectProject(project) {
    setSelectedProject(project);
    setSelectedRun(null);
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <h1 className="text-lg font-semibold tracking-tight">VaultShield</h1>
          <p className="text-sm text-slate-500">Agent run review</p>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-10 px-6 py-8">
        <ProjectListView onSelectProject={selectProject} />
        <RunHistoryView
          projectId={selectedProject?.id ?? null}
          projectName={selectedProject?.name ?? ''}
          onSelectRun={setSelectedRun}
        />
        {/* `run_id`, not the pk: `RunViewSet` looks a Run up by it
            (`lookup_field = "run_id"`). */}
        <RunDetailView runId={selectedRun?.run_id ?? null} />
      </main>
    </div>
  );
}
