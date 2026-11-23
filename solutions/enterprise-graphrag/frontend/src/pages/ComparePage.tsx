import { GitCompare, Play, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { streamCompare } from "../api/client";
import { CitedAnswer } from "../components/CitedAnswer";
import { StageRail } from "../components/StageRail";
import type {
  ComparisonPayload,
  PackSummary,
  StageEvent,
} from "../types/graphrag";
import { formatPercent } from "../utils/format";

interface ComparePageProps {
  pack: PackSummary;
  mode: "replay" | "live";
  synthesisModel: string;
}

export function ComparePage({ pack, mode, synthesisModel }: ComparePageProps) {
  const [questionIdx, setQuestionIdx] = useState(0);
  const [graphEngine, setGraphEngine] = useState<"bq_graphrag" | "adk_graph_coordinator">(
    "bq_graphrag"
  );
  const [isRunning, setIsRunning] = useState(false);
  const [flatStages, setFlatStages] = useState<StageEvent[]>([]);
  const [graphStages, setGraphStages] = useState<StageEvent[]>([]);
  const [comparison, setComparison] = useState<ComparisonPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setQuestionIdx(0);
    setComparison(null);
    setFlatStages([]);
    setGraphStages([]);
    setError(null);
  }, [pack.pack_id]);

  const activeQ = pack.questions[questionIdx] ?? pack.questions[0];

  const handleRunCompare = async () => {
    if (!activeQ) return;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setIsRunning(true);
    setFlatStages([]);
    setGraphStages([]);
    setComparison(null);
    setError(null);

    try {
      await streamCompare(
        {
          pack_id: pack.pack_id,
          query: activeQ.question,
          graph_engine_id: graphEngine,
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
              setFlatStages((prev) => [...prev, ev]);
            } else {
              setGraphStages((prev) => [...prev, ev]);
            }
          },
          onComparison: (res) => setComparison(res),
          onError: (msg) => setError(msg),
        },
        ctrl.signal
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setError((err as Error).message);
      }
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>Flat Vector RAG vs. Hybrid GraphRAG Battle Lab</h1>
          <p className="page-subtitle">
            Execute both architectures side by side on the exact same multi-hop question to
            measure structural entity/edge recall, citation provenance, latency, and unit cost.
          </p>
        </div>
        <span className="badge">{pack.title}</span>
      </div>

      <section className="panel" aria-label="Comparison controls">
        <div className="panel-body stack-md">
          <div className="grid-compare-bar">
            <div>
              <label className="field-label" htmlFor="compare-question-select">
                Golden Multi-Hop Benchmark Question
              </label>
              <select
                id="compare-question-select"
                className="select-control w-full"
                value={questionIdx}
                onChange={(e) => setQuestionIdx(Number(e.target.value))}
              >
                {pack.questions.map((q, idx) => (
                  <option key={q.question_id} value={idx}>
                    [{q.question_id} · {q.hop_depth}-hop {q.direction}] {q.question}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="field-label" htmlFor="compare-graph-engine">
                GraphRAG Arm
              </label>
              <select
                id="compare-graph-engine"
                className="select-control w-full"
                value={graphEngine}
                onChange={(e) =>
                  setGraphEngine(e.target.value as "bq_graphrag" | "adk_graph_coordinator")
                }
              >
                <option value="bq_graphrag">BigQuery Property Graph (ISO GQL)</option>
                <option value="adk_graph_coordinator">Google ADK 2.15 Coordinator</option>
              </select>
            </div>

            <div className="row-wrap">
              <button
                type="button"
                id="run-compare-btn"
                className="btn btn-primary"
                disabled={isRunning || !activeQ}
                onClick={handleRunCompare}
              >
                <Play size={14} aria-hidden="true" />
                {isRunning ? "Comparing…" : "Run Side-by-Side"}
              </button>
              {isRunning && (
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    abortRef.current?.abort();
                    setIsRunning(false);
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
              <strong>Why single-hop vector search struggles here:</strong>{" "}
              {activeQ.why_flat_vector_misses}
            </div>
          )}

          {error && (
            <div role="alert" className="error-banner">
              {error}
            </div>
          )}
        </div>
      </section>

      {comparison && (
        <div className="recall-banner" role="status">
          <div className="row-wrap gap-10">
            <GitCompare size={18} aria-hidden="true" />
            <div>
              <strong>Measured Multi-Hop Path Recall Lift:</strong> Flat Vector RAG{" "}
              <span className="mono">
                {formatPercent(comparison.flat_vector_rag.recall?.path_recall)}
              </span>{" "}
              vs. Hybrid GraphRAG{" "}
              <span className="mono">
                {formatPercent(comparison.graphrag.recall?.path_recall)}
              </span>
            </div>
          </div>
          {comparison.path_recall_delta !== null && (
            <span className="badge badge-verified badge-lg">
              +{Math.round(comparison.path_recall_delta * 100)} pts GraphRAG advantage
            </span>
          )}
        </div>
      )}

      <div className="compare-grid">
        <div className="stack-md">
          <StageRail
            title="Arm A · Flat Vector RAG (Chunks Only)"
            events={flatStages}
            isRunning={isRunning && !comparison}
          />
          {comparison && <CitedAnswer result={comparison.flat_vector_rag} />}
        </div>

        <div className="stack-md">
          <StageRail
            title={`Arm B · ${
              graphEngine === "adk_graph_coordinator"
                ? "Google ADK 2.15 Graph Coordinator"
                : "BigQuery Property Graph (ISO GQL)"
            }`}
            events={graphStages}
            isRunning={isRunning && !comparison}
          />
          {comparison && <CitedAnswer result={comparison.graphrag} />}
        </div>
      </div>
    </div>
  );
}
