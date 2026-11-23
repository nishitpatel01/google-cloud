"""Offline unit tests for Vertex AI GenAI Eval, cross-family judges, redaction & leaderboard."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, validate_model_pair
from app.core.models import GraphPath, ProvenanceReport, TraversalDirection
from app.core.pricing import TokenUsage
from app.core.stage_emitter import RecordingStageEmitter
from app.engines.base import EngineFlags, EngineResult
from app.engines.replay import load_recording_evaluation, replay_recording, save_recording
from app.evaluation.judge import (
    JudgeEvaluation,
    JudgeResponseError,
    build_evaluate_instance_requests,
    claude_raw_predict_url,
    compute_composite_score,
    evaluate_engine_result,
    evaluate_instances_url,
    evaluate_with_claude_vertex,
    evaluate_with_vertex_genai_eval,
    normalize_likert_score,
    parse_judge_json,
    redact_gcp_identifiers,
)
from app.evaluation.leaderboard import summarize_recordings
from app.main import create_app


def _sample_result(pack_id: str = "supply_chain_subtier_risk", engine_id: str = "bq_graphrag") -> EngineResult:
    return EngineResult(
        engine_id=engine_id,
        engine_label="BigQuery Property Graph (ISO GQL)",
        pack_id=pack_id,
        query="Which semiconductor OEMs share sub-tier exposure to China tantalum?",
        synthesis_model="gemini-2.5-flash",
        answer="Intel [ENTITY:ISSUER-INTEL] connects via [EDGE:EDGE-SC-01] supported by [CHUNK:CHK-SC-01].",
        paths=(
            GraphPath(
                path_id="P1",
                hop_count=2,
                node_ids=("ISSUER-INTEL", "SMELTER-1", "COUNTRY-CN"),
                edge_ids=("EDGE-SC-01", "EDGE-SC-02"),
                path_exposure_usd=1_000_000.0,
                min_confidence=0.95,
            ),
        ),
        retrieved_entity_ids=("ISSUER-INTEL", "SMELTER-1", "COUNTRY-CN"),
        retrieved_edge_ids=("EDGE-SC-01", "EDGE-SC-02"),
        retrieved_chunk_ids=("CHK-SC-01",),
        retrieved_nodes=(
            {"entity_id": "ISSUER-INTEL", "canonical_name": "Intel Corporation"},
        ),
        retrieved_edges=(
            {
                "edge_id": "EDGE-SC-01",
                "source_entity_id": "ISSUER-INTEL",
                "target_entity_id": "SMELTER-1",
                "relation_type": "SOURCED_FROM_SMELTER",
                "evidence_excerpt": "Intel Form SD discloses smelter sourcing.",
            },
        ),
        retrieved_chunks=(
            {
                "chunk_id": "CHK-SC-01",
                "doc_name": "Intel 2026 Form SD",
                "page_number": 1,
                "content": "Intel Corporation conflict minerals report excerpt.",
            },
        ),
        provenance=ProvenanceReport(
            cited_entity_ids=("ISSUER-INTEL",),
            verified_entity_ids=("ISSUER-INTEL",),
            unverified_entity_ids=(),
            cited_edge_ids=("EDGE-SC-01",),
            verified_edge_ids=("EDGE-SC-01",),
            unverified_edge_ids=(),
            cited_chunk_ids=("CHK-SC-01",),
            verified_chunk_ids=("CHK-SC-01",),
            unverified_chunk_ids=(),
        ),
        usage=TokenUsage(input_tokens=1200, output_tokens=220),
        engine_cost_usd=0.00091,
        total_duration_ms=2150.0,
        flags=EngineFlags(graph_backend_used="bigquery", reranker_used=True, model_armor_checked=True),
    )


def test_model_pair_enforces_distinct_judge_and_synthesis_capabilities() -> None:
    synth, judge = validate_model_pair("gemini-2.5-flash", "vertex-genai-eval")
    assert synth.supports_synthesis is True
    assert judge.supports_judge is True

    with pytest.raises(ValueError, match="differ from synthesis"):
        validate_model_pair("gemini-2.5-flash", "gemini-2.5-flash")

    with pytest.raises(ValueError, match="judge-only"):
        validate_model_pair("vertex-genai-eval", "claude-opus-5-5")


def test_url_builders_and_identifier_redaction() -> None:
    assert evaluate_instances_url("demo-proj", "us-central1") == (
        "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/demo-proj/locations/us-central1:evaluateInstances"
    )
    assert claude_raw_predict_url("demo-proj", "claude-opus-5-5", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/demo-proj/locations/global/publishers/anthropic/models/claude-opus-5-5:rawPredict"
    )
    fake_pnum = "9" * 12
    redacted = redact_gcp_identifiers(
        f"Error calling projects/{fake_pnum}/locations/us-central1 in my-secret-proj",
        "my-secret-proj",
    )
    assert "my-secret-proj" not in redacted
    assert fake_pnum not in redacted
    assert "<redacted-project>" in redacted


def test_likert_and_json_parser_validation() -> None:
    assert normalize_likert_score(5.0, "QA") == 1.0
    assert normalize_likert_score(1.0, "QA") == 0.0
    assert normalize_likert_score(3.0, "QA") == 0.5

    g, q, p, rat = parse_judge_json(
        '```json\n{"groundedness": 0.95, "qa_relevance": 0.9, "context_precision": 0.85, "rationale": "Fully grounded."}\n```'
    )
    assert (g, q, p, rat) == (0.95, 0.9, 0.85, "Fully grounded.")

    with pytest.raises(JudgeResponseError):
        parse_judge_json('{"groundedness": 1.5, "qa_relevance": 0.9, "context_precision": 0.8, "rationale": "bad"}')


def test_vertex_genai_eval_client_scores_three_rubrics_and_splits_cost() -> None:
    settings = Settings(gcp_project_id="demo-proj", gcp_region="us-central1", demo_mode="live")
    reqs = build_evaluate_instance_requests(
        query="Trace tantalum risk",
        context="Intel sources via smelter X in China.",
        candidate_answer="Intel has sub-tier China exposure.",
        reference_answer="Intel -> Smelter X -> China",
        autorater_model_resource="projects/demo-proj/locations/us-central1/publishers/google/models/gemini-2.5-pro",
    )
    assert set(reqs.keys()) == {"GROUNDEDNESS", "QUESTION_ANSWERING_QUALITY", "FULFILLMENT"}
    assert "autoraterConfig" in reqs["GROUNDEDNESS"]

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: object) -> httpx.Response:
        assert ":evaluateInstances" in url
        assert headers["Authorization"] == "Bearer fake-token"
        tmpl = json["pointwiseMetricInput"]["metricSpec"]["metricPromptTemplate"]
        if "Groundedness" in tmpl:
            body = {"pointwiseMetricResult": {"score": 5.0, "explanation": "Claims verified."}}
        elif "Completeness" in tmpl:
            body = {"pointwiseMetricResult": {"score": 5.0, "explanation": "Complete answer."}}
        else:
            body = {"pointwiseMetricResult": {"score": 4.0, "explanation": "Focused evidence."}}
        return httpx.Response(200, json=body)

    ev = evaluate_with_vertex_genai_eval(
        settings=settings,
        query="Trace tantalum risk",
        context="Intel sources via smelter X in China.",
        candidate_answer="Intel has sub-tier China exposure.",
        reference_answer="Intel -> Smelter X -> China",
        path_recall=1.0,
        token_provider=lambda: "fake-token",
        http_post=_fake_post,
    )
    assert ev.judge_model == "vertex-genai-eval"
    assert ev.groundedness == 1.0
    assert ev.qa_relevance == 1.0
    assert ev.context_precision == 0.75
    assert ev.composite_score == compute_composite_score(
        groundedness=1.0, qa_relevance=1.0, context_precision=0.75, path_recall=1.0
    )
    assert ev.judge_cost_usd >= 0.0


def test_claude_opus_vertex_judge_omits_temperature_and_parses_usage() -> None:
    settings = Settings(gcp_project_id="demo-proj", demo_mode="live")
    seen_bodies: list[dict] = []

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: object) -> httpx.Response:
        assert "/publishers/anthropic/models/claude-opus-5-5:rawPredict" in url
        seen_bodies.append(json)
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": '{"groundedness": 0.96, "qa_relevance": 0.92, "context_precision": 0.88, "rationale": "Strong multi-hop grounding."}',
                    }
                ],
                "usage": {"input_tokens": 900, "output_tokens": 120},
            },
        )

    ev = evaluate_with_claude_vertex(
        settings=settings,
        judge_model="claude-opus-5-5",
        query="Trace risk",
        context="Graph paths",
        candidate_answer="Grounded answer",
        path_recall=1.0,
        token_provider=lambda: "fake-token",
        http_post=_fake_post,
    )
    assert "temperature" not in seen_bodies[0]  # Claude Opus 5.5 rejects temperature
    assert ev.judge_provider == "anthropic_vertex"
    assert ev.input_tokens == 900
    assert ev.output_tokens == 120
    assert ev.judge_cost_usd == 0.006


def test_recording_scrubs_project_id_and_aggregates_leaderboard(tmp_path: Path) -> None:
    emitter = RecordingStageEmitter()

    async def _emit() -> None:
        h = await emitter.started("gql_traverse", label="GQL", service="bigquery")
        await emitter.completed(
            h,
            service_used="bigquery",
            payload={"gql": "GRAPH `secret-corp-proj.acc05_enterprise_graphrag.EnterpriseGraph` MATCH ..."},
        )

    asyncio.run(_emit())
    res = _sample_result()
    fake_pnum = "1" * 12
    ev = JudgeEvaluation(
        judge_model="vertex-genai-eval",
        judge_provider="vertex_genai_eval",
        groundedness=0.95,
        qa_relevance=0.92,
        context_precision=0.88,
        path_recall=1.0,
        provenance_verified=True,
        composite_score=0.951,
        rationale=f"Verified against projects/{fake_pnum} in secret-corp-proj",
        input_tokens=600,
        output_tokens=80,
        judge_cost_usd=0.00038,
        duration_ms=420.0,
    )
    saved = save_recording(
        tmp_path,
        result=res,
        stage_events=emitter.events,
        question_id="sc_q01",
        evaluation=ev,
        recall={"entity_recall": 1.0, "edge_recall": 1.0, "path_recall": 1.0},
        redact_project_id="secret-corp-proj",
    )

    raw_text = saved.read_text(encoding="utf-8")
    assert "secret-corp-proj" not in raw_text
    assert fake_pnum not in raw_text
    assert "<project_id>" in raw_text

    loaded_ev = load_recording_evaluation(
        tmp_path,
        engine_id="bq_graphrag",
        pack_id="supply_chain_subtier_risk",
        synthesis_model="gemini-2.5-flash",
        question_id="sc_q01",
    )
    assert loaded_ev is not None
    assert loaded_ev.judge_model == "vertex-genai-eval"
    assert loaded_ev.groundedness == 0.95

    summary = summarize_recordings(tmp_path, pack_id="supply_chain_subtier_risk")
    assert summary["total_recordings"] == 1
    assert summary["leaderboard"][0]["avg_path_recall"] == 1.0
    assert summary["leaderboard"][0]["avg_groundedness"] == 0.95
    assert summary["per_question"][0]["question_id"] == "sc_q01"


def test_api_rejects_same_judge_and_synthesis_and_serves_benchmarks_summary() -> None:
    app = create_app(settings=Settings(demo_mode="replay"))
    client = TestClient(app)

    bad = client.post(
        "/api/v1/query/stream",
        json={
            "pack_id": "supply_chain_subtier_risk",
            "query": "Which OEMs share China tantalum smelters?",
            "engine_id": "bq_graphrag",
            "synthesis_model": "gemini-2.5-flash",
            "judge_model": "gemini-2.5-flash",
            "mode": "replay",
        },
    )
    assert bad.status_code == 422

    summary_resp = client.get("/api/v1/benchmarks/summary?pack_id=supply_chain_subtier_risk")
    assert summary_resp.status_code == 200
    body = summary_resp.json()
    assert "leaderboard" in body
    assert "per_question" in body


def test_committed_replay_recordings_roundtrip_offline_with_zero_gcp_creds() -> None:
    """Verify every recorded live fixture replays cleanly offline with zero leaked project IDs."""
    settings = Settings(demo_mode="replay", gcp_project_id="")
    root = settings.recordings_dir
    if not root.is_dir():
        return
    json_files = sorted(root.rglob("*.json"))
    assert len(json_files) > 0, "Expected recorded live replay fixtures under data/recordings/"

    forbidden_home = "/" + "home/"
    forbidden_users = "/" + "Users/"
    forbidden_adc = "application_default_" + "credentials"
    for fpath in json_files:
        raw_text = fpath.read_text(encoding="utf-8")
        assert forbidden_users not in raw_text and forbidden_home not in raw_text, f"Local path in {fpath}"
        assert forbidden_adc not in raw_text, f"ADC ref in {fpath}"
        raw = json.loads(raw_text)
        hdr = raw["provenance_header"]
        emitter = RecordingStageEmitter()
        replayed = asyncio.run(
            replay_recording(
                root,
                engine_id=hdr["engine_id"],
                pack_id=hdr["pack_id"],
                synthesis_model=hdr["synthesis_model"],
                question_id=hdr["question_id"],
                emitter=emitter,
            )
        )
        assert replayed.engine_id == hdr["engine_id"]
        assert replayed.pack_id == hdr["pack_id"]
        assert len(emitter.events) > 0
        ev = load_recording_evaluation(
            root,
            engine_id=hdr["engine_id"],
            pack_id=hdr["pack_id"],
            synthesis_model=hdr["synthesis_model"],
            question_id=hdr["question_id"],
        )
        assert ev is not None, f"Expected evaluation block in {fpath}"
        assert 0.0 <= ev.groundedness <= 1.0
        assert 0.0 <= ev.composite_score <= 1.0
