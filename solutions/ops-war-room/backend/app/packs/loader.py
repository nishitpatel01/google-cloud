"""Strict declarative industry pack loader with SHA-256 MANIFEST verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.core.models import (
    PACK_SLUG_RE,
    DataOrigin,
    IncidentRecord,
    IncidentSeverity,
    IncidentVerdict,
    IntelRecord,
    PackConfig,
    RemediationOption,
    RunbookRecord,
    SpecialistRole,
    TelemetryEvent,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_remediation_option(raw: dict[str, Any]) -> RemediationOption:
    return RemediationOption(
        option_id=str(raw["option_id"]),
        title=str(raw["title"]),
        action_type=raw["action_type"],
        workflow_name=str(raw["workflow_name"]),
        command_or_diff=str(raw["command_or_diff"]),
        estimated_action_cost_usd=float(raw["estimated_action_cost_usd"]),
        avoided_loss_usd=float(raw["avoided_loss_usd"]),
        sla_risk_hours=float(raw["sla_risk_hours"]),
        requires_vp_escalation=bool(raw.get("requires_vp_escalation", False)),
        is_recommended=bool(raw.get("is_recommended", False)),
        compensation_steps=tuple(str(s) for s in raw.get("compensation_steps", [])),
    )


def load_pack(pack_dir: Path, *, verify_manifest: bool = True) -> PackConfig:
    """Load and validate a single declarative industry pack from disk."""
    yaml_path = pack_dir / "pack.yaml"
    if not yaml_path.is_file():
        raise FileNotFoundError(f"Missing pack.yaml in {pack_dir}")

    meta = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    pack_id = str(meta["pack_id"])
    if not PACK_SLUG_RE.match(pack_id):
        raise ValueError(f"Invalid pack_id in {yaml_path}: {pack_id!r}")

    corpus_dir = pack_dir / "corpus"
    manifest_path = corpus_dir / "MANIFEST.json"
    manifest_ok = False
    if verify_manifest:
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Missing MANIFEST.json in {corpus_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for fname, expected_hash in manifest.get("files", {}).items():
            fpath = corpus_dir / fname
            if not fpath.is_file():
                raise FileNotFoundError(f"Manifest file missing: {fpath}")
            actual = _sha256_file(fpath)
            if actual != expected_hash:
                raise ValueError(
                    f"SHA-256 mismatch for {fpath}: expected {expected_hash}, got {actual}"
                )
        manifest_ok = True

    raw_incidents = json.loads((corpus_dir / "incidents.json").read_text(encoding="utf-8"))
    raw_telemetry = json.loads((corpus_dir / "telemetry.json").read_text(encoding="utf-8"))
    raw_intel = json.loads((corpus_dir / "intel.json").read_text(encoding="utf-8"))
    raw_runbooks = json.loads((corpus_dir / "runbooks.json").read_text(encoding="utf-8"))

    incidents = tuple(
        IncidentRecord(
            incident_id=str(i["incident_id"]),
            pack_id=pack_id,
            title=str(i["title"]),
            summary=str(i["summary"]),
            severity=IncidentSeverity(i["severity"]),
            sla_minutes_remaining=int(i["sla_minutes_remaining"]),
            expected_verdict=IncidentVerdict(i["expected_verdict"]),
            expected_root_cause=str(i["expected_root_cause"]),
            expected_event_ids=tuple(str(x) for x in i.get("expected_event_ids", [])),
            expected_intel_ids=tuple(str(x) for x in i.get("expected_intel_ids", [])),
            expected_action_type=str(i["expected_action_type"]),
            has_prompt_injection=bool(i.get("has_prompt_injection", False)),
            financial_exposure_usd=float(i.get("financial_exposure_usd", 0.0)),
            remediation_options=tuple(
                _parse_remediation_option(opt) for opt in i.get("remediation_options", [])
            ),
            data_origin=DataOrigin(i.get("data_origin", "MODELED_BRIDGE")),
        )
        for i in raw_incidents
    )

    telemetry = tuple(
        TelemetryEvent(
            event_id=str(t["event_id"]),
            pack_id=pack_id,
            incident_id=str(t["incident_id"]),
            timestamp_iso=str(t["timestamp_iso"]),
            source_table=str(t["source_table"]),
            actor=str(t["actor"]),
            resource=str(t["resource"]),
            action=str(t["action"]),
            status=str(t["status"]),
            severity=str(t["severity"]),
            raw_message=str(t["raw_message"]),
            structured_fields=dict(t.get("structured_fields", {})),
            contains_prompt_injection=bool(t.get("contains_prompt_injection", False)),
            data_origin=DataOrigin(t.get("data_origin", "MODELED_BRIDGE")),
        )
        for t in raw_telemetry
    )

    intel = tuple(
        IntelRecord(
            intel_id=str(r["intel_id"]),
            pack_id=pack_id,
            intel_type=r["intel_type"],
            title=str(r["title"]),
            summary=str(r["summary"]),
            source_name=str(r["source_name"]),
            source_url=str(r["source_url"]),
            license_name=str(r["license_name"]),
            metadata=dict(r.get("metadata", {})),
            data_origin=DataOrigin(r.get("data_origin", "REAL_PUBLIC")),
        )
        for r in raw_intel
    )

    runbooks = tuple(
        RunbookRecord(
            runbook_id=str(rb["runbook_id"]),
            pack_id=pack_id,
            title=str(rb["title"]),
            category=str(rb["category"]),
            content=str(rb["content"]),
            safe_commands=tuple(str(c) for c in rb.get("safe_commands", [])),
            data_origin=DataOrigin(rb.get("data_origin", "REAL_PUBLIC")),
        )
        for rb in raw_runbooks
    )

    specialists = tuple(
        SpecialistRole(
            agent_id=str(s["agent_id"]),
            display_name=str(s["display_name"]),
            responsibility=str(s["responsibility"]),
            tool_source=str(s["tool_source"]),
        )
        for s in meta.get("specialists", [])
    )

    # Calculate real_public_share_pct across intel + runbooks + telemetry records
    all_origins = (
        [r.data_origin for r in intel]
        + [rb.data_origin for rb in runbooks]
        + [t.data_origin for t in telemetry]
    )
    real_pct = (
        round(100.0 * sum(1 for o in all_origins if o == DataOrigin.REAL_PUBLIC) / len(all_origins), 1)
        if all_origins
        else 0.0
    )

    return PackConfig(
        pack_id=pack_id,
        title=str(meta["title"]),
        subtitle=str(meta["subtitle"]),
        industry=str(meta["industry"]),
        persona=str(meta["persona"]),
        manual_baseline_cost_usd=float(meta["manual_baseline_cost_usd"]),
        manual_baseline_minutes=int(meta["manual_baseline_minutes"]),
        default_workflow_name=str(meta["default_workflow_name"]),
        margin_floor_ratio=float(meta.get("margin_floor_ratio", 0.65)),
        primary_sources=tuple(str(s) for s in meta.get("primary_sources", [])),
        specialists=specialists,
        incidents=incidents,
        telemetry_events=telemetry,
        intel_records=intel,
        runbooks=runbooks,
        manifest_sha256_verified=manifest_ok,
        real_public_share_pct=real_pct,
    )


def load_all_packs(packs_dir: Path, *, verify_manifest: bool = True) -> dict[str, PackConfig]:
    """Load all valid industry packs under packs_dir in deterministic sorted order."""
    loaded: dict[str, PackConfig] = {}
    if not packs_dir.is_dir():
        return loaded
    for child in sorted(packs_dir.iterdir()):
        if child.is_dir() and (child / "pack.yaml").is_file():
            cfg = load_pack(child, verify_manifest=verify_manifest)
            loaded[cfg.pack_id] = cfg
    return loaded
