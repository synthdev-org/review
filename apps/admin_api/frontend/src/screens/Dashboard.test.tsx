// Component tests for the Dashboard screen — loading/error/empty/success
// states for both its stats and usage halves, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section.
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import type { AdminStatsView, UsageView } from "../api/types";
import { DashboardScreen } from "./Dashboard";

const getStats = vi.fn();
const getUsage = vi.fn();

vi.mock("../api/endpoints", () => ({
  getStats: () => getStats(),
  getUsage: () => getUsage(),
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

const emptyUsage: UsageView = { threshold: 20, orgs: [] };

describe("DashboardScreen", () => {
  beforeEach(() => {
    getStats.mockReset();
    getUsage.mockReset();
  });

  it("shows loading states for both sections while requests are in flight", () => {
    getStats.mockReturnValue(new Promise(() => {}));
    getUsage.mockReturnValue(new Promise(() => {}));
    render(<DashboardScreen />);

    expect(screen.getByText(/loading pipeline stats/i)).toBeInTheDocument();
    expect(screen.getByText(/loading usage/i)).toBeInTheDocument();
  });

  it("shows an empty state when there is no usage/findings data yet", async () => {
    getStats.mockResolvedValue(emptyStats);
    getUsage.mockResolvedValue(emptyUsage);
    render(<DashboardScreen />);

    expect(await screen.findByText(/no reviews counted yet today/i)).toBeInTheDocument();
    expect(screen.getByText(/no llm token usage recorded yet/i)).toBeInTheDocument();
    expect(screen.getByText(/no findings recorded yet/i)).toBeInTheDocument();
  });

  it("shows an error state, independently per section, when a request fails", async () => {
    const { ApiError } = await import("../api/client");
    getStats.mockRejectedValue(new ApiError(502, "prometheus unreachable"));
    getUsage.mockResolvedValue(emptyUsage);
    render(<DashboardScreen />);

    expect(await screen.findByText(/prometheus unreachable/i)).toBeInTheDocument();
    // The usage section still renders successfully despite the stats failure.
    expect(await screen.findByText(/no reviews counted yet today/i)).toBeInTheDocument();
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
    getUsage.mockResolvedValue({
      threshold: 20,
      orgs: [
        {
          installation_id: 1,
          org_id: "org-1",
          org_name: "Acme",
          day: "2026-09-30",
          count: 25,
          over_threshold: true,
        },
      ],
    });
    render(<DashboardScreen />);

    expect(await screen.findByText("7")).toBeInTheDocument();
    expect(screen.getByText("gpt-x")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText(/over guideline/i)).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    getStats.mockResolvedValue(emptyStats);
    getUsage.mockResolvedValue(emptyUsage);
    const { container } = render(<DashboardScreen />);
    await screen.findByText(/no reviews counted yet today/i);
    expect(await axe(container)).toHaveNoViolations();
  });
});
