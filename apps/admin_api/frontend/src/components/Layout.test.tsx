// The persistent nav hides the Admin-only "Users" link entirely for a
// Viewer session — apps/admin_api/user_routes.py gates every route in
// that module (including the list read) behind require_admin_role, so
// there is nothing for a Viewer to usefully reach there.
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";

import { Layout } from "./Layout";

const mockUseAuth = vi.fn();
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

describe("Layout", () => {
  it("Admin: shows the Users nav link", () => {
    mockUseAuth.mockReturnValue({
      username: "root-admin",
      role: "admin",
      isAdmin: true,
      logout: vi.fn(),
    });
    render(
      <MemoryRouter>
        <Layout>
          <div />
        </Layout>
      </MemoryRouter>
    );
    expect(screen.getByRole("link", { name: /users/i })).toBeInTheDocument();
  });

  it("Viewer: hides the Users nav link entirely", () => {
    mockUseAuth.mockReturnValue({
      username: "read-only",
      role: "viewer",
      isAdmin: false,
      logout: vi.fn(),
    });
    render(
      <MemoryRouter>
        <Layout>
          <div />
        </Layout>
      </MemoryRouter>
    );
    expect(screen.queryByRole("link", { name: /users/i })).not.toBeInTheDocument();
    // Read-only screens remain reachable.
    expect(screen.getByRole("link", { name: /dashboard/i })).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    mockUseAuth.mockReturnValue({
      username: "root-admin",
      role: "admin",
      isAdmin: true,
      logout: vi.fn(),
    });
    const { container } = render(
      <MemoryRouter>
        <Layout>
          <div />
        </Layout>
      </MemoryRouter>
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
