// Component tests for the Users screen — loading/empty/error/success
// states, Viewer-role hiding (the whole screen, not just the write
// controls — every route in apps/admin_api/user_routes.py, including the
// list read, is Admin-only), and create-user form validation, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AdminUser } from "../api/types";
import { UsersScreen } from "./Users";

const listUsers = vi.fn();
const createUser = vi.fn();
const deactivateUser = vi.fn();

vi.mock("../api/endpoints", () => ({
  listUsers: (...args: unknown[]) => listUsers(...args),
  createUser: (...args: unknown[]) => createUser(...args),
  deactivateUser: (...args: unknown[]) => deactivateUser(...args),
}));

let mockIsAdmin = true;
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: mockIsAdmin, handleSessionExpired: vi.fn() }),
}));

const activeUser: AdminUser = {
  id: "user-1",
  username: "alice",
  role: "viewer",
  is_active: true,
  created_at: "2026-09-30T00:00:00Z",
  updated_at: "2026-09-30T00:00:00Z",
};

describe("UsersScreen", () => {
  beforeEach(() => {
    listUsers.mockReset();
    createUser.mockReset();
    deactivateUser.mockReset();
    mockIsAdmin = true;
  });

  it("Viewer: sees a not-available message and never calls the list API", async () => {
    mockIsAdmin = false;
    render(<UsersScreen />);
    expect(screen.getByText(/not available for your role/i)).toBeInTheDocument();
    expect(listUsers).not.toHaveBeenCalled();
    // The guarded fetcher still resolves its `Promise.resolve([])` short
    // circuit on the next microtask — await it so that resolution (and its
    // state update) happens inside this test's act() scope rather than
    // logging an act() warning after the test body returns.
    await waitFor(() => expect(listUsers).not.toHaveBeenCalled());
  });

  it("Admin: shows a loading state while users are being fetched", () => {
    listUsers.mockReturnValue(new Promise(() => {}));
    render(<UsersScreen />);
    expect(screen.getByText(/loading users/i)).toBeInTheDocument();
  });

  it("Admin: shows an empty state with no users", async () => {
    listUsers.mockResolvedValue([]);
    render(<UsersScreen />);
    expect(await screen.findByText(/no users yet/i)).toBeInTheDocument();
  });

  it("Admin: shows an error state on failure", async () => {
    const { ApiError } = await import("../api/client");
    listUsers.mockRejectedValue(new ApiError(500, "db down"));
    render(<UsersScreen />);
    expect(await screen.findByText("db down")).toBeInTheDocument();
  });

  it("Admin: create-user form declares the server's 12-character minimum client-side", async () => {
    // jsdom doesn't implement the `minlength` constraint-validation
    // algorithm (verified directly: `input.checkValidity()` stays `true`
    // for an under-length value even with the attribute present), so this
    // asserts the form declares the same minimum
    // `AdminAPISettings.min_password_length` (12, per Assumption 14)
    // enforces server-side — the attribute a real browser actually
    // enforces on submit — rather than asserting jsdom's unsupported
    // constraint-validation behavior.
    listUsers.mockResolvedValue([]);
    render(<UsersScreen />);
    await screen.findByText(/no users yet/i);

    const passwordInput = screen.getByLabelText(/initial password/i);
    expect(passwordInput).toBeRequired();
    expect(passwordInput).toHaveAttribute("minlength", "12");
    expect(passwordInput).toHaveAttribute("type", "password");
  });

  it("Admin: creating a user shows a distinct success confirmation and reloads the list", async () => {
    listUsers.mockResolvedValue([]);
    createUser.mockResolvedValue({ ...activeUser, username: "bob" });
    render(<UsersScreen />);
    await screen.findByText(/no users yet/i);

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText(/initial password/i), {
      target: { value: "a-very-long-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create user$/i }));

    await waitFor(() => expect(createUser).toHaveBeenCalledWith("bob", "a-very-long-password", "viewer"));
    expect(await screen.findByTestId("notice")).toHaveTextContent(/user bob created/i);
  });

  it("Admin: deactivating a user calls the API and shows a success confirmation", async () => {
    listUsers.mockResolvedValue([activeUser]);
    deactivateUser.mockResolvedValue({ ...activeUser, is_active: false });
    render(<UsersScreen />);

    await screen.findByText("alice");
    fireEvent.click(screen.getByRole("button", { name: /deactivate/i }));

    await waitFor(() => expect(deactivateUser).toHaveBeenCalledWith("user-1"));
    expect(await screen.findByTestId("notice")).toHaveTextContent(/user alice deactivated/i);
  });
});
