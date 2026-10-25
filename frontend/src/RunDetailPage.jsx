/**
 * The `/dashboard/projects/:projectId/runs/:runId` route: one Run's detail.
 *
 * Wraps the existing `RunDetailView` (sticky header, decision bar for an
 * awaiting-review Run, and the four tabs) as a full page with a back link to
 * the Project's runs. The active tab lives in the URL as `?tab=`, so deep
 * links and refreshes open on the right tab; an unknown or missing value falls
 * back to the default (diff). Switching tabs replaces the history entry rather
 * than stacking one per tab.
 */

import { Link, useParams, useSearchParams } from 'react-router-dom';

import RunDetailView, { DEFAULT_TAB_ID, TABS } from './RunDetailView.jsx';
import { useAuth } from './auth.jsx';

const TAB_IDS = TABS.map((tab) => tab.id);

export default function RunDetailPage() {
  const { projectId, runId } = useParams();
  const { client } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();

  const requested = searchParams.get('tab');
  const activeTab = TAB_IDS.includes(requested) ? requested : DEFAULT_TAB_ID;

  function onTabChange(id) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('tab', id);
        return next;
      },
      { replace: true },
    );
  }

  return (
    <div className="space-y-4">
      <Link
        to={`/dashboard/projects/${encodeURIComponent(projectId)}`}
        className="inline-flex items-center gap-1 text-sm font-medium text-accent-text hover:underline"
      >
        <span aria-hidden="true">←</span> Back to runs
      </Link>
      <RunDetailView client={client} runId={runId} tab={activeTab} onTabChange={onTabChange} />
    </div>
  );
}
