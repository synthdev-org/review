import { useCallback } from "react";

import * as api from "../api/endpoints";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { useAsyncData } from "../hooks/useAsyncData";

// US-A17 (stats) + US-A18 (usage disclosure) — both read-only, Viewer
// parity, per this phase's own plan section grouping them on one screen
// ("dashboard (stats + usage)"). Each half fetches independently so a
// slow/unreachable Prometheus (stats) never blocks the usage table, which
// reads Postgres only.
export function DashboardScreen(): JSX.Element {
  const statsFetch = useCallback(() => api.getStats(), []);
  const usageFetch = useCallback(() => api.getUsage(), []);
  const stats = useAsyncData(statsFetch);
  const usage = useAsyncData(usageFetch);

  return (
    <div className="screen">
      <h1>Dashboard</h1>

      <section aria-labelledby="stats-heading">
        <h2 id="stats-heading">Pipeline stats</h2>
        {stats.state.status === "loading" && <LoadingState label="Loading pipeline stats…" />}
        {stats.state.status === "error" && (
          <ErrorState message={stats.state.message} onRetry={stats.reload} />
        )}
        {stats.state.status === "success" && (
          <div className="stats-grid">
            <div className="stat-card">
              <h3>Queue depth</h3>
              <p className="stat-card__value">{stats.state.data.queue_depth.depth}</p>
              <p className="stat-card__window">{stats.state.data.queue_depth.window}</p>
            </div>
            <div className="stat-card">
              <h3>Verification funnel</h3>
              <dl>
                <dt>Judged</dt>
                <dd>{stats.state.data.verification_funnel.judged}</dd>
                <dt>Rejected</dt>
                <dd>{stats.state.data.verification_funnel.rejected}</dd>
                <dt>Unmatched</dt>
                <dd>{stats.state.data.verification_funnel.unmatched}</dd>
              </dl>
            </div>
            <div className="stat-card stat-card--wide">
              <h3>LLM token usage</h3>
              {stats.state.data.llm_token_usage.entries.length === 0 ? (
                <EmptyState title="No LLM token usage recorded yet." />
              ) : (
                <table>
                  <caption className="sr-only">LLM token usage by stage, model, and type</caption>
                  <thead>
                    <tr>
                      <th scope="col">Stage</th>
                      <th scope="col">Model</th>
                      <th scope="col">Token type</th>
                      <th scope="col">Tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stats.state.data.llm_token_usage.entries.map((entry) => (
                      <tr key={`${entry.stage}-${entry.model}-${entry.token_type}`}>
                        <td>{entry.stage}</td>
                        <td>{entry.model}</td>
                        <td>{entry.token_type}</td>
                        <td>{entry.tokens}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="stat-card stat-card--wide">
              <h3>Findings by severity</h3>
              {stats.state.data.findings_by_severity.entries.length === 0 ? (
                <EmptyState title="No findings recorded yet." />
              ) : (
                <table>
                  <caption className="sr-only">Findings grouped by severity and source</caption>
                  <thead>
                    <tr>
                      <th scope="col">Severity</th>
                      <th scope="col">Source</th>
                      <th scope="col">Count</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stats.state.data.findings_by_severity.entries.map((entry) => (
                      <tr key={`${entry.severity}-${entry.source}`}>
                        <td>{entry.severity}</td>
                        <td>{entry.source}</td>
                        <td>{entry.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        )}
      </section>

      <section aria-labelledby="usage-heading">
        <h2 id="usage-heading">Free-tier usage</h2>
        {usage.state.status === "loading" && <LoadingState label="Loading usage…" />}
        {usage.state.status === "error" && (
          <ErrorState message={usage.state.message} onRetry={usage.reload} />
        )}
        {usage.state.status === "success" &&
          (usage.state.data.orgs.length === 0 ? (
            <EmptyState
              title="No reviews counted yet today."
              description={`Free-tier guideline: ${usage.state.data.threshold} reviews/day per org.`}
            />
          ) : (
            <table>
              <caption className="sr-only">Per-org usage for the current day</caption>
              <thead>
                <tr>
                  <th scope="col">Org</th>
                  <th scope="col">Day</th>
                  <th scope="col">Count</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {usage.state.data.orgs.map((org) => (
                  <tr key={org.installation_id}>
                    <td>{org.org_name ?? org.org_id ?? org.installation_id}</td>
                    <td>{org.day}</td>
                    <td>{org.count}</td>
                    <td>
                      {org.over_threshold ? (
                        <span className="badge badge--warning">Over guideline</span>
                      ) : (
                        <span className="badge">Within guideline</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
      </section>
    </div>
  );
}
