import { AlertCircle, CheckCircle2, Clock, MinusCircle } from "lucide-react";
import type { StageEvent } from "../types/graphrag";
import { formatMs } from "../utils/format";

interface StageRailProps {
  title?: string;
  events: StageEvent[];
  isRunning?: boolean;
}

interface MergedStage {
  stage: string;
  label: string;
  service: string;
  status: "running" | "completed" | "failed" | "skipped";
  duration_ms?: number;
  payload?: Record<string, unknown>;
  error_message?: string;
  reason?: string;
}

const BUSINESS_STAGE_LABELS: Record<string, string> = {
  query_prep: "Prepare Investigation Scope",
  model_armor_input: "Screen Prompt for Safety",
  embed_query: "Interpret Business Question",
  vector_seed: "Locate Starting Entities",
  gql_traverse: "Trace Multi-Tier Risk Chains",
  spanner_traverse: "Trace Real-Time Risk Chains",
  retrieve_chunks: "Search Filing Excerpts",
  rerank: "Prioritize Highest-Relevance Evidence",
  adk_coordinate: "Run Autonomous Multi-Tool Investigation",
  synthesize: "Draft Cited Executive Brief",
  verify_provenance: "Audit Every Source Citation",
  evaluate: "Evaluate Groundedness & Answer Quality",
  model_armor_output: "Verify Output Safety",
};

const BUSINESS_SERVICE_LABELS: Record<string, string> = {
  bigquery_property_graph: "BigQuery Property Graph",
  bigquery_vector: "BigQuery Vector Search",
  vertex_embeddings: "Vertex AI Embeddings",
  vertex_gemini: "Vertex AI Gemini",
  discovery_engine_rerank: "Discovery Engine Semantic Reranker",
  google_adk: "Google ADK 2.15 Agent",
  model_armor: "Google Cloud Model Armor",
  spanner_graph: "Spanner Graph",
  provenance_verifier: "Deterministic Citation Auditor",
  vertex_genai_eval: "Vertex AI GenAI Evaluation",
  vertex_eval: "Vertex AI GenAI Evaluation",
  anthropic_vertex: "Claude Cross-Family Judge (Vertex)",
  claude: "Claude Cross-Family Judge (Vertex)",
  replay_fixture: "Verified Replay Cache",
};

function friendlyStageLabel(stage: string, fallback: string): string {
  return BUSINESS_STAGE_LABELS[stage] ?? fallback;
}

function friendlyServiceLabel(service: string): string {
  return BUSINESS_SERVICE_LABELS[service] ?? service;
}

function mergeStageEvents(events: StageEvent[]): MergedStage[] {
  const order: string[] = [];
  const map = new Map<string, MergedStage>();

  for (const ev of events) {
    if (!map.has(ev.stage)) {
      order.push(ev.stage);
      map.set(ev.stage, {
        stage: ev.stage,
        label: friendlyStageLabel(ev.stage, ev.label || ev.stage),
        service: ev.service || ev.service_used || "runtime",
        status: "running",
      });
    }
    const item = map.get(ev.stage)!;
    if (ev.event_type === "stage_started") {
      item.label = friendlyStageLabel(ev.stage, ev.label || item.label);
      item.service = ev.service || item.service;
      item.status = "running";
    } else if (ev.event_type === "stage_completed") {
      item.status = "completed";
      item.service = ev.service_used || item.service;
      item.duration_ms =
        typeof ev.payload?.recorded_duration_ms === "number"
          ? ev.payload.recorded_duration_ms
          : ev.duration_ms;
      item.payload = ev.payload;
    } else if (ev.event_type === "stage_failed") {
      item.status = "failed";
      item.duration_ms = ev.duration_ms;
      item.error_message = ev.error_message || ev.error_type || "Step failed";
    } else if (ev.event_type === "stage_skipped") {
      item.status = "skipped";
      item.reason = ev.reason;
    }
  }
  return order.map((k) => map.get(k)!);
}

export function StageRail({
  title = "Live Investigation Steps",
  events,
  isRunning,
}: StageRailProps) {
  const stages = mergeStageEvents(events);

  return (
    <section className="panel" aria-label={title}>
      <div className="panel-header">
        <h2 className="panel-title">{title}</h2>
        <span className={`badge ${isRunning ? "badge-upload" : ""}`}>
          {isRunning ? "Running live…" : `${stages.length} steps`}
        </span>
      </div>

      <div className="panel-body" aria-live="polite">
        {stages.length === 0 ? (
          <div className="muted-copy">
            Click <strong>Run Investigation</strong> to watch each step complete in real time —
            from locating connected entities and tracing multi-tier risk chains to auditing every
            citation against source filings.
          </div>
        ) : (
          <ol className="stage-list">
            {stages.map((st) => {
              const gqlOrSql =
                typeof st.payload?.gql === "string"
                  ? st.payload.gql
                  : typeof st.payload?.sql === "string"
                    ? st.payload.sql
                    : null;

              return (
                <li
                  key={st.stage}
                  className={`stage-item${st.status === "failed" ? " failed" : ""}`}
                >
                  <div className="row-between">
                    <div className="row-wrap gap-6">
                      {st.status === "completed" && (
                        <CheckCircle2
                          size={14}
                          color="var(--status-verified-ink)"
                          aria-hidden="true"
                        />
                      )}
                      {st.status === "failed" && (
                        <AlertCircle
                          size={14}
                          color="var(--status-danger-ink)"
                          aria-hidden="true"
                        />
                      )}
                      {st.status === "skipped" && (
                        <MinusCircle
                          size={14}
                          color="var(--ink-muted)"
                          aria-hidden="true"
                        />
                      )}
                      {st.status === "running" && (
                        <Clock
                          size={14}
                          color="var(--accent-primary)"
                          aria-hidden="true"
                        />
                      )}
                      <strong className="stage-label">{st.label}</strong>
                    </div>
                    <span className="mono stage-duration">
                      {st.status === "skipped" ? "skipped" : formatMs(st.duration_ms)}
                    </span>
                  </div>

                  <div className="row-wrap stage-meta">
                    <span>{friendlyServiceLabel(st.service)}</span>
                    {typeof st.payload?.paths_found === "number" && (
                      <span className="badge badge-verified">
                        {st.payload.paths_found} risk chains found
                      </span>
                    )}
                    {typeof st.payload?.rows_returned === "number" && (
                      <span className="badge">{st.payload.rows_returned} records</span>
                    )}
                    {typeof st.payload?.turns === "number" && (
                      <span className="badge badge-upload">
                        {st.payload.turns} agent steps
                      </span>
                    )}
                    {Array.isArray(st.payload?.tool_calls) && (
                      <span className="badge">
                        {st.payload.tool_calls.length} tool checks
                      </span>
                    )}
                    {st.reason && <span>· {st.reason}</span>}
                  </div>

                  {st.error_message && <div className="stage-err">{st.error_message}</div>}

                  {gqlOrSql && (
                    <details className="stage-details">
                      <summary className="stage-summary">
                        Technical detail · View parameterized{" "}
                        {st.payload?.gql ? "ISO GQL" : "BigQuery SQL"}
                      </summary>
                      <pre className="stage-code">{gqlOrSql}</pre>
                    </details>
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </section>
  );
}
