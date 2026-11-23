#!/usr/bin/env python3
"""Record genuine live GCP benchmark runs & Vertex AI evaluations into data/recordings/.

Usage:
  PYTHONPATH=backend .venv/bin/python scripts/record_live_benchmarks.py --smoke
  PYTHONPATH=backend .venv/bin/python scripts/record_live_benchmarks.py --pack all --skip-existing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = SOLUTION_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings, validate_model_pair
from app.core.security import screen_with_model_armor
from app.core.stage_emitter import RecordingStageEmitter
from app.engines.adk_graph_agent import ADKGraphCoordinatorEngine
from app.engines.base import QueryRequest
from app.engines.bq_graphrag import BigQueryGraphRAGEngine
from app.engines.flat_vector_rag import FlatVectorRAGEngine
from app.engines.provenance_verifier import compute_multihop_recall
from app.engines.replay import recording_path, save_recording
from app.evaluation.judge import evaluate_engine_result
from app.evaluation.leaderboard import summarize_recordings
from app.packs.loader import LoadedPack, load_all_packs


def _git_short_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=SOLUTION_ROOT, text=True
        )
        return out.strip()
    except Exception:
        return "dev"


async def record_one(
    *,
    settings: Settings,
    packs: dict[str, LoadedPack],
    pack: LoadedPack,
    question_idx: int,
    engine_id: str,
    synthesis_model: str,
    judge_model: str,
    skip_existing: bool,
    git_sha: str,
) -> dict[str, object]:
    q = pack.benchmark.questions[question_idx]
    target = recording_path(
        settings.recordings_dir,
        engine_id=engine_id,
        pack_id=pack.pack_id,
        synthesis_model=synthesis_model,
        question_id=q.question_id,
    )
    if skip_existing and target.is_file():
        print(f"[skip] {pack.pack_id}/{q.question_id}/{engine_id} already recorded", flush=True)
        return {"skipped": True, "path": str(target)}

    emitter = RecordingStageEmitter()
    req = QueryRequest(
        pack_id=pack.pack_id,
        query=q.question,
        synthesis_model=synthesis_model,
        question_id=q.question_id,
        max_hops=q.hop_depth,
        direction=q.direction,
        enable_rerank=True,
    )

    if engine_id == "flat_vector_rag":
        engine = FlatVectorRAGEngine(settings)
    elif engine_id == "bq_graphrag":
        engine = BigQueryGraphRAGEngine(settings)
    elif engine_id == "adk_graph_coordinator":
        engine = ADKGraphCoordinatorEngine(settings, packs=packs)
    else:
        raise ValueError(f"Unsupported engine_id: {engine_id}")

    # Screen prompt with Model Armor when configured (ADK engine runs its own stage)
    ma_checked = False
    if engine_id != "adk_graph_coordinator" and settings.model_armor_template_id.strip():
        verdict = screen_with_model_armor(settings=settings, text=req.query, mode="user_prompt")
        ma_checked = verdict.checked

    result = await engine.run_query(req, emitter=emitter)
    if ma_checked and not result.flags.model_armor_checked:
        from dataclasses import replace

        result = replace(result, flags=replace(result.flags, model_armor_checked=True))

    recall = compute_multihop_recall(
        expected_entity_ids=q.expected_entity_ids,
        expected_edge_ids=q.expected_edge_ids,
        retrieved_entity_ids=frozenset(result.retrieved_entity_ids),
        retrieved_edge_ids=frozenset(result.retrieved_edge_ids),
    )

    evaluation = await evaluate_engine_result(
        settings=settings,
        result=result,
        judge_model=judge_model,
        reference_answer=" ".join(q.expected_answer_points),
        path_recall=recall["path_recall"],
        emitter=emitter,
    )

    saved_path = save_recording(
        settings.recordings_dir,
        result=result,
        stage_events=emitter.events,
        question_id=q.question_id,
        git_sha=git_sha,
        evaluation=evaluation,
        recall=recall,
        redact_project_id=settings.gcp_project_id,
    )

    print(
        f"[recorded] {pack.pack_id:<26} {q.question_id:<8} {engine_id:<22} "
        f"recall={recall['path_recall']:.2f} grounded={evaluation.groundedness:.2f} "
        f"qa={evaluation.qa_relevance:.2f} comp={evaluation.composite_score:.2f} "
        f"prov={'OK' if result.provenance.is_fully_verified else 'FLAG'} "
        f"eng=${result.engine_cost_usd:.4f} jdg=${evaluation.judge_cost_usd:.4f} "
        f"ms={result.total_duration_ms:.0f}",
        flush=True,
    )
    return {
        "skipped": False,
        "path": str(saved_path),
        "engine_cost_usd": result.engine_cost_usd,
        "judge_cost_usd": evaluation.judge_cost_usd,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Record live GraphRAG benchmarks into Replay fixtures.")
    parser.add_argument("--pack", default="all", help="Pack ID or 'all'")
    parser.add_argument("--synthesis-model", default="gemini-2.5-flash")
    parser.add_argument("--judge-model", default="vertex-genai-eval")
    parser.add_argument("--include-adk-hero", action="store_true", default=True)
    parser.add_argument("--no-adk-hero", dest="include_adk_hero", action="store_false")
    parser.add_argument("--smoke", action="store_true", help="Record 1 pack hero question across both arms")
    parser.add_argument("--skip-existing", action="store_true", help="Skip fixtures already present on disk")
    args = parser.parse_args()

    validate_model_pair(args.synthesis_model, args.judge_model)
    settings = Settings()
    if not settings.gcp_project_id:
        raise SystemExit("ERROR: GCP_PROJECT_ID must be set to record live GCP fixtures.")

    packs = load_all_packs(settings.packs_dir)
    selected_pack_ids = sorted(packs.keys()) if args.pack == "all" else [args.pack]
    git_sha = _git_short_sha()

    print(
        f"=== Recording live benchmarks: packs={selected_pack_ids} "
        f"synth={args.synthesis_model} judge={args.judge_model} smoke={args.smoke} ===",
        flush=True,
    )

    total_eng_cost = 0.0
    total_jdg_cost = 0.0
    recorded_count = 0

    for pid in selected_pack_ids:
        pack = packs[pid]
        q_indices = [0] if args.smoke else list(range(len(pack.benchmark.questions)))
        for idx in q_indices:
            for eid in ("flat_vector_rag", "bq_graphrag"):
                info = await record_one(
                    settings=settings,
                    packs=packs,
                    pack=pack,
                    question_idx=idx,
                    engine_id=eid,
                    synthesis_model=args.synthesis_model,
                    judge_model=args.judge_model,
                    skip_existing=args.skip_existing,
                    git_sha=git_sha,
                )
                if not info["skipped"]:
                    recorded_count += 1
                    total_eng_cost += float(info["engine_cost_usd"])
                    total_jdg_cost += float(info["judge_cost_usd"])
        if args.include_adk_hero:
            info = await record_one(
                settings=settings,
                packs=packs,
                pack=pack,
                question_idx=0,
                engine_id="adk_graph_coordinator",
                synthesis_model=args.synthesis_model,
                judge_model=args.judge_model,
                skip_existing=args.skip_existing,
                git_sha=git_sha,
            )
            if not info["skipped"]:
                recorded_count += 1
                total_eng_cost += float(info["engine_cost_usd"])
                total_jdg_cost += float(info["judge_cost_usd"])
        if args.smoke:
            break

    summary = summarize_recordings(settings.recordings_dir)
    print(
        f"\n=== Recording complete: new_runs={recorded_count} "
        f"total_fixtures={summary['total_recordings']} "
        f"engine_spend=${total_eng_cost:.4f} judge_spend=${total_jdg_cost:.4f} ===",
        flush=True,
    )
    print(json.dumps(summary["leaderboard"], indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
