"""Tests for config validation, judge != synthesis guard, and Tier-1/Tier-2 pricing math."""

from __future__ import annotations

import pytest

from app.core.config import Settings, validate_model_pair
from app.core.pricing import estimate_monthly_tco, estimate_query_cost


def test_validate_model_pair_enforces_independent_judge() -> None:
    assert validate_model_pair("gemini-2.5-flash", "vertex-genai-eval") == (
        "gemini-2.5-flash",
        "vertex-genai-eval",
    )
    assert validate_model_pair("gemini-2.5-flash", "claude-opus-5-5") == (
        "gemini-2.5-flash",
        "claude-opus-5-5",
    )
    assert validate_model_pair("gemini-2.5-flash", None) == ("gemini-2.5-flash", None)

    with pytest.raises(ValueError, match="judge_model != synthesis_model"):
        validate_model_pair("gemini-2.5-flash", "gemini-2.5-flash")

    with pytest.raises(ValueError, match="Unsupported synthesis model"):
        validate_model_pair("vertex-genai-eval", "gemini-2.5-pro")


def test_settings_rejects_wildcard_cors() -> None:
    s = Settings(allowed_origins="https://app.example.com,*")
    with pytest.raises(ValueError, match="Wildcard CORS"):
        _ = s.cors_origins


def test_query_and_monthly_tco_monotonicity() -> None:
    q = estimate_query_cost(
        model_id="gemini-2.5-flash",
        input_tokens=24_000,
        output_tokens=3_500,
        bq_bytes_billed=150 * 1024 * 1024,
        workflow_steps=12,
    )
    assert q.total_usd > 0
    assert q.total_usd < 0.05

    tco_small = estimate_monthly_tco(monthly_alerts=1_000, manual_baseline_cost_usd=55.0)
    tco_large = estimate_monthly_tco(monthly_alerts=25_000, manual_baseline_cost_usd=55.0)

    assert tco_small["tier1_serverless"].total_monthly_usd < tco_small["tier2_enterprise"].total_monthly_usd
    assert tco_small["tier1_serverless"].total_monthly_usd < tco_large["tier1_serverless"].total_monthly_usd
    assert tco_small["tier1_serverless"].roi_multiple > 100.0
    assert tco_large["tier1_serverless"].annual_savings_usd > tco_small["tier1_serverless"].annual_savings_usd
