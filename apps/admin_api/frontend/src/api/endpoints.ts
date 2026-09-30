// Thin, typed wrappers over apiRequest — one function per
// apps/admin_api route, per docs/plans/plan-deliver-admin-gui.md's Phase 8
// section. Screens call these, never `apiRequest` directly, so every
// endpoint's path/method/response type is named and typo-checked in one
// place.

import { apiRequest } from "./client";
import type {
  AdminStatsView,
  AdminUser,
  ConfigTree,
  DeleteMemoryEntryResponse,
  ListMemoryEntriesResponse,
  ListUsersResponse,
  MemoryEntry,
  ModelRoutingView,
  Role,
  SessionInfo,
  SettingRow,
  UsageView,
} from "./types";

// --- Auth (Unit B) ---

export function login(username: string, password: string): Promise<SessionInfo> {
  return apiRequest<SessionInfo>("/auth/login", { method: "POST", body: { username, password } });
}

export function logout(): Promise<{ logged_out: boolean }> {
  return apiRequest("/auth/logout", { method: "POST" });
}

export function getSession(): Promise<SessionInfo> {
  return apiRequest<SessionInfo>("/auth/session");
}

// --- Users (Unit C) ---

export async function listUsers(): Promise<AdminUser[]> {
  const response = await apiRequest<ListUsersResponse>("/users");
  return response.users;
}

export function createUser(username: string, password: string, role: Role): Promise<AdminUser> {
  return apiRequest<AdminUser>("/users", { method: "POST", body: { username, password, role } });
}

export function deactivateUser(userId: string): Promise<AdminUser> {
  return apiRequest<AdminUser>(`/users/${encodeURIComponent(userId)}/deactivate`, {
    method: "PATCH",
  });
}

// --- Memory entries (Unit D) ---

export interface ListMemoryEntriesParams {
  orgId?: string;
  repoId?: string;
  teamId?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export function listMemoryEntries(
  params: ListMemoryEntriesParams = {}
): Promise<ListMemoryEntriesResponse> {
  const query = new URLSearchParams();
  if (params.orgId) query.set("org_id", params.orgId);
  if (params.repoId) query.set("repo_id", params.repoId);
  if (params.teamId) query.set("team_id", params.teamId);
  if (params.status) query.set("status", params.status);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return apiRequest<ListMemoryEntriesResponse>(`/memory-entries${qs ? `?${qs}` : ""}`);
}

export function approveMemoryEntry(
  entryId: string,
  entryType: "dismissed_finding" | "accepted_fix" | "correction"
): Promise<MemoryEntry> {
  return apiRequest<MemoryEntry>(`/memory-entries/${encodeURIComponent(entryId)}/approve`, {
    method: "POST",
    body: { entry_type: entryType },
  });
}

export function rejectMemoryEntry(entryId: string): Promise<MemoryEntry> {
  return apiRequest<MemoryEntry>(`/memory-entries/${encodeURIComponent(entryId)}/reject`, {
    method: "POST",
  });
}

export function deleteMemoryEntry(entryId: string): Promise<DeleteMemoryEntryResponse> {
  return apiRequest<DeleteMemoryEntryResponse>(
    `/memory-entries/${encodeURIComponent(entryId)}?confirm=true`,
    { method: "DELETE" }
  );
}

// --- Org/repo/team configuration (Unit E) ---

export function getConfig(): Promise<ConfigTree> {
  return apiRequest<ConfigTree>("/config");
}

export function setConfig(
  scope: string,
  key: string,
  value: Record<string, unknown>
): Promise<SettingRow> {
  return apiRequest<SettingRow>(
    `/config/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
    { method: "PUT", body: { value } }
  );
}

// --- Model routing (Unit F) ---

export function getModelRouting(orgId?: string): Promise<ModelRoutingView> {
  const qs = orgId ? `?org_id=${encodeURIComponent(orgId)}` : "";
  return apiRequest<ModelRoutingView>(`/model-routing${qs}`);
}

export function setModelRouting(
  stage: string,
  modelName: string,
  orgId: string | null
): Promise<unknown> {
  return apiRequest(`/model-routing/${encodeURIComponent(stage)}`, {
    method: "PUT",
    body: { org_id: orgId, model_name: modelName },
  });
}

// --- Admin-stats dashboard (Unit G) ---

export function getStats(): Promise<AdminStatsView> {
  return apiRequest<AdminStatsView>("/stats");
}

// --- Free-tier usage counter (Unit H) ---

export function getUsage(): Promise<UsageView> {
  return apiRequest<UsageView>("/usage");
}
