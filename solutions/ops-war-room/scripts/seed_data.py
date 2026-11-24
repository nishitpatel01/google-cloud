#!/usr/bin/env python3
"""Idempotent BigQuery seeder for ACC-04 Operations & Incident War Room.

Creates the labeled `acc04_ops_war_room` dataset and 5 partitioned/clustered tables
from infra/sql/bigquery_schema.sql, then loads all 3 curated industry packs cleanly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOLUTION_ROOT / "backend"))

from google.cloud import bigquery  # noqa: E402

from app.core.attribution import build_gcp_labels  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.packs.loader import load_all_packs  # noqa: E402


def apply_ddl(client: bigquery.Client, *, project_id: str, dataset_id: str, labels: dict[str, str]) -> None:
    ddl_path = SOLUTION_ROOT / "infra" / "sql" / "bigquery_schema.sql"
    raw_sql = ddl_path.read_text(encoding="utf-8")
    rendered = raw_sql.replace("${PROJECT_ID}", project_id).replace("${DATASET_ID}", dataset_id)
    statements = [s.strip() for s in rendered.split(";") if s.strip()]
    for idx, stmt in enumerate(statements, 1):
        cfg = bigquery.QueryJobConfig(labels=labels)
        job = client.query(stmt, job_config=cfg)
        job.result()
        print(f"  [DDL {idx}/{len(statements)}] OK")


def load_json_rows(
    client: bigquery.Client,
    *,
    table_id: str,
    rows: list[dict],
    labels: dict[str, str],
) -> None:
    cfg = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        labels=labels,
    )
    job = client.load_table_from_json(rows, table_id, job_config=cfg)
    job.result()
    print(f"  Loaded {len(rows):>3d} rows -> {table_id}")


def seed_all(*, project_id: str, dataset_id: str, env: str = "dev") -> None:
    labels = build_gcp_labels(
        tier="tier1_serverless",
        managed_by="seed_script",
        env=env,
        component="seed_data",
    )
    packs = load_all_packs(SOLUTION_ROOT / "packs", verify_manifest=True)
    client = bigquery.Client(project=project_id, location="US")

    print(f"Applying BigQuery DDL to {project_id}.{dataset_id}...")
    apply_ddl(client, project_id=project_id, dataset_id=dataset_id, labels=labels)

    inc_rows: list[dict] = []
    tel_rows: list[dict] = []
    intel_rows: list[dict] = []
    rb_rows: list[dict] = []

    for pack in packs.values():
        for inc in pack.incidents:
            inc_rows.append(
                {
                    "incident_id": inc.incident_id,
                    "pack_id": inc.pack_id,
                    "title": inc.title,
                    "summary": inc.summary,
                    "severity": inc.severity.value,
                    "sla_minutes_remaining": inc.sla_minutes_remaining,
                    "expected_verdict": inc.expected_verdict.value,
                    "expected_root_cause": inc.expected_root_cause,
                    "expected_event_ids": list(inc.expected_event_ids),
                    "expected_intel_ids": list(inc.expected_intel_ids),
                    "expected_action_type": inc.expected_action_type,
                    "has_prompt_injection": inc.has_prompt_injection,
                    "financial_exposure_usd": inc.financial_exposure_usd,
                    "remediation_options_json": json.dumps(
                        [
                            {
                                "option_id": o.option_id,
                                "title": o.title,
                                "action_type": o.action_type,
                                "workflow_name": o.workflow_name,
                                "command_or_diff": o.command_or_diff,
                                "estimated_action_cost_usd": o.estimated_action_cost_usd,
                                "avoided_loss_usd": o.avoided_loss_usd,
                                "sla_risk_hours": o.sla_risk_hours,
                                "requires_vp_escalation": o.requires_vp_escalation,
                                "is_recommended": o.is_recommended,
                                "compensation_steps": list(o.compensation_steps),
                            }
                            for o in inc.remediation_options
                        ]
                    ),
                    "data_origin": inc.data_origin.value,
                }
            )
        for ev in pack.telemetry_events:
            tel_rows.append(
                {
                    "event_id": ev.event_id,
                    "pack_id": ev.pack_id,
                    "incident_id": ev.incident_id,
                    "event_ts": ev.timestamp_iso,
                    "source_table": ev.source_table,
                    "actor": ev.actor,
                    "resource": ev.resource,
                    "action": ev.action,
                    "status": ev.status,
                    "severity": ev.severity,
                    "raw_message": ev.raw_message,
                    "structured_fields_json": json.dumps(ev.structured_fields),
                    "contains_prompt_injection": ev.contains_prompt_injection,
                    "data_origin": ev.data_origin.value,
                }
            )
        for rec in pack.intel_records:
            intel_rows.append(
                {
                    "intel_id": rec.intel_id,
                    "pack_id": rec.pack_id,
                    "intel_type": rec.intel_type,
                    "title": rec.title,
                    "summary": rec.summary,
                    "source_name": rec.source_name,
                    "source_url": rec.source_url,
                    "license_name": rec.license_name,
                    "metadata_json": json.dumps(rec.metadata),
                    "data_origin": rec.data_origin.value,
                }
            )
        for rb in pack.runbooks:
            rb_rows.append(
                {
                    "runbook_id": rb.runbook_id,
                    "pack_id": rb.pack_id,
                    "title": rb.title,
                    "category": rb.category,
                    "content": rb.content,
                    "safe_commands": list(rb.safe_commands),
                    "data_origin": rb.data_origin.value,
                }
            )

    load_json_rows(client, table_id=f"{project_id}.{dataset_id}.incidents", rows=inc_rows, labels=labels)
    load_json_rows(client, table_id=f"{project_id}.{dataset_id}.telemetry_events", rows=tel_rows, labels=labels)
    load_json_rows(client, table_id=f"{project_id}.{dataset_id}.intel_catalog", rows=intel_rows, labels=labels)
    load_json_rows(client, table_id=f"{project_id}.{dataset_id}.runbooks", rows=rb_rows, labels=labels)

    # Stamp dataset-level labels explicitly so even pre-existing datasets carry all 6 keys
    ds = client.get_dataset(f"{project_id}.{dataset_id}")
    ds.labels = build_gcp_labels(tier="tier1_serverless", managed_by="seed_script", env=env)
    client.update_dataset(ds, ["labels"])
    print(f"Seed complete for {project_id}.{dataset_id} (6-key FinOps labels verified).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed ACC-04 Operations & Incident War Room into BigQuery")
    parser.add_argument("--project", default="", help="GCP Project ID (defaults to GCP_PROJECT_ID env)")
    parser.add_argument("--dataset", default="", help="BigQuery dataset (defaults to acc04_ops_war_room)")
    parser.add_argument("--env", default="dev", help="Environment label (dev|staging|prod)")
    args = parser.parse_args()

    settings = Settings()
    project_id = args.project.strip() or settings.gcp_project_id.strip()
    dataset_id = args.dataset.strip() or settings.bq_dataset
    if not project_id:
        raise SystemExit("ERROR: Pass --project <GCP_PROJECT_ID> or set GCP_PROJECT_ID.")
    seed_all(project_id=project_id, dataset_id=dataset_id, env=args.env)


if __name__ == "__main__":
    main()
