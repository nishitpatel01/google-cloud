"""Zero-credential Replay recorder, identifier scrubber, and SSE fixture player."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app.core.evidence_verifier import ProvenanceReport
from app.core.models import IncidentVerdict
from app.core.pricing import CostBreakdown
from app.core.stage_emitter import StageEmitter
from app.engines.base import AgentDAGNode, TriageResult

_PROJECT_NUM_RE = re.compile(r"projects/[0-9]{6,}")
_RUN_APP_RE = re.compile(r"[a-z0-9-]+-[a-z0-9]{8,}-[a-z]{2}\.a\.run\.app")
_HOME_PATH_RE = re.compile(r"/(Users|home|usr/local/[^/]+/home)/[^\s\"']+")


def redact_gcp_identifiers(text: str, *, project_id: str = "") -> str:
    """Scrub GCP project IDs, project numbers, live Cloud Run URLs, and local paths."""
    cleaned = text or ""
    if project_id.strip():
        cleaned = cleaned.replace(project_id.strip(), "PROJECT_ID")
    cleaned = _PROJECT_NUM_RE.sub("projects/PROJECT_NUMBER", cleaned)
    cleaned = _RUN_APP_RE.sub("SERVICE_URL.run.app", cleaned)
    cleaned = _HOME_PATH_RE.sub("/app", cleaned)
    return cleaned


def _scrub_obj(obj: Any, *, project_id: str = "") -> Any:
    if isinstance(obj, str):
        return redact_gcp_identifiers(obj, project_id=project_id)
    if isinstance(obj, list):
        return [_scrub_obj(x, project_id=project_id) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_obj(v, project_id=project_id) for k, v in obj.items()}
    return obj


def recording_path(
    recordings_dir: Path,
    *,
    engine_id: str,
    pack_id: str,
    synthesis_model: str,
    incident_id: str,
) -> Path:
    return recordings_dir / engine_id / pack_id / synthesis_model / f"{incident_id}.json"


def save_recording(
    recordings_dir: Path,
    *,
    result: TriageResult,
    evaluation: dict[str, Any] | None = None,
    project_id: str = "",
) -> Path:
    """Persist an identifier-scrubbed live run fixture for zero-credential Replay."""
    path = recording_path(
        recordings_dir,
        engine_id=result.engine_id,
        pack_id=result.pack_id,
        synthesis_model=result.synthesis_model,
        incident_id=result.incident_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _scrub_obj(
        {
            "schema_version": 1,
            "result": result.to_dict(),
            "evaluation": evaluation,
        },
        project_id=project_id,
    )
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_recording_raw(
    recordings_dir: Path,
    *,
    engine_id: str,
    pack_id: str,
    synthesis_model: str,
    incident_id: str,
) -> dict[str, Any] | None:
    path = recording_path(
        recordings_dir,
        engine_id=engine_id,
        pack_id=pack_id,
        synthesis_model=synthesis_model,
        incident_id=incident_id,
    )
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


async def replay_recording(
    recordings_dir: Path,
    *,
    engine_id: str,
    pack_id: str,
    synthesis_model: str,
    incident_id: str,
    emitter: StageEmitter,
    step_delay_s: float = 0.05,
) -> tuple[TriageResult, dict[str, Any] | None]:
    """Replay a recorded live run through SSE StageEmitter without any GCP credentials."""
    raw = load_recording_raw(
        recordings_dir,
        engine_id=engine_id,
        pack_id=pack_id,
        synthesis_model=synthesis_model,
        incident_id=incident_id,
    )
    if raw is None:
        raise FileNotFoundError(
            f"No Replay recording found for {engine_id}/{pack_id}/{synthesis_model}/{incident_id}"
        )

    res_dict = raw["result"]
    for ev in res_dict.get("stage_events", []):
        if ev.get("status") == "started":
            h = await emitter.start(
                str(ev["stage_id"]),
                str(ev["label"]),
                service=str(ev.get("service", "Replay")),
                agent_node=str(ev.get("agent_node", "coordinator")),
                detail=str(ev.get("detail", "")),
            )
            if step_delay_s > 0:
                await asyncio.sleep(step_delay_s)
            matching_end = next(
                (
                    e
                    for e in res_dict.get("stage_events", [])
                    if e.get("stage_id") == ev.get("stage_id") and e.get("status") != "started"
                ),
                None,
            )
            if matching_end and matching_end.get("status") == "completed":
                await emitter.complete(
                    h,
                    records_count=matching_end.get("records_count"),
                    detail=str(matching_end.get("detail", "")),
                    sql_or_tool=str(matching_end.get("sql_or_tool", "")),
                )
            elif matching_end and matching_end.get("status") == "failed":
                await emitter.fail(h, error=str(matching_end.get("detail", "failed")))

    p = res_dict["provenance"]
    c = res_dict["cost"]
    result = TriageResult(
        incident_id=res_dict["incident_id"],
        pack_id=res_dict["pack_id"],
        engine_id=res_dict["engine_id"],
        synthesis_model=res_dict["synthesis_model"],
        mode="replay",
        verdict=IncidentVerdict(res_dict["verdict"]),
        executive_brief=res_dict["executive_brief"],
        recommended_option_id=res_dict["recommended_option_id"],
        recommended_action_type=res_dict["recommended_action_type"],
        remediation_options=tuple(res_dict.get("remediation_options", [])),
        prompt_injection_detected=bool(res_dict.get("prompt_injection_detected", False)),
        injection_signals=tuple(res_dict.get("injection_signals", [])),
        dag_nodes=tuple(AgentDAGNode(**n) for n in res_dict.get("dag_nodes", [])),
        retrieved_events=tuple(res_dict.get("retrieved_events", [])),
        retrieved_intel=tuple(res_dict.get("retrieved_intel", [])),
        retrieved_runbooks=tuple(res_dict.get("retrieved_runbooks", [])),
        provenance=ProvenanceReport(
            verified_event_ids=tuple(p.get("verified_event_ids", [])),
            unverified_event_ids=tuple(p.get("unverified_event_ids", [])),
            verified_intel_ids=tuple(p.get("verified_intel_ids", [])),
            unverified_intel_ids=tuple(p.get("unverified_intel_ids", [])),
            verified_runbook_ids=tuple(p.get("verified_runbook_ids", [])),
            unverified_runbook_ids=tuple(p.get("unverified_runbook_ids", [])),
            total_citations=int(p.get("total_citations", 0)),
            citation_precision=float(p.get("citation_precision", 0.0)),
            provenance_verified=bool(p.get("provenance_verified", False)),
            root_cause_recall=float(p.get("root_cause_recall", 0.0)),
            injection_resisted=p.get("injection_resisted"),
        ),
        cost=CostBreakdown(**c),
        latency_ms=int(res_dict.get("latency_ms", 0)),
        stage_events=tuple(res_dict.get("stage_events", [])),
    )
    return result, raw.get("evaluation")
