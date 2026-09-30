// Component tests for the model-routing screen — loading/error/success
// states, Viewer-role write-form hiding, and form validation (empty model
// name disables submit), per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelRoutingView } from "../api/types";
import { ModelRoutingScreen } from "./ModelRouting";

const getModelRouting = vi.fn();
const setModelRouting = vi.fn();

vi.mock("../api/endpoints", () => ({
  getModelRouting: (...args: unknown[]) => getModelRouting(...args),
  setModelRouting: (...args: unknown[]) => setModelRouting(...args),
}));

let mockIsAdmin = true;
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: mockIsAdmin, handleSessionExpired: vi.fn() }),
}));

const view: ModelRoutingView = {
  org_id: null,
  stages: [
    { stage: "intent_summary", model_name: "local-model", source: "static_default" },
    { stage: "review_generation", model_name: "local-model", source: "static_default" },
    { stage: "verification", model_name: "local-model", source: "static_default" },
  ],
};

describe("ModelRoutingScreen", () => {
  beforeEach(() => {
    getModelRouting.mockReset();
    setModelRouting.mockReset();
    mockIsAdmin = true;
  });

  it("shows a loading state while routing is being fetched", () => {
    getModelRouting.mockReturnValue(new Promise(() => {}));
    render(<ModelRoutingScreen />);
    expect(screen.getByText(/loading model routing/i)).toBeInTheDocument();
  });

  it("shows an error state on failure", async () => {
    const { ApiError } = await import("../api/client");
    getModelRouting.mockRejectedValue(new ApiError(500, "routing unavailable"));
    render(<ModelRoutingScreen />);
    expect(await screen.findByText("routing unavailable")).toBeInTheDocument();
  });

  it("shows populated stage data on success", async () => {
    getModelRouting.mockResolvedValue(view);
    render(<ModelRoutingScreen />);
    expect(await screen.findByText("review_generation")).toBeInTheDocument();
    expect(screen.getAllByText("local-model").length).toBeGreaterThan(0);
    expect(screen.getAllByText("static_default").length).toBeGreaterThan(0);
  });

  it("Admin: disables Save until a model name is entered, then shows success on save", async () => {
    getModelRouting.mockResolvedValue(view);
    setModelRouting.mockResolvedValue({});
    render(<ModelRoutingScreen />);
    await screen.findByText("review_generation");

    const saveButton = screen.getByRole("button", { name: /save routing/i });
    expect(saveButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/model name/i), { target: { value: "gpt-new" } });
    expect(saveButton).not.toBeDisabled();

    fireEvent.click(saveButton);
    await waitFor(() => expect(setModelRouting).toHaveBeenCalledWith("intent_summary", "gpt-new", null));
    expect(await screen.findByTestId("notice")).toHaveTextContent(/updated intent_summary routing/i);
  });

  it("Viewer: sees the table but no write form", async () => {
    mockIsAdmin = false;
    getModelRouting.mockResolvedValue(view);
    render(<ModelRoutingScreen />);
    await screen.findByText("review_generation");
    expect(screen.queryByRole("button", { name: /save routing/i })).not.toBeInTheDocument();
  });
});
