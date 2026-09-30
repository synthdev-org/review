// Component tests for the Memory review queue screen — loading/empty/
// error/success states plus Viewer-role control hiding (US-A7-A11), per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MemoryEntry } from "../api/types";
import { MemoryQueueScreen } from "./MemoryQueue";

const listMemoryEntries = vi.fn();
const approveMemoryEntry = vi.fn();
const rejectMemoryEntry = vi.fn();
const deleteMemoryEntry = vi.fn();

vi.mock("../api/endpoints", () => ({
  listMemoryEntries: (...args: unknown[]) => listMemoryEntries(...args),
  approveMemoryEntry: (...args: unknown[]) => approveMemoryEntry(...args),
  rejectMemoryEntry: (...args: unknown[]) => rejectMemoryEntry(...args),
  deleteMemoryEntry: (...args: unknown[]) => deleteMemoryEntry(...args),
}));

let mockIsAdmin = true;
const handleSessionExpired = vi.fn();
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: mockIsAdmin, handleSessionExpired: (...args: unknown[]) => handleSessionExpired(...args) }),
}));

const pendingEntry: MemoryEntry = {
  id: "entry-1",
  repo_id: "repo-1",
  org_id: "org-1",
  team_id: null,
  entry_type: null,
  status: "pending",
  raw_signal_type: "review_comment_reaction",
  raw_signal_detail: {},
  posted_finding_comment_id: 42,
  embedding: null,
  approved_at: null,
  approved_by: null,
  approved_by_user_id: null,
  created_at: "2026-09-30T00:00:00Z",
  updated_at: "2026-09-30T00:00:00Z",
};

describe("MemoryQueueScreen", () => {
  beforeEach(() => {
    listMemoryEntries.mockReset();
    approveMemoryEntry.mockReset();
    rejectMemoryEntry.mockReset();
    deleteMemoryEntry.mockReset();
    handleSessionExpired.mockReset();
    mockIsAdmin = true;
  });

  it("shows a loading state while the queue is being fetched", () => {
    listMemoryEntries.mockReturnValue(new Promise(() => {}));
    render(<MemoryQueueScreen />);
    expect(screen.getByText(/loading memory queue/i)).toBeInTheDocument();
  });

  it("shows an empty state when nothing is pending", async () => {
    listMemoryEntries.mockResolvedValue({ entries: [], total: 0 });
    render(<MemoryQueueScreen />);
    expect(await screen.findByText(/nothing pending/i)).toBeInTheDocument();
  });

  it("shows an error state with a retry control on failure", async () => {
    const { ApiError } = await import("../api/client");
    listMemoryEntries.mockRejectedValue(new ApiError(500, "boom"));
    render(<MemoryQueueScreen />);
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("Admin: shows approve/reject/delete controls and a distinct success confirmation on approve", async () => {
    listMemoryEntries.mockResolvedValue({ entries: [pendingEntry], total: 1 });
    approveMemoryEntry.mockResolvedValue({ ...pendingEntry, status: "approved" });
    render(<MemoryQueueScreen />);

    await screen.findByText("repo-1");
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));

    await waitFor(() => expect(approveMemoryEntry).toHaveBeenCalledWith("entry-1", "dismissed_finding"));
    expect(await screen.findByTestId("notice")).toHaveTextContent(/entry approved/i);
  });

  it("Admin: delete requires an explicit confirm step before calling the API", async () => {
    listMemoryEntries.mockResolvedValue({ entries: [pendingEntry], total: 1 });
    deleteMemoryEntry.mockResolvedValue({ deleted: true, entry_id: "entry-1" });
    render(<MemoryQueueScreen />);

    await screen.findByText("repo-1");
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    expect(deleteMemoryEntry).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /confirm delete/i }));
    await waitFor(() => expect(deleteMemoryEntry).toHaveBeenCalledWith("entry-1"));
  });

  it("Admin: a 401 mid-action (expired/revoked session) routes back to login instead of showing a generic error", async () => {
    const { ApiError } = await import("../api/client");
    listMemoryEntries.mockResolvedValue({ entries: [pendingEntry], total: 1 });
    approveMemoryEntry.mockRejectedValue(new ApiError(401, "session expired"));
    render(<MemoryQueueScreen />);

    await screen.findByText("repo-1");
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));

    await waitFor(() => expect(handleSessionExpired).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("notice")).not.toBeInTheDocument();
  });

  it("Viewer: hides every mutating control (not just disables it)", async () => {
    mockIsAdmin = false;
    listMemoryEntries.mockResolvedValue({ entries: [pendingEntry], total: 1 });
    render(<MemoryQueueScreen />);

    await screen.findByText("repo-1");
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reject/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /actions/i })).not.toBeInTheDocument();
  });
});
