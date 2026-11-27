// Component tests for the Login screen — loading (submitting), error
// (invalid credentials / rate-limited), and success (redirect) states.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import { ApiError } from "../api/client";
import * as endpoints from "../api/endpoints";
import { LoginScreen } from "./Login";

const mockUseAuth = vi.fn();

vi.mock("../auth/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("../api/endpoints", () => ({
  getOidcConfig: vi.fn(),
}));

function renderLogin(url = "/login") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <LoginScreen />
    </MemoryRouter>
  );
}

describe("LoginScreen", () => {
  beforeEach(() => {
    mockUseAuth.mockReset();
    vi.mocked(endpoints.getOidcConfig).mockResolvedValue({ enabled: false });
  });

  it("shows the SSO link only when the server reports OIDC enabled", async () => {
    mockUseAuth.mockReturnValue({ status: "unauthenticated", login: vi.fn() });
    vi.mocked(endpoints.getOidcConfig).mockResolvedValue({ enabled: true });
    renderLogin();

    const link = await screen.findByRole("link", { name: /sign in with sso/i });
    expect(link).toHaveAttribute("href", "/api/v1/auth/oidc/login");
  });

  it("hides the SSO link when the OIDC lookup fails", async () => {
    mockUseAuth.mockReturnValue({ status: "unauthenticated", login: vi.fn() });
    vi.mocked(endpoints.getOidcConfig).mockRejectedValue(new ApiError(500, "boom"));
    renderLogin();

    await waitFor(() => expect(endpoints.getOidcConfig).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: /sign in with sso/i })).toBeNull();
  });

  it("shows a generic error after a failed SSO round trip", async () => {
    mockUseAuth.mockReturnValue({ status: "unauthenticated", login: vi.fn() });
    renderLogin("/login?error=sso_failed");

    expect(await screen.findByRole("alert")).toHaveTextContent(/single sign-on failed/i);
  });

  it("shows a submitting (loading) state while the login request is in flight", async () => {
    let resolveLogin: () => void = () => {};
    const loginPromise = new Promise<void>((resolve) => {
      resolveLogin = resolve;
    });
    mockUseAuth.mockReturnValue({
      status: "unauthenticated",
      login: vi.fn().mockReturnValue(loginPromise),
    });
    renderLogin();

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "hunter2hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("button", { name: /signing in/i })).toBeDisabled();
    resolveLogin();
  });

  it("shows a distinct error state on invalid credentials, with no username-existence detail", async () => {
    mockUseAuth.mockReturnValue({
      status: "unauthenticated",
      login: vi.fn().mockRejectedValue(new ApiError(401, "invalid username or password")),
    });
    renderLogin();

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "wrong-password" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("invalid username or password");
  });

  it("shows a distinct rate-limit message on 429", async () => {
    mockUseAuth.mockReturnValue({
      status: "unauthenticated",
      login: vi.fn().mockRejectedValue(new ApiError(429, "too many login attempts")),
    });
    renderLogin();

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "hunter2hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/too many login attempts/i);
  });

  it("redirects once the session becomes authenticated (success)", async () => {
    mockUseAuth.mockReturnValue({ status: "authenticated", login: vi.fn() });
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <LoginScreen />
        <div data-testid="elsewhere" />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /sign in/i })).not.toBeInTheDocument();
    });
  });

  it("has no detectable accessibility violations", async () => {
    mockUseAuth.mockReturnValue({ status: "unauthenticated", login: vi.fn() });
    const { container } = renderLogin();
    expect(await axe(container)).toHaveNoViolations();
  });
});
