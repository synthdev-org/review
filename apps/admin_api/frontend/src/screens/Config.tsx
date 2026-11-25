import { useCallback, useMemo, useState } from "react";

import * as api from "../api/endpoints";
import { ApiError, isSessionExpiredError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Notice } from "../components/Notice";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { useAsyncData } from "../hooks/useAsyncData";
import { useNotice } from "../hooks/useNotice";
import type { ConfigTree, ScopeSettings, SettingKey } from "../api/types";
import { ClassifierPanel } from "./ClassifierPanel";
import { ProductKnowledgePanel, type ProductKnowledgeSupport } from "./ProductKnowledgePanel";

const SEVERITY_OPTIONS = ["low", "medium", "high", "critical"] as const;

interface SettingFormProps {
  scope: string;
  onSaved: (scope: string, key: string) => void;
}

// US-A13: one small form per scope, closed against
// tenancy.settings.SETTING_KEY_REGISTRY's two known keys — an unknown key
// is rejected server-side (422), so this form only ever offers the known
// set, never a free-text key field.
function SettingForm({ scope, onSaved }: SettingFormProps): JSX.Element {
  const { isAdmin, handleSessionExpired } = useAuth();
  const [key, setKey] = useState<SettingKey>("memory_suppression_enabled");
  const [enabled, setEnabled] = useState(true);
  const [threshold, setThreshold] = useState<(typeof SEVERITY_OPTIONS)[number]>("medium");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isAdmin) {
    return <p className="hint">Viewers can see configuration but not change it.</p>;
  }

  async function handleSubmit(): Promise<void> {
    setSaving(true);
    setError(null);
    try {
      const value = key === "memory_suppression_enabled" ? { enabled } : { threshold };
      await api.setConfig(scope, key, value);
      onSaved(scope, key);
    } catch (submitError) {
      if (isSessionExpiredError(submitError)) {
        handleSessionExpired();
        return;
      }
      setError(submitError instanceof ApiError ? submitError.detail : "save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="setting-form">
      <label htmlFor={`setting-key-${scope}`}>Setting</label>
      <select
        id={`setting-key-${scope}`}
        value={key}
        onChange={(event) => setKey(event.target.value as SettingKey)}
        disabled={saving}
      >
        <option value="memory_suppression_enabled">Memory suppression enabled</option>
        <option value="findings_severity_threshold">Findings severity threshold</option>
      </select>

      {key === "memory_suppression_enabled" ? (
        <label>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            disabled={saving}
          />
          Enabled
        </label>
      ) : (
        <label>
          Threshold
          <select
            value={threshold}
            onChange={(event) => setThreshold(event.target.value as (typeof SEVERITY_OPTIONS)[number])}
            disabled={saving}
          >
            {SEVERITY_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      )}

      <button type="button" onClick={() => void handleSubmit()} disabled={saving}>
        {saving ? "Saving…" : "Save setting"}
      </button>
      {error && (
        <p role="alert" className="form-error">
          {error}
        </p>
      )}
    </div>
  );
}

function ScopeCard({
  title,
  node,
  onSaved,
  support,
}: {
  title: string;
  node: ScopeSettings;
  onSaved: (scope: string, key: string) => void;
  support: ProductKnowledgeSupport;
}): JSX.Element {
  const entries = Object.entries(node.settings);
  return (
    <div className="scope-card">
      <h3>{title}</h3>
      {node.using_platform_defaults ? (
        <EmptyState title="Using platform defaults." />
      ) : entries.length === 0 ? (
        <EmptyState title="Nothing configured yet at this scope." />
      ) : (
        <dl>
          {entries.map(([settingKey, value]) => (
            <div key={settingKey}>
              <dt>{settingKey}</dt>
              <dd>{JSON.stringify(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      <SettingForm scope={node.scope} onSaved={onSaved} />
      {/* Provider settings are team-scope-unsupported (tenancy.settings). */}
      {!node.scope.startsWith("team:") && (
        <ProductKnowledgePanel node={node} support={support} onSaved={onSaved} />
      )}
      {/* The classifier is org scope only (the API refuses every other scope). */}
      {node.scope.startsWith("org:") && (
        <ClassifierPanel node={node} support={support} onSaved={onSaved} />
      )}
    </div>
  );
}

function scopeNamesOf(tree: ConfigTree | null): Record<string, string> {
  const names: Record<string, string> = { platform: "platform" };
  for (const org of tree?.orgs ?? []) {
    names[org.scope] = `org ${org.name}`;
    for (const repo of org.repos) names[repo.scope] = `repo ${repo.name}`;
  }
  return names;
}

// Unit E (US-A12, US-A13): org → repo → team configuration tree, per
// docs/plans/plan-deliver-admin-gui.md's Phase 4 section.
export function ConfigScreen(): JSX.Element {
  const fetcher = useCallback(() => api.getConfig(), []);
  const { state, reload } = useAsyncData(fetcher);
  // Independent of the tree fetch so a credentials failure never blanks the forms.
  const credentialsFetcher = useCallback(() => api.getProductKnowledgeCredentials(), []);
  const { state: credentials, reload: reloadCredentials } = useAsyncData(credentialsFetcher);
  const { notice, showSuccess, dismiss } = useNotice();
  const tree = state.status === "success" ? state.data : null;
  const support: ProductKnowledgeSupport = useMemo(
    () => ({
      credentials,
      onRetryCredentials: reloadCredentials,
      scopeNames: scopeNamesOf(tree),
    }),
    [credentials, reloadCredentials, tree]
  );

  function handleSaved(_scope: string, key: string): void {
    showSuccess(`Saved ${key}.`);
    reload();
  }

  return (
    <div className="screen">
      <h1>Org / repo / team configuration</h1>
      <Notice notice={notice} onDismiss={dismiss} />

      {state.status === "loading" && <LoadingState label="Loading configuration…" />}
      {state.status === "error" && <ErrorState message={state.message} onRetry={reload} />}
      {state.status === "success" && (
        <>
          <section aria-labelledby="platform-heading">
            <h2 id="platform-heading">Platform</h2>
            <ScopeCard
              title="Platform-wide"
              node={{
                id: "platform",
                name: "Platform",
                scope: "platform",
                settings: state.data.platform_settings,
                using_platform_defaults: false,
                effective: state.data.platform_effective,
              }}
              onSaved={handleSaved}
              support={support}
            />
          </section>

          <section aria-labelledby="orgs-heading">
            <h2 id="orgs-heading">Organizations</h2>
            {state.data.orgs.length === 0 ? (
              <EmptyState
                title="No organizations connected yet."
                description="Nothing to configure at the org/repo/team level until an org connects."
              />
            ) : (
              state.data.orgs.map((org) => (
                <div key={org.id} className="org-block">
                  <ScopeCard title={`Org: ${org.name}`} node={org} onSaved={handleSaved} support={support} />
                  {org.repos.map((repo) => (
                    <ScopeCard key={repo.id} title={`Repo: ${repo.name}`} node={repo} onSaved={handleSaved} support={support} />
                  ))}
                  {org.teams.map((team) => (
                    <ScopeCard key={team.id} title={`Team: ${team.name}`} node={team} onSaved={handleSaved} support={support} />
                  ))}
                </div>
              ))
            )}
          </section>
        </>
      )}
    </div>
  );
}
