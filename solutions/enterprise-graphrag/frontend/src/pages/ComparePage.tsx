import { GitCompare, Play, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { fetchPackGraph, streamCompare } from "../api/client";
import { CitedAnswer } from "../components/CitedAnswer";
import { StageRail } from "../components/StageRail";
import type {
  ComparisonPayload,
  PackGraphResponse,
  PackSummary,
  StageEvent,
} from "../types/graphrag";
import { formatPercent } from "../utils/format";

interface ComparePageProps {
  pack: PackSummary;
  mode: "replay" | "live";
  synthesisModel: string;
  onRunningChange?: (running: boolean) => void;
}

interface ComparePackState {
  questionIdx: number;
  graphEngine: "bq_graphrag" | "adk_graph_coordinator";
  isRunning: boolean;
  flatStages: StageEvent[];
  graphStages: StageEvent[];
  comparison: ComparisonPayload | null;
  error: string | null;
}

function createInitialCompareState(): ComparePackState {
  return {
    questionIdx: 0,
    graphEngine: "bq_graphrag",
    isRunning: false,
    flatStages: [],
    graphStages: [],
    comparison: null,
    error: null,
  };
}

export function ComparePage({
  pack,
  mode,
  synthesisModel,
  onRunningChange,
}: ComparePageProps) {
  const [stateByPack, setStateByPack] = useState<Record<string, ComparePackState>>(() => ({
    [pack.pack_id]: createInitialCompareState(),
  }));
  const [graphCache, setGraphCache] = useState<Record<string, PackGraphResponse>>({});
  const abortRefs = useRef<Record<string, AbortController>>({});

  useEffect(() => {
    setStateByPack((prev) =>
      prev[pack.pack_id] ? prev : { ...prev, [pack.pack_id]: createInitialCompareState() }
    );
    if (graphCache[pack.pack_id]) return;
    let cancelled = false;
    fetchPackGraph(pack.pack_id)
      .then((g) => {
        if (!cancelled) setGraphCache((prev) => ({ ...prev, [pack.pack_id]: g }));
      })
      .catch(() => {
        // Non-blocking: citation chips degrade gracefully to raw IDs if graph cache isn't ready
      });
    return () => {
      cancelled = true;
    };
  }, [pack.pack_id, graphCache]);

  const anyRunning = useMemo(
    () => Object.values(stateByPack).some((s) => s.isRunning),
    [stateByPack]
  );

  useEffect(() => {
    onRunningChange?.(anyRunning);
  }, [anyRunning, onRunningChange]);

  const state = stateByPack[pack.pack_id] ?? createInitialCompareState();
  const activeQ = pack.questions[state.questionIdx] ?? pack.questions[0];
  const graph = graphCache[pack.pack_id] ?? null;

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

  const patchState = (
    targetPackId: string,
    updater: (prev: ComparePackState) => ComparePackState
  ) => {
    setStateByPack((prev) => {
      const base = prev[targetPackId] ?? createInitialCompareState();
      return { ...prev, [targetPackId]: updater(base) };
    });
  };

  const handleRunCompare = async () => {
    if (!activeQ) return;
    const pid = pack.pack_id;
    const current = stateByPack[pid] ?? createInitialCompareState();

    abortRefs.current[pid]?.abort();
    const ctrl = new AbortController();
    abortRefs.current[pid] = ctrl;

    patchState(pid, (s) => ({
      ...s,
      isRunning: true,
      flatStages: [],
      graphStages: [],
      comparison: null,
      error: null,
    }));

    try {
      await streamCompare(
        {
          pack_id: pid,
          query: activeQ.question,
          graph_engine_id: current.graphEngine,
          synthesis_model: synthesisModel,
          question_id: activeQ.question_id,
          max_hops: activeQ.hop_depth,
          direction: activeQ.direction,
          enable_rerank: true,
          mode,
        },
        {
          onStage: (ev) => {
            if (ev.arm === "flat_vector_rag") {
              patchState(pid, (s) => ({ ...s, flatStages: [...s.flatStages, ev] }));
            } else {
              patchState(pid, (s) => ({ ...s, graphStages: [...s.graphStages, ev] }));
            }
          },
          onComparison: (res) => patchState(pid, (s) => ({ ...s, comparison: res })),
          onError: (msg) => patchState(pid, (s) => ({ ...s, error: msg })),
        },
        ctrl.signal
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        patchState(pid, (s) => ({ ...s, error: (err as Error).message }));
      }
    } finally {
      patchState(pid, (s) => ({ ...s, isRunning: false }));
    }
  };

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>Standard AI Search vs. Graph Intelligence</h1>
          <p className="page-subtitle">
            Run the exact same multi-tier business question through both approaches side by side
            to see what standard document search misses — and compare accuracy, speed, and cost.
          </p>
        </div>
        <span className="badge">{pack.title}</span>
      </div>

      <section className="panel" aria-label="Comparison controls">
        <div className="panel-body stack-md">
          <div className="grid-compare-bar">
            <div>
              <label className="field-label" htmlFor="compare-question-select">
                Select a Multi-Tier Business Scenario
              </label>
              <select
                id="compare-question-select"
                className="select-control w-full"
                value={state.questionIdx}
                onChange={(e) =>
                  patchState(pack.pack_id, (s) => ({
                    ...s,
                    questionIdx: Number(e.target.value),
                  }))
                }
              >
                {pack.questions.map((q, idx) => (
                  <option key={q.question_id} value={idx}>
                    [Scenario {idx + 1} · {q.hop_depth}-tier] {q.question}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="field-label" htmlFor="compare-graph-engine">
                Graph Intelligence Engine
              </label>
              <select
                id="compare-graph-engine"
                className="select-control w-full"
                value={state.graphEngine}
                onChange={(e) =>
                  patchState(pack.pack_id, (s) => ({
                    ...s,
                    graphEngine: e.target.value as "bq_graphrag" | "adk_graph_coordinator",
                  }))
                }
              >
                <option value="bq_graphrag">Graph Intelligence (BigQuery Property Graph)</option>
                <option value="adk_graph_coordinator">
                  Autonomous Risk Agent (Google ADK 2.15)
                </option>
              </select>
            </div>

            <div className="row-wrap">
              <button
                type="button"
                id="run-compare-btn"
                className="btn btn-primary"
                disabled={state.isRunning || !activeQ}
                onClick={handleRunCompare}
              >
                <Play size={14} aria-hidden="true" />
                {state.isRunning ? "Comparing…" : "Compare Side by Side"}
              </button>
              {state.isRunning && (
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    abortRefs.current[pack.pack_id]?.abort();
                    patchState(pack.pack_id, (s) => ({ ...s, isRunning: false }));
                  }}
                >
                  <Square size={13} aria-hidden="true" />
                  Stop
                </button>
              )}
            </div>
          </div>

          {activeQ && (
            <div className="info-banner">
              <strong>Why standard document search misses hidden links here:</strong>{" "}
              {activeQ.why_flat_vector_misses}
            </div>
          )}

          {state.error && (
            <div role="alert" className="error-banner">
              {state.error}
            </div>
          )}
        </div>
      </section>

      {state.comparison && (
        <div className="recall-banner" role="status">
          <div className="row-wrap gap-10">
            <GitCompare size={18} aria-hidden="true" />
            <div>
              <strong>Multi-Tier Accuracy Comparison:</strong> Standard AI Search{" "}
              <span className="mono">
                {formatPercent(state.comparison.flat_vector_rag.recall?.path_recall)}
              </span>{" "}
              vs. Graph Intelligence{" "}
              <span className="mono">
                {formatPercent(state.comparison.graphrag.recall?.path_recall)}
              </span>
            </div>
          </div>
          {state.comparison.path_recall_delta !== null && (
            <span className="badge badge-verified badge-lg">
              +{Math.round(state.comparison.path_recall_delta * 100)} pts Graph Advantage
            </span>
          )}
        </div>
      )}

      <div className="compare-grid">
        <div className="stack-md">
          <StageRail
            title="Baseline · Standard AI Search (Documents Only)"
            events={state.flatStages}
            isRunning={state.isRunning && !state.comparison}
          />
          {state.comparison && (
            <CitedAnswer
              result={state.comparison.flat_vector_rag}
              nodesById={nodesById}
              edgesById={edgesById}
              chunksById={chunksById}
            />
          )}
        </div>

        <div className="stack-md">
          <StageRail
            title={`Recommended · ${
              state.graphEngine === "adk_graph_coordinator"
                ? "Autonomous Risk Agent (Google ADK 2.15)"
                : "Graph Intelligence (BigQuery)"
            }`}
            events={state.graphStages}
            isRunning={state.isRunning && !state.comparison}
          />
          {state.comparison && (
            <CitedAnswer
              result={state.comparison.graphrag}
              nodesById={nodesById}
              edgesById={edgesById}
              chunksById={chunksById}
            />
          )}
        </div>
      </div>
    </div>
  );
}
