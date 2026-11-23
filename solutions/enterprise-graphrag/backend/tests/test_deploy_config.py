"""Static & dry-run verification tests for Dockerfile, Terraform IaC, and deploy/teardown scripts."""

from __future__ import annotations

import subprocess
from pathlib import Path

SOLUTION_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = SOLUTION_ROOT / "infra" / "terraform"


def test_dockerfile_enforces_multistage_nonroot_and_pinned_lockfile() -> None:
    dockerfile = (SOLUTION_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22-slim AS frontend-builder" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "requirements.lock" in dockerfile
    assert "USER 10001" in dockerfile
    assert "COPY --from=frontend-builder /build/frontend/dist /app/frontend/dist" in dockerfile
    assert (SOLUTION_ROOT / "backend" / "requirements.lock").is_file()

    gcloudignore = (SOLUTION_ROOT / ".gcloudignore").read_text(encoding="utf-8")
    for pattern in (".venv/", "frontend/node_modules/", ".env", "deploy/*.env"):
        assert pattern in gcloudignore, f".gcloudignore must exclude {pattern}"


def test_terraform_enforces_6key_labels_and_tier2_default_off() -> None:
    locals_tf = (TF_DIR / "locals.tf").read_text(encoding="utf-8")
    for required_key in ("portfolio", "solution_id", "solution", "tier", "managed_by", "env"):
        assert required_key in locals_tf, f"Missing mandatory label key {required_key} in locals.tf"
    assert '"gcp_ai_accelerators"' in locals_tf
    assert '"acc05"' in locals_tf

    vars_tf = (TF_DIR / "variables.tf").read_text(encoding="utf-8")
    assert 'variable "enable_enterprise_scale"' in vars_tf
    assert "default     = false" in vars_tf

    # Every label-capable Terraform resource file must reference local.common_labels
    for fname in ("storage_and_bigquery.tf", "cloudrun.tf", "spanner_tier2.tf"):
        text = (TF_DIR / fname).read_text(encoding="utf-8")
        assert "local.common_labels" in text, f"{fname} must apply local.common_labels"

    storage_tf = (TF_DIR / "storage_and_bigquery.tf").read_text(encoding="utf-8")
    assert "uniform_bucket_level_access = true" in storage_tf
    assert 'public_access_prevention    = "enforced"' in storage_tf

    spanner_tf = (TF_DIR / "spanner_tier2.tf").read_text(encoding="utf-8")
    assert "count            = var.enable_enterprise_scale ? 1 : 0" in spanner_tf


def test_deploy_script_dry_run_validates_config_and_prints_acc05_plan(tmp_path: Path) -> None:
    env_file = tmp_path / "cloudrun.env"
    env_file.write_text(
        "\n".join(
            [
                "GCP_PROJECT_ID=demo-customer-proj",
                "GCP_REGION=us-central1",
                "APP_ENV=staging",
                "ACCESS_MODE=iap",
                "IAP_MEMBERS=user:analyst@example.com",
                "DEPLOY_REPLAY=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", str(SOLUTION_ROOT / "scripts" / "deploy.sh"), "--env-file", str(env_file), "--dry-run"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "portfolio=gcp_ai_accelerators,solution_id=acc05,solution=enterprise_graphrag" in proc.stdout
    assert "acc05-graphrag-ui" in proc.stdout
    assert "acc05-graphrag-replay" in proc.stdout
    assert "DRY RUN:" in proc.stdout


def test_teardown_script_dry_run_preserves_data_by_default(tmp_path: Path) -> None:
    env_file = tmp_path / "cloudrun.env"
    env_file.write_text("GCP_PROJECT_ID=demo-customer-proj\nGCP_REGION=us-central1\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(SOLUTION_ROOT / "scripts" / "teardown.sh"), "--env-file", str(env_file), "--dry-run"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "PRESERVE BigQuery/GCS" in proc.stdout
    assert "DRY RUN:" in proc.stdout
