import { useEffect, useRef, useState } from "react";

import * as api from "../api/endpoints";
import { ApiError, isSessionExpiredError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ErrorState } from "../components/StateViews";
import type { AsyncState } from "../hooks/useAsyncData";
import {
  PRODUCT_KNOWLEDGE_PROVIDER_KEY,
  type ProductKnowledgeCredential,
  type ScopeSettings,
  type TestConnectionResult,
} from "../api/types";
import { PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE } from "./productKnowledgeCopy";

// Unit H (US-PK8/PK10/PK15): the optional external product-knowledge provider
// for one platform/org/repo scope. Plan: plan-deliver-product-knowledge.md
// Phase 8.

type FieldName =
  | "enabled"
  | "display_name"
  | "endpoint_url"
  | "credential_ref"
  | "allow_public_repos"
  | "issue_key_prefixes";
const FIELD_ORDER: FieldName[] = [
  "enabled",
  "display_name",
  "endpoint_url",
  "credential_ref",
  "allow_public_repos",
  "issue_key_prefixes",
];

interface FormState {
  enabled: boolean;
  display_name: string;
  endpoint_url: string;
  credential_ref: string; // "" = None (unauthenticated)
  allow_public_repos: boolean;
  issue_key_prefixes: string; // comma-separated
}

const DEFAULT_FORM: FormState = {
  enabled: true,
  display_name: "",
  endpoint_url: "",
  credential_ref: "",
  allow_public_repos: false,
  issue_key_prefixes: "",
};

function fromSaved(saved: Record<string, unknown> | undefined): FormState {
  if (!saved) return DEFAULT_FORM;
  const prefixes = Array.isArray(saved.issue_key_prefixes) ? saved.issue_key_prefixes : [];
  return {
    enabled: saved.enabled !== false,
    display_name: typeof saved.display_name === "string" ? saved.display_name : "",
    endpoint_url: typeof saved.endpoint_url === "string" ? saved.endpoint_url : "",
    credential_ref: typeof saved.credential_ref === "string" ? saved.credential_ref : "",
    allow_public_repos: saved.allow_public_repos === true,
    issue_key_prefixes: prefixes.filter((p): p is string => typeof p === "string").join(", "),
  };
}

function toValue(form: FormState): Record<string, unknown> {
  return {
    enabled: form.enabled,
    display_name: form.display_name.trim(),
    endpoint_url: form.endpoint_url.trim(),
    credential_ref: form.credential_ref === "" ? null : form.credential_ref,
    allow_public_repos: form.allow_public_repos,
    issue_key_prefixes: form.issue_key_prefixes
      .split(",")
      .map((p) => p.trim())
      .filter((p) => p !== ""),
  };
}

const UNAUTHENTICATED_HOST_COPY =
  "This host isn't allowed without a credential. Choose a credential bound to this host, or ask your operator to add the host to the endpoint allow-list.";

// 422 `detail` is either a write-time code (credential binding, team scope)
// or the registry's "<field>: <message>; ..." string.
function mapFieldErrors(detail: string): Partial<Record<FieldName, string>> {
  const errors: Partial<Record<FieldName, string>> = {};
  if (detail === "unauthenticated_host_not_allowed") {
    errors.endpoint_url = UNAUTHENTICATED_HOST_COPY;
    return errors;
  }
  if (CREDENTIAL_CODES.has(detail)) {
    errors.credential_ref = detail;
    return errors;
  }
  for (const part of detail.split("; ")) {
    const parsed = parseFieldMessage(part);
    if (parsed) errors[parsed.field] = errors[parsed.field] ?? parsed.message;
  }
  return errors;
}

const CREDENTIAL_CODES = new Set([
  "unknown_credential_ref",
  "credential_host_not_bound",
  "credential_map_unavailable",
]);

// One "<field>[.<n>]: <message>" segment of the registry's 422 detail.
function parseFieldMessage(part: string): { field: FieldName; message: string } | null {
  const match = /^(\w+)(?:\.\d+)?: (.*)$/.exec(part);
  if (!match || !FIELD_ORDER.includes(match[1] as FieldName)) return null;
  return { field: match[1] as FieldName, message: match[2] };
}

// Failure of the test request itself, mapped to what the panel should do.
type TestFailure =
  | { kind: "rate_limited" }
  | { kind: "invalid" }
  | { kind: "error"; message: string };

function classifyTestFailure(error: unknown): TestFailure {
  if (error instanceof ApiError && error.status === 429) return { kind: "rate_limited" };
  if (error instanceof ApiError && error.status === 422) return { kind: "invalid" };
  return { kind: "error", message: error instanceof ApiError ? error.detail : "test failed" };
}

type TestState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "result"; result: TestConnectionResult }
  | { kind: "rate_limited" }
  | { kind: "disabled"; message: string }
  // Not a probe status: a transport/server failure of the test request itself.
  | { kind: "error"; message: string };

const OK_STATUSES = new Set(["ok", "deprecated_version"]);

export interface ProductKnowledgeSupport {
  credentials: AsyncState<ProductKnowledgeCredential[]>;
  onRetryCredentials: () => void;
  scopeNames: Record<string, string>;
}

export function ProductKnowledgePanel({
  node,
  support,
  onSaved,
}: {
  node: ScopeSettings;
  support: ProductKnowledgeSupport;
  onSaved: (scope: string, key: string) => void;
}): JSX.Element {
  const { isAdmin, handleSessionExpired } = useAuth();
  const saved = node.settings[PRODUCT_KNOWLEDGE_PROVIDER_KEY];
  const savedKey = JSON.stringify(saved ?? null);
  const [form, setForm] = useState<FormState>(() => fromSaved(saved));
  const [saving, setSaving] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<FieldName, string>>>({});
  const [generalError, setGeneralError] = useState<string | null>(null);
  const [testState, setTestState] = useState<TestState>({ kind: "idle" });
  const [focusRequest, setFocusRequest] = useState<{ field: FieldName } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const fieldRefs = useRef<Partial<Record<FieldName, HTMLElement | null>>>({});
  const id = `pk-${node.scope}`;

  const dirty = JSON.stringify(toValue(form)) !== JSON.stringify(toValue(fromSaved(saved)));

  // Re-sync the form when the saved value changes underneath it (after save + reload).
  useEffect(() => {
    setForm(fromSaved(saved));
    setFieldErrors({});
    setGeneralError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);

  // The test result is only ever about the values currently saved: dirtying
  // the form or changing scope aborts any in-flight probe and clears it.
  useEffect(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setTestState({ kind: "idle" });
  }, [dirty, node.scope, savedKey]);
  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (focusRequest) fieldRefs.current[focusRequest.field]?.focus();
  }, [focusRequest]);

  function update<K extends keyof FormState>(field: K, value: FormState[K]): void {
    setForm((current) => ({ ...current, [field]: value }));
    setFieldErrors((current) => {
      const { [field as FieldName]: _removed, ...rest } = current;
      return rest;
    });
  }

  function applyApiError(error: unknown, fallback: string): Partial<Record<FieldName, string>> | null {
    if (error instanceof ApiError && error.status === 422) {
      const mapped = mapFieldErrors(error.detail);
      if (Object.keys(mapped).length > 0) return mapped;
      setGeneralError(error.detail);
      return null;
    }
    setGeneralError(error instanceof ApiError ? error.detail : fallback);
    return null;
  }

  function showFieldErrors(mapped: Partial<Record<FieldName, string>> | null): void {
    if (!mapped) return;
    setFieldErrors(mapped);
    const first = FIELD_ORDER.find((f) => mapped[f] !== undefined);
    if (first) setFocusRequest({ field: first });
  }

  async function handleSave(): Promise<void> {
    setSaving(true);
    setGeneralError(null);
    setFieldErrors({});
    try {
      await api.setConfig(node.scope, PRODUCT_KNOWLEDGE_PROVIDER_KEY, toValue(form));
      onSaved(node.scope, PRODUCT_KNOWLEDGE_PROVIDER_KEY);
    } catch (error) {
      if (isSessionExpiredError(error)) {
        handleSessionExpired();
        return;
      }
      showFieldErrors(applyApiError(error, "save failed"));
    } finally {
      setSaving(false);
    }
  }

  async function handleTest(): Promise<void> {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setTestState({ kind: "pending" });
    setGeneralError(null);
    try {
      const body = toValue(form);
      const result = await api.testProductKnowledgeConnection(
        body.endpoint_url as string,
        body.credential_ref as string | null,
        controller.signal
      );
      if (controller.signal.aborted) return;
      setTestState(
        result.status === "disabled"
          ? { kind: "disabled", message: result.message }
          : { kind: "result", result }
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      if (isSessionExpiredError(error)) {
        handleSessionExpired();
        return;
      }
      const failure = classifyTestFailure(error);
      if (failure.kind === "invalid") {
        setTestState({ kind: "idle" });
        showFieldErrors(applyApiError(error, "test failed"));
      } else {
        setTestState(failure);
      }
    }
  }

  // --- effective preview (server-computed; never re-derived here) ---
  const effective = node.effective?.[PRODUCT_KNOWLEDGE_PROVIDER_KEY];
  let preview: string;
  if (!effective) {
    preview = "Not configured — reviews use the codebase only.";
  } else {
    const where =
      effective.source === node.scope
        ? "set at this scope"
        : `inherited from ${support.scopeNames[effective.source] ?? effective.source}`;
    const name = typeof effective.value.display_name === "string" ? effective.value.display_name : "provider";
    preview =
      effective.value.enabled === false
        ? `Disabled (${where}) — reviews use the codebase only.`
        : `Using: ${name} (${where})`;
  }

  // --- credentials dropdown ---
  const creds = support.credentials;
  const loadedCreds = creds.status === "success" ? creds.data : [];
  const storedRefMissing =
    creds.status === "success" && form.credential_ref !== "" &&
    !loadedCreds.some((c) => c.ref === form.credential_ref);

  const readOnly = !isAdmin;
  const disabled = readOnly || saving;
  const errorId = (f: FieldName): string => `${id}-${f}-error`;
  const invalid = (f: FieldName): { "aria-invalid": boolean; "aria-describedby"?: string } => ({
    "aria-invalid": fieldErrors[f] !== undefined,
    ...(fieldErrors[f] !== undefined ? { "aria-describedby": errorId(f) } : {}),
  });
  const fieldError = (f: FieldName): JSX.Element | null =>
    fieldErrors[f] !== undefined ? (
      <p id={errorId(f)} className="form-error">
        {fieldErrors[f]}
      </p>
    ) : null;

  const testDisabled = readOnly || dirty || form.endpoint_url.trim() === "" || testState.kind === "pending";
  const testFailed =
    testState.kind === "result" && !OK_STATUSES.has(testState.result.status);

  return (
    <div className="setting-form product-knowledge-panel" aria-labelledby={`${id}-heading`} role="group">
      <h4 id={`${id}-heading`}>Product knowledge</h4>
      <p data-testid="pk-effective-preview" className="hint">
        {preview}
      </p>
      {readOnly && <p className="hint">Viewers can see configuration but not change it.</p>}

      <label>
        <input
          type="checkbox"
          ref={(el) => {
            fieldRefs.current.enabled = el;
          }}
          checked={form.enabled}
          onChange={(event) => update("enabled", event.target.checked)}
          disabled={disabled}
          {...invalid("enabled")}
        />
        Enabled
      </label>
      {fieldError("enabled")}

      <label htmlFor={`${id}-display_name`}>Display name</label>
      <input
        id={`${id}-display_name`}
        ref={(el) => {
          fieldRefs.current.display_name = el;
        }}
        type="text"
        value={form.display_name}
        maxLength={40}
        onChange={(event) => update("display_name", event.target.value)}
        disabled={disabled}
        {...invalid("display_name")}
      />
      {fieldError("display_name")}

      <p id={`${id}-disclosure`} className="hint">
        {PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE}
      </p>
      <label htmlFor={`${id}-endpoint_url`}>Endpoint URL</label>
      <input
        id={`${id}-endpoint_url`}
        ref={(el) => {
          fieldRefs.current.endpoint_url = el;
        }}
        type="url"
        value={form.endpoint_url}
        maxLength={512}
        onChange={(event) => update("endpoint_url", event.target.value)}
        disabled={disabled}
        aria-invalid={fieldErrors.endpoint_url !== undefined}
        aria-describedby={
          fieldErrors.endpoint_url !== undefined
            ? `${id}-disclosure ${errorId("endpoint_url")}`
            : `${id}-disclosure`
        }
      />
      {fieldError("endpoint_url")}

      <label htmlFor={`${id}-credential_ref`}>Credential</label>
      <select
        id={`${id}-credential_ref`}
        ref={(el) => {
          fieldRefs.current.credential_ref = el;
        }}
        value={form.credential_ref}
        onChange={(event) => update("credential_ref", event.target.value)}
        disabled={disabled}
        {...invalid("credential_ref")}
      >
        <option value="">None (unauthenticated)</option>
        {loadedCreds.map((credential) => (
          <option key={credential.ref} value={credential.ref}>
            {credential.allowed_hosts
              ? `${credential.ref} (bound to ${credential.allowed_hosts.join(", ")})`
              : credential.ref}
          </option>
        ))}
        {storedRefMissing && (
          <option value={form.credential_ref} disabled>
            Unknown credential `{form.credential_ref}` (removed by operator)
          </option>
        )}
        {creds.status !== "success" && form.credential_ref !== "" && (
          <option value={form.credential_ref}>{form.credential_ref}</option>
        )}
      </select>
      {creds.status === "loading" && (
        <p className="hint" role="status">
          Loading credentials&hellip;
        </p>
      )}
      {creds.status === "error" && (
        <ErrorState
          title="Couldn&rsquo;t load credentials"
          message={creds.message}
          onRetry={support.onRetryCredentials}
        />
      )}
      {fieldError("credential_ref")}

      <label>
        <input
          type="checkbox"
          ref={(el) => {
            fieldRefs.current.allow_public_repos = el;
          }}
          checked={form.allow_public_repos}
          onChange={(event) => update("allow_public_repos", event.target.checked)}
          disabled={disabled}
          aria-describedby={`${id}-public-hint`}
          {...invalid("allow_public_repos")}
        />
        Also consult for public repositories and fork pull requests
      </label>
      <p id={`${id}-public-hint`} className="hint">
        Off by default: provider content could be paraphrased into a comment anyone can read.
      </p>
      {fieldError("allow_public_repos")}

      <label htmlFor={`${id}-issue_key_prefixes`}>Issue key prefixes (optional, comma-separated)</label>
      <input
        id={`${id}-issue_key_prefixes`}
        ref={(el) => {
          fieldRefs.current.issue_key_prefixes = el;
        }}
        type="text"
        value={form.issue_key_prefixes}
        placeholder="HR, ENG"
        onChange={(event) => update("issue_key_prefixes", event.target.value)}
        disabled={disabled}
        {...invalid("issue_key_prefixes")}
      />
      {fieldError("issue_key_prefixes")}

      <button type="button" onClick={() => void handleSave()} disabled={disabled}>
        {saving ? "Saving…" : "Save provider"}
      </button>
      {generalError && (
        <p role="alert" className="form-error">
          {generalError}
        </p>
      )}

      <button
        type="button"
        onClick={() => void handleTest()}
        disabled={testDisabled}
        aria-disabled={testDisabled}
      >
        {testState.kind === "pending"
          ? "Testing…"
          : dirty
            ? "Save changes to test connection"
            : "Test connection"}
      </button>
      <p className="hint">A test can take up to 10 s.</p>
      {/* Mounted before any result arrives so a screen reader announces it. */}
      <div role="status" aria-live="polite" className="pk-test-result" style={{ minHeight: "3rem" }}>
        {testState.kind === "pending" && <span>Testing connection&hellip;</span>}
        {testState.kind === "disabled" && (
          <span>
            <span className="badge badge--warning">&#9888; disabled</span> {testState.message}
          </span>
        )}
        {testState.kind === "result" && !testFailed && (
          <span>
            <span className="badge">&#10003; {testState.result.status}</span> {testState.result.message}
            {testState.result.negotiated_version ? ` (contract v${testState.result.negotiated_version}` : ""}
            {testState.result.negotiated_version && testState.result.latency_ms != null
              ? `, ${testState.result.latency_ms} ms)`
              : testState.result.negotiated_version
                ? ")"
                : ""}
          </span>
        )}
      </div>
      {testFailed && testState.kind === "result" && (
        <p role="alert" className="form-error">
          <span className="badge badge--warning">&#10007; {testState.result.status}</span>{" "}
          {testState.result.message}
        </p>
      )}
      {testState.kind === "rate_limited" && (
        <p role="alert" className="form-error">
          <span className="badge badge--warning">&#10007; rate limited</span> Too many connection tests
          &mdash; wait a minute and try again.
        </p>
      )}
      {testState.kind === "error" && (
        <p role="alert" className="form-error">
          {testState.message}
        </p>
      )}
    </div>
  );
}
