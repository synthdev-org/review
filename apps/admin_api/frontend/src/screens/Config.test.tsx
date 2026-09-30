// Component tests for the org/repo/team configuration screen —
// loading/empty/error/success states, Viewer-role write-form hiding, and
// US-A13's scoped setting form, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ConfigTree } from "../api/types";
import { ConfigScreen } from "./Config";

const getConfig = vi.fn();
const setConfig = vi.fn();

vi.mock("../api/endpoints", () => ({
  getConfig: () => getConfig(),
  setConfig: (...args: unknown[]) => setConfig(...args),
}));

let mockIsAdmin = true;
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: mockIsAdmin, handleSessionExpired: vi.fn() }),
}));

const emptyTree: ConfigTree = { platform_settings: {}, orgs: [] };

describe("ConfigScreen", () => {
  beforeEach(() => {
    getConfig.mockReset();
    setConfig.mockReset();
    mockIsAdmin = true;
  });

  it("shows a loading state while the config tree is being fetched", () => {
    getConfig.mockReturnValue(new Promise(() => {}));
    render(<ConfigScreen />);
    expect(screen.getByText(/loading configuration/i)).toBeInTheDocument();
  });

  it("shows an empty state when no orgs are connected", async () => {
    getConfig.mockResolvedValue(emptyTree);
    render(<ConfigScreen />);
    expect(await screen.findByText(/no organizations connected yet/i)).toBeInTheDocument();
  });

  it("shows an error state on failure", async () => {
    const { ApiError } = await import("../api/client");
    getConfig.mockRejectedValue(new ApiError(500, "db unreachable"));
    render(<ConfigScreen />);
    expect(await screen.findByText("db unreachable")).toBeInTheDocument();
  });

  it("Admin: can save a setting and sees a distinct success confirmation", async () => {
    getConfig.mockResolvedValue(emptyTree);
    setConfig.mockResolvedValue({
      org_id: null,
      repo_id: null,
      team_id: null,
      key: "memory_suppression_enabled",
      value: { enabled: true },
    });
    render(<ConfigScreen />);

    await screen.findByText("Platform-wide");
    fireEvent.click(screen.getAllByRole("button", { name: /save setting/i })[0]);

    await waitFor(() =>
      expect(setConfig).toHaveBeenCalledWith("platform", "memory_suppression_enabled", {
        enabled: true,
      })
    );
    expect(await screen.findByTestId("notice")).toHaveTextContent(/saved memory_suppression_enabled/i);
  });

  it("Viewer: sees configuration read-only, no save control", async () => {
    mockIsAdmin = false;
    getConfig.mockResolvedValue(emptyTree);
    render(<ConfigScreen />);

    await screen.findByText("Platform-wide");
    expect(screen.queryByRole("button", { name: /save setting/i })).not.toBeInTheDocument();
    expect(screen.getByText(/viewers can see configuration but not change it/i)).toBeInTheDocument();
  });
});
