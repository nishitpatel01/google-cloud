"""Token pricing, split Engine vs Judge cost math, and Monthly GCP TCO estimator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import MODEL_CATALOG

# Published reference unit rates (US us-central1, 2026)
EMBEDDING_USD_PER_1K_CHARS = 0.0001
RERANK_USD_PER_1K_QUERIES = 1.00
BQ_ONDEMAND_USD_PER_TB = 6.25
BQ_ENTERPRISE_AUTOSCALE_USD_PER_SLOT_HOUR = 0.06
SPANNER_100PU_MONTHLY_USD = 65.70
SPANNER_1NODE_MONTHLY_USD = 657.00
CLOUD_RUN_WARM_1VCPU_MONTHLY_USD = 58.00
ALB_IAP_MONTHLY_USD = 22.00


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


def compute_llm_cost_usd(model_id: str, usage: TokenUsage) -> float:
    """Compute exact USD cost for a model invocation from measured token counts."""
    spec = MODEL_CATALOG[model_id]
    cost = (
        (max(0, usage.input_tokens) / 1_000_000.0) * spec.input_per_1m_usd
        + (max(0, usage.output_tokens) / 1_000_000.0) * spec.output_per_1m_usd
    )
    return round(cost, 6)


@dataclass(frozen=True)
class TcoEstimate:
    tier: Literal["tier1_bigquery_serverless", "tier2_spanner_enterprise"]
    monthly_queries: int
    synthesis_model: str
    llm_monthly_usd: float
    graph_compute_monthly_usd: float
    cloud_run_and_ops_monthly_usd: float
    total_gcp_monthly_usd: float
    unit_cost_per_query_usd: float
    manual_baseline_monthly_usd: float
    net_monthly_savings_usd: float
    roi_multiple: float


def estimate_monthly_tco(
    *,
    monthly_queries: int,
    synthesis_model: str,
    manual_baseline_cost_usd: float,
    tier: Literal["tier1_bigquery_serverless", "tier2_spanner_enterprise"] = "tier1_bigquery_serverless",
    avg_input_tokens: int = 16_000,
    avg_output_tokens: int = 1_800,
    spanner_processing_units: int = 100,
) -> TcoEstimate:
    """Estimate transparent monthly GCP TCO vs. manual investigation baseline."""
    if monthly_queries < 0:
        raise ValueError("monthly_queries must be >= 0")
    if manual_baseline_cost_usd <= 0:
        raise ValueError("manual_baseline_cost_usd must be > 0")
    if synthesis_model not in MODEL_CATALOG:
        raise ValueError(f"Unknown synthesis_model: {synthesis_model!r}")

    per_query_llm = compute_llm_cost_usd(
        synthesis_model, TokenUsage(input_tokens=avg_input_tokens, output_tokens=avg_output_tokens)
    )
    per_query_rerank = RERANK_USD_PER_1K_QUERIES / 1000.0
    llm_monthly = round(monthly_queries * (per_query_llm + per_query_rerank), 2)

    if tier == "tier1_bigquery_serverless":
        # 0-slot baseline Enterprise reservation bills only burst slot-seconds (~50 slots for ~1.5s/query)
        slot_hours = (monthly_queries * 50.0 * 1.5) / 3600.0
        graph_compute = round(1.50 + slot_hours * BQ_ENTERPRISE_AUTOSCALE_USD_PER_SLOT_HOUR, 2)
        cloud_run_ops = round(0.50 + (monthly_queries / 1000.0) * 1.25, 2)
    else:
        spanner_base = (spanner_processing_units / 100.0) * SPANNER_100PU_MONTHLY_USD
        graph_compute = round(spanner_base + (monthly_queries / 1000.0) * 0.40, 2)
        cloud_run_ops = round(CLOUD_RUN_WARM_1VCPU_MONTHLY_USD + ALB_IAP_MONTHLY_USD + (monthly_queries / 1000.0) * 1.40, 2)

    total_gcp = round(llm_monthly + graph_compute + cloud_run_ops, 2)
    unit_cost = round(total_gcp / monthly_queries, 4) if monthly_queries > 0 else 0.0
    # Conservative 10% of queries replace a full manual investigation (rest are exploratory follow-ups)
    manual_monthly = round((monthly_queries * 0.10) * manual_baseline_cost_usd, 2)
    net_savings = round(manual_monthly - total_gcp, 2)
    roi = round(manual_monthly / total_gcp, 1) if total_gcp > 0 else 0.0

    return TcoEstimate(
        tier=tier,
        monthly_queries=monthly_queries,
        synthesis_model=synthesis_model,
        llm_monthly_usd=llm_monthly,
        graph_compute_monthly_usd=graph_compute,
        cloud_run_and_ops_monthly_usd=cloud_run_ops,
        total_gcp_monthly_usd=total_gcp,
        unit_cost_per_query_usd=unit_cost,
        manual_baseline_monthly_usd=manual_monthly,
        net_monthly_savings_usd=net_savings,
        roi_multiple=roi,
    )
