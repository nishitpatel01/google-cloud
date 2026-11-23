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

function mergeStageEvents(events: StageEvent[]): MergedStage[] {
  const order: string[] = [];
  const map = new Map<string, MergedStage>();

  for (const ev of events) {
    if (!map.has(ev.stage)) {
      order.push(ev.stage);
      map.set(ev.stage, {
        stage: ev.stage,
        label: ev.label || ev.stage,
        service: ev.service || ev.service_used || "runtime",
        status: "running",
      });
    }
    const item = map.get(ev.stage)!;
    if (ev.event_type === "stage_started") {
      item.label = ev.label || item.label;
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
      item.error_message = ev.error_message || ev.error_type || "Stage failed";
    } else if (ev.event_type === "stage_skipped") {
      item.status = "skipped";
      item.reason = ev.reason;
    }
  }
  return order.map((k) => map.get(k)!);
}

export function StageRail({ title = "Measured Pipeline Telemetry", events, isRunning }: StageRailProps) {
  const stages = mergeStageEvents(events);

  return (
    <section className="panel" aria-label={title}>
      <div className="panel-header">
        <h2 className="panel-title">{title}</h2>
        <span className={`badge ${isRunning ? "badge-upload" : ""}`}>
          {isRunning ? "Streaming SSE…" : `${stages.length} stages`}
        </span>
      </div>

      <div className="panel-body" aria-live="polite">
        {stages.length === 0 ? (
          <div className="muted-copy">
            Run an investigation to stream live BigQuery Property Graph ISO GQL, Discovery Engine
            reranking, Vertex AI synthesis, and provenance audit timings.
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
                        <CheckCircle2 size={14} color="#065f46" aria-hidden="true" />
                      )}
                      {st.status === "failed" && (
                        <AlertCircle size={14} color="#991b1b" aria-hidden="true" />
                      )}
                      {st.status === "skipped" && (
                        <MinusCircle size={14} color="#64748b" aria-hidden="true" />
                      )}
                      {st.status === "running" && (
                        <Clock size={14} color="#1a56db" aria-hidden="true" />
                      )}
                      <strong className="stage-label">{st.label}</strong>
                    </div>
                    <span className="mono stage-duration">
                      {st.status === "skipped" ? "skipped" : formatMs(st.duration_ms)}
                    </span>
                  </div>

                  <div className="row-wrap stage-meta">
                    <span>{st.service}</span>
                    {typeof st.payload?.paths_found === "number" && (
                      <span className="badge badge-verified">
                        {st.payload.paths_found} paths
                      </span>
                    )}
                    {typeof st.payload?.rows_returned === "number" && (
                      <span className="badge">{st.payload.rows_returned} rows</span>
                    )}
                    {typeof st.payload?.turns === "number" && (
                      <span className="badge badge-upload">{st.payload.turns} ADK turns</span>
                    )}
                    {Array.isArray(st.payload?.tool_calls) && (
                      <span className="badge">
                        {st.payload.tool_calls.length} tool call(s)
                      </span>
                    )}
                    {st.reason && <span>· {st.reason}</span>}
                  </div>

                  {st.error_message && <div className="stage-err">{st.error_message}</div>}

                  {gqlOrSql && (
                    <details className="stage-details">
                      <summary className="stage-summary">
                        Inspect parameterized {st.payload?.gql ? "ISO GQL" : "BigQuery SQL"}
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
