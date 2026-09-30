import { useCallback, useState } from "react";

import * as api from "../api/endpoints";
import { ApiError } from "../api/client";
import { Notice } from "../components/Notice";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { useAsyncData } from "../hooks/useAsyncData";
import { useNotice } from "../hooks/useNotice";
import { useAuth } from "../auth/AuthContext";
import type { MemoryEntry, MemoryStatus } from "../api/types";

const STATUS_FILTERS: { label: string; value: MemoryStatus | "" }[] = [
  { label: "All", value: "" },
  { label: "Pending", value: "pending" },
  { label: "Approved", value: "approved" },
  { label: "Rejected", value: "rejected" },
];

const ENTRY_TYPES = [
  { label: "Dismissed finding", value: "dismissed_finding" },
  { label: "Accepted fix", value: "accepted_fix" },
  { label: "Correction", value: "correction" },
] as const;

// Unit D (US-A7-A11): the Memory review queue — list (Viewer parity) plus
// approve/reject/delete (Admin-only, per docs/plans/plan-deliver-admin-gui.md's
// Phase 3 section). Every mutating control is hidden entirely for a
// Viewer session (`isAdmin` gate below), not merely disabled — the same
// "never clickable-but-failing" rule the PRD's Permission-denied UX
// section states.
export function MemoryQueueScreen(): JSX.Element {
  const { isAdmin } = useAuth();
  const [statusFilter, setStatusFilter] = useState<MemoryStatus | "">("");
  const [pendingEntryType, setPendingEntryType] = useState<Record<string, string>>({});
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [actionInFlightId, setActionInFlightId] = useState<string | null>(null);
  const { notice, showSuccess, showError, dismiss } = useNotice();

  const fetcher = useCallback(
    () => api.listMemoryEntries({ status: statusFilter || undefined, limit: 50 }),
    [statusFilter]
  );
  const { state, reload } = useAsyncData(fetcher, [statusFilter]);

  async function runAction(entryId: string, action: () => Promise<MemoryEntry | unknown>, successMessage: string) {
    setActionInFlightId(entryId);
    try {
      await action();
      showSuccess(successMessage);
      reload();
    } catch (error) {
      showError(error instanceof ApiError ? error.detail : "action failed");
    } finally {
      setActionInFlightId(null);
      setConfirmingDeleteId(null);
    }
  }

  return (
    <div className="screen">
      <h1>Memory review queue</h1>
      <Notice notice={notice} onDismiss={dismiss} />

      <div className="filter-bar">
        <label htmlFor="status-filter">Status</label>
        <select
          id="status-filter"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as MemoryStatus | "")}
        >
          {STATUS_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      {state.status === "loading" && <LoadingState label="Loading Memory queue…" />}
      {state.status === "error" && <ErrorState message={state.message} onRetry={reload} />}
      {state.status === "success" &&
        (state.data.entries.length === 0 ? (
          <EmptyState
            title="Nothing pending."
            description="No Memory entries match the current filter."
          />
        ) : (
          <table>
            <caption className="sr-only">Memory entries pending or already reviewed</caption>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Type</th>
                <th scope="col">Repo</th>
                <th scope="col">Created</th>
                {isAdmin && <th scope="col">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {state.data.entries.map((entry) => {
                const busy = actionInFlightId === entry.id;
                return (
                  <tr key={entry.id}>
                    <td>{entry.status}</td>
                    <td>{entry.entry_type ?? "—"}</td>
                    <td>{entry.repo_id}</td>
                    <td>{new Date(entry.created_at).toLocaleString()}</td>
                    {isAdmin && (
                      <td>
                        {entry.status === "pending" && (
                          <div className="row-actions">
                            <label className="sr-only" htmlFor={`entry-type-${entry.id}`}>
                              Classification for entry {entry.id}
                            </label>
                            <select
                              id={`entry-type-${entry.id}`}
                              value={pendingEntryType[entry.id] ?? ENTRY_TYPES[0].value}
                              onChange={(event) =>
                                setPendingEntryType((prev) => ({
                                  ...prev,
                                  [entry.id]: event.target.value,
                                }))
                              }
                              disabled={busy}
                            >
                              {ENTRY_TYPES.map((option) => (
                                <option key={option.value} value={option.value}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                void runAction(
                                  entry.id,
                                  () =>
                                    api.approveMemoryEntry(
                                      entry.id,
                                      (pendingEntryType[entry.id] ?? ENTRY_TYPES[0].value) as
                                        | "dismissed_finding"
                                        | "accepted_fix"
                                        | "correction"
                                    ),
                                  "Entry approved."
                                )
                              }
                            >
                              Approve
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                void runAction(
                                  entry.id,
                                  () => api.rejectMemoryEntry(entry.id),
                                  "Entry rejected."
                                )
                              }
                            >
                              Reject
                            </button>
                          </div>
                        )}
                        {confirmingDeleteId === entry.id ? (
                          <span className="row-actions">
                            <span>Delete this entry?</span>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                void runAction(
                                  entry.id,
                                  () => api.deleteMemoryEntry(entry.id),
                                  "Entry deleted."
                                )
                              }
                            >
                              Confirm delete
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => setConfirmingDeleteId(null)}
                            >
                              Cancel
                            </button>
                          </span>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => setConfirmingDeleteId(entry.id)}
                          >
                            Delete
                          </button>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        ))}
    </div>
  );
}
