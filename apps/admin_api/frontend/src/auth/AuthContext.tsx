// Session state for the whole app — one source of truth for "who is
// signed in and what role do they hold." Every screen
// reads the current role from here (never re-derives it) to decide
// whether to disable/hide a mutating control (US-A5's "Viewer never sees a
// clickable-but-failing control" UX rule).
import type { ReactNode } from "react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import * as api from "../api/endpoints";
import type { Role } from "../api/types";

export type AuthStatus = "checking" | "authenticated" | "unauthenticated";

export interface AuthState {
  status: AuthStatus;
  username: string | null;
  role: Role | null;
  isAdmin: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Called by any screen's data hook when a request comes back 401 mid-session
   * (US-A2's idle-expiry / a revoked session) — routes the whole app back to
   * "unauthenticated" without a full page reload, preserving whatever the
   * router itself can preserve. */
  handleSessionExpired: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const [status, setStatus] = useState<AuthStatus>("checking");
  const [username, setUsername] = useState<string | null>(null);
  const [role, setRole] = useState<Role | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getSession()
      .then((session) => {
        if (cancelled) return;
        setUsername(session.username);
        setRole(session.role);
        setStatus("authenticated");
      })
      .catch(() => {
        if (cancelled) return;
        setStatus("unauthenticated");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (loginUsername: string, password: string) => {
    const session = await api.login(loginUsername, password);
    setUsername(session.username);
    setRole(session.role);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setUsername(null);
      setRole(null);
      setStatus("unauthenticated");
    }
  }, []);

  const handleSessionExpired = useCallback(() => {
    setUsername(null);
    setRole(null);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      status,
      username,
      role,
      isAdmin: role === "admin",
      login,
      logout,
      handleSessionExpired,
    }),
    [status, username, role, login, logout, handleSessionExpired]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
