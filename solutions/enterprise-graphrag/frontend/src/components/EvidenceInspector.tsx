import { ExternalLink, FileText, GitBranch, Layers } from "lucide-react";
import type { DataOrigin, EntityNode, GraphEdge, SourceChunk } from "../types/graphrag";
import { formatUsd } from "../utils/format";

interface EvidenceInspectorProps {
  selectedNode: EntityNode | null;
  selectedEdge: GraphEdge | null;
  selectedChunk: SourceChunk | null;
  chunksById: Map<string, SourceChunk>;
  nodesById: Map<string, EntityNode>;
  onSelectChunk: (chunk: SourceChunk) => void;
}

function originBadgeClass(origin: DataOrigin): string {
  if (origin === "REAL_PUBLIC") return "badge badge-verified";
  if (origin === "CUSTOMER_UPLOAD") return "badge badge-upload";
  return "badge badge-modeled";
}

function originLabel(origin: DataOrigin): string {
  if (origin === "REAL_PUBLIC") return "REAL PUBLIC DATA";
  if (origin === "CUSTOMER_UPLOAD") return "CUSTOMER UPLOAD";
  return "MODELED BRIDGE";
}

export function EvidenceInspector({
  selectedNode,
  selectedEdge,
  selectedChunk,
  chunksById,
  nodesById,
  onSelectChunk,
}: EvidenceInspectorProps) {
  if (!selectedNode && !selectedEdge && !selectedChunk) {
    return (
      <section className="panel" aria-label="Evidence inspector">
        <div className="panel-header">
          <h2 className="panel-title">Source Evidence Inspector</h2>
          <span className="badge">Click any node, edge, or citation</span>
        </div>
        <div className="panel-body muted-copy">
          Select a vertex or directed relationship on the knowledge graph — or click any{" "}
          <span className="mono">[ENTITY]</span>, <span className="mono">[EDGE]</span>, or{" "}
          <span className="mono">[CHUNK]</span> citation chip in the contagion brief — to
          audit exact SEC Form SD, OFAC SDN, USGS, or CUAD contract provenance.
        </div>
      </section>
    );
  }

  if (selectedEdge) {
    const srcName = nodesById.get(selectedEdge.source_entity_id)?.canonical_name ?? selectedEdge.source_entity_id;
    const dstName = nodesById.get(selectedEdge.target_entity_id)?.canonical_name ?? selectedEdge.target_entity_id;
    const linkedChunk = chunksById.get(selectedEdge.source_chunk_id);

    return (
      <section className="panel" aria-label="Selected edge evidence">
        <div className="panel-header">
          <div className="row-wrap">
            <GitBranch size={15} aria-hidden="true" />
            <h2 className="panel-title">Edge Provenance · {selectedEdge.relation_type}</h2>
          </div>
          <span className={originBadgeClass(selectedEdge.data_origin)}>
            {originLabel(selectedEdge.data_origin)}
          </span>
        </div>
        <div className="panel-body stack-sm">
          <div className="edge-title">
            {srcName} → {dstName}
          </div>
          <div className="row-wrap">
            <span className="badge mono">{selectedEdge.edge_id}</span>
            <span className="badge">Confidence: {Math.round(selectedEdge.confidence * 100)}%</span>
            {selectedEdge.exposure_usd > 0 && (
              <span className="badge">Exposure: {formatUsd(selectedEdge.exposure_usd)}</span>
            )}
            <span className="badge">{selectedEdge.source_license}</span>
          </div>
          <div>
            <div className="field-label">Verbatim Evidence Excerpt</div>
            <blockquote className="evidence-quote">{selectedEdge.evidence_excerpt}</blockquote>
          </div>
          <div className="row-between">
            <span className="muted-copy">
              Document: <strong>{selectedEdge.source_doc_name}</strong>
            </span>
            <div className="row-wrap">
              {linkedChunk && (
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => onSelectChunk(linkedChunk)}
                >
                  <FileText size={13} aria-hidden="true" />
                  Read Full Chunk ({linkedChunk.chunk_id})
                </button>
              )}
              {selectedEdge.source_url && (
                <a
                  href={selectedEdge.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn btn-sm"
                >
                  <ExternalLink size={13} aria-hidden="true" />
                  Primary Source
                </a>
              )}
            </div>
          </div>
        </div>
      </section>
    );
  }

  if (selectedNode) {
    const linkedChunks = selectedNode.source_chunk_ids
      .map((cid) => chunksById.get(cid))
      .filter((c): c is SourceChunk => Boolean(c));

    return (
      <section className="panel" aria-label="Selected entity inspector">
        <div className="panel-header">
          <div className="row-wrap">
            <Layers size={15} aria-hidden="true" />
            <h2 className="panel-title">Entity · {selectedNode.entity_type}</h2>
          </div>
          <span className={originBadgeClass(selectedNode.data_origin)}>
            {originLabel(selectedNode.data_origin)}
          </span>
        </div>
        <div className="panel-body stack-sm">
          <div className="node-title">{selectedNode.canonical_name}</div>
          <div className="row-wrap">
            <span className="badge mono">{selectedNode.entity_id}</span>
            <span className="badge">Jurisdiction: {selectedNode.jurisdiction}</span>
            <span
              className={`badge ${
                selectedNode.risk_tier === "CRITICAL"
                  ? "badge-danger"
                  : selectedNode.risk_tier === "HIGH"
                    ? "badge-modeled"
                    : ""
              }`}
            >
              Risk: {selectedNode.risk_tier}
            </span>
            {selectedNode.exposure_usd > 0 && (
              <span className="badge">Exposure: {formatUsd(selectedNode.exposure_usd)}</span>
            )}
            <span className="badge">{selectedNode.source_license}</span>
          </div>

          {Object.keys(selectedNode.attributes || {}).length > 0 && (
            <div>
              <div className="field-label">Structured Attributes</div>
              <div className="row-wrap">
                {Object.entries(selectedNode.attributes).map(([k, v]) => (
                  <span key={k} className="badge mono">
                    {k}: {String(v)}
                  </span>
                ))}
              </div>
            </div>
          )}

          {linkedChunks.length > 0 && (
            <div className="stack-sm">
              <div className="field-label">Linked Source Chunks ({linkedChunks.length})</div>
              <div className="row-wrap">
                {linkedChunks.map((ch) => (
                  <button
                    key={ch.chunk_id}
                    type="button"
                    className="btn btn-sm"
                    onClick={() => onSelectChunk(ch)}
                  >
                    <FileText size={12} aria-hidden="true" />
                    {ch.chunk_id} · {ch.doc_name.slice(0, 28)}
                  </button>
                ))}
              </div>
            </div>
          )}

          {selectedNode.source_url && (
            <div>
              <a
                href={selectedNode.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="btn btn-sm"
              >
                <ExternalLink size={13} aria-hidden="true" />
                Open Public Source Filing
              </a>
            </div>
          )}
        </div>
      </section>
    );
  }

  return (
    <section className="panel" aria-label="Source chunk viewer">
      <div className="panel-header">
        <div className="row-wrap">
          <FileText size={15} aria-hidden="true" />
          <h2 className="panel-title">Source Document Chunk · {selectedChunk?.chunk_id}</h2>
        </div>
        {selectedChunk && (
          <span className={originBadgeClass(selectedChunk.data_origin)}>
            {originLabel(selectedChunk.data_origin)}
          </span>
        )}
      </div>
      {selectedChunk && (
        <div className="panel-body stack-sm">
          <div className="row-wrap">
            <span className="badge">{selectedChunk.doc_name}</span>
            <span className="badge">Page {selectedChunk.page_number}</span>
            <span className="badge">{selectedChunk.source_license}</span>
          </div>
          <div className="chunk-viewer">{selectedChunk.content}</div>
        </div>
      )}
    </section>
  );
}
