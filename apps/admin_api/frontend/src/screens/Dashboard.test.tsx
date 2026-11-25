// Component tests for the Dashboard screen — loading/error/empty/success
// states for its stats section, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section.
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import type { AdminStatsView } from "../api/types";
import { DashboardScreen } from "./Dashboard";

const getStats = vi.fn();

vi.mock("../api/endpoints", () => ({
  getStats: () => getStats(),
}));

vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ handleSessionExpired: vi.fn() }),
}));

const emptyStats: AdminStatsView = {
  queue_depth: { depth: 0, window: "now" },
  llm_token_usage: { entries: [], window: "24h" },
  verification_funnel: { judged: 0, rejected: 0, unmatched: 0, window: "24h" },
  findings_by_severity: { entries: [], window: "24h" },
};

describe("DashboardScreen", () => {
  beforeEach(() => {
    getStats.mockReset();
  });

  it("shows a loading state while the request is in flight", () => {
    getStats.mockReturnValue(new Promise(() => {}));
    render(<DashboardScreen />);

    expect(screen.getByText(/loading pipeline stats/i)).toBeInTheDocument();
  });

  it("shows an empty state when there is no findings data yet", async () => {
    getStats.mockResolvedValue(emptyStats);
    render(<DashboardScreen />);

    expect(await screen.findByText(/no llm token usage recorded yet/i)).toBeInTheDocument();
    expect(screen.getByText(/no findings recorded yet/i)).toBeInTheDocument();
  });

  it("shows an error state when the request fails", async () => {
    const { ApiError } = await import("../api/client");
    getStats.mockRejectedValue(new ApiError(502, "prometheus unreachable"));
    render(<DashboardScreen />);

    expect(await screen.findByText(/prometheus unreachable/i)).toBeInTheDocument();
  });

  it("renders populated data on success", async () => {
    getStats.mockResolvedValue({
      ...emptyStats,
      queue_depth: { depth: 7, window: "now" },
      llm_token_usage: {
        entries: [{ stage: "review_generation", model: "gpt-x", token_type: "output", tokens: 123 }],
        window: "24h",
      },
    });
    render(<DashboardScreen />);

    expect(await screen.findByText("7")).toBeInTheDocument();
    expect(screen.getByText("gpt-x")).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    getStats.mockResolvedValue(emptyStats);
    const { container } = render(<DashboardScreen />);
    await screen.findByText(/no llm token usage recorded yet/i);
    expect(await axe(container)).toHaveNoViolations();
  });
});
