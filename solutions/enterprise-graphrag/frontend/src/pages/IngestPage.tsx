import { RotateCcw, ShieldCheck, Upload } from "lucide-react";
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
  onRunningChange?: (running: boolean) => void;
}

interface IngestPackState {
  ontologyYaml: string;
  selectedFile: File | null;
  persistToBq: boolean;
  isUploading: boolean;
  response: IngestUploadResponse | null;
  error: string | null;
  selectedNode: EntityNode | null;
  selectedEdge: GraphEdge | null;
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

function createInitialIngestState(pack: PackSummary): IngestPackState {
  return {
    ontologyYaml: ontologyToYamlPreview(pack.ontology),
    selectedFile: null,
    persistToBq: false,
    isUploading: false,
    response: null,
    error: null,
    selectedNode: null,
    selectedEdge: null,
  };
}

export function IngestPage({
  pack,
  mode,
  synthesisModel,
  onRunningChange,
}: IngestPageProps) {
  const [stateByPack, setStateByPack] = useState<Record<string, IngestPackState>>(() => ({
    [pack.pack_id]: createInitialIngestState(pack),
  }));

  useEffect(() => {
    setStateByPack((prev) =>
      prev[pack.pack_id]
        ? prev
        : { ...prev, [pack.pack_id]: createInitialIngestState(pack) }
    );
  }, [pack]);

  const anyUploading = useMemo(
    () => Object.values(stateByPack).some((s) => s.isUploading),
    [stateByPack]
  );

  useEffect(() => {
    onRunningChange?.(anyUploading);
  }, [anyUploading, onRunningChange]);

  const state = stateByPack[pack.pack_id] ?? createInitialIngestState(pack);

  const patchState = (
    targetPackId: string,
    updater: (prev: IngestPackState) => IngestPackState
  ) => {
    setStateByPack((prev) => {
      const base = prev[targetPackId] ?? createInitialIngestState(pack);
      return { ...prev, [targetPackId]: updater(base) };
    });
  };

  const nodesById = useMemo(
    () => new Map((state.response?.entities ?? []).map((n) => [n.entity_id, n])),
    [state.response]
  );

  const handleUpload = async () => {
    const pid = pack.pack_id;
    const current = stateByPack[pid] ?? createInitialIngestState(pack);
    if (!current.selectedFile) return;

    patchState(pid, (s) => ({
      ...s,
      isUploading: true,
      error: null,
      response: null,
      selectedNode: null,
      selectedEdge: null,
    }));
    try {
      const res = await uploadDocument({
        packId: pid,
        file: current.selectedFile,
        customOntologyYaml: current.ontologyYaml,
        persistToBigquery: current.persistToBq,
        extractionModel: synthesisModel,
      });
      patchState(pid, (s) => ({ ...s, response: res }));
    } catch (err) {
      patchState(pid, (s) => ({ ...s, error: (err as Error).message }));
    } finally {
      patchState(pid, (s) => ({ ...s, isUploading: false }));
    }
  };

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>Upload Your Own Filings, Contracts &amp; Diagrams</h1>
          <p className="page-subtitle">
            Upload a PDF, PNG/JPEG diagram, CSV, TXT, or Markdown filing (up to 10 MB) to
            automatically map entities and hidden connections, screen for sensitive PII, and
            optionally merge them into your live risk graph.
          </p>
        </div>
        <span className="badge">Target Scenario: {pack.title}</span>
      </div>

      <div className="compare-grid">
        <section className="panel" aria-label="Upload configuration">
          <div className="panel-header">
            <h2 className="panel-title">1. Select Document &amp; Governance Options</h2>
            <span className="badge badge-verified">
              <ShieldCheck size={12} aria-hidden="true" />
              File Safety &amp; 10 MB Check
            </span>
          </div>
          <div className="panel-body stack-md">
            <div className="dropzone">
              <Upload size={24} color="var(--accent-primary)" aria-hidden="true" />
              <div className="dropzone-title">
                {state.selectedFile
                  ? `${state.selectedFile.name} (${(state.selectedFile.size / 1024).toFixed(1)} KB)`
                  : "Choose a PDF, PNG, JPEG, TXT, MD, or CSV file"}
              </div>
              <div className="dropzone-sub">
                Verified server-side for file authenticity with randomized secure cloud storage keys
              </div>
              <div className="dropzone-actions">
                <label htmlFor="ingest-file-input" className="btn btn-sm">
                  Choose File
                </label>
                <input
                  id="ingest-file-input"
                  type="file"
                  accept=".pdf,.png,.jpg,.jpeg,.txt,.md,.csv,.yaml,.yml"
                  className="hidden-input"
                  onChange={(e) =>
                    patchState(pack.pack_id, (s) => ({
                      ...s,
                      selectedFile: e.target.files?.[0] ?? null,
                    }))
                  }
                />
              </div>
            </div>

            <label className="row-wrap clickable-label">
              <input
                type="checkbox"
                checked={state.persistToBq}
                disabled={mode !== "live"}
                onChange={(e) =>
                  patchState(pack.pack_id, (s) => ({
                    ...s,
                    persistToBq: e.target.checked,
                  }))
                }
              />
              Save extracted entities &amp; connections to live BigQuery Graph
            </label>

            {mode === "replay" && (
              <div className="info-banner">
                Currently in <strong>Replay</strong> mode: uploads run file validation, chunking,
                and privacy preview without incurring cloud AI charges. Switch to{" "}
                <strong>Live Cloud</strong> in the header for full multimodal extraction.
              </div>
            )}

            <button
              type="button"
              id="run-ingest-btn"
              className="btn btn-primary"
              disabled={!state.selectedFile || state.isUploading}
              onClick={handleUpload}
            >
              <Upload size={14} aria-hidden="true" />
              {state.isUploading ? "Validating & Extracting…" : "Analyze & Map Document"}
            </button>

            {state.error && (
              <div role="alert" className="error-banner">
                {state.error}
              </div>
            )}
          </div>
        </section>

        <section className="panel" aria-label="Entity and relationship rules">
          <div className="panel-header">
            <h2 className="panel-title">2. Custom Entity &amp; Relationship Rules (YAML)</h2>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() =>
                patchState(pack.pack_id, (s) => ({
                  ...s,
                  ontologyYaml: ontologyToYamlPreview(pack.ontology),
                }))
              }
            >
              <RotateCcw size={12} aria-hidden="true" />
              Reset Defaults
            </button>
          </div>
          <div className="panel-body stack-sm">
            <label className="field-label" htmlFor="ontology-yaml-editor">
              Customize entity types, risk relationships, and sensitive PII checks
            </label>
            <textarea
              id="ontology-yaml-editor"
              className="code-editor"
              value={state.ontologyYaml}
              onChange={(e) =>
                patchState(pack.pack_id, (s) => ({ ...s, ontologyYaml: e.target.value }))
              }
            />
          </div>
        </section>
      </div>

      {state.response && (
        <div className="stack-md">
          <section className="panel" aria-label="Extraction results summary">
            <div className="panel-header">
              <h2 className="panel-title">Extraction &amp; Privacy Summary</h2>
              <span className="badge badge-upload">{state.response.extraction_mode}</span>
            </div>
            <div className="panel-body stack-md">
              {state.response.message && (
                <div className="info-banner">{state.response.message}</div>
              )}
              <div className="kpi-grid">
                <div className="kpi-box">
                  <div className="kpi-label">Document Excerpts</div>
                  <div className="kpi-value">{state.response.chunks_count}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Entities Found</div>
                  <div className="kpi-value">{state.response.entities.length}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Connections Found</div>
                  <div className="kpi-value">{state.response.edges.length}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Duplicates Merged</div>
                  <div className="kpi-value">{state.response.merged_entities_count ?? 0}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Sensitive PII Alerts</div>
                  <div className="kpi-value">
                    {state.response.dlp.enabled ? state.response.dlp.findings_count : "Off"}
                  </div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Processing Cost</div>
                  <div className="kpi-value">
                    {formatUsd(state.response.extraction_cost_usd ?? 0)}
                  </div>
                </div>
              </div>

              <details className="stage-details">
                <summary className="stage-summary">
                  Technical storage &amp; privacy audit receipt
                </summary>
                <div className="row-wrap stage-meta">
                  <span className="badge mono">
                    Storage Key: {state.response.upload.gcs_object_key}
                  </span>
                  <span className="badge mono">
                    SHA-256: {state.response.upload.sha256.slice(0, 16)}…
                  </span>
                  <span className="badge">{state.response.dlp.reason}</span>
                </div>
              </details>
            </div>
          </section>

          {state.response.entities.length > 0 && (
            <div className="compare-grid">
              <GraphCanvas
                nodes={state.response.entities}
                edges={state.response.edges}
                ontology={pack.ontology}
                selectedNodeId={state.selectedNode?.entity_id ?? null}
                selectedEdgeId={state.selectedEdge?.edge_id ?? null}
                onSelectNode={(n) =>
                  patchState(pack.pack_id, (s) => ({
                    ...s,
                    selectedNode: n,
                    selectedEdge: null,
                  }))
                }
                onSelectEdge={(e) =>
                  patchState(pack.pack_id, (s) => ({
                    ...s,
                    selectedEdge: e,
                    selectedNode: null,
                  }))
                }
              />
              <EvidenceInspector
                selectedNode={state.selectedNode}
                selectedEdge={state.selectedEdge}
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
