import { Play, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { fetchPackGraph, streamQuery } from "../api/client";
import { CitedAnswer } from "../components/CitedAnswer";
import { EvidenceInspector } from "../components/EvidenceInspector";
import { GraphCanvas } from "../components/GraphCanvas";
import { StageRail } from "../components/StageRail";
import type {
  EngineId,
  EngineResultPayload,
  EntityNode,
  GraphEdge,
  PackGraphResponse,
  PackSummary,
  SourceChunk,
  StageEvent,
  TraversalDirection,
} from "../types/graphrag";
import { formatUsd, type CitationKind } from "../utils/format";

interface ExplorerPageProps {
  pack: PackSummary;
  mode: "replay" | "live";
  synthesisModel: string;
  onRunningChange?: (running: boolean) => void;
}

interface PackWorkState {
  selectedQuestionId: string | null;
  queryText: string;
  engineId: EngineId;
  maxHops: number;
  direction: TraversalDirection;
  enableRerank: boolean;
  isRunning: boolean;
  stageEvents: StageEvent[];
  result: EngineResultPayload | null;
  runError: string | null;
  selectedNode: EntityNode | null;
  selectedEdge: GraphEdge | null;
  selectedChunk: SourceChunk | null;
}

function createInitialWorkState(pack: PackSummary): PackWorkState {
  const firstQ = pack.questions[0];
  return {
    selectedQuestionId: firstQ?.question_id ?? null,
    queryText: firstQ?.question ?? "",
    engineId: "bq_graphrag",
    maxHops: firstQ?.hop_depth ?? 3,
    direction: firstQ?.direction ?? "both",
    enableRerank: true,
    isRunning: false,
    stageEvents: [],
    result: null,
    runError: null,
    selectedNode: null,
    selectedEdge: null,
    selectedChunk: null,
  };
}

function directionLabel(dir: TraversalDirection): string {
  if (dir === "outgoing") return "Downstream Impact";
  if (dir === "incoming") return "Upstream Root Cause";
  return "Full 360° Network";
}

export function ExplorerPage({
  pack,
  mode,
  synthesisModel,
  onRunningChange,
}: ExplorerPageProps) {
  const [graphCache, setGraphCache] = useState<Record<string, PackGraphResponse>>({});
  const [loadErrors, setLoadErrors] = useState<Record<string, string | null>>({});
  const [workByPack, setWorkByPack] = useState<Record<string, PackWorkState>>(() => ({
    [pack.pack_id]: createInitialWorkState(pack),
  }));

  const abortRefs = useRef<Record<string, AbortController>>({});

  // Ensure every selected pack has its own preserved work state & cached graph
  useEffect(() => {
    setWorkByPack((prev) => {
      if (prev[pack.pack_id]) return prev;
      return { ...prev, [pack.pack_id]: createInitialWorkState(pack) };
    });

    if (graphCache[pack.pack_id]) return;

    let cancelled = false;
    setLoadErrors((prev) => ({ ...prev, [pack.pack_id]: null }));
    fetchPackGraph(pack.pack_id)
      .then((g) => {
        if (!cancelled) {
          setGraphCache((prev) => ({ ...prev, [pack.pack_id]: g }));
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setLoadErrors((prev) => ({ ...prev, [pack.pack_id]: err.message }));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [pack, graphCache]);

  const anyRunning = useMemo(
    () => Object.values(workByPack).some((w) => w.isRunning),
    [workByPack]
  );

  useEffect(() => {
    onRunningChange?.(anyRunning);
  }, [anyRunning, onRunningChange]);

  const work = workByPack[pack.pack_id] ?? createInitialWorkState(pack);
  const graph = graphCache[pack.pack_id] ?? null;
  const loadError = loadErrors[pack.pack_id] ?? null;

  const patchWork = (
    targetPackId: string,
    updater: (prev: PackWorkState) => PackWorkState
  ) => {
    setWorkByPack((prev) => {
      const base = prev[targetPackId] ?? createInitialWorkState(pack);
      return { ...prev, [targetPackId]: updater(base) };
    });
  };

  const nodesById = useMemo(
    () => new Map((graph?.nodes ?? []).map((n) => [n.entity_id, n])),
    [graph]
  );
  const edgesById = useMemo(
    () => new Map((graph?.edges ?? []).map((e) => [e.edge_id, e])),
    [graph]
  );
  const chunksById = useMemo(
    () => new Map((graph?.chunks ?? []).map((c) => [c.chunk_id, c])),
    [graph]
  );

  const highlightedNodeIds = useMemo(() => {
    const s = new Set<string>();
    if (!work.result) return s;
    for (const id of work.result.retrieved_entity_ids) s.add(id);
    for (const p of work.result.paths) {
      for (const nid of p.node_ids) s.add(nid);
    }
    return s;
  }, [work.result]);

  const highlightedEdgeIds = useMemo(() => {
    const s = new Set<string>();
    if (!work.result) return s;
    for (const id of work.result.retrieved_edge_ids) s.add(id);
    for (const p of work.result.paths) {
      for (const eid of p.edge_ids) s.add(eid);
    }
    return s;
  }, [work.result]);

  const handleCitationClick = (kind: CitationKind, id: string) => {
    const pid = pack.pack_id;
    if (kind === "ENTITY") {
      const node = nodesById.get(id);
      if (node) {
        patchWork(pid, (w) => ({
          ...w,
          selectedNode: node,
          selectedEdge: null,
          selectedChunk: null,
        }));
      }
    } else if (kind === "EDGE") {
      const edge = edgesById.get(id);
      if (edge) {
        patchWork(pid, (w) => ({
          ...w,
          selectedEdge: edge,
          selectedNode: null,
          selectedChunk: null,
        }));
      }
    } else if (kind === "CHUNK") {
      const chunk = chunksById.get(id);
      if (chunk) {
        patchWork(pid, (w) => ({
          ...w,
          selectedChunk: chunk,
          selectedNode: null,
          selectedEdge: null,
        }));
      }
    }
  };

  const handleRun = async () => {
    const pid = pack.pack_id;
    const current = workByPack[pid] ?? createInitialWorkState(pack);
    if (!current.queryText.trim()) return;

    abortRefs.current[pid]?.abort();
    const ctrl = new AbortController();
    abortRefs.current[pid] = ctrl;

    patchWork(pid, (w) => ({
      ...w,
      isRunning: true,
      stageEvents: [],
      result: null,
      runError: null,
    }));

    try {
      await streamQuery(
        {
          pack_id: pid,
          query: current.queryText.trim(),
          engine_id: current.engineId,
          synthesis_model: synthesisModel,
          question_id: current.selectedQuestionId,
          max_hops: current.maxHops,
          direction: current.direction,
          enable_rerank: current.enableRerank,
          mode,
        },
        {
          onStage: (ev) =>
            patchWork(pid, (w) => ({ ...w, stageEvents: [...w.stageEvents, ev] })),
          onResult: (res) => patchWork(pid, (w) => ({ ...w, result: res })),
          onError: (msg) => patchWork(pid, (w) => ({ ...w, runError: msg })),
        },
        ctrl.signal
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        patchWork(pid, (w) => ({ ...w, runError: (err as Error).message }));
      }
    } finally {
      patchWork(pid, (w) => ({ ...w, isRunning: false }));
    }
  };

  const handleStop = () => {
    const pid = pack.pack_id;
    abortRefs.current[pid]?.abort();
    patchWork(pid, (w) => ({ ...w, isRunning: false }));
  };

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>{pack.title}</h1>
          <p className="page-subtitle">{pack.subtitle}</p>
        </div>
        <div className="row-wrap">
          <span className="badge badge-verified">
            {Math.round(pack.real_public_overall_ratio * 100)}% Verified Public Filings
          </span>
          <span className="badge">{pack.industry}</span>
          <span className="badge">Built for {pack.persona}</span>
        </div>
      </div>

      {/* Executive Business Value Strip */}
      <section className="value-strip" aria-label="Business impact overview">
        <div className="value-tile">
          <div className="value-tile-label">Manual Analyst Effort</div>
          <div className="value-tile-main">
            {formatUsd(pack.manual_baseline_cost_usd)} · {pack.manual_baseline_minutes} min
          </div>
          <div className="value-tile-sub">Typical cross-filing investigation</div>
        </div>
        <div className="value-tile">
          <div className="value-tile-label">AI Multi-Tier Trace</div>
          <div className="value-tile-main">~$0.003 · Under 15s</div>
          <div className="value-tile-sub">Serverless BigQuery Graph + Vertex AI</div>
        </div>
        <div className="value-tile">
          <div className="value-tile-label">Source Grounding</div>
          <div className="value-tile-main">
            {Math.round(pack.real_public_overall_ratio * 100)}% Public Records
          </div>
          <div className="value-tile-sub">
            {pack.primary_sources[0] ?? "Auditable regulatory & contract filings"}
          </div>
        </div>
        <div className="value-tile">
          <div className="value-tile-label">Active Coverage</div>
          <div className="value-tile-main">
            {graph ? `${graph.nodes.length} Entities · ${graph.edges.length} Links` : "Loading…"}
          </div>
          <div className="value-tile-sub">
            {pack.questions.length} verified multi-tier risk scenarios ready
          </div>
        </div>
      </section>

      {loadError && (
        <div role="alert" className="error-banner">
          Unable to load scenario network: {loadError}
        </div>
      )}

      <div className="explorer-grid">
        {/* Left Rail: Investigation Controls & Sample Questions */}
        <aside className="panel" aria-label="Investigation controls">
          <div className="panel-header">
            <h2 className="panel-title">Risk Investigation</h2>
            <span className="badge">
              {mode === "live" ? "Live Cloud" : "Replay Cache"}
            </span>
          </div>
          <div className="panel-body stack-md">
            <div>
              <label className="field-label" htmlFor="engine-select">
                Analysis Approach
              </label>
              <select
                id="engine-select"
                className="select-control w-full"
                value={work.engineId}
                onChange={(e) =>
                  patchWork(pack.pack_id, (w) => ({
                    ...w,
                    engineId: e.target.value as EngineId,
                  }))
                }
              >
                <option value="bq_graphrag">
                  Graph Intelligence (Recommended · BigQuery)
                </option>
                <option value="adk_graph_coordinator">
                  Autonomous Risk Agent (Google ADK 2.15)
                </option>
                <option value="flat_vector_rag">
                  Standard Document Search (Baseline)
                </option>
                <option value="spanner_graphrag">
                  Real-Time Enterprise Graph (Spanner Tier-2)
                </option>
              </select>
            </div>

            <div className="grid-2">
              <div>
                <label className="field-label" htmlFor="direction-select">
                  Impact Scope
                </label>
                <select
                  id="direction-select"
                  className="select-control w-full"
                  value={work.direction}
                  onChange={(e) =>
                    patchWork(pack.pack_id, (w) => ({
                      ...w,
                      direction: e.target.value as TraversalDirection,
                    }))
                  }
                >
                  <option value="outgoing">Downstream Impact</option>
                  <option value="incoming">Upstream Root Cause</option>
                  <option value="both">Full 360° Network</option>
                </select>
              </div>
              <div>
                <label className="field-label" htmlFor="hops-select">
                  Search Depth
                </label>
                <select
                  id="hops-select"
                  className="select-control w-full"
                  value={work.maxHops}
                  onChange={(e) =>
                    patchWork(pack.pack_id, (w) => ({
                      ...w,
                      maxHops: Number(e.target.value),
                    }))
                  }
                >
                  <option value={2}>2 tiers deep</option>
                  <option value={3}>3 tiers deep</option>
                  <option value={4}>4 tiers deep</option>
                </select>
              </div>
            </div>

            <label className="row-wrap clickable-label">
              <input
                type="checkbox"
                checked={work.enableRerank}
                onChange={(e) =>
                  patchWork(pack.pack_id, (w) => ({
                    ...w,
                    enableRerank: e.target.checked,
                  }))
                }
              />
              Prioritize highest-relevance evidence (Semantic Reranking)
            </label>

            <div>
              <label className="field-label" htmlFor="analyst-prompt">
                Ask a Business Risk Question
              </label>
              <textarea
                id="analyst-prompt"
                className="textarea-input"
                value={work.queryText}
                onChange={(e) =>
                  patchWork(pack.pack_id, (w) => ({
                    ...w,
                    queryText: e.target.value,
                    selectedQuestionId: null,
                  }))
                }
              />
            </div>

            <div className="row-wrap">
              <button
                type="button"
                id="run-investigation-btn"
                className="btn btn-primary flex-1"
                disabled={work.isRunning || !work.queryText.trim()}
                onClick={handleRun}
              >
                <Play size={14} aria-hidden="true" />
                {work.isRunning ? "Investigating…" : "Run Investigation"}
              </button>
              {work.isRunning && (
                <button type="button" className="btn" onClick={handleStop}>
                  <Square size={13} aria-hidden="true" />
                  Stop
                </button>
              )}
            </div>

            {work.runError && (
              <div role="alert" className="error-banner">
                {work.runError}
              </div>
            )}

            <div className="stack-sm">
              <div className="field-label">Sample Multi-Tier Scenarios</div>
              {pack.questions.map((q, idx) => (
                <button
                  key={q.question_id}
                  type="button"
                  className={`preset-btn${
                    work.selectedQuestionId === q.question_id ? " selected" : ""
                  }`}
                  onClick={() =>
                    patchWork(pack.pack_id, (w) => ({
                      ...w,
                      selectedQuestionId: q.question_id,
                      queryText: q.question,
                      maxHops: q.hop_depth,
                      direction: q.direction,
                    }))
                  }
                >
                  <div className="preset-meta">
                    <span>Scenario {idx + 1}</span>
                    <span>·</span>
                    <span>
                      {q.hop_depth}-tier · {directionLabel(q.direction)}
                    </span>
                  </div>
                  <div className="preset-text">{q.question}</div>
                </button>
              ))}
            </div>
          </div>
        </aside>

        {/* Center: Interactive Network Visualization + Verbatim Source Evidence */}
        <div className="stack-md">
          {graph ? (
            <GraphCanvas
              nodes={graph.nodes}
              edges={graph.edges}
              ontology={graph.ontology}
              highlightedNodeIds={highlightedNodeIds}
              highlightedEdgeIds={highlightedEdgeIds}
              selectedNodeId={work.selectedNode?.entity_id ?? null}
              selectedEdgeId={work.selectedEdge?.edge_id ?? null}
              onSelectNode={(n) =>
                patchWork(pack.pack_id, (w) => ({
                  ...w,
                  selectedNode: n,
                  selectedEdge: null,
                  selectedChunk: null,
                }))
              }
              onSelectEdge={(e) =>
                patchWork(pack.pack_id, (w) => ({
                  ...w,
                  selectedEdge: e,
                  selectedNode: null,
                  selectedChunk: null,
                }))
              }
            />
          ) : (
            <div className="panel">
              <div className="panel-body">Loading connected risk network…</div>
            </div>
          )}

          <EvidenceInspector
            selectedNode={work.selectedNode}
            selectedEdge={work.selectedEdge}
            selectedChunk={work.selectedChunk}
            chunksById={chunksById}
            nodesById={nodesById}
            onSelectChunk={(c) =>
              patchWork(pack.pack_id, (w) => ({
                ...w,
                selectedChunk: c,
                selectedNode: null,
                selectedEdge: null,
              }))
            }
          />
        </div>

        {/* Right: Live Investigation Steps + Grounded Executive Risk Brief */}
        <div className="explorer-right stack-md">
          <StageRail events={work.stageEvents} isRunning={work.isRunning} />
          {work.result && (
            <CitedAnswer
              result={work.result}
              nodesById={nodesById}
              edgesById={edgesById}
              chunksById={chunksById}
              onClickCitation={handleCitationClick}
            />
          )}
        </div>
      </div>
    </div>
  );
}
