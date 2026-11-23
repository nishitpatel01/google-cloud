import { Download } from "lucide-react";
import { useEffect, useState } from "react";
import { estimateTco } from "../api/client";
import type { PackSummary, TcoEstimateResponse } from "../types/graphrag";
import { buildExecutiveBriefCsv, formatUsd } from "../utils/format";

interface BenchmarkPageProps {
  pack: PackSummary;
  synthesisModel: string;
}

export function BenchmarkPage({ pack, synthesisModel }: BenchmarkPageProps) {
  const [monthlyQueries, setMonthlyQueries] = useState(5000);
  const [spannerPu, setSpannerPu] = useState(100);
  const [tier1, setTier1] = useState<TcoEstimateResponse | null>(null);
  const [tier2, setTier2] = useState<TcoEstimateResponse | null>(null);
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

  const handleExportCsv = () => {
    if (!tier1 || !tier2) return;
    const csv = buildExecutiveBriefCsv([
      { metric: "Industry Pack", value: pack.title, notes: pack.industry },
      { metric: "Executive Persona", value: pack.persona, notes: pack.pack_id },
      {
        metric: "Real Public Data Share",
        value: `${Math.round(pack.real_public_overall_ratio * 100)}%`,
        notes: pack.primary_sources.join(" | "),
      },
      { metric: "Monthly Query Volume", value: monthlyQueries, notes: synthesisModel },
      {
        metric: "Tier-1 BigQuery Property Graph Monthly GCP TCO",
        value: formatUsd(tier1.total_gcp_monthly_usd),
        notes: `Unit cost ${formatUsd(tier1.unit_cost_per_query_usd)}/query (0-slot Enterprise autoscale)`,
      },
      {
        metric: "Tier-2 Spanner Graph Monthly GCP TCO",
        value: formatUsd(tier2.total_gcp_monthly_usd),
        notes: `${spannerPu} Spanner PU + warm Cloud Run + Cloud IAP`,
      },
      {
        metric: "Manual Investigation Baseline (Monthly)",
        value: formatUsd(tier1.manual_baseline_monthly_usd),
        notes: `${formatUsd(pack.manual_baseline_cost_usd)}/investigation × 10% full audits`,
      },
      {
        metric: "Tier-1 Estimated Net Monthly Savings",
        value: formatUsd(tier1.net_monthly_savings_usd),
        notes: `${tier1.roi_multiple}x ROI multiple`,
      },
    ]);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `executive-brief-${pack.pack_id}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>Benchmark Suite, Monthly GCP TCO &amp; Executive ROI</h1>
          <p className="page-subtitle">
            Compare Tier-1 Serverless BigQuery Property Graph (0-slot autoscale) against
            Tier-2 Spanner Graph at your query volume and export a formula-safe CSV brief.
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
          Export CSV Executive Brief
        </button>
      </div>

      {error && (
        <div role="alert" className="error-banner">
          {error}
        </div>
      )}

      <section className="panel" aria-label="TCO calculator parameters">
        <div className="panel-header">
          <h2 className="panel-title">Workload Sizing Controls</h2>
          <span className="badge mono">Model: {synthesisModel}</span>
        </div>
        <div className="panel-body">
          <div className="grid-tco-bar">
            <div>
              <label className="field-label" htmlFor="monthly-queries-slider">
                Monthly Multi-Hop Queries: <strong>{monthlyQueries.toLocaleString()}</strong>
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
                Tier-2 Spanner Processing Units
              </label>
              <select
                id="spanner-pu-select"
                className="select-control w-full"
                value={spannerPu}
                onChange={(e) => setSpannerPu(Number(e.target.value))}
              >
                <option value={100}>100 PU (Pilot / Low-Latency)</option>
                <option value={300}>300 PU (Departmental)</option>
                <option value={1000}>1,000 PU (1 Full Node · Enterprise)</option>
              </select>
            </div>
          </div>
        </div>
      </section>

      {tier1 && tier2 && (
        <div className="compare-grid">
          <section className="panel" aria-label="Tier 1 Serverless BigQuery TCO">
            <div className="panel-header">
              <h2 className="panel-title">Tier-1 Serverless · BigQuery Property Graph</h2>
              <span className="badge badge-verified">Default · $0–$3/mo Idle</span>
            </div>
            <div className="panel-body stack-md">
              <div className="kpi-grid">
                <div className="kpi-box">
                  <div className="kpi-label">Monthly GCP TCO</div>
                  <div className="kpi-value">{formatUsd(tier1.total_gcp_monthly_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Unit Cost / Query</div>
                  <div className="kpi-value">{formatUsd(tier1.unit_cost_per_query_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Est. ROI Multiple</div>
                  <div className="kpi-value">{tier1.roi_multiple}x</div>
                </div>
              </div>
              <table className="data-table">
                <tbody>
                  <tr>
                    <td>Vertex AI LLM + Reranker</td>
                    <td className="mono">{formatUsd(tier1.llm_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>BigQuery Property Graph (0-slot autoscale)</td>
                    <td className="mono">{formatUsd(tier1.graph_compute_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>Cloud Run (scale-to-zero) + Storage/Ops</td>
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

          <section className="panel" aria-label="Tier 2 Spanner Graph TCO">
            <div className="panel-header">
              <h2 className="panel-title">Tier-2 Enterprise · Spanner Graph ({spannerPu} PU)</h2>
              <span className="badge">Sub-10ms Toggle</span>
            </div>
            <div className="panel-body stack-md">
              <div className="kpi-grid">
                <div className="kpi-box">
                  <div className="kpi-label">Monthly GCP TCO</div>
                  <div className="kpi-value">{formatUsd(tier2.total_gcp_monthly_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Unit Cost / Query</div>
                  <div className="kpi-value">{formatUsd(tier2.unit_cost_per_query_usd)}</div>
                </div>
                <div className="kpi-box">
                  <div className="kpi-label">Est. ROI Multiple</div>
                  <div className="kpi-value">{tier2.roi_multiple}x</div>
                </div>
              </div>
              <table className="data-table">
                <tbody>
                  <tr>
                    <td>Vertex AI LLM + Reranker</td>
                    <td className="mono">{formatUsd(tier2.llm_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>Spanner Graph ({spannerPu} PU)</td>
                    <td className="mono">{formatUsd(tier2.graph_compute_monthly_usd)}</td>
                  </tr>
                  <tr>
                    <td>Warm Cloud Run + Cloud IAP + Ops</td>
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

      <section className="panel" aria-label="Pack golden multi-hop benchmark questions">
        <div className="panel-header">
          <h2 className="panel-title">
            Verified Multi-Hop Golden Benchmark Suite ({pack.questions.length} Questions)
          </h2>
          <span className="badge badge-verified">
            Manual baseline: {formatUsd(pack.manual_baseline_cost_usd)} ·{" "}
            {pack.manual_baseline_minutes} min
          </span>
        </div>
        <div className="panel-body table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Hops / Dir</th>
                <th>Multi-Hop Question</th>
                <th>Expected Entities &amp; Edges</th>
                <th>Why Flat Vector Misses</th>
              </tr>
            </thead>
            <tbody>
              {pack.questions.map((q) => (
                <tr key={q.question_id}>
                  <td className="mono">{q.question_id}</td>
                  <td className="mono">
                    {q.hop_depth}-hop {q.direction}
                  </td>
                  <td>{q.question}</td>
                  <td className="mono">
                    {q.expected_entity_ids.length} entities · {q.expected_edge_ids.length} edges
                  </td>
                  <td className="secondary-cell">{q.why_flat_vector_misses}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
