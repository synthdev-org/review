// Types mirroring apps/admin_api's pydantic response models, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section. Kept
// hand-written (not codegen'd from an OpenAPI schema) since no codegen
// pipeline exists anywhere else in this repo yet — matches every field
// name/shape in the Python route modules exactly, checked against source
// during drafting, not guessed.

export type Role = "admin" | "viewer";

export interface SessionInfo {
  username: string;
  role: Role;
}

// --- Users (Unit C) ---

export interface AdminUser {
  id: string;
  username: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ListUsersResponse {
  users: AdminUser[];
}

// --- Memory entries (Unit D) ---

export type MemoryStatus = "pending" | "approved" | "rejected";
export type MemoryEntryType = "dismissed_finding" | "accepted_fix" | "correction" | null;

export interface MemoryEntry {
  id: string;
  repo_id: string;
  org_id: string | null;
  team_id: string | null;
  entry_type: MemoryEntryType;
  status: MemoryStatus;
  raw_signal_type: string;
  raw_signal_detail: Record<string, unknown>;
  posted_finding_comment_id: number;
  embedding: number[] | null;
  approved_at: string | null;
  approved_by: string | null;
  approved_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ListMemoryEntriesResponse {
  entries: MemoryEntry[];
  total: number;
}

export interface DeleteMemoryEntryResponse {
  deleted: boolean;
  entry_id: string;
}

// US-A23: one selected id's own outcome inside a bulk-approve/bulk-reject
// call — mirrors `apps/admin_api/memory_routes.py`'s `_BulkItemResult`.
export interface BulkItemResult {
  entry_id: string;
  success: boolean;
  error: string | null;
}

export interface BulkOperationResponse {
  results: BulkItemResult[];
}

// --- Org/repo/team configuration (Unit E) ---

// One key's resolved value at a node plus the scope descriptor it came from
// (`tenancy.settings.EffectiveSetting`). Computed server-side so the UI never
// re-implements precedence.
export interface EffectiveSetting {
  value: Record<string, unknown>;
  source: string;
}

export interface ScopeSettings {
  id: string;
  name: string;
  scope: string;
  settings: Record<string, Record<string, unknown>>;
  using_platform_defaults: boolean;
  effective?: Record<string, EffectiveSetting>;
}

export interface OrgConfig extends ScopeSettings {
  repos: ScopeSettings[];
  teams: ScopeSettings[];
}

export interface ConfigTree {
  platform_settings: Record<string, Record<string, unknown>>;
  platform_effective?: Record<string, EffectiveSetting>;
  orgs: OrgConfig[];
}

export interface SettingRow {
  org_id: string | null;
  repo_id: string | null;
  team_id: string | null;
  key: string;
  value: Record<string, unknown>;
}

// The generic setting form's keys (tenancy/settings.py's
// SETTING_KEY_REGISTRY, minus `product_knowledge_provider`) — kept in sync by
// hand, same rationale as the rest of this file. `product_knowledge_provider`
// is deliberately NOT here: its value has a multi-field shape the generic
// form cannot build, so it is only ever written by ProductKnowledgePanel.
export const SETTING_KEYS = ["memory_suppression_enabled", "findings_severity_threshold"] as const;
export type SettingKey = (typeof SETTING_KEYS)[number];

export const PRODUCT_KNOWLEDGE_PROVIDER_KEY = "product_knowledge_provider";

// --- Product knowledge provider (Phase 6 routes; Unit H) ---

// `allowed_hosts` is null for a Viewer (bound hosts are Admin-only).
export interface ProductKnowledgeCredential {
  ref: string;
  allowed_hosts: string[] | null;
}

export type TestConnectionStatus =
  | "ok"
  | "blocked_by_policy"
  | "disabled"
  | "unreachable"
  | "no_contract"
  | "malformed"
  | "auth_failed"
  | "unsupported_version"
  | "deprecated_version"
  | "timeout";

export interface TestConnectionResult {
  status: TestConnectionStatus;
  message: string;
  negotiated_version?: string | null;
  provider_supported_versions?: string[] | null;
  tools_present?: string[] | null;
  latency_ms?: number | null;
}

// --- Model routing (Unit F) ---

export type ModelRoutingSource = "org" | "platform" | "static_default" | "fallback_on_error";

export const MODEL_ROUTING_STAGES = ["intent_summary", "review_generation", "verification"] as const;
export type ModelRoutingStage = (typeof MODEL_ROUTING_STAGES)[number];

export interface ModelRoutingStageView {
  stage: string;
  model_name: string;
  source: ModelRoutingSource;
}

export interface ModelRoutingView {
  org_id: string | null;
  stages: ModelRoutingStageView[];
}

// --- Admin-stats dashboard (Unit G) ---

export interface QueueDepthStats {
  depth: number;
  window: string;
}

export interface LLMTokenUsageEntry {
  stage: string;
  model: string;
  token_type: string;
  tokens: number;
}

export interface LLMTokenUsageStats {
  entries: LLMTokenUsageEntry[];
  window: string;
}

export interface VerificationFunnelStats {
  judged: number;
  rejected: number;
  unmatched: number;
  window: string;
}

export interface FindingsBySeverityEntry {
  severity: string;
  source: string;
  count: number;
}

export interface FindingsStats {
  entries: FindingsBySeverityEntry[];
  window: string;
}

export interface AdminStatsView {
  queue_depth: QueueDepthStats;
  llm_token_usage: LLMTokenUsageStats;
  verification_funnel: VerificationFunnelStats;
  findings_by_severity: FindingsStats;
}

// --- Free-tier usage counter (Unit H) ---

export interface UsageOrgView {
  installation_id: number;
  org_id: string | null;
  org_name: string | null;
  day: string;
  count: number;
  over_threshold: boolean;
}

export interface UsageView {
  threshold: number;
  orgs: UsageOrgView[];
}
