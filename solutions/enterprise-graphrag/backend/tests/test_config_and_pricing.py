"""Unit tests for configuration invariants, model catalog, and TCO/pricing math."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import MODEL_CATALOG, Settings, validate_model_pair
from app.core.pricing import TokenUsage, compute_llm_cost_usd, estimate_monthly_tco


def test_validate_model_pair_rejects_same_synthesis_and_judge() -> None:
    with pytest.raises(ValueError, match="differ"):
        validate_model_pair("gemini-2.5-flash", "gemini-2.5-flash")


def test_validate_model_pair_rejects_unknown_model() -> None:
    with pytest.raises(ValueError, match="Unsupported synthesis model"):
        validate_model_pair("nonexistent-model", "gemini-2.5-pro")
    with pytest.raises(ValueError, match="Unsupported judge model"):
        validate_model_pair("gemini-2.5-flash", "nonexistent-judge")


def test_settings_default_replay_mode_requires_zero_gcp_credentials() -> None:
    s = Settings(_env_file=None)
    assert s.demo_mode == "replay"
    assert s.graph_backend == "bigquery"
    assert s.vertex_model_synthesis == "gemini-2.5-flash"
    assert s.vertex_model_judge == "gemini-2.5-pro"
    assert s.allowed_origins_list == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_settings_rejects_wildcard_cors() -> None:
    s = Settings(_env_file=None, cors_allowed_origins="*")
    with pytest.raises(ValueError, match="Wildcard"):
        _ = s.allowed_origins_list


def test_settings_rejects_sql_injection_in_bq_dataset() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bq_dataset="graphrag; DROP TABLE entities;--")


def test_settings_requires_project_id_in_live_mode() -> None:
    with pytest.raises(ValidationError, match="GCP_PROJECT_ID"):
        Settings(_env_file=None, demo_mode="live", gcp_project_id="")


def test_settings_requires_iap_audience_when_iap_enabled() -> None:
    with pytest.raises(ValidationError, match="IAP_JWT_AUDIENCE"):
        Settings(_env_file=None, require_iap_jwt=True, iap_jwt_audience="")


def test_settings_requires_spanner_instance_when_spanner_live() -> None:
    with pytest.raises(ValidationError, match="SPANNER_INSTANCE_ID"):
        Settings(
            _env_file=None,
            demo_mode="live",
            gcp_project_id="demo-proj",
            graph_backend="spanner",
            spanner_instance_id="",
        )


def test_pricing_and_tco_tier1_vs_tier2() -> None:
    cost = compute_llm_cost_usd(
        "gemini-2.5-flash", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(2.80, rel=1e-4)

    idle = estimate_monthly_tco(
        monthly_queries=0,
        synthesis_model="gemini-2.5-flash",
        manual_baseline_cost_usd=4500.0,
        tier="tier1_bigquery_serverless",
    )
    assert idle.total_gcp_monthly_usd <= 3.0
    assert idle.unit_cost_per_query_usd == 0.0

    t1 = estimate_monthly_tco(
        monthly_queries=1_000,
        synthesis_model="gemini-2.5-flash",
        manual_baseline_cost_usd=4500.0,
        tier="tier1_bigquery_serverless",
    )
    t2 = estimate_monthly_tco(
        monthly_queries=1_000,
        synthesis_model="gemini-2.5-flash",
        manual_baseline_cost_usd=4500.0,
        tier="tier2_spanner_enterprise",
    )
    assert t1.total_gcp_monthly_usd < t2.total_gcp_monthly_usd
    assert t1.net_monthly_savings_usd > 0
    assert t1.roi_multiple > 10.0
    assert "claude-opus-5-5" in MODEL_CATALOG
