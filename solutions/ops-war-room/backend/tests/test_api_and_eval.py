"""Tests for FastAPI War Room routes, two-click remediation lifecycle, MCP SSRF API, and evaluation."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.evaluation.judge import _normalize_five_point_score
from app.evaluation.leaderboard import summarize_recordings
from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app(Settings(demo_mode="replay")))


def test_meta_packs_incident_releases_and_tco_endpoints() -> None:
    client = _client()

    meta = client.get("/api/v1/meta")
    assert meta.status_code == 200
    assert meta.json()["solution_id"] == "acc04"
    assert len(meta.json()["engines"]) == 2

    packs = client.get("/api/v1/packs")
    assert packs.status_code == 200
    assert len(packs.json()["packs"]) == 3

    detail = client.get("/api/v1/packs/secops_siem_triage/incidents/sec_inc02")
    assert detail.status_code == 200
    d = detail.json()
    assert d["incident"]["has_prompt_injection"] is True
    assert len(d["telemetry_events"]) == 2
    assert len(d["incident"]["remediation_options"][0]["plan_sha256"]) == 64

    rel = client.get("/api/v1/releases")
    assert rel.status_code == 200
    assert rel.json()["solution_id"] == "acc04"

    tco = client.post(
        "/api/v1/tco/estimate",
        json={"pack_id": "secops_siem_triage", "monthly_alerts": 2000},
    )
    assert tco.status_code == 200
    assert tco.json()["tier1_serverless"]["roi_multiple"] > 100.0


def test_mcp_registry_and_ssrf_validation_endpoint() -> None:
    client = _client()
    reg = client.get("/api/v1/mcp/registry")
    assert reg.status_code == 200
    assert len(reg.json()["adapters"]) == 5

    ok = client.post(
        "/api/v1/mcp/validate-url",
        json={"url": "https://chronicle.googleapis.com/v1alpha/projects/demo"},
    )
    assert ok.status_code == 200
    assert ok.json()["valid"] is True

    bad = client.post(
        "/api/v1/mcp/validate-url",
        json={"url": "http://169.254.169.254/computeMetadata/v1/"},
    )
    assert bad.status_code == 200
    assert bad.json()["valid"] is False


def test_investigate_stream_rejects_same_judge_and_synthesis_model() -> None:
    client = _client()
    r = client.post(
        "/api/v1/investigate/stream",
        json={
            "pack_id": "secops_siem_triage",
            "incident_id": "sec_inc01",
            "synthesis_model": "gemini-2.5-flash",
            "judge_model": "gemini-2.5-flash",
            "mode": "replay",
        },
    )
    assert r.status_code == 422
    assert "judge_model != synthesis_model" in r.json()["detail"]


def test_two_click_remediation_api_flow_and_rollback() -> None:
    client = _client()

    # Step 1: Preview dry-run diff
    prev = client.post(
        "/api/v1/remediate/preview",
        json={
            "pack_id": "supply_chain_o2c_saga",
            "incident_id": "o2c_inc01",
            "option_id": "opt_o2c01_split",
            "verdict": "CONFIRMED_INCIDENT",
        },
    )
    assert prev.status_code == 200
    assert prev.json()["policy_allowed"] is True

    # Fail-closed: execute without approval returns 403
    unapproved = client.post(
        "/api/v1/remediate/execute",
        json={
            "pack_id": "supply_chain_o2c_saga",
            "incident_id": "o2c_inc01",
            "option_id": "opt_o2c01_split",
            "approval_token": "not-approved-token",
            "idempotency_key": "api-idem-01",
            "mode": "replay",
        },
    )
    assert unapproved.status_code == 403

    # Step 2a: Two-click approval
    appr = client.post(
        "/api/v1/remediate/approve",
        json={
            "pack_id": "supply_chain_o2c_saga",
            "incident_id": "o2c_inc01",
            "option_id": "opt_o2c01_split",
            "verdict": "CONFIRMED_INCIDENT",
            "idempotency_key": "api-idem-rb",
            "approver_identity": "ops-lead@example.com",
            "mode": "replay",
        },
    )
    assert appr.status_code == 200
    token = appr.json()["approval_token"]

    # Step 2b: Execute with simulated Step-2 Carrier API failure -> automatic WMS rollback!
    exe = client.post(
        "/api/v1/remediate/execute",
        json={
            "pack_id": "supply_chain_o2c_saga",
            "incident_id": "o2c_inc01",
            "option_id": "opt_o2c01_split",
            "approval_token": token,
            "idempotency_key": "api-idem-rb",
            "dry_run": True,
            "simulate_step2_failure": True,
            "mode": "replay",
        },
    )
    assert exe.status_code == 200
    body = exe.json()
    assert body["final_state"] == "SAGA_ROLLED_BACK"
    assert body["compensation_triggered"] is True
    assert [s["status"] for s in body["steps"]] == ["SUCCEEDED", "FAILED", "COMPENSATED"]


def test_eval_score_normalization_and_leaderboard_aggregation(tmp_path: Path) -> None:
    assert _normalize_five_point_score(5.0) == 1.0
    assert _normalize_five_point_score(1.0) == 0.0
    assert _normalize_five_point_score(3.0) == 0.5

    rec_dir = tmp_path / "adk_parallel_war_room" / "secops_siem_triage" / "gemini-2.5-flash"
    rec_dir.mkdir(parents=True)
    (rec_dir / "sec_inc02.json").write_text(
        json.dumps(
            {
                "result": {
                    "pack_id": "secops_siem_triage",
                    "engine_id": "adk_parallel_war_room",
                    "synthesis_model": "gemini-2.5-flash",
                    "latency_ms": 14000,
                    "provenance": {
                        "root_cause_recall": 1.0,
                        "provenance_verified": True,
                        "injection_resisted": True,
                    },
                    "cost": {"total_usd": 0.0072},
                },
                "evaluation": {
                    "judge_model": "vertex-genai-eval",
                    "groundedness": 1.0,
                    "root_cause_precision": 1.0,
                    "remediation_safety": 1.0,
                    "composite_score": 1.0,
                    "judge_cost_usd": 0.012,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_recordings(tmp_path)
    assert summary["total_recordings"] == 1
    row = summary["rows"][0]
    assert row["avg_composite_score"] == 1.0
    assert row["injection_defense_rate"] == 1.0
    assert row["avg_engine_cost_usd"] == 0.0072
    assert row["avg_judge_cost_usd"] == 0.012
