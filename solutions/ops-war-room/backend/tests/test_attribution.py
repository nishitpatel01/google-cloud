"""Tests for ACC-04 4-layer GCP naming, 6-key FinOps labels, and AST label guardrail."""

from __future__ import annotations

import ast
from pathlib import Path

from app.core.attribution import (
    DEFAULT_AR_REPO,
    DEFAULT_BQ_DATASET,
    DEFAULT_FIRESTORE_DATABASE,
    DEFAULT_MODEL_ARMOR_TEMPLATE,
    DEFAULT_REPLAY_SERVICE,
    DEFAULT_UI_SERVICE,
    DEFAULT_WORKFLOW_O2C,
    DEFAULT_WORKFLOW_SECOPS,
    DEFAULT_WORKFLOW_SRE,
    MANDATORY_LABEL_KEYS,
    SOLUTION_ID,
    build_gcp_labels,
    default_gcs_bucket_name,
    sanitize_gcp_label_value,
    validate_gcp_labels,
)


def test_deterministic_acc04_names() -> None:
    assert SOLUTION_ID == "acc04"
    assert DEFAULT_BQ_DATASET.startswith("acc04_")
    for hyphen_name in (
        DEFAULT_FIRESTORE_DATABASE,
        DEFAULT_MODEL_ARMOR_TEMPLATE,
        DEFAULT_AR_REPO,
        DEFAULT_UI_SERVICE,
        DEFAULT_REPLAY_SERVICE,
        DEFAULT_WORKFLOW_O2C,
        DEFAULT_WORKFLOW_SECOPS,
        DEFAULT_WORKFLOW_SRE,
    ):
        assert hyphen_name.startswith("acc04-"), f"{hyphen_name} missing acc04- prefix"


def test_mandatory_six_key_label_schema() -> None:
    labels = build_gcp_labels(
        tier="tier1_serverless",
        managed_by="terraform",
        env="prod",
        component="parallel_agent",
    )
    for key in MANDATORY_LABEL_KEYS:
        assert key in labels
    assert labels["component"] == "parallel_agent"
    assert validate_gcp_labels(labels) == []


def test_label_sanitization_and_validation_rejects_bad_values() -> None:
    assert sanitize_gcp_label_value("My Cool Component! v2") == "my_cool_component_v2"
    assert sanitize_gcp_label_value("   ") == "unknown"
    assert len(sanitize_gcp_label_value("a" * 90)) == 63

    bad = build_gcp_labels()
    bad["solution_id"] = "acc99"
    bad["bad key"] = "UPPERCASE"
    violations = validate_gcp_labels(bad)
    assert len(violations) >= 3


def test_default_gcs_bucket_naming() -> None:
    bucket = default_gcs_bucket_name("Example-Corp-Prod")
    assert bucket == "example-corp-prod-acc04-ops-war-room"
    assert len(bucket) <= 63


def test_ast_guardrail_bq_and_vertex_calls_carry_labels() -> None:
    """Prevent future QueryJobConfig or GenerateContentConfig calls without labels=."""
    app_root = Path(__file__).resolve().parents[1] / "app"
    missing: list[str] = []
    for py_file in app_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name in ("QueryJobConfig", "GenerateContentConfig"):
                    kw_names = {kw.arg for kw in node.keywords}
                    if "labels" not in kw_names:
                        missing.append(f"{py_file.name}:{node.lineno} {func_name}")
    assert missing == [], f"Missing labels= on GCP SDK configs: {missing}"
