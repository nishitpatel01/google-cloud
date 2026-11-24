"""Transparent Google Cloud unit pricing and Tier-1 vs. Tier-2 TCO & ROI modeling."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import MODEL_CATALOG


@dataclass(frozen=True)
class CostBreakdown:
    input_tokens: int
    output_tokens: int
    bq_bytes_billed: int
    workflow_steps: int
    model_cost_usd: float
    bq_cost_usd: float
    workflow_cost_usd: float
    total_usd: float


# Public US (us-central1) list pricing baselines
BQ_COST_PER_TB_USD = 6.25
CLOUD_WORKFLOWS_COST_PER_1K_STEPS_USD = 0.01
MODEL_ARMOR_COST_PER_1K_REQ_USD = 1.50
FIRESTORE_COST_PER_100K_OPS_USD = 0.18


def estimate_query_cost(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    bq_bytes_billed: int = 0,
    workflow_steps: int = 0,
) -> CostBreakdown:
    """Compute exact per-investigation cost split cleanly across Vertex AI, BQ, and Workflows."""
    spec = MODEL_CATALOG.get(model_id)
    if spec is None:
        raise ValueError(f"Unknown model_id for pricing: {model_id!r}")
    in_tok = max(0, input_tokens)
    out_tok = max(0, output_tokens)
    bytes_b = max(0, bq_bytes_billed)
    steps = max(0, workflow_steps)

    model_cost = (in_tok / 1_000_000.0) * spec.input_cost_per_1m_usd + (
        out_tok / 1_000_000.0
    ) * spec.output_cost_per_1m_usd
    bq_cost = (bytes_b / (1024.0**4)) * BQ_COST_PER_TB_USD
    wf_cost = (steps / 1_000.0) * CLOUD_WORKFLOWS_COST_PER_1K_STEPS_USD
    total = model_cost + bq_cost + wf_cost
    return CostBreakdown(
        input_tokens=in_tok,
        output_tokens=out_tok,
        bq_bytes_billed=bytes_b,
        workflow_steps=steps,
        model_cost_usd=round(model_cost, 6),
        bq_cost_usd=round(bq_cost, 6),
        workflow_cost_usd=round(wf_cost, 6),
        total_usd=round(total, 6),
    )


@dataclass(frozen=True)
class TierTCOEstimate:
    tier_name: str
    monthly_alerts: int
    ai_reasoning_usd: float
    telemetry_and_state_usd: float
    hosting_and_governance_usd: float
    total_monthly_usd: float
    cost_per_alert_usd: float
    manual_monthly_baseline_usd: float
    net_monthly_savings_usd: float
    annual_savings_usd: float
    roi_multiple: float


def estimate_monthly_tco(
    *,
    monthly_alerts: int,
    manual_baseline_cost_usd: float,
    synthesis_model: str = "gemini-2.5-flash",
    enable_tier2_connectors: bool = True,
) -> dict[str, TierTCOEstimate]:
    """Compare Tier-1 Serverless War Room vs. Tier-2 Enterprise War Room monthly TCO & ROI."""
    alerts = max(100, min(500_000, int(monthly_alerts)))
    manual_unit = max(1.0, float(manual_baseline_cost_usd))

    # Typical 4-agent parallel investigation: ~24k input tokens, ~3.5k output tokens
    per_alert_ai = estimate_query_cost(
        model_id=synthesis_model,
        input_tokens=24_000,
        output_tokens=3_500,
        bq_bytes_billed=150 * 1024 * 1024,  # 150 MiB scanned across 3 log tables
        workflow_steps=12,
    )

    ai_monthly = alerts * per_alert_ai.model_cost_usd
    # BigQuery 1 TB/mo free tier applied conservatively for Tier-1
    bq_tb = (alerts * 150 * 1024 * 1024) / (1024.0**4)
    bq_monthly_t1 = max(0.0, bq_tb - 1.0) * BQ_COST_PER_TB_USD + 1.50  # storage + Firestore free/low
    wf_ma_monthly = (alerts / 1_000.0) * (MODEL_ARMOR_COST_PER_1K_REQ_USD + 0.12)
    run_t1 = max(0.0, (alerts * 0.0009) - 2.0) + 1.50  # min-instances=0 serverless

    t1_total = ai_monthly + bq_monthly_t1 + wf_ma_monthly + run_t1
    manual_total = alerts * manual_unit
    t1_savings = max(0.0, manual_total - t1_total)
    t1_roi = round(t1_savings / max(1.0, t1_total), 1)

    # Tier-2 Enterprise: warm Cloud Run (min-instances=1), Cloud IAP ALB, PSC, Apigee/Integration
    t2_telemetry = bq_tb * BQ_COST_PER_TB_USD + 35.0
    t2_hosting_gov = (
        65.0  # warm Cloud Run min-instances=1
        + 25.0  # Cloud IAP + HTTPS LB
        + (260.0 if enable_tier2_connectors else 95.0)  # Apigee / Integration / Chronicle / PSC
        + wf_ma_monthly
    )
    t2_total = ai_monthly + t2_telemetry + t2_hosting_gov
    t2_savings = max(0.0, manual_total - t2_total)
    t2_roi = round(t2_savings / max(1.0, t2_total), 1)

    return {
        "tier1_serverless": TierTCOEstimate(
            tier_name="Tier 1 · Serverless War Room (BigQuery + ADK + Workflows)",
            monthly_alerts=alerts,
            ai_reasoning_usd=round(ai_monthly, 2),
            telemetry_and_state_usd=round(bq_monthly_t1, 2),
            hosting_and_governance_usd=round(run_t1 + wf_ma_monthly, 2),
            total_monthly_usd=round(t1_total, 2),
            cost_per_alert_usd=round(t1_total / alerts, 4),
            manual_monthly_baseline_usd=round(manual_total, 2),
            net_monthly_savings_usd=round(t1_savings, 2),
            annual_savings_usd=round(t1_savings * 12.0, 2),
            roi_multiple=t1_roi,
        ),
        "tier2_enterprise": TierTCOEstimate(
            tier_name="Tier 2 · Enterprise War Room (Warm Run + IAP + Connectors/PSC)",
            monthly_alerts=alerts,
            ai_reasoning_usd=round(ai_monthly, 2),
            telemetry_and_state_usd=round(t2_telemetry, 2),
            hosting_and_governance_usd=round(t2_hosting_gov, 2),
            total_monthly_usd=round(t2_total, 2),
            cost_per_alert_usd=round(t2_total / alerts, 4),
            manual_monthly_baseline_usd=round(manual_total, 2),
            net_monthly_savings_usd=round(t2_savings, 2),
            annual_savings_usd=round(t2_savings * 12.0, 2),
            roi_multiple=t2_roi,
        ),
    }
