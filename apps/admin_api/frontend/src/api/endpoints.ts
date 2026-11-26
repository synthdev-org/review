// Thin, typed wrappers over apiRequest — one function per
// apps/admin_api route. Screens call these, never `apiRequest` directly, so every
// endpoint's path/method/response type is named and typo-checked in one
// place.

import { apiRequest } from "./client";
import type {
  ClassifierProbeResult,
  AdminStatsView,
  AdminUser,
  BulkOperationResponse,
  ConfigTree,
  DeleteMemoryEntryResponse,
  ListMemoryEntriesResponse,
  ListUsersResponse,
  MemoryEntry,
  ModelRoutingView,
  ProductKnowledgeCredential,
  Role,
  SessionInfo,
  SettingRow,
  TestConnectionResult,
} from "./types";

// --- Auth ---

export function login(username: string, password: string): Promise<SessionInfo> {
  return apiRequest<SessionInfo>("/auth/login", { method: "POST", body: { username, password } });
}

export function getOidcConfig(): Promise<{ enabled: boolean }> {
  return apiRequest<{ enabled: boolean }>("/auth/oidc/config");
}

export function logout(): Promise<{ logged_out: boolean }> {
  return apiRequest("/auth/logout", { method: "POST" });
}

export function getSession(): Promise<SessionInfo> {
  return apiRequest<SessionInfo>("/auth/session");
}

// --- Users ---

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

// --- Memory entries ---

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

// US-A23: bulk-approve/bulk-reject — one explicit `entry_type` applied to
// every selected id for approve, none needed for reject (matches
// `_BulkApproveRequest`/`_BulkRejectRequest` on the backend).
export function bulkApproveMemoryEntries(
  entryIds: string[],
  entryType: "dismissed_finding" | "accepted_fix" | "correction"
): Promise<BulkOperationResponse> {
  return apiRequest<BulkOperationResponse>("/memory-entries/bulk-approve", {
    method: "POST",
    body: { entry_ids: entryIds, entry_type: entryType },
  });
}

export function bulkRejectMemoryEntries(entryIds: string[]): Promise<BulkOperationResponse> {
  return apiRequest<BulkOperationResponse>("/memory-entries/bulk-reject", {
    method: "POST",
    body: { entry_ids: entryIds },
  });
}

// --- Org/repo/team configuration ---

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

// --- Product knowledge provider ---

export function getProductKnowledgeCredentials(): Promise<ProductKnowledgeCredential[]> {
  return apiRequest<ProductKnowledgeCredential[]>("/product-knowledge/credentials");
}

export function testProductKnowledgeConnection(
  endpointUrl: string,
  credentialRef: string | null,
  signal?: AbortSignal
): Promise<TestConnectionResult> {
  return apiRequest<TestConnectionResult>("/product-knowledge/test-connection", {
    method: "POST",
    body: { endpoint_url: endpointUrl, credential_ref: credentialRef },
    signal,
  });
}

// --- Classifier provider ---

export function testClassifierConnection(
  body: { org_id: string; endpoint_url: string; credential_ref: string; model: string },
  signal?: AbortSignal
): Promise<ClassifierProbeResult> {
  return apiRequest<ClassifierProbeResult>("/classifier/test-connection", {
    method: "POST",
    body,
    signal,
  });
}

// --- Model routing ---

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

// --- Admin-stats dashboard ---

export function getStats(): Promise<AdminStatsView> {
  return apiRequest<AdminStatsView>("/stats");
}
