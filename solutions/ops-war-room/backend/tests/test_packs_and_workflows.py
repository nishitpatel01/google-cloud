"""Tests for curated real public packs, SHA-256 manifests, BigQuery DDL, and Cloud Workflows YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core.attribution import MANDATORY_LABEL_KEYS
from app.core.log_sanitizer import sanitize_telemetry_batch
from app.core.models import IncidentVerdict
from app.packs.loader import load_all_packs

SOLUTION_ROOT = Path(__file__).resolve().parents[2]


def test_all_three_packs_load_and_verify_sha256_manifest() -> None:
    packs = load_all_packs(SOLUTION_ROOT / "packs", verify_manifest=True)
    assert set(packs.keys()) == {
        "secops_siem_triage",
        "supply_chain_o2c_saga",
        "sre_outage_commander",
    }

    total_incidents = 0
    for pack_id, cfg in packs.items():
        assert cfg.manifest_sha256_verified is True
        assert cfg.real_public_share_pct >= 70.0, f"{pack_id} real_public too low: {cfg.real_public_share_pct}"
        assert len(cfg.specialists) == 3
        assert len(cfg.incidents) == 5
        total_incidents += len(cfg.incidents)

        event_ids = {ev.event_id for ev in cfg.telemetry_events}
        intel_ids = {rec.intel_id for rec in cfg.intel_records}

        # Every pack must have at least one benign false-positive verification scenario
        assert any(
            i.expected_verdict == IncidentVerdict.BENIGN_FALSE_POSITIVE for i in cfg.incidents
        ), f"{pack_id} missing benign verification incident"

        for inc in cfg.incidents:
            assert len(inc.remediation_options) >= 1
            for eid in inc.expected_event_ids:
                assert eid in event_ids, f"{pack_id}/{inc.incident_id} missing telemetry {eid}"
            for iid in inc.expected_intel_ids:
                assert iid in intel_ids, f"{pack_id}/{inc.incident_id} missing public intel {iid}"

    assert total_incidents == 15


def test_adversarial_prompt_injection_events_detected_by_sanitizer() -> None:
    packs = load_all_packs(SOLUTION_ROOT / "packs", verify_manifest=True)
    flagged_count = 0
    for cfg in packs.values():
        reports = sanitize_telemetry_batch(cfg.telemetry_events)
        for ev, rep in zip(cfg.telemetry_events, reports):
            if ev.contains_prompt_injection:
                assert rep.injection_detected is True, f"Missed injection in {ev.event_id}"
                flagged_count += 1
            else:
                assert rep.injection_detected is False, f"False positive injection in {ev.event_id}"
    assert flagged_count >= 2


def test_bigquery_ddl_contract_has_tables_labels_and_partitioning() -> None:
    ddl = (SOLUTION_ROOT / "infra" / "sql" / "bigquery_schema.sql").read_text(encoding="utf-8")
    for table in ("incidents", "telemetry_events", "intel_catalog", "runbooks", "remediation_audit"):
        assert f"${{PROJECT_ID}}.${{DATASET_ID}}.{table}" in ddl
    for key in MANDATORY_LABEL_KEYS:
        assert f'("{key}",' in ddl
    assert "PARTITION BY DATE(event_ts)" in ddl
    assert "PARTITION BY DATE(executed_at)" in ddl


def test_cloud_workflows_yaml_sagas_parse_and_include_compensation() -> None:
    wf_dir = SOLUTION_ROOT / "infra" / "workflows"
    expected_files = ("o2c_saga.yaml", "secops_contain.yaml", "sre_rollback.yaml")
    for fname in expected_files:
        raw = (wf_dir / fname).read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
        assert "main" in doc and "steps" in doc["main"]
        assert "verify_approval_digest" in raw
        assert "SAGA_ROLLED_BACK" in raw
        assert "SAGA_COMPLETED" in raw
        assert "compensation_triggered" in raw


def test_pack_loader_detects_manifest_tampering(tmp_path: Path) -> None:
    import shutil
    import pytest
    from app.packs.loader import load_pack

    src = SOLUTION_ROOT / "packs" / "secops_siem_triage"
    dst = tmp_path / "secops_siem_triage"
    shutil.copytree(src, dst)
    # Tamper with incidents.json after MANIFEST.json was generated
    inc_file = dst / "corpus" / "incidents.json"
    inc_file.write_text(inc_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_pack(dst, verify_manifest=True)

