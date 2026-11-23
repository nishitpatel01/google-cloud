"""Unit and static AST tests enforcing 6-key GCP resource/job attribution & /api/v1/releases."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.attribution import gcp_job_labels, gcp_resource_labels, validate_gcp_labels
from app.core.config import Settings
from app.main import create_app

SOLUTION_ROOT = Path(__file__).resolve().parents[2]


def test_gcp_resource_and_job_labels_match_6_key_schema() -> None:
    s_t1 = Settings(demo_mode="replay", app_env="demo")
    res_labels = gcp_resource_labels(s_t1, managed_by="terraform")
    assert res_labels == {
        "portfolio": "gcp_ai_accelerators",
        "solution_id": "acc05",
        "solution": "enterprise_graphrag",
        "tier": "tier1_serverless",
        "managed_by": "terraform",
        "env": "demo",
    }

    job_labels = gcp_job_labels(s_t1, component="gql_traverse")
    assert job_labels["solution_id"] == "acc05"
    assert job_labels["portfolio"] == "gcp_ai_accelerators"
    assert job_labels["component"] == "gql_traverse"

    s_t2 = Settings(demo_mode="replay", spanner_instance_id="acc05-enterprise-graphrag")
    assert gcp_resource_labels(s_t2)["tier"] == "tier2_enterprise"

    with pytest.raises(ValueError):
        validate_gcp_labels({"InvalidKey": "ok"})
    with pytest.raises(ValueError):
        validate_gcp_labels({"ok_key": "UPPER_BAD"})


def test_every_bigquery_job_config_sets_attribution_labels() -> None:
    """AST guardrail: every QueryJobConfig / LoadJobConfig in app & seed_data sets labels=."""
    targets = list((SOLUTION_ROOT / "backend" / "app").rglob("*.py"))
    targets.append(SOLUTION_ROOT / "scripts" / "seed_data.py")

    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("QueryJobConfig", "LoadJobConfig"):
                    kw_names = {kw.arg for kw in node.keywords}
                    assert "labels" in kw_names, (
                        f"Missing labels= on {node.func.attr} at "
                        f"{path.relative_to(SOLUTION_ROOT)}:{node.lineno}"
                    )


def test_releases_endpoint_returns_structured_timeline() -> None:
    app = create_app(settings=Settings(demo_mode="replay"))
    client = TestClient(app)
    resp = client.get("/api/v1/releases")
    assert resp.status_code == 200
    body = resp.json()
    assert body["solution_id"] == "acc05"
    assert len(body["releases"]) >= 4
    assert body["releases"][0]["version"] == body["current_version"]
    assert body["current_version"] == Settings().app_version
