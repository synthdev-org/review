// Component tests for the classifier panel's disclosure gate and probe (plan Phase 8).
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import { ApiError } from "../api/client";
import type { ScopeSettings } from "../api/types";
import { ClassifierPanel } from "./ClassifierPanel";
import { CLASSIFIER_EGRESS_DISCLOSURE } from "./classifierCopy";

const setConfig = vi.fn();
const testClassifierConnection = vi.fn();
vi.mock("../api/endpoints", () => ({
  setConfig: (...args: unknown[]) => setConfig(...args),
  testClassifierConnection: (...args: unknown[]) => testClassifierConnection(...args),
}));
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: true, handleSessionExpired: vi.fn() }),
}));

const support = {
  credentials: {
    status: "success" as const,
    data: [{ ref: "typesafe", allowed_hosts: ["api.typesafe.ai"] }],
  },
  onRetryCredentials: vi.fn(),
  scopeNames: {},
};
const node = (settings: ScopeSettings["settings"] = {}): ScopeSettings => ({
  id: "o1",
  name: "acme",
  scope: "org:o1",
  settings,
  using_platform_defaults: true,
});
const saved = {
  enabled: false,
  backend: "typesafe",
  endpoint_url: "https://api.typesafe.ai",
  credential_ref: "typesafe",
  model: "jev-latest",
  mode: "shadow",
  disclosure_acknowledged: false,
};

function fillForm(): void {
  fireEvent.change(screen.getByLabelText("Endpoint URL (https)"), {
    target: { value: "https://api.typesafe.ai" },
  });
  fireEvent.change(screen.getByLabelText("Credential"), { target: { value: "typesafe" } });
  fireEvent.change(screen.getByLabelText("Model"), { target: { value: "jev-latest" } });
}

describe("ClassifierPanel", () => {
  beforeEach(() => {
    setConfig.mockReset().mockResolvedValue({});
    testClassifierConnection.mockReset();
  });
  afterEach(cleanup);

  it("states the disclosure without claiming zero retention or that no secret leaves", () => {
    render(<ClassifierPanel node={node()} support={support} onSaved={vi.fn()} />);

    expect(screen.getByText(CLASSIFIER_EGRESS_DISCLOSURE)).toBeInTheDocument();
    expect(CLASSIFIER_EGRESS_DISCLOSURE).toMatch(/detectable by the vendored ruleset are redacted/);
    expect(CLASSIFIER_EGRESS_DISCLOSURE).toMatch(/shared by every organization/);
    expect(CLASSIFIER_EGRESS_DISCLOSURE).not.toMatch(/no secrets|zero retention/i);
  });

  it("blocks saving an enabled classifier until the disclosure is accepted", () => {
    render(<ClassifierPanel node={node({ classifier_provider: saved })} support={support} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Enabled" }));

    expect(screen.getByRole("button", { name: "Save classifier" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /accept this disclosure/i }));
    expect(screen.getByRole("button", { name: "Save classifier" })).toBeEnabled();
  });

  it("saves the acknowledgement with the value, and surfaces the API's refusal", async () => {
    setConfig.mockRejectedValueOnce(new ApiError(422, "disclosure_not_acknowledged"));
    render(<ClassifierPanel node={node()} support={support} onSaved={vi.fn()} />);
    fillForm();
    fireEvent.click(screen.getByRole("checkbox", { name: /accept this disclosure/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Enabled" }));
    fireEvent.click(screen.getByRole("button", { name: "Save classifier" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("disclosure_not_acknowledged");
    expect(setConfig).toHaveBeenCalledWith(
      "org:o1",
      "classifier_provider",
      expect.objectContaining({ enabled: true, disclosure_acknowledged: true, backend: "typesafe" })
    );
  });

  it("lets an admin disable without accepting the disclosure", () => {
    render(<ClassifierPanel node={node({ classifier_provider: saved })} support={support} onSaved={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "jev-1.14.0" } });

    expect(screen.getByRole("button", { name: "Save classifier" })).toBeEnabled();
  });

  it("tests only saved values and shows the closed-set result", async () => {
    testClassifierConnection.mockResolvedValue({ status: "unauthorized", message: "The provider rejected the credential." });
    render(<ClassifierPanel node={node({ classifier_provider: saved })} support={support} onSaved={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "other" } });
    expect(screen.getByRole("button", { name: /save changes to test/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "jev-latest" } });
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("unauthorized");
    });
    expect(testClassifierConnection).toHaveBeenCalledWith({
      org_id: "o1",
      endpoint_url: "https://api.typesafe.ai",
      credential_ref: "typesafe",
      model: "jev-latest",
    });
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<ClassifierPanel node={node()} support={support} onSaved={vi.fn()} />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
