/**
 * System / Light / Dark segmented control.
 *
 * A labelled group of three toggle buttons (`aria-pressed`), so it is one Tab
 * stop per option and operable with Enter/Space like any button. The choice is
 * persisted and applied through `src/theme.js`; the inline script in
 * `index.html` applies it again before first paint on the next load.
 */

import { useEffect, useState } from 'react';

import { THEME_OPTIONS, applyTheme, readStoredTheme, storeTheme } from './theme.js';

const LABELS = { system: 'System', light: 'Light', dark: 'Dark' };

function Icon({ name }) {
  const common = {
    viewBox: '0 0 20 20',
    'aria-hidden': true,
    focusable: false,
    className: 'h-4 w-4',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.6,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
  };
  if (name === 'light') {
    return (
      <svg {...common}>
        <circle cx="10" cy="10" r="3.4" />
        <path d="M10 2.5v1.8M10 15.7v1.8M2.5 10h1.8M15.7 10h1.8M4.7 4.7l1.3 1.3M14 14l1.3 1.3M4.7 15.3 6 14M14 6l1.3-1.3" />
      </svg>
    );
  }
  if (name === 'dark') {
    return (
      <svg {...common}>
        <path d="M15.8 12.4A6.5 6.5 0 0 1 7.6 4.2a6.5 6.5 0 1 0 8.2 8.2Z" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <rect x="3" y="4" width="14" height="9.5" rx="1.8" />
      <path d="M7.5 16.5h5" />
    </svg>
  );
}

/**
 * @param {{ className?: string }} props
 */
export default function ThemeSwitch({ className = '' }) {
  const [preference, setPreference] = useState(readStoredTheme);

  useEffect(() => {
    applyTheme(preference);
  }, [preference]);

  function choose(next) {
    storeTheme(next);
    setPreference(next);
  }

  return (
    <div
      role="group"
      aria-label="Color theme"
      className={`inline-flex items-center gap-0.5 rounded-pill border border-border bg-surface p-0.5 ${className}`}
    >
      {THEME_OPTIONS.map((option) => {
        const active = preference === option;
        return (
          <button
            key={option}
            type="button"
            aria-pressed={active}
            aria-label={LABELS[option]}
            title={`${LABELS[option]} theme`}
            onClick={() => choose(option)}
            className={`inline-flex h-7 w-7 items-center justify-center rounded-pill transition-colors ${
              active ? 'bg-accent text-accent-fg' : 'text-fg-muted hover:text-fg'
            }`}
          >
            <Icon name={option} />
          </button>
        );
      })}
    </div>
  );
}
