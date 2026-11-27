// Shared loading/empty/error building blocks every screen uses, so no screen
// silently shows nothing while a request is in flight or has failed.
// `role`/`aria-live` on each keeps a screen-reader user informed of the
// same state a sighted user sees, per the PRD's WCAG 2.1 AA baseline.

export function LoadingState({ label = "Loading…" }: { label?: string }): JSX.Element {
  return (
    <div className="state-view state-view--loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({
  title,
  description,
}: {
  title: string;
  description?: string;
}): JSX.Element {
  return (
    <div className="state-view state-view--empty" role="status">
      <p className="state-view-title">{title}</p>
      {description && <p className="state-view-description">{description}</p>}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  title = "Couldn\u2019t load this screen",
}: {
  message: string;
  onRetry?: () => void;
  title?: string;
}): JSX.Element {
  return (
    <div className="state-view state-view--error" role="alert">
      <p className="state-view-title">{title}</p>
      <p className="state-view-description">{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
