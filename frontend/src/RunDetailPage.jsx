/**
 * The `/dashboard/projects/:projectId/runs/:runId` route: one Run's detail.
 *
 * Step 1 wires the existing `RunDetailView` to the URL (the run id is a route
 * param) and adds a back link to the Project's runs. Step 7 lifts the active
 * tab into the URL and gives the page the sticky header treatment.
 */

import { Link, useParams } from 'react-router-dom';

import RunDetailView from './RunDetailView.jsx';
import { useAuth } from './auth.jsx';

export default function RunDetailPage() {
  const { projectId, runId } = useParams();
  const { client } = useAuth();

  return (
    <div className="space-y-4">
      <Link
        to={`/dashboard/projects/${encodeURIComponent(projectId)}`}
        className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline"
      >
        <span aria-hidden="true">←</span> Back to runs
      </Link>
      <RunDetailView client={client} runId={runId} />
    </div>
  );
}
