/**
 * Create a Project from the dashboard, so the demo needs no `curl`.
 *
 * Rendered inside `ProjectListView` when the Reviewer opens the "New project"
 * form. It owns only the form: the two fields, their validation, and the one
 * `POST /api/projects/`. What happens after a successful create — closing the
 * form, reloading the list, selecting the new Project — belongs to the list
 * view, so this reports success through `onCreated(project)` and lets the view
 * decide. `onCancel` closes the form without creating anything.
 *
 * ### Validation is deliberately thin
 *
 * Name and Repository URL are both required and trimmed, and that is the whole
 * of the client-side check. The URL field is a plain text input, not
 * `type="url"`, because a remote is written either as `https://…` or as the
 * scp-style `git@host:org/repo.git`, and the latter is not a URL the browser
 * would accept — rejecting it here would refuse a perfectly valid remote. The
 * backend is the authority on everything else, including whether a name is
 * taken: duplicate names are allowed, so there is no client-side uniqueness
 * check (the backend decides, and a 400 is shown if it refuses).
 *
 * ### Errors come back next to the field
 *
 * A 400 from DRF carries `{field: ["message", …]}` in `ApiError.payload`, so a
 * `name` or `repo_url` error is shown under that input; any other key (a
 * `detail`, a `non_field_errors`) and any non-400 failure become one
 * form-level message. The form-level message falls back to `ApiError.message`,
 * which the client already built from the response.
 */

import { useEffect, useState } from 'react';

import api from './api.js';

/**
 * @param {{ client?: typeof api,
 *           onCreated?: (project: object) => void,
 *           onCancel?: () => void,
 *           onPendingChange?: (pending: boolean) => void }} props
 *   `onPendingChange` lets a container (e.g. the New project modal) learn when
 *   a create is in flight, so it can refuse to close mid-request.
 */
export default function NewProjectForm({ client = api, onCreated, onCancel, onPendingChange }) {
  const [name, setName] = useState('');
  const [repoUrl, setRepoUrl] = useState('');
  const [pending, setPending] = useState(false);
  // Keyed by the backend's field names (`name`, `repo_url`), so a 400 maps
  // straight onto the inputs.
  const [fieldErrors, setFieldErrors] = useState({});
  const [formError, setFormError] = useState(null);

  // Report the in-flight state up so a container can block its own close.
  useEffect(() => {
    if (typeof onPendingChange === 'function') onPendingChange(pending);
  }, [pending, onPendingChange]);

  async function handleSubmit(event) {
    event.preventDefault();
    // One create at a time: a second submit while the first is in flight is
    // ignored, so a double click cannot create two Projects.
    if (pending) return;

    const trimmedName = name.trim();
    const trimmedRepoUrl = repoUrl.trim();
    const required = {};
    if (trimmedName === '') required.name = 'Name is required.';
    if (trimmedRepoUrl === '') required.repo_url = 'Repository URL is required.';
    if (Object.keys(required).length > 0) {
      setFieldErrors(required);
      setFormError(null);
      return;
    }

    setPending(true);
    setFieldErrors({});
    setFormError(null);
    try {
      const created = await client.createProject({ name: trimmedName, repo_url: trimmedRepoUrl });
      if (typeof onCreated === 'function') onCreated(created);
    } catch (error) {
      applyError(error);
    } finally {
      setPending(false);
    }
  }

  function applyError(error) {
    const payload = error?.payload;
    if (
      error?.status === 400 &&
      payload &&
      typeof payload === 'object' &&
      !Array.isArray(payload)
    ) {
      const next = {};
      const other = [];
      for (const [field, messages] of Object.entries(payload)) {
        const text = [].concat(messages).join(', ');
        if (field === 'name' || field === 'repo_url') next[field] = text;
        else other.push(`${field}: ${text}`);
      }
      setFieldErrors(next);
      setFormError(other.length > 0 ? other.join('; ') : null);
      return;
    }
    // Non-field failures (a transport error, a 500, an unexpected shape) keep
    // the client's own one-line message.
    setFieldErrors({});
    setFormError(error?.message || 'Could not create the project.');
  }

  const inputClass =
    'mt-1.5 w-full rounded-control border border-border-strong bg-surface px-3.5 py-2.5 text-sm text-fg placeholder:text-fg-subtle focus:border-ring';

  return (
    <form
      onSubmit={handleSubmit}
      aria-busy={pending}
      aria-label="New project"
      className="space-y-5"
    >
      <div>
        <label htmlFor="new-project-name" className="block text-sm font-medium text-fg">
          Name
        </label>
        <input
          id="new-project-name"
          name="name"
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          disabled={pending}
          aria-invalid={Boolean(fieldErrors.name)}
          aria-describedby={fieldErrors.name ? 'new-project-name-error' : undefined}
          className={inputClass}
        />
        {fieldErrors.name && (
          <p id="new-project-name-error" className="mt-1 text-xs text-danger-fg">
            {fieldErrors.name}
          </p>
        )}
      </div>

      <div>
        <label htmlFor="new-project-repo-url" className="block text-sm font-medium text-fg">
          Repository URL
        </label>
        <input
          id="new-project-repo-url"
          name="repo_url"
          type="text"
          value={repoUrl}
          onChange={(event) => setRepoUrl(event.target.value)}
          disabled={pending}
          placeholder="https://… or git@…"
          aria-invalid={Boolean(fieldErrors.repo_url)}
          aria-describedby={fieldErrors.repo_url ? 'new-project-repo-url-error' : undefined}
          className={`${inputClass} font-mono text-[13px]`}
        />
        {fieldErrors.repo_url && (
          <p id="new-project-repo-url-error" className="mt-1 text-xs text-danger-fg">
            {fieldErrors.repo_url}
          </p>
        )}
      </div>

      {formError !== null && (
        <p
          role="alert"
          className="rounded-card border border-danger/30 bg-danger-soft p-2 text-xs text-danger-fg"
        >
          {formError}
        </p>
      )}

      <div className="flex flex-wrap justify-end gap-3 pt-1">
        <button
          type="submit"
          disabled={pending}
          className="rounded-pill bg-accent px-4 py-2 text-sm font-semibold text-accent-fg shadow-soft transition-colors hover:bg-accent-hover hover:text-accent-hover-fg disabled:opacity-60"
        >
          {pending ? 'Creating…' : 'Create project'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={pending}
          className="rounded-pill border border-border-strong bg-surface px-4 py-2 text-sm font-medium text-fg transition-colors hover:bg-surface-2 disabled:opacity-60"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
