/**
 * The `/dashboard/projects/:projectId` route: one Project's run history.
 *
 * Step 1 wires the existing `RunHistoryView` to the URL — the Project id comes
 * from the route, and selecting a run navigates to its detail route rather
 * than setting shell state. Step 6 replaces the body with the full-width table
 * (status filter, search, Load more). The project name for the heading comes
 * from the layout's resolved project, passed down via the Outlet context.
 */

import { useNavigate, useOutletContext, useParams } from 'react-router-dom';

import RunHistoryView from './RunHistoryView.jsx';
import { useAuth } from './auth.jsx';

export default function ProjectRunsPage() {
  const { projectId } = useParams();
  const { client } = useAuth();
  const { currentProject } = useOutletContext();
  const navigate = useNavigate();

  return (
    <RunHistoryView
      client={client}
      projectId={projectId}
      projectName={currentProject?.name ?? ''}
      onSelectRun={(run) => navigate(`runs/${encodeURIComponent(run.run_id)}`)}
    />
  );
}
