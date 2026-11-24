"""BYO operational log parser, prompt-injection scanner, and labeled BigQuery appender."""

from __future__ import annotations

import csv
import io
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.config import Settings
from app.core.gcp_clients import GCPClientPool
from app.core.log_sanitizer import sanitize_telemetry_batch
from app.core.models import DataOrigin, TelemetryEvent
from app.ingestion.dlp_guard import DLPInspectionResult, inspect_and_redact_pii
from app.ingestion.upload_validator import ValidatedUpload

MAX_PARSED_EVENTS = 100


@dataclass(frozen=True)
class IngestionPreviewResult:
    pack_id: str
    incident_id: str
    filename: str
    size_bytes: int
    parsed_count: int
    prompt_injections_found: int
    injection_event_ids: tuple[str, ...]
    dlp_summary: dict[str, Any]
    committed_to_bigquery: bool
    events_preview: tuple[dict[str, Any], ...]


def _parse_rows(upload: ValidatedUpload, redacted_text: str) -> list[dict[str, Any]]:
    ext = upload.extension
    if ext == ".jsonl":
        out = []
        for line in redacted_text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
            if len(out) >= MAX_PARSED_EVENTS:
                break
        return out
    if ext == ".json":
        parsed = json.loads(redacted_text)
        if isinstance(parsed, list):
            return [x for x in parsed[:MAX_PARSED_EVENTS] if isinstance(x, dict)]
        if isinstance(parsed, dict):
            return [parsed]
        return []
    if ext == ".csv":
        reader = csv.DictReader(io.StringIO(redacted_text))
        return [dict(r) for _, r in zip(range(MAX_PARSED_EVENTS), reader)]
    # .log / .txt line-oriented
    out = []
    for line in redacted_text.splitlines():
        s = line.strip()
        if not s:
            continue
        out.append({"raw_message": s, "action": "LOG_LINE", "severity": "INFO"})
        if len(out) >= MAX_PARSED_EVENTS:
            break
    return out


def process_log_upload(
    *,
    upload: ValidatedUpload,
    pack_id: str,
    incident_id: str | None,
    settings: Settings,
    gcp_clients: GCPClientPool | None = None,
    commit_to_bigquery: bool = False,
    use_live_dlp: bool = False,
) -> IngestionPreviewResult:
    """Validate, DLP-scrub, parse, injection-scan, and optionally append BYO logs to BigQuery."""
    dlp_res: DLPInspectionResult = inspect_and_redact_pii(
        upload.text_content,
        settings=settings,
        gcp_clients=gcp_clients,
        use_live_dlp=use_live_dlp,
    )
    raw_rows = _parse_rows(upload, dlp_res.redacted_text)
    target_inc = (incident_id or f"byo_{secrets.token_hex(3)}").strip()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    events: list[TelemetryEvent] = []
    for idx, row in enumerate(raw_rows, 1):
        eid = str(row.get("event_id") or f"evt_byo_{idx:03d}_{secrets.token_hex(2)}")
        raw_msg = str(
            row.get("raw_message")
            or row.get("message")
            or row.get("detail")
            or json.dumps(row)[:500]
        )
        known_keys = {
            "event_id", "timestamp_iso", "event_ts", "source_table",
            "actor", "resource", "action", "status", "severity", "raw_message", "message",
        }
        extra = {k: v for k, v in row.items() if k not in known_keys}
        events.append(
            TelemetryEvent(
                event_id=eid,
                pack_id=pack_id,
                incident_id=target_inc,
                timestamp_iso=str(row.get("timestamp_iso") or row.get("event_ts") or now_iso),
                source_table=str(row.get("source_table") or "byo_uploaded_logs"),
                actor=str(row.get("actor") or "uploaded_source"),
                resource=str(row.get("resource") or upload.safe_filename),
                action=str(row.get("action") or "LOG_EVENT"),
                status=str(row.get("status") or "OBSERVED"),
                severity=str(row.get("severity") or "MEDIUM").upper(),
                raw_message=raw_msg,
                structured_fields=extra,
                contains_prompt_injection=False,
                data_origin=DataOrigin.MODELED_BRIDGE,
            )
        )

    sanitized = sanitize_telemetry_batch(events)
    inj_ids: list[str] = []
    preview_rows: list[dict[str, Any]] = []
    for ev, rep in zip(events, sanitized):
        if rep.injection_detected:
            inj_ids.append(ev.event_id)
        preview_rows.append(
            {
                "event_id": ev.event_id,
                "timestamp_iso": ev.timestamp_iso,
                "source_table": ev.source_table,
                "actor": ev.actor,
                "resource": ev.resource,
                "action": ev.action,
                "severity": ev.severity,
                "injection_detected": rep.injection_detected,
                "matched_rules": list(rep.matched_rules),
                "quarantined_message": rep.quarantined_message[:280],
            }
        )

    committed = False
    if commit_to_bigquery and events:
        if gcp_clients is None or not settings.live_capable:
            raise RuntimeError("Live Cloud mode (GCP_PROJECT_ID) is required to commit uploads to BigQuery.")
        from google.cloud import bigquery

        bq = gcp_clients.bigquery()
        table_id = f"{gcp_clients.project_id}.{settings.bq_dataset}.telemetry_events"
        bq_rows = [
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
                "contains_prompt_injection": rep.injection_detected,
                "data_origin": ev.data_origin.value,
            }
            for ev, rep in zip(events, sanitized)
        ]
        cfg = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            labels=settings.gcp_labels(component="byo_log_ingest"),
        )
        bq.load_table_from_json(bq_rows, table_id, job_config=cfg).result()
        committed = True

    return IngestionPreviewResult(
        pack_id=pack_id,
        incident_id=target_inc,
        filename=upload.safe_filename,
        size_bytes=upload.size_bytes,
        parsed_count=len(events),
        prompt_injections_found=len(inj_ids),
        injection_event_ids=tuple(inj_ids),
        dlp_summary={
            "engine": dlp_res.engine,
            "total_findings": dlp_res.total_findings,
            "findings_by_type": dlp_res.findings_by_type,
        },
        committed_to_bigquery=committed,
        events_preview=tuple(preview_rows[:25]),
    )
