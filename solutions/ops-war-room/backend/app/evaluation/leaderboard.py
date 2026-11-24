"""Deterministic benchmark leaderboard aggregator over recorded live fixtures (zero hardcoded scores)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def summarize_recordings(recordings_dir: Path) -> dict[str, Any]:
    """Aggregate real recorded metrics from data/recordings/**/*.json by (pack_id, engine_id, model)."""
    if not recordings_dir.is_dir():
        return {"total_recordings": 0, "rows": [], "by_pack": {}}

    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    total = 0

    for json_file in sorted(recordings_dir.rglob("*.json")):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        res = payload.get("result")
        if not isinstance(res, dict):
            continue
        total += 1
        key = (
            str(res.get("pack_id", "unknown")),
            str(res.get("engine_id", "unknown")),
            str(res.get("synthesis_model", "unknown")),
        )
        buckets[key].append(payload)

    rows: list[dict[str, Any]] = []
    by_pack: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for (pack_id, engine_id, model), items in sorted(buckets.items()):
        n = len(items)
        recalls: list[float] = []
        grounded_list: list[float] = []
        rc_prec_list: list[float] = []
        safety_list: list[float] = []
        comp_list: list[float] = []
        prov_ok_count = 0
        inj_tested = 0
        inj_resisted = 0
        latencies: list[int] = []
        eng_costs: list[float] = []
        jdg_costs: list[float] = []
        judge_models: set[str] = set()

        for item in items:
            res = item["result"]
            ev = item.get("evaluation") or {}
            prov = res.get("provenance") or {}
            cost = res.get("cost") or {}

            recalls.append(float(prov.get("root_cause_recall", 0.0)))
            if prov.get("provenance_verified"):
                prov_ok_count += 1
            if prov.get("injection_resisted") is not None:
                inj_tested += 1
                if prov.get("injection_resisted") is True:
                    inj_resisted += 1

            latencies.append(int(res.get("latency_ms", 0)))
            eng_costs.append(float(cost.get("total_usd", 0.0)))

            if ev:
                grounded_list.append(float(ev.get("groundedness", 0.0)))
                rc_prec_list.append(float(ev.get("root_cause_precision", 0.0)))
                safety_list.append(float(ev.get("remediation_safety", 0.0)))
                comp_list.append(float(ev.get("composite_score", 0.0)))
                jdg_costs.append(float(ev.get("judge_cost_usd", 0.0)))
                if ev.get("judge_model"):
                    judge_models.add(str(ev["judge_model"]))

        row = {
            "pack_id": pack_id,
            "engine_id": engine_id,
            "synthesis_model": model,
            "judge_model": sorted(judge_models)[0] if judge_models else "none",
            "runs": n,
            "avg_root_cause_recall": round(sum(recalls) / n, 4),
            "avg_groundedness": round(sum(grounded_list) / len(grounded_list), 4) if grounded_list else 0.0,
            "avg_root_cause_precision": round(sum(rc_prec_list) / len(rc_prec_list), 4) if rc_prec_list else 0.0,
            "avg_remediation_safety": round(sum(safety_list) / len(safety_list), 4) if safety_list else 0.0,
            "avg_composite_score": round(sum(comp_list) / len(comp_list), 4) if comp_list else 0.0,
            "provenance_verified_rate": round(prov_ok_count / n, 4),
            "injection_tests": inj_tested,
            "injection_defense_rate": round(inj_resisted / inj_tested, 4) if inj_tested > 0 else None,
            "avg_latency_ms": int(sum(latencies) / n),
            "avg_engine_cost_usd": round(sum(eng_costs) / n, 6),
            "avg_judge_cost_usd": round(sum(jdg_costs) / len(jdg_costs), 6) if jdg_costs else 0.0,
        }
        rows.append(row)
        by_pack[pack_id].append(row)

    return {
        "total_recordings": total,
        "rows": rows,
        "by_pack": dict(by_pack),
    }
