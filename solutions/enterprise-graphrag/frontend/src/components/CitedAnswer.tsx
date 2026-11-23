import { AlertTriangle, CheckCircle2 } from "lucide-react";
import type {
  EngineResultPayload,
  EntityNode,
  GraphEdge,
  SourceChunk,
} from "../types/graphrag";
import {
  formatMs,
  formatPercent,
  formatUsd,
  parseCitations,
  type CitationKind,
} from "../utils/format";

interface CitedAnswerProps {
  result: EngineResultPayload;
  nodesById?: Map<string, EntityNode>;
  edgesById?: Map<string, GraphEdge>;
  chunksById?: Map<string, SourceChunk>;
  onClickCitation?: (kind: CitationKind, id: string) => void;
}

function humanizeRelation(relation: string): string {
  return relation
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function friendlyChipLabel(
  kind: CitationKind | undefined,
  id: string | undefined,
  nodesById?: Map<string, EntityNode>,
  edgesById?: Map<string, GraphEdge>,
  chunksById?: Map<string, SourceChunk>
): string {
  if (!kind || !id) return "Source";
  if (kind === "ENTITY") {
    const node = nodesById?.get(id);
    return node ? node.canonical_name : id;
  }
  if (kind === "EDGE") {
    const edge = edgesById?.get(id);
    if (edge) {
      const src = nodesById?.get(edge.source_entity_id)?.canonical_name;
      const dst = nodesById?.get(edge.target_entity_id)?.canonical_name;
      if (src && dst) {
        const sShort = src.length > 16 ? `${src.slice(0, 15)}…` : src;
        const dShort = dst.length > 16 ? `${dst.slice(0, 15)}…` : dst;
        return `${sShort} → ${dShort}`;
      }
      return humanizeRelation(edge.relation_type);
    }
    return `Link ${id}`;
  }
  if (kind === "CHUNK") {
    const chunk = chunksById?.get(id);
    if (chunk) {
      const docShort =
        chunk.doc_name.length > 22
          ? `${chunk.doc_name.slice(0, 21)}…`
          : chunk.doc_name;
      return `${docShort} · p.${chunk.page_number}`;
    }
    return `Filing ${id}`;
  }
  return `${kind}:${id}`;
}

export function CitedAnswer({
  result,
  nodesById,
  edgesById,
  chunksById,
  onClickCitation,
}: CitedAnswerProps) {
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
    <section className="panel" aria-label="Grounded executive risk brief">
      <div className="panel-header">
        <div className="row-wrap">
          <h2 className="panel-title">Executive Risk Brief</h2>
          <span
            className={`badge ${unverifiedTotal === 0 ? "badge-verified" : "badge-danger"}`}
          >
            {unverifiedTotal === 0 ? (
              <>
                <CheckCircle2 size={12} aria-hidden="true" />
                All {verifiedTotal} Citations Verified
              </>
            ) : (
              <>
                <AlertTriangle size={12} aria-hidden="true" />
                {unverifiedTotal} Citation(s) Need Review
              </>
            )}
          </span>
        </div>
        <span className="badge">{result.synthesis_model}</span>
      </div>

      <div className="panel-body stack-md">
        <div className="kpi-grid">
          <div className="kpi-box">
            <div className="kpi-label">Response Time</div>
            <div className="kpi-value">{formatMs(result.total_duration_ms)}</div>
          </div>
          <div className="kpi-box">
            <div className="kpi-label">AI Analysis Cost</div>
            <div className="kpi-value">{formatUsd(result.engine_cost_usd)}</div>
          </div>
          <div className="kpi-box">
            <div className="kpi-label">Risk Chains / Links</div>
            <div className="kpi-value">
              {result.paths.length} / {result.retrieved_edge_ids.length}
            </div>
          </div>
          {result.recall && (
            <div className="kpi-box">
              <div className="kpi-label">Multi-Tier Accuracy</div>
              <div className="kpi-value">{formatPercent(result.recall.path_recall)}</div>
            </div>
          )}
        </div>

        <div className="answer-prose">
          {segments.map((seg, idx) => {
            if (seg.type === "text") {
              return <span key={idx}>{seg.text}</span>;
            }
            const isUnverified = Boolean(seg.id && unverifiedSet.has(seg.id));
            const label = friendlyChipLabel(
              seg.kind,
              seg.id,
              nodesById,
              edgesById,
              chunksById
            );
            return (
              <button
                key={`${seg.kind}-${seg.id}-${idx}`}
                type="button"
                className={`cite-chip${isUnverified ? " unverified" : ""}`}
                title={
                  isUnverified
                    ? `Needs review — ${seg.kind}:${seg.id} was not matched in verified source records`
                    : `Click to inspect source evidence (${seg.kind}: ${seg.id})`
                }
                onClick={() => {
                  if (seg.kind && seg.id && onClickCitation) {
                    onClickCitation(seg.kind, seg.id);
                  }
                }}
              >
                {label}
              </button>
            );
          })}
        </div>

        <details className="stage-details">
          <summary className="stage-summary">
            Technical audit metadata &amp; token usage
          </summary>
          <div className="row-wrap stage-meta">
            <span className="badge">Graph Engine: {result.flags.graph_backend_used}</span>
            <span className={`badge ${result.flags.reranker_used ? "badge-verified" : ""}`}>
              Semantic Reranker: {result.flags.reranker_used ? "Active" : "Off"}
            </span>
            <span
              className={`badge ${
                result.flags.model_armor_checked ? "badge-verified" : ""
              }`}
            >
              Safety Guardrails:{" "}
              {result.flags.model_armor_checked ? "Checked" : "Optional"}
            </span>
            <span className="badge mono">
              Tokens: {result.usage.input_tokens.toLocaleString()} in /{" "}
              {result.usage.output_tokens.toLocaleString()} out
            </span>
          </div>
        </details>
      </div>
    </section>
  );
}
