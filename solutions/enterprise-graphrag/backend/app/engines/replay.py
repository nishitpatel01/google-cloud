"""Zero-credential Replay runner and recorder for genuine recorded live runs.

Replay never improvises or fabricates answers: missing recordings raise FileNotFoundError.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.models import (
    GraphPath,
    ProvenanceReport,
    StageCompletedEvent,
    StageEvent,
    StageFailedEvent,
    StageSkippedEvent,
    StageStartedEvent,
)
from app.core.pricing import TokenUsage
from app.core.stage_emitter import StageEmitter
from app.engines.base import EngineFlags, EngineResult

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _safe_segment(val: str) -> str:
    if not _SAFE_SEGMENT_RE.match(val) or ".." in val:
        raise ValueError(f"Unsafe recording path segment: {val!r}")
    return val


def recording_path(
    recordings_root: Path,
    *,
    engine_id: str,
    pack_id: str,
    synthesis_model: str,
    question_id: str,
) -> Path:
    """Resolve a sandboxed recording JSON path with strict path-traversal prevention."""
    root = recordings_root.resolve()
    target = (
        root
        / _safe_segment(engine_id)
        / _safe_segment(pack_id)
        / _safe_segment(synthesis_model)
        / f"{_safe_segment(question_id)}.json"
    ).resolve()
    if not str(target).startswith(str(root) + "/"):
        raise ValueError("Recording path escaped recordings_root boundary")
    return target


def save_recording(
    recordings_root: Path,
    *,
    result: EngineResult,
    stage_events: tuple[StageEvent, ...],
    question_id: str,
    git_sha: str = "dev",
) -> Path:
    """Persist a verified live run as a zero-credential replay fixture."""
    path = recording_path(
        recordings_root,
        engine_id=result.engine_id,
        pack_id=result.pack_id,
        synthesis_model=result.synthesis_model,
        question_id=question_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provenance_header": {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "engine_id": result.engine_id,
            "pack_id": result.pack_id,
            "synthesis_model": result.synthesis_model,
            "question_id": question_id,
            "git_sha": git_sha,
        },
        "result": {
            "engine_id": result.engine_id,
            "engine_label": result.engine_label,
            "pack_id": result.pack_id,
            "query": result.query,
            "synthesis_model": result.synthesis_model,
            "answer": result.answer,
            "paths": [p.model_dump(mode="json") for p in result.paths],
            "retrieved_entity_ids": list(result.retrieved_entity_ids),
            "retrieved_edge_ids": list(result.retrieved_edge_ids),
            "retrieved_chunk_ids": list(result.retrieved_chunk_ids),
            "retrieved_nodes": list(result.retrieved_nodes),
            "retrieved_edges": list(result.retrieved_edges),
            "retrieved_chunks": list(result.retrieved_chunks),
            "provenance": result.provenance.model_dump(mode="json"),
            "usage": asdict(result.usage),
            "engine_cost_usd": result.engine_cost_usd,
            "total_duration_ms": result.total_duration_ms,
            "flags": asdict(result.flags),
        },
        "stage_events": [ev.model_dump(mode="json") for ev in stage_events],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


async def replay_recording(
    recordings_root: Path,
    *,
    engine_id: str,
    pack_id: str,
    synthesis_model: str,
    question_id: str,
    emitter: StageEmitter,
) -> EngineResult:
    """Replay a recorded live run through StageEmitter; fails loud if missing."""
    path = recording_path(
        recordings_root,
        engine_id=engine_id,
        pack_id=pack_id,
        synthesis_model=synthesis_model,
        question_id=question_id,
    )
    if not path.is_file():
        raise FileNotFoundError(
            f"No recorded live fixture found for engine={engine_id}, pack={pack_id}, "
            f"model={synthesis_model}, question={question_id}"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    res_raw: dict[str, Any] = raw["result"]
    events_raw: list[dict[str, Any]] = raw.get("stage_events", [])

    # Emit recorded stages cleanly in original order without fake sleep timers
    active_handles: dict[str, Any] = {}
    parsed_events: list[StageEvent] = []
    for ev in events_raw:
        etype = ev.get("event_type")
        if etype == "stage_started":
            parsed = StageStartedEvent.model_validate(ev)
            parsed_events.append(parsed)
            active_handles[parsed.stage] = await emitter.started(
                parsed.stage,
                label=parsed.label,
                service=parsed.service,
                request=parsed.request,
            )
        elif etype == "stage_completed":
            parsed = StageCompletedEvent.model_validate(ev)
            parsed_events.append(parsed)
            handle = active_handles.pop(parsed.stage, None)
            if handle is not None:
                await emitter.completed(
                    handle,
                    service_used=parsed.service_used,
                    payload={**parsed.payload, "recorded_duration_ms": parsed.duration_ms},
                )
        elif etype == "stage_skipped":
            parsed = StageSkippedEvent.model_validate(ev)
            parsed_events.append(parsed)
            await emitter.skipped(parsed.stage, reason=parsed.reason)
        elif etype == "stage_failed":
            parsed = StageFailedEvent.model_validate(ev)
            parsed_events.append(parsed)

    return EngineResult(
        engine_id=res_raw["engine_id"],
        engine_label=res_raw["engine_label"],
        pack_id=res_raw["pack_id"],
        query=res_raw["query"],
        synthesis_model=res_raw["synthesis_model"],
        answer=res_raw["answer"],
        paths=tuple(GraphPath.model_validate(p) for p in res_raw.get("paths", [])),
        retrieved_entity_ids=tuple(res_raw.get("retrieved_entity_ids", [])),
        retrieved_edge_ids=tuple(res_raw.get("retrieved_edge_ids", [])),
        retrieved_chunk_ids=tuple(res_raw.get("retrieved_chunk_ids", [])),
        retrieved_nodes=tuple(res_raw.get("retrieved_nodes", [])),
        retrieved_edges=tuple(res_raw.get("retrieved_edges", [])),
        retrieved_chunks=tuple(res_raw.get("retrieved_chunks", [])),
        provenance=ProvenanceReport.model_validate(res_raw["provenance"]),
        usage=TokenUsage(**res_raw["usage"]),
        engine_cost_usd=float(res_raw["engine_cost_usd"]),
        total_duration_ms=float(res_raw["total_duration_ms"]),
        flags=EngineFlags(**res_raw["flags"]),
        stage_log=tuple(parsed_events),
    )
