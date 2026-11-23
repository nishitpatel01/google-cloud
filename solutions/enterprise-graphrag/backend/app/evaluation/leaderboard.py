"""Pure deterministic leaderboard & benchmark aggregator over recorded live runs.

Reads genuine recorded JSON fixtures from data/recordings/ — zero hardcoded scores.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def summarize_recordings(
    recordings_root: Path,
    *,
    pack_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate measured recall, judge scores, latency, and split costs from disk recordings."""
    root = recordings_root.resolve()
    if not root.is_dir():
        return {
            "total_recordings": 0,
            "pack_filter": pack_id,
            "leaderboard": [],
            "per_question": [],
        }

    records: list[dict[str, Any]] = []
    for json_path in sorted(root.rglob("*.json")):
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        header = raw.get("provenance_header") or {}
        res = raw.get("result") or {}
        rec_pack = str(header.get("pack_id") or res.get("pack_id") or "")
        if pack_id and rec_pack != pack_id:
            continue
        if not rec_pack or not res.get("engine_id"):
            continue
        records.append(raw)

    # Group by (pack_id, engine_id, synthesis_model, judge_model)
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    per_question_map: dict[tuple[str, str], dict[str, Any]] = {}

    for raw in records:
        header = raw.get("provenance_header") or {}
        res = raw["result"]
        ev = raw.get("evaluation") or {}
        recall = raw.get("recall") or {}

        pid = str(header.get("pack_id") or res.get("pack_id"))
        eid = str(res["engine_id"])
        synth = str(res.get("synthesis_model") or header.get("synthesis_model") or "")
        judge = str(ev.get("judge_model") or "none")
        qid = str(header.get("question_id") or "")

        grouped[(pid, eid, synth, judge)].append(raw)

        if qid:
            q_entry = per_question_map.setdefault(
                (pid, qid),
                {
                    "pack_id": pid,
                    "question_id": qid,
                    "query": res.get("query", ""),
                    "arms": {},
                },
            )
            prov = res.get("provenance") or {}
            unverified = (
                len(prov.get("unverified_entity_ids", []))
                + len(prov.get("unverified_edge_ids", []))
                + len(prov.get("unverified_chunk_ids", []))
            )
            q_entry["arms"][eid] = {
                "engine_id": eid,
                "engine_label": res.get("engine_label", eid),
                "synthesis_model": synth,
                "judge_model": ev.get("judge_model"),
                "path_recall": recall.get("path_recall"),
                "entity_recall": recall.get("entity_recall"),
                "edge_recall": recall.get("edge_recall"),
                "groundedness": ev.get("groundedness"),
                "qa_relevance": ev.get("qa_relevance"),
                "context_precision": ev.get("context_precision"),
                "composite_score": ev.get("composite_score"),
                "provenance_verified": unverified == 0,
                "paths_count": len(res.get("paths", [])),
                "duration_ms": float(res.get("total_duration_ms", 0.0)),
                "engine_cost_usd": float(res.get("engine_cost_usd", 0.0)),
                "judge_cost_usd": float(ev.get("judge_cost_usd", 0.0)),
                "rationale": ev.get("rationale", ""),
            }

    leaderboard: list[dict[str, Any]] = []
    for (pid, eid, synth, judge), items in sorted(grouped.items()):
        path_recalls: list[float] = []
        entity_recalls: list[float] = []
        edge_recalls: list[float] = []
        grounded_vals: list[float] = []
        qa_vals: list[float] = []
        precision_vals: list[float] = []
        composite_vals: list[float] = []
        durations: list[float] = []
        engine_costs: list[float] = []
        judge_costs: list[float] = []
        verified_count = 0

        for raw in items:
            res = raw["result"]
            ev = raw.get("evaluation") or {}
            recall = raw.get("recall") or {}
            prov = res.get("provenance") or {}
            unverified = (
                len(prov.get("unverified_entity_ids", []))
                + len(prov.get("unverified_edge_ids", []))
                + len(prov.get("unverified_chunk_ids", []))
            )
            if unverified == 0:
                verified_count += 1
            if isinstance(recall.get("path_recall"), (int, float)):
                path_recalls.append(float(recall["path_recall"]))
            if isinstance(recall.get("entity_recall"), (int, float)):
                entity_recalls.append(float(recall["entity_recall"]))
            if isinstance(recall.get("edge_recall"), (int, float)):
                edge_recalls.append(float(recall["edge_recall"]))
            if isinstance(ev.get("groundedness"), (int, float)):
                grounded_vals.append(float(ev["groundedness"]))
            if isinstance(ev.get("qa_relevance"), (int, float)):
                qa_vals.append(float(ev["qa_relevance"]))
            if isinstance(ev.get("context_precision"), (int, float)):
                precision_vals.append(float(ev["context_precision"]))
            if isinstance(ev.get("composite_score"), (int, float)):
                composite_vals.append(float(ev["composite_score"]))
            durations.append(float(res.get("total_duration_ms", 0.0)))
            engine_costs.append(float(res.get("engine_cost_usd", 0.0)))
            if isinstance(ev.get("judge_cost_usd"), (int, float)):
                judge_costs.append(float(ev["judge_cost_usd"]))

        leaderboard.append(
            {
                "pack_id": pid,
                "engine_id": eid,
                "engine_label": items[0]["result"].get("engine_label", eid),
                "synthesis_model": synth,
                "judge_model": None if judge == "none" else judge,
                "runs_count": len(items),
                "avg_path_recall": _mean(path_recalls),
                "avg_entity_recall": _mean(entity_recalls),
                "avg_edge_recall": _mean(edge_recalls),
                "avg_groundedness": _mean(grounded_vals),
                "avg_qa_relevance": _mean(qa_vals),
                "avg_context_precision": _mean(precision_vals),
                "avg_composite_score": _mean(composite_vals),
                "provenance_pass_rate": round(verified_count / len(items), 4),
                "avg_duration_ms": round(sum(durations) / len(durations), 2),
                "avg_engine_cost_usd": round(sum(engine_costs) / len(engine_costs), 6),
                "avg_judge_cost_usd": round(sum(judge_costs) / len(judge_costs), 6) if judge_costs else 0.0,
            }
        )

    leaderboard.sort(
        key=lambda row: (
            row["pack_id"],
            -(row["avg_composite_score"] if row["avg_composite_score"] is not None else -1.0),
            -(row["avg_path_recall"] if row["avg_path_recall"] is not None else -1.0),
        )
    )

    per_question = [per_question_map[k] for k in sorted(per_question_map.keys())]
    return {
        "total_recordings": len(records),
        "pack_filter": pack_id,
        "leaderboard": leaderboard,
        "per_question": per_question,
    }
