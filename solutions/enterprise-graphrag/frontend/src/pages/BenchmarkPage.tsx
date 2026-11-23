import { Award, Download } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { estimateTco, fetchBenchmarkSummary } from "../api/client";
import type {
  BenchmarkSummaryResponse,
  PackSummary,
  TcoEstimateResponse,
} from "../types/graphrag";
import {
  buildExecutiveBriefCsv,
  formatMs,
  formatPercent,
  formatUsd,
} from "../utils/format";

interface BenchmarkPageProps {
  pack: PackSummary;
  synthesisModel: string;
}

function friendlyEngineName(engineId: string, fallback: string): string {
  if (engineId === "bq_graphrag") return "Graph Intelligence (BigQuery)";
  if (engineId === "adk_graph_coordinator") return "Autonomous Risk Agent (ADK 2.15)";
  if (engineId === "flat_vector_rag") return "Standard AI Search (Baseline)";
  if (engineId === "spanner_graphrag") return "Real-Time Graph (Spanner Tier-2)";
  return fallback;
}

export function BenchmarkPage({ pack, synthesisModel }: BenchmarkPageProps) {
  const [monthlyQueries, setMonthlyQueries] = useState(5000);
  const [spannerPu, setSpannerPu] = useState(100);
  const [tier1, setTier1] = useState<TcoEstimateResponse | null>(null);
  const [tier2, setTier2] = useState<TcoEstimateResponse | null>(null);
  const [benchmarks, setBenchmarks] = useState<BenchmarkSummaryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    Promise.all([
      estimateTco({
        pack_id: pack.pack_id,
        monthly_queries: monthlyQueries,
        synthesis_model: synthesisModel,
        tier: "tier1_bigquery_serverless",
      }),
      estimateTco({
        pack_id: pack.pack_id,
        monthly_queries: monthlyQueries,
        synthesis_model: synthesisModel,
        tier: "tier2_spanner_enterprise",
        spanner_processing_units: spannerPu,
      }),
    ])
      .then(([t1, t2]) => {
        if (!cancelled) {
          setTier1(t1);
          setTier2(t2);
        }
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [pack.pack_id, monthlyQueries, synthesisModel, spannerPu]);

  useEffect(() => {
    let cancelled = false;
    fetchBenchmarkSummary(pack.pack_id)
      .then((res) => {
        if (!cancelled) setBenchmarks(res);
      })
      .catch(() => {
        if (!cancelled) setBenchmarks(null);
      });
    return () => {
      cancelled = true;
    };
  }, [pack.pack_id]);

  const perQuestionById = useMemo(() => {
    const map = new Map<string, BenchmarkSummaryResponse["per_question"][number]>();
    for (const row of benchmarks?.per_question ?? []) {
      map.set(row.question_id, row);
    }
    return map;
  }, [benchmarks]);

  const handleExportCsv = () => {
    if (!tier1 || !tier2) return;
    const rows: Array<{ metric: string; value: string | number; notes: string }> = [
      { metric: "Industry Scenario", value: pack.title, notes: pack.industry },
      { metric: "Target Executive Role", value: pack.persona, notes: pack.pack_id },
      {
        metric: "Verified Public Filing Coverage",
        value: `${Math.round(pack.real_public_overall_ratio * 100)}%`,
        notes: pack.primary_sources.join(" | "),
      },
      { metric: "Monthly Investigation Volume", value: monthlyQueries, notes: synthesisModel },
      {
        metric: "Tier-1 Serverless BigQuery Graph Monthly Cost",
        value: formatUsd(tier1.total_gcp_monthly_usd),
        notes: `Unit cost ${formatUsd(tier1.unit_cost_per_query_usd)}/investigation (0-slot autoscale)`,
      },
      {
        metric: "Tier-2 Spanner Real-Time Graph Monthly Cost",
        value: formatUsd(tier2.total_gcp_monthly_usd),
        notes: `${spannerPu} Spanner PU + warm Cloud Run + Cloud IAP`,
      },
      {
        metric: "Manual Investigation Baseline (Monthly)",
        value: formatUsd(tier1.manual_baseline_monthly_usd),
        notes: `${formatUsd(pack.manual_baseline_cost_usd)}/audit × 10% escalated investigations`,
      },
      {
        metric: "Estimated Net Monthly Savings (Tier-1)",
        value: formatUsd(tier1.net_monthly_savings_usd),
        notes: `${tier1.roi_multiple}x ROI multiple`,
      },
      {
        metric: "Estimated Annual Net Savings (Tier-1)",
        value: formatUsd(tier1.net_monthly_savings_usd * 12),
        notes: "12-month annualized projection",
      },
    ];

    for (const lb of benchmarks?.leaderboard ?? []) {
      rows.push({
        metric: `Measured Eval · ${friendlyEngineName(lb.engine_id, lb.engine_label)}`,
        value: `Recall ${formatPercent(lb.avg_path_recall)} | Groundedness ${formatPercent(lb.avg_groundedness)} | Composite ${formatPercent(lb.avg_composite_score)}`,
        notes: `${lb.runs_count} recorded live runs (${lb.synthesis_model} judged by ${lb.judge_model ?? "none"}); Engine ${formatUsd(lb.avg_engine_cost_usd)}, Judge ${formatUsd(lb.avg_judge_cost_usd)}`,
      });
    }

    const csv = buildExecutiveBriefCsv(rows);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `executive-roi-brief-${pack.pack_id}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>Business ROI, Evaluation Leaderboard &amp; Cost Estimator</h1>
          <p className="page-subtitle">
            Review real recorded Vertex AI evaluations across every multi-tier benchmark scenario,
            model serverless vs. real-time cloud spend against manual analyst effort, and export a
            spreadsheet-safe executive brief.
          </p>
        </div>
        <button
          type="button"
          id="export-brief-btn"
          className="btn btn-primary"
          disabled={!tier1 || !tier2}
          onClick={handleExportCsv}
        >
          <Download size={14} aria-hidden="true" />
          Download Executive Brief (CSV)
        </button>
      </div>

      {error && (
        <div role="alert" className="error-banner">
          {error}
        </div>
      )}

      {/* Measured Evaluation Leaderboard from Recorded Live Runs */}
      {benchmarks && benchmarks.leaderboard.length > 0 && (
        <section className="panel" aria-label="Measured evaluation leaderboard">
          <div className="panel-header">
            <div className="row-wrap">
              <Award size={15} aria-hidden="true" />
              <h2 className="panel-title">
                Measured Evaluation Leaderboard · {pack.title}
              </h2>
            </div>
            <span className="badge badge-verified">
              {benchmarks.total_recordings} Real Recorded Live Runs (Zero Hardcoded Scores)
            </span>
          </div>
          <div className="panel-body table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Approach</th>
                  <th>Runs</th>
                  <th>Multi-Tier Recall</th>
                  <th>Groundedness</th>
                  <th>Answer Quality</th>
                  <th>Evidence Precision</th>
                  <th>Composite</th>
                  <th>Citations Verified</th>
                  <th>Avg Latency</th>
                  <th>Engine Cost</th>
                  <th>Judge Cost</th>
                </tr>
              </thead>
              <tbody>
                {benchmarks.leaderboard.map((row) => {
                  const isBaseline = row.engine_id === "flat_vector_rag";
                  return (
                    <tr key={`${row.engine_id}-${row.synthesis_model}-${row.judge_model}`}>
                      <td>
                        <strong>{friendlyEngineName(row.engine_id, row.engine_label)}</strong>
                        <div className="muted-copy mono">
                          {row.synthesis_model} · judge: {row.judge_model ?? "none"}
                        </div>
                      </td>
                      <td className="mono">{row.runs_count}</td>
                      <td>
                        <span
                          className={`badge mono ${isBaseline ? "badge-danger" : "badge-verified"}`}
                        >
                          {formatPercent(row.avg_path_recall)}
                        </span>
                      </td>
                      <td className="mono">{formatPercent(row.avg_groundedness)}</td>
                      <td className="mono">{formatPercent(row.avg_qa_relevance)}</td>
                      <td className="mono">{formatPercent(row.avg_context_precision)}</td>
                      <td>
                        <span
                          className={`badge mono ${isBaseline ? "" : "badge-verified"}`}
                        >
                          {formatPercent(row.avg_composite_score)}
                        </span>
                      </td>
                      <td className="mono">{formatPercent(row.provenance_pass_rate)}</td>
                      <td className="mono">{formatMs(row.avg_duration_ms)}</td>
                      <td className="mono">{formatUsd(row.avg_engine_cost_usd)}</td>
                      <td className="mono">{formatUsd(row.avg_judge_cost_usd)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="panel" aria-label="Workload sizing parameters">
        <div className="panel-header">
          <h2 className="panel-title">Workload Sizing Controls</h2>
          <span className="badge">AI Model: {synthesisModel}</span>
        </div>
        <div className="panel-body">
          <div className="grid-tco-bar">
            <div>
              <label className="field-label" htmlFor="monthly-queries-slider">
                Monthly Risk Investigations: <strong>{monthlyQueries.toLocaleString()}</strong>
              </label>
              <input
                id="monthly-queries-slider"
                type="range"
                min={500}
                max={50000}
                step={500}
                value={monthlyQueries}
                onChange={(e) => setMonthlyQueries(Number(e.target.value))}
                className="w-full"
              />
            </div>
            <div>
              <label className="field-label" htmlFor="spanner-pu-select">
                Real-Time Tier Capacity (Spanner Processing Units)
              </label>
              <select
                id="spanner-pu-select"
                className="select-control w-full"
                value={spannerPu}
                onChange={(e) => setSpannerPu(Number(e.target.value))}
              >
                <option value={100}>100 PU (Pilot / Sub-10ms)</option>
                <option value={300}>300 PU (Departmental)</option>
                <option value={1000}>1,000 PU (1 Full Node · Global Enterprise)</option>
              </select>
            </div>
          </div>
        </div>
      </section>

      {tier1 && tier2 && (
        <div className="compare-grid">
          <section className="panel" aria-label="Tier 1 Serverless Cost and ROI">
            <div className="panel-header">
              <h2 className="panel-title">Tier 1 · Serverless Cloud (BigQuery Graph)</h2>
              <span className="badge badge-verified">Recommended · $0–$3/mo Idle</span>
            </div>
            <div className="panel-body stack-md">
              <div className="kpi-grid">
                <div className="kpi-box">
                  <div className="kpi-label">Monthly Cloud Cost</div>
                  <div className="kpi-value">{formatUsd(tier1.total_gcp_monthly_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Cost / Investigation</div>
                  <div className="kpi-value">{formatUsd(tier1.unit_cost_per_query_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Est. Annual Savings</div>
                  <div className="kpi-value">
                    {formatUsd(tier1.net_monthly_savings_usd * 12)}
                  </div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">ROI Multiple</div>
                  <div className="kpi-value">{tier1.roi_multiple}x</div>
                </div>
              </div>
              <table className="data-table">
                <tbody>
                  <tr>
                    <td>Vertex AI Reasoning &amp; Semantic Reranking</td>
                    <td className="mono">{formatUsd(tier1.llm_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>BigQuery Property Graph (0-slot autoscale)</td>
                    <td className="mono">{formatUsd(tier1.graph_compute_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>Serverless App Hosting (Cloud Run) &amp; Storage</td>
                    <td className="mono">{formatUsd(tier1.cloud_run_and_ops_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>
                      <strong>Estimated Net Monthly Savings</strong>
                    </td>
                    <td className="mono">
                      <strong>{formatUsd(tier1.net_monthly_savings_usd)}</strong>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel" aria-label="Tier 2 Real-Time Enterprise Cost and ROI">
            <div className="panel-header">
              <h2 className="panel-title">
                Tier 2 · Real-Time Enterprise (Spanner Graph · {spannerPu} PU)
              </h2>
              <span className="badge">Sub-10ms SLA Option</span>
            </div>
            <div className="panel-body stack-md">
              <div className="kpi-grid">
                <div className="kpi-box">
                  <div className="kpi-label">Monthly Cloud Cost</div>
                  <div className="kpi-value">{formatUsd(tier2.total_gcp_monthly_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Cost / Investigation</div>
                  <div className="kpi-value">{formatUsd(tier2.unit_cost_per_query_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Est. Annual Savings</div>
                  <div className="kpi-value">
                    {formatUsd(tier2.net_monthly_savings_usd * 12)}
                  </div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">ROI Multiple</div>
                  <div className="kpi-value">{tier2.roi_multiple}x</div>
                </div>
              </div>
              <table className="data-table">
                <tbody>
                  <tr>
                    <td>Vertex AI Reasoning &amp; Semantic Reranking</td>
                    <td className="mono">{formatUsd(tier2.llm_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>Spanner Graph Real-Time Engine ({spannerPu} PU)</td>
                    <td className="mono">{formatUsd(tier2.graph_compute_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>Warm App Hosting, Enterprise Identity (IAP) &amp; Ops</td>
                    <td className="mono">{formatUsd(tier2.cloud_run_and_ops_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>
                      <strong>Estimated Net Monthly Savings</strong>
                    </td>
                    <td className="mono">
                      <strong>{formatUsd(tier2.net_monthly_savings_usd)}</strong>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      <section className="panel" aria-label="Verified multi-tier benchmark suite">
        <div className="panel-header">
          <h2 className="panel-title">
            Verified Multi-Tier Benchmark Scenarios ({pack.questions.length} Scenarios)
          </h2>
          <span className="badge badge-verified">
            Manual baseline: {formatUsd(pack.manual_baseline_cost_usd)} ·{" "}
            {pack.manual_baseline_minutes} min per audit
          </span>
        </div>
        <div className="panel-body table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Scenario</th>
                <th>Depth &amp; Scope</th>
                <th>Business Question</th>
                <th>Recorded Accuracy (Flat vs. Graph)</th>
                <th>Why Standard Search Falls Short</th>
              </tr>
            </thead>
            <tbody>
              {pack.questions.map((q, idx) => {
                const recorded = perQuestionById.get(q.question_id);
                const flatArm = recorded?.arms.flat_vector_rag;
                const graphArm = recorded?.arms.bq_graphrag;
                return (
                  <tr key={q.question_id}>
                    <td className="mono">#{idx + 1}</td>
                    <td>
                      {q.hop_depth}-tier ({q.direction})
                    </td>
                    <td>{q.question}</td>
                    <td>
                      {flatArm && graphArm ? (
                        <div className="row-wrap">
                          <span className="badge badge-danger mono">
                            Flat {formatPercent(flatArm.path_recall)}
                          </span>
                          <span className="badge badge-verified mono">
                            Graph {formatPercent(graphArm.path_recall)}
                          </span>
                        </div>
                      ) : (
                        <span className="muted-copy mono">
                          {q.expected_entity_ids.length} entities ·{" "}
                          {q.expected_edge_ids.length} links
                        </span>
                      )}
                    </td>
                    <td className="secondary-cell">{q.why_flat_vector_misses}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
