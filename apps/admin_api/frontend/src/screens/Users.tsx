import type { FormEvent } from "react";
import { useCallback, useState } from "react";

import * as api from "../api/endpoints";
import { ApiError, isSessionExpiredError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Notice } from "../components/Notice";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { useAsyncData } from "../hooks/useAsyncData";
import { useNotice } from "../hooks/useNotice";
import type { Role } from "../api/types";

// Unit C (US-A4): user management — every route in
// apps/admin_api/user_routes.py, including the list read, is gated behind
// `require_admin_role` (a deliberate departure from this plan's usual
// Viewer-read-parity posture, per that module's own docstring: "who else
// can sign in and what role they hold" is itself sensitive). This screen
// therefore never issues a request at all for a Viewer session — it shows
// a plain "not available for your role" message instead of a 403 round
// trip, matching the PRD's "never clickable-but-failing" rule one step
// further upstream.
export function UsersScreen(): JSX.Element {
  const { isAdmin, handleSessionExpired } = useAuth();
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [deactivatingId, setDeactivatingId] = useState<string | null>(null);
  const { notice, showSuccess, showError, dismiss } = useNotice();

  // Guarded so a Viewer session never actually issues the `GET /api/v1/users`
  // request (which would 403) — hooks still run unconditionally (React's
  // rules of hooks), but the fetcher itself short-circuits before any
  // network call when the role check below would hide the screen anyway.
  const fetcher = useCallback(
    () => (isAdmin ? api.listUsers() : Promise.resolve([])),
    [isAdmin]
  );
  const { state, reload } = useAsyncData(fetcher, [isAdmin]);

  if (!isAdmin) {
    return (
      <div className="screen">
        <h1>Users</h1>
        <EmptyState title="Not available for your role." description="User management requires the Admin role." />
      </div>
    );
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      await api.createUser(newUsername, newPassword, newRole);
      showSuccess(`User ${newUsername} created.`);
      setNewUsername("");
      setNewPassword("");
      setNewRole("viewer");
      reload();
    } catch (error) {
      if (isSessionExpiredError(error)) {
        handleSessionExpired();
        return;
      }
      setCreateError(error instanceof ApiError ? error.detail : "create failed");
    } finally {
      setCreating(false);
    }
  }

  async function handleDeactivate(userId: string, username: string): Promise<void> {
    setDeactivatingId(userId);
    try {
      await api.deactivateUser(userId);
      showSuccess(`User ${username} deactivated.`);
      reload();
    } catch (error) {
      if (isSessionExpiredError(error)) {
        handleSessionExpired();
        return;
      }
      showError(error instanceof ApiError ? error.detail : "deactivate failed");
    } finally {
      setDeactivatingId(null);
    }
  }

  return (
    <div className="screen">
      <h1>Users</h1>
      <Notice notice={notice} onDismiss={dismiss} />

      <section aria-labelledby="create-user-heading">
        <h2 id="create-user-heading">Create user</h2>
        <form onSubmit={(event) => void handleCreate(event)}>
          <div className="form-field">
            <label htmlFor="new-username">Username</label>
            <input
              id="new-username"
              type="text"
              required
              value={newUsername}
              onChange={(event) => setNewUsername(event.target.value)}
              disabled={creating}
            />
          </div>
          <div className="form-field">
            <label htmlFor="new-password">Initial password</label>
            <input
              id="new-password"
              type="password"
              required
              minLength={12}
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              disabled={creating}
            />
          </div>
          <div className="form-field">
            <label htmlFor="new-role">Role</label>
            <select
              id="new-role"
              value={newRole}
              onChange={(event) => setNewRole(event.target.value as Role)}
              disabled={creating}
            >
              <option value="viewer">Viewer</option>
              <option value="admin">Admin</option>
            </select>
          </div>
          {createError && (
            <p role="alert" className="form-error">
              {createError}
            </p>
          )}
          <button type="submit" disabled={creating}>
            {creating ? "Creating…" : "Create user"}
          </button>
        </form>
      </section>

      <section aria-labelledby="users-list-heading">
        <h2 id="users-list-heading">Existing users</h2>
        {state.status === "loading" && <LoadingState label="Loading users…" />}
        {state.status === "error" && <ErrorState message={state.message} onRetry={reload} />}
        {state.status === "success" &&
          (state.data.length === 0 ? (
            <EmptyState title="No users yet." />
          ) : (
            <table>
              <caption className="sr-only">Admin UI user accounts</caption>
              <thead>
                <tr>
                  <th scope="col">Username</th>
                  <th scope="col">Role</th>
                  <th scope="col">Status</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {state.data.map((user) => (
                  <tr key={user.id}>
                    <td>{user.username}</td>
                    <td>{user.role}</td>
                    <td>{user.is_active ? "Active" : "Deactivated"}</td>
                    <td>
                      {user.is_active && (
                        <button
                          type="button"
                          disabled={deactivatingId === user.id}
                          onClick={() => void handleDeactivate(user.id, user.username)}
                        >
                          Deactivate
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
      </section>
    </div>
  );
}
