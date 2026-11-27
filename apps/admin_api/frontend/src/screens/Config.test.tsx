// Component tests for the org/repo/team configuration screen —
// loading/empty/error/success states, Viewer-role write-form hiding, and
// US-A13's scoped setting form.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import { ErrorState } from "../components/StateViews";
import type { ConfigTree, ProductKnowledgeCredential, TestConnectionStatus } from "../api/types";
import { ConfigScreen } from "./Config";
import { ProductKnowledgePanel } from "./ProductKnowledgePanel";
import { SETTING_KEYS } from "../api/types";
import { PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE } from "./productKnowledgeCopy";

const getConfig = vi.fn();
const setConfig = vi.fn();
const getCredentials = vi.fn();
const testConnection = vi.fn();

vi.mock("../api/endpoints", () => ({
  getConfig: () => getConfig(),
  setConfig: (...args: unknown[]) => setConfig(...args),
  getProductKnowledgeCredentials: () => getCredentials(),
  testProductKnowledgeConnection: (...args: unknown[]) => testConnection(...args),
}));

let mockIsAdmin = true;
const handleSessionExpired = vi.fn();
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: mockIsAdmin, handleSessionExpired: (...args: unknown[]) => handleSessionExpired(...args) }),
}));

// A promise that never settles (in-flight request stand-in).
function never(): void {
  return undefined;
}

const emptyTree: ConfigTree = { platform_settings: {}, orgs: [] };

describe("ConfigScreen", () => {
  beforeEach(() => {
    getConfig.mockReset();
    setConfig.mockReset();
    getCredentials.mockReset();
    getCredentials.mockResolvedValue([]);
    testConnection.mockReset();
    handleSessionExpired.mockReset();
    mockIsAdmin = true;
  });

  it("shows a loading state while the config tree is being fetched", () => {
    getConfig.mockReturnValue(new Promise(never));
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

  it("Admin: a 401 mid-save (expired/revoked session) routes back to login instead of showing a generic error", async () => {
    const { ApiError } = await import("../api/client");
    getConfig.mockResolvedValue(emptyTree);
    setConfig.mockRejectedValue(new ApiError(401, "session expired"));
    render(<ConfigScreen />);

    await screen.findByText("Platform-wide");
    fireEvent.click(screen.getAllByRole("button", { name: /save setting/i })[0]);

    await waitFor(() => expect(handleSessionExpired).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("notice")).not.toBeInTheDocument();
    expect(screen.queryByText(/save failed/i)).not.toBeInTheDocument();
  });

  it("Viewer: sees configuration read-only, no save control", async () => {
    mockIsAdmin = false;
    getConfig.mockResolvedValue(emptyTree);
    render(<ConfigScreen />);

    await screen.findByText("Platform-wide");
    expect(screen.queryByRole("button", { name: /save setting/i })).not.toBeInTheDocument();
    expect(screen.getAllByText(/viewers can see configuration but not change it/i).length).toBeGreaterThan(0);
  });

  it("has no detectable accessibility violations", async () => {
    getConfig.mockResolvedValue(emptyTree);
    const { container } = render(<ConfigScreen />);
    await screen.findByText("Platform-wide");
    expect(await axe(container)).toHaveNoViolations();
  });
});

// --- ProductKnowledgePanel (US-PK8/PK10/PK15) ---

const PK = "product_knowledge_provider";
const savedProvider = {
  enabled: true,
  display_name: "Acme Wiki",
  endpoint_url: "https://wiki.example.com/mcp",
  credential_ref: "wiki-prod",
  allow_public_repos: false,
  issue_key_prefixes: ["HR"],
};
const credentials: ProductKnowledgeCredential[] = [
  { ref: "wiki-prod", allowed_hosts: ["wiki.example.com"] },
  { ref: "other", allowed_hosts: ["other.example.com"] },
];

function platformTree(value: Record<string, unknown> | null = savedProvider): ConfigTree {
  return {
    platform_settings: value ? { [PK]: value } : {},
    platform_effective: value ? { [PK]: { value, source: "platform" } } : {},
    orgs: [],
  };
}

function orgTree(): ConfigTree {
  return {
    platform_settings: { [PK]: savedProvider },
    platform_effective: { [PK]: { value: savedProvider, source: "platform" } },
    orgs: [
      {
        id: "o1",
        name: "payments",
        scope: "org:o1",
        settings: {},
        using_platform_defaults: true,
        effective: { [PK]: { value: savedProvider, source: "platform" } },
        repos: [
          {
            id: "r1",
            name: "ledger",
            scope: "repo:o1:r1",
            settings: {},
            using_platform_defaults: true,
            effective: {
              [PK]: { value: { ...savedProvider, display_name: "Org Wiki" }, source: "org:o1" },
            },
          },
        ],
        teams: [
          { id: "t1", name: "core", scope: "team:o1:t1", settings: {}, using_platform_defaults: true },
        ],
      },
    ],
  };
}

async function renderPanel(tree: ConfigTree = platformTree()): Promise<HTMLElement> {
  getConfig.mockResolvedValue(tree);
  getCredentials.mockResolvedValue(credentials);
  const view = render(<ConfigScreen />);
  await screen.findByText("Platform-wide");
  await screen.findAllByRole("option", { name: /wiki-prod/ });
  return view.container;
}

const endpointField = (): HTMLInputElement => screen.getByLabelText("Endpoint URL");
const testButton = (): HTMLElement => screen.getByRole("button", { name: /test connection|save changes to test/i });

describe("ErrorState title", () => {
  it("keeps the default copy without a title and uses a given one", () => {
    const { rerender } = render(<ErrorState message="boom" />);
    expect(screen.getByText(/couldn.t load this screen/i)).toBeInTheDocument();
    rerender(<ErrorState message="boom" title="Could not load credentials" />);
    expect(screen.getByText("Could not load credentials")).toBeInTheDocument();
    expect(screen.queryByText(/couldn.t load this screen/i)).not.toBeInTheDocument();
  });
});

describe("ProductKnowledgePanel", () => {
  beforeEach(() => {
    getConfig.mockReset();
    setConfig.mockReset();
    getCredentials.mockReset();
    testConnection.mockReset();
    handleSessionExpired.mockReset();
    mockIsAdmin = true;
  });

  it("shows the shared egress disclosure inline, tied to the endpoint field", async () => {
    await renderPanel();
    const disclosure = screen.getByText(PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE);
    expect(disclosure.tagName).toBe("P");
    expect(endpointField().getAttribute("aria-describedby")).toContain(disclosure.id);
  });

  it("disclosure literal is non-empty and says what is sent, independent of the import", () => {
    expect(PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE.length).toBeGreaterThan(0);
    expect(PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE.startsWith("Sent to this endpoint")).toBe(true);
  });

  it("disclosure pins the safety claim: what is Never sent (code, diff, description)", () => {
    // Asserted against hard-coded words, not derived from the constant, so a
    // copy edit that weakens the promise fails here.
    const [, never] = PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE.split("Never sent");
    expect(never).toBeDefined();
    expect(never).toMatch(/\bcode\b/);
    expect(never).toMatch(/\bdiff\b/);
    expect(never).toMatch(/\bdescription\b/);
  });

  it("the generic setting select and SETTING_KEYS omit product_knowledge_provider", async () => {
    expect(SETTING_KEYS).not.toContain(PK);
    await renderPanel();
    for (const select of document.querySelectorAll<HTMLSelectElement>("select[id^='setting-key-']")) {
      const values = Array.from(select.options).map((o) => o.value);
      expect(values).not.toContain(PK);
      expect(values.length).toBeGreaterThan(0);
    }
    expect(document.querySelectorAll("select[id^='setting-key-']").length).toBeGreaterThan(0);
  });

  it("the credential select stays enabled and None selectable while credentials load", async () => {
    getConfig.mockResolvedValue(platformTree(null));
    getCredentials.mockReturnValue(new Promise(never));
    render(<ConfigScreen />);
    await screen.findByText("Platform-wide");
    expect(await screen.findByText(/loading credentials/i)).toBeInTheDocument();
    const select = screen.getByLabelText("Credential");
    expect(select).toBeEnabled();
    fireEvent.change(select, { target: { value: "" } });
    expect(select).toHaveValue("");
  });

  it("credentials dropdown shows None and bound hosts", async () => {
    await renderPanel();
    expect(screen.getByRole("option", { name: "None (unauthenticated)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /wiki-prod \(bound to wiki\.example\.com\)/ })).toBeInTheDocument();
  });

  it("a stored credential_ref missing from the operator map is a disabled 'removed' option", async () => {
    getConfig.mockResolvedValue(platformTree({ ...savedProvider, credential_ref: "gone" }));
    getCredentials.mockResolvedValue(credentials);
    render(<ConfigScreen />);
    const option = await screen.findByRole("option", { name: /unknown credential `gone` \(removed by operator\)/i });
    expect(option).toBeDisabled();
    expect(screen.getByLabelText("Credential")).toHaveValue("gone");
  });

  it("a Viewer's credential list (no hosts) shows names only", async () => {
    mockIsAdmin = false;
    getConfig.mockResolvedValue(platformTree());
    getCredentials.mockResolvedValue([{ ref: "wiki-prod", allowed_hosts: null }]);
    render(<ConfigScreen />);
    expect(await screen.findByRole("option", { name: "wiki-prod" })).toBeInTheDocument();
    expect(screen.queryByText(/bound to/)).not.toBeInTheDocument();
  });

  it("a credentials fetch failure renders inline with retry and does not blank the form", async () => {
    const { ApiError } = await import("../api/client");
    getConfig.mockResolvedValue(platformTree());
    getCredentials.mockRejectedValueOnce(new ApiError(500, "creds down"));
    render(<ConfigScreen />);
    expect(await screen.findByText("Couldn\u2019t load credentials")).toBeInTheDocument();
    expect(screen.getByText("creds down")).toBeInTheDocument();
    expect(endpointField()).toHaveValue("https://wiki.example.com/mcp");

    getCredentials.mockResolvedValue(credentials);
    const errorRegion = screen.getByText("creds down").closest<HTMLElement>("[role=alert]");
    expect(errorRegion).not.toBeNull();
    fireEvent.click(within(errorRegion!).getByRole("button", { name: /retry/i }));
    expect(await screen.findByRole("option", { name: /wiki-prod \(bound/ })).toBeInTheDocument();
    expect(getCredentials).toHaveBeenCalledTimes(2);
  });

  it("effective preview names the inheriting source and 'Not configured'", async () => {
    await renderPanel(orgTree());
    const previews = screen.getAllByTestId("pk-effective-preview").map((n) => n.textContent);
    expect(previews).toContain("Using: Acme Wiki (inherited from platform)");
    expect(previews).toContain("Using: Org Wiki (inherited from org payments)");
  });

  it("shows 'Not configured' with no provider, and no panel at team scope", async () => {
    await renderPanel(platformTree(null));
    expect(screen.getByTestId("pk-effective-preview")).toHaveTextContent(
      "Not configured \u2014 reviews use the codebase only."
    );
    cleanup();
    await renderPanel(orgTree());
    // platform + org + repo panels only: team scope has none.
    expect(screen.getAllByTestId("pk-effective-preview")).toHaveLength(3);
  });

  it("saves the form value and maps a 422 to the field with focus + aria", async () => {
    const { ApiError } = await import("../api/client");
    await renderPanel(platformTree(null));
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Wiki" } });
    fireEvent.change(endpointField(), { target: { value: "https://wiki.example.com/mcp" } });
    fireEvent.change(screen.getByLabelText("Credential"), { target: { value: "other" } });
    setConfig.mockRejectedValueOnce(new ApiError(422, "credential_host_not_bound"));
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));

    await waitFor(() => expect(setConfig).toHaveBeenCalledWith("platform", PK, {
      enabled: true,
      display_name: "Wiki",
      endpoint_url: "https://wiki.example.com/mcp",
      credential_ref: "other",
      allow_public_repos: false,
      issue_key_prefixes: [],
    }));
    const select = screen.getByLabelText("Credential");
    await waitFor(() => expect(select).toHaveAttribute("aria-invalid", "true"));
    expect(select).toHaveFocus();
    const describedBy = select.getAttribute("aria-describedby");
    expect(describedBy).not.toBeNull();
    expect(document.getElementById(describedBy!)).toHaveTextContent("credential_host_not_bound");
  });

  it("maps unauthenticated_host_not_allowed to the endpoint field on save, with friendly copy", async () => {
    const { ApiError } = await import("../api/client");
    await renderPanel(platformTree(null));
    setConfig.mockRejectedValueOnce(new ApiError(422, "unauthenticated_host_not_allowed"));
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));
    await waitFor(() => expect(endpointField()).toHaveAttribute("aria-invalid", "true"));
    expect(endpointField()).toHaveFocus();
    const describedBy = endpointField().getAttribute("aria-describedby");
    expect(describedBy).not.toBeNull();
    const errorEl = document.getElementById(describedBy!.split(" ").pop()!);
    expect(errorEl).toHaveTextContent(/isn't allowed without a credential/i);
    expect(errorEl).not.toHaveTextContent("unauthenticated_host_not_allowed");
    expect(screen.getByLabelText("Credential")).toHaveAttribute("aria-invalid", "false");
  });

  it("maps unauthenticated_host_not_allowed to the endpoint field on test-connection", async () => {
    const { ApiError } = await import("../api/client");
    testConnection.mockRejectedValue(new ApiError(422, "unauthenticated_host_not_allowed"));
    await renderPanel();
    fireEvent.click(testButton());
    await waitFor(() => expect(endpointField()).toHaveAttribute("aria-invalid", "true"));
    expect(endpointField()).toHaveFocus();
    expect(endpointField().getAttribute("aria-describedby")).toMatch(/endpoint_url-error/);
    expect(screen.getByText(/isn't allowed without a credential/i)).toBeInTheDocument();
  });

  it("maps registry '<field>: <message>' details to their fields", async () => {
    const { ApiError } = await import("../api/client");
    await renderPanel(platformTree(null));
    setConfig.mockRejectedValueOnce(new ApiError(422, "endpoint_url: Value error, bad; display_name: too short"));
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));
    await waitFor(() => expect(endpointField()).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByLabelText("Display name")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Display name")).toHaveAttribute("aria-describedby");
    // focus lands on the first invalid field in form order
    expect(screen.getByLabelText("Display name")).toHaveFocus();
  });

  it("reports a successful save through the shared notice", async () => {
    await renderPanel(platformTree(null));
    setConfig.mockResolvedValue({});
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));
    expect(await screen.findByTestId("notice")).toHaveTextContent(`Saved ${PK}.`);
  });

  it("test-connection is disabled and relabelled while the form is dirty", async () => {
    await renderPanel();
    expect(testButton()).toBeEnabled();
    fireEvent.change(endpointField(), { target: { value: "https://other.example.com/mcp" } });
    const button = screen.getByRole("button", { name: /save changes to test connection/i });
    expect(button).toBeDisabled();
  });

  it("test-connection walks idle -> pending -> result and back to idle when the form goes dirty", async () => {
    let resolve: (value: unknown) => void = never;
    testConnection.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      })
    );
    await renderPanel();
    const statusRegion = screen.getAllByRole("status").find((n) => n.className.includes("pk-test-result"))!;
    expect(statusRegion).toHaveTextContent("");

    fireEvent.click(testButton());
    expect(statusRegion).toHaveTextContent(/testing connection/i);
    expect(screen.getByRole("button", { name: /testing/i })).toBeDisabled();
    expect(testConnection).toHaveBeenCalledWith(
      "https://wiki.example.com/mcp", "wiki-prod", expect.any(AbortSignal)
    );

    resolve({ status: "ok", message: "Connected: fine.", negotiated_version: "1", latency_ms: 12 });
    await waitFor(() => expect(statusRegion).toHaveTextContent("Connected: fine."));
    expect(statusRegion).toHaveTextContent("ok");

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Changed" } });
    await waitFor(() => expect(statusRegion).toHaveTextContent(""));
  });

  it("aborts the in-flight probe when the form goes dirty", async () => {
    let signal: AbortSignal | undefined;
    testConnection.mockImplementation((...args: [string, string | null, AbortSignal]) => {
      signal = args[2];
      return new Promise(never);
    });
    await renderPanel();
    fireEvent.click(testButton());
    fireEvent.change(endpointField(), { target: { value: "https://x.example.com/mcp" } });
    await waitFor(() => expect(signal?.aborted).toBe(true));
  });

  it("test-connection 422 moves focus to the first invalid field", async () => {
    const { ApiError } = await import("../api/client");
    testConnection.mockRejectedValue(
      new ApiError(422, "issue_key_prefixes: bad; endpoint_url: Value error, bad")
    );
    await renderPanel();
    fireEvent.click(testButton());
    await waitFor(() => expect(endpointField()).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByLabelText(/issue key prefixes/i)).toHaveAttribute("aria-invalid", "true");
    expect(endpointField()).toHaveFocus();
  });

  it("clears the test result and aborts the probe when the scope changes under the panel", async () => {
    let signal: AbortSignal | undefined;
    testConnection.mockImplementation((...args: [string, string | null, AbortSignal]) => {
      signal = args[2];
      return Promise.resolve({ status: "ok", message: "Connected: fine." });
    });
    const support = { credentials: { status: "success" as const, data: credentials }, onRetryCredentials: never, scopeNames: {} };
    const node = (scope: string) => ({ id: "x", name: "x", scope, settings: { [PK]: savedProvider }, using_platform_defaults: false });
    const { rerender } = render(<ProductKnowledgePanel node={node("org:o1")} support={support} onSaved={never} />);
    fireEvent.click(testButton());
    expect(await screen.findByText("Connected: fine.")).toBeInTheDocument();
    rerender(<ProductKnowledgePanel node={node("org:o2")} support={support} onSaved={never} />);
    await waitFor(() => expect(screen.queryByText("Connected: fine.")).not.toBeInTheDocument());
    expect(signal?.aborted).toBe(true);
  });

  it("aborts the in-flight probe when the panel unmounts", async () => {
    let signal: AbortSignal | undefined;
    testConnection.mockImplementation((...args: [string, string | null, AbortSignal]) => {
      signal = args[2];
      return new Promise(never);
    });
    await renderPanel();
    fireEvent.click(testButton());
    expect(signal?.aborted).toBe(false);
    cleanup();
    expect(signal?.aborted).toBe(true);
  });

  it.each<[TestConnectionStatus, string]>([
    ["blocked_by_policy", "Blocked by policy message"],
    ["unreachable", "Unreachable message"],
    ["no_contract", "No contract message"],
    ["malformed", "Malformed message"],
    ["auth_failed", "Auth failed message"],
    ["unsupported_version", "Unsupported message"],
    ["timeout", "Timeout message"],
  ])("renders the server message for failing status %s as an alert", async (status, message) => {
    testConnection.mockResolvedValue({ status, message });
    await renderPanel();
    fireEvent.click(testButton());
    const shown = await screen.findByText(message);
    expect(shown.closest("[role=alert]")).not.toBeNull();
    expect(shown.closest("[role=alert]")).toHaveTextContent(status);
  });

  it("renders deprecated_version as a success in the status region, not an alert", async () => {
    testConnection.mockResolvedValue({
      status: "deprecated_version",
      message: "Connected on a deprecated contract version.",
      negotiated_version: "1",
      latency_ms: 7,
    });
    await renderPanel();
    fireEvent.click(testButton());
    const text = await screen.findByText(/deprecated contract version/i);
    expect(text.closest("[role=status]")).not.toBeNull();
    expect(text.closest("[role=alert]")).toBeNull();
    expect(text.closest("[role=status]")).toHaveTextContent("deprecated_version");
  });

  it("renders a non-422/429 failure of the test request as a generic alert", async () => {
    const { ApiError } = await import("../api/client");
    testConnection.mockRejectedValue(new ApiError(500, "internal_error"));
    await renderPanel();
    fireEvent.click(testButton());
    const shown = await screen.findByText("internal_error");
    expect(shown.closest("[role=alert]")).not.toBeNull();
    expect(screen.queryByText(/too many connection tests/i)).not.toBeInTheDocument();
    expect(testButton()).toBeEnabled();
  });

  it("renders the 'disabled' status in the status region", async () => {
    testConnection.mockResolvedValue({ status: "disabled", message: "Kill switch is off." });
    await renderPanel();
    fireEvent.click(testButton());
    const text = await screen.findByText(/kill switch is off/i);
    expect(text.closest("[role=status]")).not.toBeNull();
  });

  it("renders a 429 as its own rate-limited state, distinct from probe statuses", async () => {
    const { ApiError } = await import("../api/client");
    testConnection.mockRejectedValue(new ApiError(429, "rate_limited"));
    await renderPanel();
    fireEvent.click(testButton());
    const shown = await screen.findByText(/too many connection tests/i);
    expect(shown.closest("[role=alert]")).not.toBeNull();
  });

  it("renders a display name containing markup as text", async () => {
    const markupName = "<" + "script>alert(1)<" + "/script>";
    const hostile = { ...savedProvider, display_name: markupName };
    getConfig.mockResolvedValue(platformTree(hostile));
    getCredentials.mockResolvedValue(credentials);
    const { container } = render(<ConfigScreen />);
    await screen.findByText("Platform-wide");
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByTestId("pk-effective-preview")).toHaveTextContent(markupName);
    expect(screen.getByLabelText("Display name")).toHaveValue(markupName);
  });

  it("Viewer: controls are disabled but values and preview still render", async () => {
    mockIsAdmin = false;
    await renderPanel();
    expect(endpointField()).toBeDisabled();
    expect(endpointField()).toHaveValue("https://wiki.example.com/mcp");
    expect(screen.getByRole("button", { name: "Save provider" })).toBeDisabled();
    expect(testButton()).toBeDisabled();
    expect(screen.getByTestId("pk-effective-preview")).toHaveTextContent("Using: Acme Wiki");
  });

  it("has no detectable accessibility violations with a populated panel", async () => {
    const container = await renderPanel(orgTree());
    expect(await axe(container)).toHaveNoViolations();
  });
});
