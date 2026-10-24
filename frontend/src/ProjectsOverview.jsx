/**
 * The `/dashboard` index route: the projects overview.
 *
 * Step 1 renders a minimal prompt so the dashboard index is a real page; step
 * 8 turns it into the project-card grid with a New project button and an empty
 * state. The Project list itself is already in the sidebar.
 */

export const OVERVIEW_PROMPT = 'Select a project from the sidebar to view its runs.';

export default function ProjectsOverview() {
  return (
    <section aria-labelledby="overview-heading">
      <h2 id="overview-heading" className="text-xl font-semibold tracking-tight text-fg">
        Projects
      </h2>
      <p className="mt-2 text-sm text-fg-muted">{OVERVIEW_PROMPT}</p>
    </section>
  );
}
