/**
 * A generic accessible modal dialog.
 *
 * Behaviours (shared by every modal in the dashboard):
 *  - `role="dialog"` + `aria-modal="true"`, labelled by its own title.
 *  - Escape closes it, and clicking the backdrop closes it — both suppressed
 *    while `closeDisabled` (a request is in flight), so an in-progress action
 *    cannot be interrupted into an inconsistent state.
 *  - Focus moves into the panel on open (first focusable, else the panel) and
 *    returns to the opener (`returnFocusRef`) on close.
 *  - Tab is trapped within the panel.
 *  - Body scroll is locked while open.
 *
 * The caller renders the body as children and owns the content and the request
 * state; this component owns only the dialog mechanics.
 *
 * @param {{ title: string, onClose: () => void, closeDisabled?: boolean,
 *           returnFocusRef?: { current: HTMLElement|null },
 *           children: React.ReactNode }} props
 */

import { useEffect, useId, useRef } from 'react';

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export default function Modal({ title, onClose, closeDisabled = false, returnFocusRef, children }) {
  const panelRef = useRef(null);
  const titleId = useId();

  // Move focus in on open; return it to the opener on close.
  useEffect(() => {
    const trigger = returnFocusRef?.current ?? null;
    const focusables = panelRef.current?.querySelectorAll(FOCUSABLE);
    const target = focusables && focusables.length > 0 ? focusables[0] : panelRef.current;
    target?.focus?.();
    return () => trigger?.focus?.();
  }, [returnFocusRef]);

  // Lock body scroll while the modal is open.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  function requestClose() {
    if (!closeDisabled) onClose();
  }

  function handleKeyDown(event) {
    if (event.key === 'Escape') {
      requestClose();
      return;
    }
    if (event.key !== 'Tab') return;

    const focusables = panelRef.current?.querySelectorAll(FOCUSABLE);
    if (!focusables || focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close dialog"
        tabIndex={-1}
        onClick={requestClose}
        className="absolute inset-0 bg-scrim backdrop-blur-[2px]"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className="relative z-10 max-h-[calc(100vh-2rem)] w-full max-w-md overflow-y-auto rounded-card-xl border border-border bg-surface p-7 shadow-lift"
      >
        <h2 id={titleId} className="font-display text-[1.75rem] leading-tight font-normal text-fg">
          {title}
        </h2>
        <div className="mt-5">{children}</div>
      </div>
    </div>
  );
}
