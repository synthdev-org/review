import { useCallback, useState } from "react";

import * as api from "../api/endpoints";
import { ApiError, isSessionExpiredError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Notice } from "../components/Notice";
import { ErrorState, LoadingState } from "../components/StateViews";
import { useAsyncData } from "../hooks/useAsyncData";
import { useNotice } from "../hooks/useNotice";
import { MODEL_ROUTING_STAGES } from "../api/types";

// US-A14, US-A15: effective model per stage + its source
// (org/platform/static default). The write form
// is Admin-only (isAdmin gate) — a Viewer sees the table with no edit
// controls at all.
export function ModelRoutingScreen(): JSX.Element {
  const { isAdmin, handleSessionExpired } = useAuth();
  const [orgId, setOrgId] = useState("");
  const [formStage, setFormStage] = useState<string>(MODEL_ROUTING_STAGES[0]);
  const [formOrgId, setFormOrgId] = useState("");
  const [formModelName, setFormModelName] = useState("");
  const [saving, setSaving] = useState(false);
  const { notice, showSuccess, showError, dismiss } = useNotice();

  const fetcher = useCallback(() => api.getModelRouting(orgId || undefined), [orgId]);
  const { state, reload } = useAsyncData(fetcher, [orgId]);

  async function handleSubmit(): Promise<void> {
    setSaving(true);
    try {
      await api.setModelRouting(formStage, formModelName, formOrgId || null);
      showSuccess(`Updated ${formStage} routing.`);
      setFormModelName("");
      reload();
    } catch (error) {
      if (isSessionExpiredError(error)) {
        handleSessionExpired();
        return;
      }
      showError(error instanceof ApiError ? error.detail : "save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="screen">
      <h1>Model routing</h1>
      <Notice notice={notice} onDismiss={dismiss} />

      <div className="filter-bar">
        <label htmlFor="routing-org-filter">Org id (optional)</label>
        <input
          id="routing-org-filter"
          type="text"
          value={orgId}
          onChange={(event) => { setOrgId(event.target.value); }}
          placeholder="leave blank for platform-wide"
        />
      </div>

      {state.status === "loading" && <LoadingState label="Loading model routing…" />}
      {state.status === "error" && <ErrorState message={state.message} onRetry={reload} />}
      {state.status === "success" && (
        <table>
          <caption className="sr-only">Effective model per review stage</caption>
          <thead>
            <tr>
              <th scope="col">Stage</th>
              <th scope="col">Effective model</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {state.data.stages.map((stage) => (
              <tr key={stage.stage}>
                <td>{stage.stage}</td>
                <td>{stage.model_name}</td>
                <td>{stage.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {isAdmin && (
        <div className="setting-form">
          <h2>Set model routing</h2>
          <label htmlFor="form-stage">Stage</label>
          <select
            id="form-stage"
            value={formStage}
            onChange={(event) => { setFormStage(event.target.value); }}
            disabled={saving}
          >
            {MODEL_ROUTING_STAGES.map((stage) => (
              <option key={stage} value={stage}>
                {stage}
              </option>
            ))}
          </select>

          <label htmlFor="form-org-id">Org id (blank = platform-wide)</label>
          <input
            id="form-org-id"
            type="text"
            value={formOrgId}
            onChange={(event) => { setFormOrgId(event.target.value); }}
            disabled={saving}
          />

          <label htmlFor="form-model-name">Model name</label>
          <input
            id="form-model-name"
            type="text"
            required
            value={formModelName}
            onChange={(event) => { setFormModelName(event.target.value); }}
            disabled={saving}
          />

          <button
            type="button"
            onClick={() => void handleSubmit()}
            disabled={saving || formModelName.trim() === ""}
          >
            {saving ? "Saving…" : "Save routing"}
          </button>
        </div>
      )}
    </div>
  );
}
