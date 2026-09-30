// A mutating action's explicit success/error confirmation — distinct from
// the screen simply re-rendering, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section ("the admin never
// has to guess whether their action took effect"). `role="status"` for
// success (polite, non-interrupting) vs. `role="alert"` for error
// (assertive — a screen-reader user should hear about a failed mutation
// immediately), matching StateViews.tsx's own role convention.
export interface NoticeValue {
  kind: "success" | "error";
  message: string;
}

export function Notice({
  notice,
  onDismiss,
}: {
  notice: NoticeValue | null;
  onDismiss: () => void;
}): JSX.Element | null {
  if (notice === null) return null;

  return (
    <div
      className={`notice notice--${notice.kind}`}
      role={notice.kind === "error" ? "alert" : "status"}
      data-testid="notice"
    >
      <span>{notice.message}</span>
      <button type="button" onClick={onDismiss} aria-label="Dismiss notification">
        ×
      </button>
    </div>
  );
}
