import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { LoadingState } from "./StateViews";
import { Layout } from "./Layout";

/** Gates every screen behind a resolved session — shows a loading state
 * while the initial `GET /api/v1/auth/session` check is in flight
 * (US-A6), and redirects to `/login` (preserving the originally-requested
 * path so a bookmark/deep-link survives a fresh login) once it resolves to
 * "unauthenticated," including mid-session expiry (US-A2). */
export function ProtectedRoute({ children }: { children: ReactNode }): JSX.Element {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "checking") {
    return <LoadingState label="Checking your session…" />;
  }

  if (status === "unauthenticated") {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <Layout>{children}</Layout>;
}
