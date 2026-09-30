import type { FormEvent } from "react";
import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

// US-A1: username + password login, no indication of whether a username
// exists (the server already guarantees this — see
// apps/admin_api/auth_routes.py's dummy constant-cost verify path; this
// screen just needs to not add its own leak, e.g. by phrasing the error
// message differently for the two cases, which it doesn't).
export function LoginScreen(): JSX.Element {
  const { status, login } = useAuth();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitState, setSubmitState] = useState<
    { kind: "idle" } | { kind: "submitting" } | { kind: "error"; message: string }
  >({ kind: "idle" });

  if (status === "authenticated") {
    const from = (location.state as { from?: { pathname: string } } | null)?.from;
    return <Navigate to={from?.pathname ?? "/dashboard"} replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSubmitState({ kind: "submitting" });
    try {
      await login(username, password);
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.status === 429
            ? "Too many login attempts. Please wait before trying again."
            : error.detail
          : "something went wrong";
      setSubmitState({ kind: "error", message });
    }
  }

  const submitting = submitState.kind === "submitting";

  return (
    <div className="login-screen">
      <form onSubmit={(event) => void handleSubmit(event)} aria-describedby="login-error">
        <h1>review Admin</h1>
        <div className="form-field">
          <label htmlFor="username">Username</label>
          <input
            id="username"
            name="username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={submitting}
          />
        </div>
        <div className="form-field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
        </div>
        {submitState.kind === "error" && (
          <p id="login-error" role="alert" className="form-error">
            {submitState.message}
          </p>
        )}
        <button type="submit" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
