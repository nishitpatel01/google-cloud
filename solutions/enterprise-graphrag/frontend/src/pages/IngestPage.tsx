import { ShieldCheck, Upload } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { uploadDocument } from "../api/client";
import { EvidenceInspector } from "../components/EvidenceInspector";
import { GraphCanvas } from "../components/GraphCanvas";
import type {
  EntityNode,
  GraphEdge,
  IngestUploadResponse,
  OntologySpec,
  PackSummary,
} from "../types/graphrag";
import { formatUsd } from "../utils/format";

interface IngestPageProps {
  pack: PackSummary;
  mode: "replay" | "live";
  synthesisModel: string;
}

function ontologyToYamlPreview(ontology: OntologySpec): string {
  const nodes = ontology.node_types
    .map(
      (n) =>
        `  - type: ${n.type}\n    label: "${n.label}"\n    color: "${n.color}"\n    shape: ${n.shape}\n    is_revenue_bearing: ${n.is_revenue_bearing}`
    )
    .join("\n");
  const edges = ontology.edge_types
    .map(
      (e) =>
        `  - type: ${e.type}\n    label: "${e.label}"\n    directed: ${e.directed}\n    propagates_risk: ${e.propagates_risk}`
    )
    .join("\n");
  const dlp = ontology.dlp_info_types.map((d) => `  - ${d}`).join("\n");
  return `node_types:\n${nodes}\nedge_types:\n${edges}\ndlp_info_types:\n${dlp}\n`;
}

export function IngestPage({ pack, mode, synthesisModel }: IngestPageProps) {
  const [ontologyYaml, setOntologyYaml] = useState(() =>
    ontologyToYamlPreview(pack.ontology)
  );
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [persistToBq, setPersistToBq] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [response, setResponse] = useState<IngestUploadResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [selectedNode, setSelectedNode] = useState<EntityNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null);

  useEffect(() => {
    setOntologyYaml(ontologyToYamlPreview(pack.ontology));
    setResponse(null);
    setError(null);
    setSelectedNode(null);
    setSelectedEdge(null);
  }, [pack.pack_id, pack.ontology]);

  const nodesById = useMemo(
    () => new Map((response?.entities ?? []).map((n) => [n.entity_id, n])),
    [response]
  );

  const handleUpload = async () => {
    if (!selectedFile) return;
    setIsUploading(true);
    setError(null);
    setResponse(null);
    setSelectedNode(null);
    setSelectedEdge(null);
    try {
      const res = await uploadDocument({
        packId: pack.pack_id,
        file: selectedFile,
        customOntologyYaml: ontologyYaml,
        persistToBigquery: persistToBq,
        extractionModel: synthesisModel,
      });
      setResponse(res);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setIsUploading(false);
    }
  };

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>BYO-Ontology &amp; Multimodal Document Ingestion</h1>
          <p className="page-subtitle">
            Upload your own PDF, PNG/JPEG diagram, TXT, CSV, or Markdown filing (max 10 MB)
            to extract ontology-constrained entities &amp; directed edges with O(N) canonical
            deduplication, Cloud DLP PII safety, and optional atomic BigQuery MERGE.
          </p>
        </div>
        <span className="badge">Target Pack: {pack.pack_id}</span>
      </div>

      <div className="compare-grid">
        <section className="panel" aria-label="Upload configuration">
          <div className="panel-header">
            <h2 className="panel-title">1. Document Upload &amp; Security Controls</h2>
            <span className="badge badge-verified">
              <ShieldCheck size={12} aria-hidden="true" />
              Magic-Byte + 10 MB Guard
            </span>
          </div>
          <div className="panel-body stack-md">
            <div className="dropzone">
              <Upload size={24} color="var(--accent-primary)" aria-hidden="true" />
              <div className="dropzone-title">
                {selectedFile
                  ? `${selectedFile.name} (${(selectedFile.size / 1024).toFixed(1)} KB)`
                  : "Choose a PDF, PNG, JPEG, TXT, MD, or CSV document"}
              </div>
              <div className="dropzone-sub">
                Validated server-side via binary headers + randomized UUID GCS object key
              </div>
              <div className="dropzone-actions">
                <label htmlFor="ingest-file-input" className="btn btn-sm">
                  Select File
                </label>
                <input
                  id="ingest-file-input"
                  type="file"
                  accept=".pdf,.png,.jpg,.jpeg,.txt,.md,.csv,.yaml,.yml"
                  className="hidden-input"
                  onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
                />
              </div>
            </div>

            <label className="row-wrap clickable-label">
              <input
                type="checkbox"
                checked={persistToBq}
                disabled={mode !== "live"}
                onChange={(e) => setPersistToBq(e.target.checked)}
              />
              Persist extracted entities &amp; edges atomically into BigQuery Property Graph (MERGE)
            </label>

            {mode === "replay" && (
              <div className="info-banner">
                Currently in <strong>Replay</strong> mode: uploads run strict validation,
                chunking, and DLP preview without calling live Gemini or BigQuery. Switch to{" "}
                <strong>Live</strong> mode in the header for full multimodal extraction.
              </div>
            )}

            <button
              type="button"
              id="run-ingest-btn"
              className="btn btn-primary"
              disabled={!selectedFile || isUploading}
              onClick={handleUpload}
            >
              <Upload size={14} aria-hidden="true" />
              {isUploading ? "Validating & Extracting…" : "Validate & Extract Knowledge Graph"}
            </button>

            {error && (
              <div role="alert" className="error-banner">
                {error}
              </div>
            )}
          </div>
        </section>

        <section className="panel" aria-label="Ontology editor">
          <div className="panel-header">
            <h2 className="panel-title">2. Editable Target Ontology (YAML)</h2>
            <span className="badge">yaml.safe_load validated</span>
          </div>
          <div className="panel-body stack-sm">
            <label className="field-label" htmlFor="ontology-yaml-editor">
              Customize allowed node_types, edge_types, and Cloud DLP info_types
            </label>
            <textarea
              id="ontology-yaml-editor"
              className="code-editor"
              value={ontologyYaml}
              onChange={(e) => setOntologyYaml(e.target.value)}
            />
          </div>
        </section>
      </div>

      {response && (
        <div className="stack-md">
          <section className="panel" aria-label="Ingestion results summary">
            <div className="panel-header">
              <h2 className="panel-title">Extraction &amp; Governance Summary</h2>
              <span className="badge badge-upload">{response.extraction_mode}</span>
            </div>
            <div className="panel-body stack-md">
              {response.message && <div className="info-banner">{response.message}</div>}
              <div className="kpi-grid">
                <div className="kpi-box">
                  <div className="kpi-label">Chunks</div>
                  <div className="kpi-value">{response.chunks_count}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Entities Extracted</div>
                  <div className="kpi-value">{response.entities.length}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Edges Extracted</div>
                  <div className="kpi-value">{response.edges.length}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Canonical Merges</div>
                  <div className="kpi-value">{response.merged_entities_count ?? 0}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Cloud DLP Findings</div>
                  <div className="kpi-value">
                    {response.dlp.enabled ? response.dlp.findings_count : "Off"}
                  </div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Extraction Cost</div>
                  <div className="kpi-value">
                    {formatUsd(response.extraction_cost_usd ?? 0)}
                  </div>
                </div>
              </div>

              <div className="row-wrap">
                <span className="badge mono">GCS Key: {response.upload.gcs_object_key}</span>
                <span className="badge mono">SHA-256: {response.upload.sha256.slice(0, 16)}…</span>
                <span className="badge">{response.dlp.reason}</span>
              </div>
            </div>
          </section>

          {response.entities.length > 0 && (
            <div className="compare-grid">
              <GraphCanvas
                nodes={response.entities}
                edges={response.edges}
                ontology={pack.ontology}
                selectedNodeId={selectedNode?.entity_id ?? null}
                selectedEdgeId={selectedEdge?.edge_id ?? null}
                onSelectNode={(n) => {
                  setSelectedNode(n);
                  setSelectedEdge(null);
                }}
                onSelectEdge={(e) => {
                  setSelectedEdge(e);
                  setSelectedNode(null);
                }}
              />
              <EvidenceInspector
                selectedNode={selectedNode}
                selectedEdge={selectedEdge}
                selectedChunk={null}
                chunksById={new Map()}
                nodesById={nodesById}
                onSelectChunk={() => {}}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
