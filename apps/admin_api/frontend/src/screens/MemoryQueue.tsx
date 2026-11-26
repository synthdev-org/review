import { useCallback, useEffect, useState } from "react";

import * as api from "../api/endpoints";
import { ApiError, isSessionExpiredError } from "../api/client";
import { Notice } from "../components/Notice";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { useAsyncData } from "../hooks/useAsyncData";
import { useNotice } from "../hooks/useNotice";
import { useAuth } from "../auth/AuthContext";
import type { BulkOperationResponse, MemoryEntry, MemoryStatus } from "../api/types";

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

// US-A7-A11: the Memory review queue — list (Viewer parity) plus
// approve/reject/delete (Admin-only). Every mutating control is hidden entirely for a
// Viewer session (`isAdmin` gate below), not merely disabled — the same
// "never clickable-but-failing" rule the PRD's Permission-denied UX
// section states.
export function MemoryQueueScreen(): JSX.Element {
  const { isAdmin, handleSessionExpired } = useAuth();
  const [statusFilter, setStatusFilter] = useState<MemoryStatus | "">("");
  const [pendingEntryType, setPendingEntryType] = useState<Record<string, string>>({});
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [actionInFlightId, setActionInFlightId] = useState<string | null>(null);
  // US-A23: bulk-select, scoped to pending entries only — the same scope
  // the single-row Approve/Reject controls below already apply (an
  // approved/rejected entry has neither control rendered for it either).
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkEntryType, setBulkEntryType] = useState<string>(ENTRY_TYPES[0].value);
  const [bulkInFlight, setBulkInFlight] = useState(false);
  const { notice, showSuccess, showError, dismiss } = useNotice();

  const fetcher = useCallback(
    () => api.listMemoryEntries({ status: statusFilter || undefined, limit: 50 }),
    [statusFilter]
  );
  const { state, reload } = useAsyncData(fetcher, [statusFilter]);

  // A stale selected id (no longer pending, or filtered out) would silently
  // apply a bulk action to the wrong row count — clearing on every filter
  // change is simpler and safer than reconciling the selection against each
  // new page of results.
  useEffect(() => {
    setSelectedIds(new Set());
  }, [statusFilter]);

  const pendingEntryIds =
    state.status === "success"
      ? state.data.entries.filter((entry) => entry.status === "pending").map((entry) => entry.id)
      : [];
  const allPendingSelected =
    pendingEntryIds.length > 0 && pendingEntryIds.every((id) => selectedIds.has(id));

  function toggleSelected(entryId: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(entryId)) {
        next.delete(entryId);
      } else {
        next.add(entryId);
      }
      return next;
    });
  }

  function toggleSelectAllPending() {
    setSelectedIds(allPendingSelected ? new Set() : new Set(pendingEntryIds));
  }

  async function runAction(entryId: string, action: () => Promise<MemoryEntry | unknown>, successMessage: string) {
    setActionInFlightId(entryId);
    try {
      await action();
      showSuccess(successMessage);
      reload();
    } catch (error) {
      // A 401 mid-action (session expired/revoked while this request was in
      // flight) routes back to login, per the PRD's "session expiring
      // mid-action returns the user to login" edge case — the same handling
      // useAsyncData already gives every GET fetch.
      if (isSessionExpiredError(error)) {
        handleSessionExpired();
        return;
      }
      showError(error instanceof ApiError ? error.detail : "action failed");
    } finally {
      setActionInFlightId(null);
      setConfirmingDeleteId(null);
    }
  }

  // US-A23: run a bulk-approve/bulk-reject call, surfacing each selected
  // id's own outcome rather than a single pass/fail flag — a partial
  // failure (e.g. one already-deleted id) is reported, never swallowed,
  // per the PRD's "the admin never has to guess whether their action took
  // effect" rule applied to the whole batch.
  async function runBulkAction(
    action: () => Promise<BulkOperationResponse>,
    pastTenseLabel: string
  ) {
    setBulkInFlight(true);
    try {
      const response = await action();
      const failed = response.results.filter((result) => !result.success).length;
      const succeeded = response.results.length - failed;
      if (failed === 0) {
        showSuccess(`${succeeded} ${pastTenseLabel}.`);
      } else if (succeeded === 0) {
        showError(`${failed} failed — none ${pastTenseLabel}.`);
      } else {
        showError(`${succeeded} ${pastTenseLabel}, ${failed} failed.`);
      }
      setSelectedIds(new Set());
      reload();
    } catch (error) {
      if (isSessionExpiredError(error)) {
        handleSessionExpired();
        return;
      }
      showError(error instanceof ApiError ? error.detail : "bulk action failed");
    } finally {
      setBulkInFlight(false);
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

      {isAdmin && selectedIds.size > 0 && (
        <div className="bulk-action-bar" role="region" aria-label="Bulk actions">
          <span>{selectedIds.size} selected</span>
          <label className="sr-only" htmlFor="bulk-entry-type">
            Classification for selected entries
          </label>
          <select
            id="bulk-entry-type"
            value={bulkEntryType}
            onChange={(event) => setBulkEntryType(event.target.value)}
            disabled={bulkInFlight}
          >
            {ENTRY_TYPES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={bulkInFlight}
            onClick={() =>
              void runBulkAction(
                () =>
                  api.bulkApproveMemoryEntries(
                    Array.from(selectedIds),
                    bulkEntryType as "dismissed_finding" | "accepted_fix" | "correction"
                  ),
                "approved"
              )
            }
          >
            Approve selected
          </button>
          <button
            type="button"
            disabled={bulkInFlight}
            onClick={() =>
              void runBulkAction(
                () => api.bulkRejectMemoryEntries(Array.from(selectedIds)),
                "rejected"
              )
            }
          >
            Reject selected
          </button>
          <button
            type="button"
            disabled={bulkInFlight}
            onClick={() => setSelectedIds(new Set())}
          >
            Clear selection
          </button>
        </div>
      )}

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
                {isAdmin && (
                  <th scope="col">
                    <input
                      type="checkbox"
                      aria-label="Select all pending entries"
                      checked={allPendingSelected}
                      onChange={toggleSelectAllPending}
                      disabled={pendingEntryIds.length === 0 || bulkInFlight}
                    />
                  </th>
                )}
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
                    {isAdmin && (
                      <td>
                        {entry.status === "pending" && (
                          <input
                            type="checkbox"
                            aria-label={`Select entry ${entry.id}`}
                            checked={selectedIds.has(entry.id)}
                            onChange={() => toggleSelected(entry.id)}
                            disabled={busy || bulkInFlight}
                          />
                        )}
                      </td>
                    )}
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
                              disabled={busy || bulkInFlight}
                            >
                              {ENTRY_TYPES.map((option) => (
                                <option key={option.value} value={option.value}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              disabled={busy || bulkInFlight}
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
                              disabled={busy || bulkInFlight}
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
                              disabled={busy || bulkInFlight}
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
                              disabled={busy || bulkInFlight}
                              onClick={() => setConfirmingDeleteId(null)}
                            >
                              Cancel
                            </button>
                          </span>
                        ) : (
                          <button
                            type="button"
                            disabled={busy || bulkInFlight}
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
