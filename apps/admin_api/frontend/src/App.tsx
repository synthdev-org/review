import { Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { ConfigScreen } from "./screens/Config";
import { DashboardScreen } from "./screens/Dashboard";
import { LoginScreen } from "./screens/Login";
import { MemoryQueueScreen } from "./screens/MemoryQueue";
import { ModelRoutingScreen } from "./screens/ModelRouting";
import { UsersScreen } from "./screens/Users";

// Primary flow per the PRD's UX & Design section: "login → dashboard
// (stats + usage) → org/repo/team config → Memory review queue → model
// routing," each reachable from Layout.tsx's persistent nav, no forced
// linear wizard — every route below is independently reachable.
export function App(): JSX.Element {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginScreen />} />
        <Route
          path="/dashboard"
          element={
            <ProtectedRoute>
              <DashboardScreen />
            </ProtectedRoute>
          }
        />
        <Route
          path="/memory"
          element={
            <ProtectedRoute>
              <MemoryQueueScreen />
            </ProtectedRoute>
          }
        />
        <Route
          path="/config"
          element={
            <ProtectedRoute>
              <ConfigScreen />
            </ProtectedRoute>
          }
        />
        <Route
          path="/model-routing"
          element={
            <ProtectedRoute>
              <ModelRoutingScreen />
            </ProtectedRoute>
          }
        />
        <Route
          path="/users"
          element={
            <ProtectedRoute>
              <UsersScreen />
            </ProtectedRoute>
          }
        />
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </AuthProvider>
  );
}
