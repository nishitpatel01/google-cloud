import { AlertTriangle, CheckCircle2 } from "lucide-react";
import type { EngineResultPayload } from "../types/graphrag";
import { formatMs, formatPercent, formatUsd, parseCitations, type CitationKind } from "../utils/format";

interface CitedAnswerProps {
  result: EngineResultPayload;
  onClickCitation?: (kind: CitationKind, id: string) => void;
}

export function CitedAnswer({ result, onClickCitation }: CitedAnswerProps) {
  const segments = parseCitations(result.answer);
  const prov = result.provenance;
  const unverifiedTotal =
    prov.unverified_entity_ids.length +
    prov.unverified_edge_ids.length +
    prov.unverified_chunk_ids.length;
  const verifiedTotal =
    prov.verified_entity_ids.length +
    prov.verified_edge_ids.length +
    prov.verified_chunk_ids.length;
  const unverifiedSet = new Set([
    ...prov.unverified_entity_ids,
    ...prov.unverified_edge_ids,
    ...prov.unverified_chunk_ids,
  ]);

  return (
    <section className="panel" aria-label="Grounded executive contagion brief">
      <div className="panel-header">
        <div className="row-wrap">
          <h2 className="panel-title">Executive Contagion Brief</h2>
          <span
            className={`badge ${unverifiedTotal === 0 ? "badge-verified" : "badge-danger"}`}
          >
            {unverifiedTotal === 0 ? (
              <>
                <CheckCircle2 size={12} aria-hidden="true" />
                Provenance Verified ({verifiedTotal}/{verifiedTotal})
              </>
            ) : (
              <>
                <AlertTriangle size={12} aria-hidden="true" />
                {unverifiedTotal} Unverified Citation(s) Flagged
              </>
            )}
          </span>
        </div>
        <span className="badge mono">{result.synthesis_model}</span>
      </div>

      <div className="panel-body stack-md">
        <div className="kpi-grid">
          <div className="kpi-box">
            <div className="kpi-label">Latency</div>
            <div className="kpi-value">{formatMs(result.total_duration_ms)}</div>
          </div>
          <div className="kpi-box">
            <div className="kpi-label">Engine LLM Cost</div>
            <div className="kpi-value">{formatUsd(result.engine_cost_usd)}</div>
          </div>
          <div className="kpi-box">
            <div className="kpi-label">Paths / Edges</div>
            <div className="kpi-value">
              {result.paths.length} / {result.retrieved_edge_ids.length}
            </div>
          </div>
          {result.recall && (
            <div className="kpi-box">
              <div className="kpi-label">Multi-Hop Recall</div>
              <div className="kpi-value">{formatPercent(result.recall.path_recall)}</div>
            </div>
          )}
        </div>

        <div className="row-wrap">
          <span className="badge">Backend: {result.flags.graph_backend_used}</span>
          <span className={`badge ${result.flags.reranker_used ? "badge-verified" : ""}`}>
            Discovery Reranker: {result.flags.reranker_used ? "Active" : "Off"}
          </span>
          <span className={`badge ${result.flags.model_armor_checked ? "badge-verified" : ""}`}>
            Model Armor: {result.flags.model_armor_checked ? "Checked" : "Not Configured"}
          </span>
          <span className="badge mono">
            Tokens: {result.usage.input_tokens.toLocaleString()} in /{" "}
            {result.usage.output_tokens.toLocaleString()} out
          </span>
        </div>

        <div className="answer-prose">
          {segments.map((seg, idx) => {
            if (seg.type === "text") {
              return <span key={idx}>{seg.text}</span>;
            }
            const isUnverified = Boolean(seg.id && unverifiedSet.has(seg.id));
            return (
              <button
                key={`${seg.kind}-${seg.id}-${idx}`}
                type="button"
                className={`cite-chip${isUnverified ? " unverified" : ""}`}
                title={
                  isUnverified
                    ? "Unverified citation — not present in retrieved graph/chunk ground truth"
                    : `Inspect ${seg.kind}: ${seg.id}`
                }
                onClick={() => {
                  if (seg.kind && seg.id && onClickCitation) {
                    onClickCitation(seg.kind, seg.id);
                  }
                }}
              >
                {seg.kind}:{seg.id}
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
