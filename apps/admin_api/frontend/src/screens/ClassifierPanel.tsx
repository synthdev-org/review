import { useEffect, useState } from "react";

import * as api from "../api/endpoints";
import { ApiError, isSessionExpiredError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { CLASSIFIER_PROVIDER_KEY, type ClassifierProbeResult, type ScopeSettings } from "../api/types";
import { CLASSIFIER_EGRESS_DISCLOSURE } from "./classifierCopy";
import type { ProductKnowledgeSupport } from "./ProductKnowledgePanel";

// The optional classifier (spec-classifier US-C2/US-C3, plan Phase 8) for one org.
// The acknowledgement gate here is a convenience: the API refuses `enabled: true`
// without `disclosure_acknowledged`, whatever this form sends.

interface FormState {
  enabled: boolean;
  endpoint_url: string;
  credential_ref: string;
  model: string;
  mode: "shadow" | "active";
  disclosure_acknowledged: boolean;
}

const DEFAULT_FORM: FormState = {
  enabled: false,
  endpoint_url: "",
  credential_ref: "",
  model: "",
  mode: "shadow",
  disclosure_acknowledged: false,
};

function fromSaved(saved: Record<string, unknown> | undefined): FormState {
  if (!saved) return DEFAULT_FORM;
  const text = (v: unknown): string => (typeof v === "string" ? v : "");
  return {
    enabled: saved.enabled === true,
    endpoint_url: text(saved.endpoint_url),
    credential_ref: text(saved.credential_ref),
    model: text(saved.model),
    mode: saved.mode === "active" ? "active" : "shadow",
    disclosure_acknowledged: saved.disclosure_acknowledged === true,
  };
}

function toValue(form: FormState): Record<string, unknown> {
  return {
    ...form,
    backend: "typesafe",
    endpoint_url: form.endpoint_url.trim(),
    model: form.model.trim(),
  };
}

export function ClassifierPanel({
  node,
  support,
  onSaved,
}: {
  node: ScopeSettings;
  support: ProductKnowledgeSupport;
  onSaved: (...saved: [scope: string, key: string]) => void;
}): JSX.Element {
  const { isAdmin, handleSessionExpired } = useAuth();
  const saved = new Map(Object.entries(node.settings)).get(CLASSIFIER_PROVIDER_KEY);
  const savedKey = JSON.stringify(saved ?? null);
  const [form, setForm] = useState<FormState>(() => fromSaved(saved));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<ClassifierProbeResult | "pending" | null>(null);
  const id = `classifier-${node.scope}`;
  const dirty = JSON.stringify(toValue(form)) !== JSON.stringify(toValue(fromSaved(saved)));

  useEffect(() => {
    setForm(fromSaved(saved));
    setError(null);
    setProbe(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);

  function update<K extends keyof FormState>(field: K, value: FormState[K]): void {
    setForm((current) => ({ ...current, [field]: value }));
    setProbe(null);
  }

  async function handleSave(): Promise<void> {
    setSaving(true);
    setError(null);
    try {
      await api.setConfig(node.scope, CLASSIFIER_PROVIDER_KEY, toValue(form));
      onSaved(node.scope, CLASSIFIER_PROVIDER_KEY);
    } catch (e) {
      if (isSessionExpiredError(e)) {
        handleSessionExpired();
        return;
      }
      setError(e instanceof ApiError ? e.detail : "save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest(): Promise<void> {
    setProbe("pending");
    setError(null);
    try {
      setProbe(
        await api.testClassifierConnection({
          org_id: node.scope.slice("org:".length),
          endpoint_url: form.endpoint_url.trim(),
          credential_ref: form.credential_ref,
          model: form.model.trim(),
        })
      );
    } catch (e) {
      setProbe(null);
      if (isSessionExpiredError(e)) {
        handleSessionExpired();
        return;
      }
      setError(e instanceof ApiError ? e.detail : "test failed");
    }
  }

  const creds = support.credentials.status === "success" ? support.credentials.data : [];
  const readOnly = !isAdmin;
  const disabled = readOnly || saving;
  // Opting out never needs the acknowledgement; opting in does.
  // With no saved row, a disabled form is blank and the API rejects it: nothing to disable yet.
  const saveBlocked =
    disabled ||
    (form.enabled && !form.disclosure_acknowledged) ||
    (!form.enabled && saved === undefined);
  const testDisabled = readOnly || dirty || form.endpoint_url.trim() === "" || probe === "pending";

  return (
    <div className="setting-form classifier-panel" role="group" aria-labelledby={`${id}-heading`}>
      <h4 id={`${id}-heading`}>Classifier</h4>
      {readOnly && <p className="hint">Viewers can see configuration but not change it.</p>}

      <p id={`${id}-disclosure`} className="hint">
        {CLASSIFIER_EGRESS_DISCLOSURE}
      </p>
      <label>
        <input
          type="checkbox"
          checked={form.disclosure_acknowledged}
          onChange={(e) => {
            update("disclosure_acknowledged", e.target.checked);
          }}
          disabled={disabled}
          aria-describedby={`${id}-disclosure`}
        />
        I have read and accept this disclosure
      </label>

      <label>
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => {
            update("enabled", e.target.checked);
          }}
          disabled={disabled}
        />
        Enabled
      </label>
      {form.enabled && !form.disclosure_acknowledged && (
        <p className="hint">Accept the disclosure to enable the classifier.</p>
      )}

      <label htmlFor={`${id}-endpoint_url`}>Endpoint URL (https)</label>
      <input
        id={`${id}-endpoint_url`}
        type="url"
        value={form.endpoint_url}
        maxLength={512}
        onChange={(e) => {
          update("endpoint_url", e.target.value);
        }}
        disabled={disabled}
      />

      <label htmlFor={`${id}-credential_ref`}>Credential</label>
      <select
        id={`${id}-credential_ref`}
        value={form.credential_ref}
        onChange={(e) => {
          update("credential_ref", e.target.value);
        }}
        disabled={disabled}
      >
        <option value="">Choose a credential</option>
        {creds.map((c) => (
          <option key={c.ref} value={c.ref}>
            {c.allowed_hosts ? `${c.ref} (bound to ${c.allowed_hosts.join(", ")})` : c.ref}
          </option>
        ))}
        {form.credential_ref !== "" && !creds.some((c) => c.ref === form.credential_ref) && (
          <option value={form.credential_ref}>{form.credential_ref}</option>
        )}
      </select>

      <label htmlFor={`${id}-model`}>Model</label>
      <input
        id={`${id}-model`}
        type="text"
        value={form.model}
        maxLength={64}
        onChange={(e) => {
          update("model", e.target.value);
        }}
        disabled={disabled}
      />

      <label htmlFor={`${id}-mode`}>Mode</label>
      <select
        id={`${id}-mode`}
        value={form.mode}
        onChange={(e) => {
          update("mode", e.target.value === "active" ? "active" : "shadow");
        }}
        disabled={disabled}
      >
        <option value="shadow">Shadow (compare only, the LLM judge decides)</option>
        <option value="active">Active (the classifier decides clear cases)</option>
      </select>

      <button
        type="button"
        onClick={() => {
          void handleSave();
        }}
        disabled={saveBlocked}
      >
        {saving ? "Saving…" : "Save classifier"}
      </button>
      {error && (
        <p role="alert" className="form-error">
          {error}
        </p>
      )}

      <button
        type="button"
        onClick={() => {
          void handleTest();
        }}
        disabled={testDisabled}
      >
        {probe === "pending" ? "Testing…" : dirty ? "Save changes to test connection" : "Test connection"}
      </button>
      <div role="status" aria-live="polite" style={{ minHeight: "2rem" }}>
        {probe !== null && probe !== "pending" && (
          <span>
            {probe.status === "ok" ? "✓" : "⚠"} {probe.status}: {probe.message}
            {probe.status === "ok" && probe.latency_ms != null ? ` (${probe.latency_ms} ms)` : ""}
          </span>
        )}
      </div>
    </div>
  );
}
