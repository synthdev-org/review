import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

// The persistent navigation element the PRD's UX & Design section calls
// for ("Each screen is reachable from a persistent navigation element;
// there is no forced linear wizard"). The "Users" link is hidden entirely
// for a Viewer session (not just disabled) — apps/admin_api/user_routes.py
// gates *every* route in that module, including the list read, behind
// `require_admin_role`, so a Viewer can't use that screen at all, unlike
// every other screen here where Viewers retain read access.
export function Layout({ children }: { children: ReactNode }): JSX.Element {
  const { username, role, isAdmin, logout } = useAuth();

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className="app-header">
        <span className="app-header-title">review Admin</span>
        <nav aria-label="Primary">
          <ul>
            <li>
              <NavLink to="/dashboard">Dashboard</NavLink>
            </li>
            <li>
              <NavLink to="/memory">Memory review</NavLink>
            </li>
            <li>
              <NavLink to="/config">Configuration</NavLink>
            </li>
            <li>
              <NavLink to="/model-routing">Model routing</NavLink>
            </li>
            {isAdmin && (
              <li>
                <NavLink to="/users">Users</NavLink>
              </li>
            )}
          </ul>
        </nav>
        <div className="app-header-account">
          <span>
            {username} <span className="role-badge">{role}</span>
          </span>
          <button type="button" onClick={() => void logout()}>
            Log out
          </button>
        </div>
      </header>
      <main id="main-content">{children}</main>
    </div>
  );
}
