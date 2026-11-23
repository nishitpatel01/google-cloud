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
import type { CitationKind } from "../utils/format";

interface ExplorerPageProps {
  pack: PackSummary;
  mode: "replay" | "live";
  synthesisModel: string;
}

export function ExplorerPage({ pack, mode, synthesisModel }: ExplorerPageProps) {
  const [graph, setGraph] = useState<PackGraphResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedQuestionId, setSelectedQuestionId] = useState<string | null>(
    pack.questions[0]?.question_id ?? null
  );
  const [queryText, setQueryText] = useState(pack.questions[0]?.question ?? "");
  const [engineId, setEngineId] = useState<EngineId>("bq_graphrag");
  const [maxHops, setMaxHops] = useState<number>(pack.questions[0]?.hop_depth ?? 3);
  const [direction, setDirection] = useState<TraversalDirection>(
    pack.questions[0]?.direction ?? "both"
  );
  const [enableRerank, setEnableRerank] = useState(true);

  const [isRunning, setIsRunning] = useState(false);
  const [stageEvents, setStageEvents] = useState<StageEvent[]>([]);
  const [result, setResult] = useState<EngineResultPayload | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const [selectedNode, setSelectedNode] = useState<EntityNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null);
  const [selectedChunk, setSelectedChunk] = useState<SourceChunk | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    setSelectedNode(null);
    setSelectedEdge(null);
    setSelectedChunk(null);
    setResult(null);
    setStageEvents([]);
    setRunError(null);

    const firstQ = pack.questions[0];
    setSelectedQuestionId(firstQ?.question_id ?? null);
    setQueryText(firstQ?.question ?? "");
    setMaxHops(firstQ?.hop_depth ?? 3);
    setDirection(firstQ?.direction ?? "both");

    fetchPackGraph(pack.pack_id)
      .then((g) => {
        if (!cancelled) setGraph(g);
      })
      .catch((err: Error) => {
        if (!cancelled) setLoadError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [pack.pack_id, pack.questions]);

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
    if (!result) return s;
    for (const id of result.retrieved_entity_ids) s.add(id);
    for (const p of result.paths) {
      for (const nid of p.node_ids) s.add(nid);
    }
    return s;
  }, [result]);

  const highlightedEdgeIds = useMemo(() => {
    const s = new Set<string>();
    if (!result) return s;
    for (const id of result.retrieved_edge_ids) s.add(id);
    for (const p of result.paths) {
      for (const eid of p.edge_ids) s.add(eid);
    }
    return s;
  }, [result]);

  const handleCitationClick = (kind: CitationKind, id: string) => {
    if (kind === "ENTITY") {
      const node = nodesById.get(id);
      if (node) {
        setSelectedNode(node);
        setSelectedEdge(null);
        setSelectedChunk(null);
      }
    } else if (kind === "EDGE") {
      const edge = edgesById.get(id);
      if (edge) {
        setSelectedEdge(edge);
        setSelectedNode(null);
        setSelectedChunk(null);
      }
    } else if (kind === "CHUNK") {
      const chunk = chunksById.get(id);
      if (chunk) {
        setSelectedChunk(chunk);
        setSelectedNode(null);
        setSelectedEdge(null);
      }
    }
  };

  const handleRun = async () => {
    if (!queryText.trim()) return;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setIsRunning(true);
    setStageEvents([]);
    setResult(null);
    setRunError(null);

    try {
      await streamQuery(
        {
          pack_id: pack.pack_id,
          query: queryText.trim(),
          engine_id: engineId,
          synthesis_model: synthesisModel,
          question_id: selectedQuestionId,
          max_hops: maxHops,
          direction,
          enable_rerank: enableRerank,
          mode,
        },
        {
          onStage: (ev) => setStageEvents((prev) => [...prev, ev]),
          onResult: (res) => setResult(res),
          onError: (msg) => setRunError(msg),
        },
        ctrl.signal
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setRunError((err as Error).message);
      }
    } finally {
      setIsRunning(false);
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
    setIsRunning(false);
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
            {Math.round(pack.real_public_overall_ratio * 100)}% Real Public Data
          </span>
          <span className="badge">{pack.industry}</span>
          <span className="badge">Persona: {pack.persona}</span>
        </div>
      </div>

      {loadError && (
        <div role="alert" className="error-banner">
          Failed to load pack graph: {loadError}
        </div>
      )}

      <div className="explorer-grid">
        {/* Left Rail: Query Controls & Golden Multi-Hop Scenarios */}
        <aside className="panel" aria-label="Investigation controls">
          <div className="panel-header">
            <h2 className="panel-title">Contagion Query</h2>
            <span className="badge">{mode === "live" ? "Live GCP" : "Replay"}</span>
          </div>
          <div className="panel-body stack-md">
            <div>
              <label className="field-label" htmlFor="engine-select">
                Retrieval &amp; Reasoning Engine
              </label>
              <select
                id="engine-select"
                className="select-control w-full"
                value={engineId}
                onChange={(e) => setEngineId(e.target.value as EngineId)}
              >
                <option value="bq_graphrag">
                  BigQuery Property Graph (ISO GQL · Tier-1)
                </option>
                <option value="adk_graph_coordinator">
                  Google ADK 2.15 Graph Coordinator (Multi-Tool)
                </option>
                <option value="flat_vector_rag">
                  Flat Vector RAG (Baseline — Chunks Only)
                </option>
                <option value="spanner_graphrag">
                  Spanner Graph ISO GQL (Tier-2 Enterprise)
                </option>
              </select>
            </div>

            <div className="grid-2">
              <div>
                <label className="field-label" htmlFor="direction-select">
                  Direction
                </label>
                <select
                  id="direction-select"
                  className="select-control w-full"
                  value={direction}
                  onChange={(e) => setDirection(e.target.value as TraversalDirection)}
                >
                  <option value="outgoing">Outgoing (Downstream)</option>
                  <option value="incoming">Incoming (Upstream)</option>
                  <option value="both">Both (UNION ALL)</option>
                </select>
              </div>
              <div>
                <label className="field-label" htmlFor="hops-select">
                  Max Hops
                </label>
                <select
                  id="hops-select"
                  className="select-control w-full"
                  value={maxHops}
                  onChange={(e) => setMaxHops(Number(e.target.value))}
                >
                  <option value={2}>2 hops</option>
                  <option value={3}>3 hops</option>
                  <option value={4}>4 hops</option>
                </select>
              </div>
            </div>

            <label className="row-wrap clickable-label">
              <input
                type="checkbox"
                checked={enableRerank}
                onChange={(e) => setEnableRerank(e.target.checked)}
              />
              Enable Discovery Engine Semantic Reranker
            </label>

            <div>
              <label className="field-label" htmlFor="analyst-prompt">
                Analyst Prompt
              </label>
              <textarea
                id="analyst-prompt"
                className="textarea-input"
                value={queryText}
                onChange={(e) => {
                  setQueryText(e.target.value);
                  setSelectedQuestionId(null);
                }}
              />
            </div>

            <div className="row-wrap">
              <button
                type="button"
                id="run-investigation-btn"
                className="btn btn-primary flex-1"
                disabled={isRunning || !queryText.trim()}
                onClick={handleRun}
              >
                <Play size={14} aria-hidden="true" />
                {isRunning ? "Running…" : "Run Investigation"}
              </button>
              {isRunning && (
                <button type="button" className="btn" onClick={handleStop}>
                  <Square size={13} aria-hidden="true" />
                  Stop
                </button>
              )}
            </div>

            {runError && (
              <div role="alert" className="error-banner">
                {runError}
              </div>
            )}

            <div className="stack-sm">
              <div className="field-label">Verified Multi-Hop Benchmark Scenarios</div>
              {pack.questions.map((q) => (
                <button
                  key={q.question_id}
                  type="button"
                  className={`preset-btn${
                    selectedQuestionId === q.question_id ? " selected" : ""
                  }`}
                  onClick={() => {
                    setSelectedQuestionId(q.question_id);
                    setQueryText(q.question);
                    setMaxHops(q.hop_depth);
                    setDirection(q.direction);
                  }}
                >
                  <div className="preset-meta">
                    <span>{q.question_id}</span>
                    <span>·</span>
                    <span>{q.hop_depth}-hop {q.direction}</span>
                  </div>
                  <div className="preset-text">{q.question}</div>
                </button>
              ))}
            </div>
          </div>
        </aside>

        {/* Center: Force-Directed Graph Canvas + Evidence Inspector */}
        <div className="stack-md">
          {graph ? (
            <GraphCanvas
              nodes={graph.nodes}
              edges={graph.edges}
              ontology={graph.ontology}
              highlightedNodeIds={highlightedNodeIds}
              highlightedEdgeIds={highlightedEdgeIds}
              selectedNodeId={selectedNode?.entity_id ?? null}
              selectedEdgeId={selectedEdge?.edge_id ?? null}
              onSelectNode={(n) => {
                setSelectedNode(n);
                setSelectedEdge(null);
                setSelectedChunk(null);
              }}
              onSelectEdge={(e) => {
                setSelectedEdge(e);
                setSelectedNode(null);
                setSelectedChunk(null);
              }}
            />
          ) : (
            <div className="panel">
              <div className="panel-body">Loading knowledge graph…</div>
            </div>
          )}

          <EvidenceInspector
            selectedNode={selectedNode}
            selectedEdge={selectedEdge}
            selectedChunk={selectedChunk}
            chunksById={chunksById}
            nodesById={nodesById}
            onSelectChunk={(c) => {
              setSelectedChunk(c);
              setSelectedNode(null);
              setSelectedEdge(null);
            }}
          />
        </div>

        {/* Right: Live SSE Pipeline Stage Rail + Cited Executive Brief */}
        <div className="explorer-right stack-md">
          <StageRail events={stageEvents} isRunning={isRunning} />
          {result && (
            <CitedAnswer result={result} onClickCitation={handleCitationClick} />
          )}
        </div>
      </div>
    </div>
  );
}
