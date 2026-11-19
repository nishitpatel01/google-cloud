"""Unit tests for FlatVectorRAGEngine, BigQueryGraphRAGEngine, SpannerGraphRAGEngine, and Replay."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.core.models import (
    StageCompletedEvent,
    StageFailedEvent,
    StageSkippedEvent,
    StageStartedEvent,
    TraversalDirection,
)
from app.core.pricing import TokenUsage
from app.core.stage_emitter import RecordingStageEmitter
from app.engines.base import QueryRequest
from app.engines.bq_graphrag import BigQueryGraphRAGEngine
from app.engines.flat_vector_rag import FlatVectorRAGEngine
from app.engines.replay import replay_recording, save_recording
from app.engines.spanner_graphrag import SpannerGraphRAGEngine


class _FakeBQJob:
    def __init__(self, rows: list[dict[str, Any]], *, bytes_processed: int = 4096, cache_hit: bool = False) -> None:
        self._rows = rows
        self.total_bytes_processed = bytes_processed
        self.cache_hit = cache_hit

    def result(self) -> list[dict[str, Any]]:
        return self._rows


def _live_settings(*, spanner_instance_id: str = "") -> Settings:
    return Settings(
        gcp_project_id="demo-project-123",
        bq_dataset="enterprise_graphrag",
        bq_location="US",
        demo_mode="live",
        spanner_instance_id=spanner_instance_id,
    )


def _sample_gql_rows() -> list[dict[str, Any]]:
    nodes = [
        {"properties": {"entity_id": "ENT_OEM", "canonical_name": "Apex Silicon", "entity_type": "OEM"}},
        {"properties": {"entity_id": "ENT_FAB", "canonical_name": "Tantalum Foundry", "entity_type": "Tier1Supplier"}},
        {"properties": {"entity_id": "ENT_SMELT", "canonical_name": "Refinery X", "entity_type": "Smelter"}},
    ]
    edges = [
        {
            "properties": {
                "edge_id": "EDG_1",
                "source_entity_id": "ENT_OEM",
                "target_entity_id": "ENT_FAB",
                "relation_type": "SOURCES_FROM",
                "evidence_excerpt": "SEC Form SD filing cites Tier-1 supplier.",
                "source_chunk_id": "CHK_1",
                "data_origin": "REAL_PUBLIC",
            }
        },
        {
            "properties": {
                "edge_id": "EDG_2",
                "source_entity_id": "ENT_FAB",
                "target_entity_id": "ENT_SMELT",
                "relation_type": "REFINES_AT",
                "evidence_excerpt": "Smelter annex lists Refinery X.",
                "source_chunk_id": "CHK_2",
                "data_origin": "REAL_PUBLIC",
            }
        },
    ]
    return [
        {
            "start_id": "ENT_OEM",
            "end_id": "ENT_SMELT",
            "hop_count": 2,
            "path_exposure_usd": 450_000_000.0,
            "min_confidence": 0.95,
            "path_nodes_json": json.dumps(nodes),
            "path_edges_json": json.dumps(edges),
        }
    ]


def test_flat_vector_engine_end_to_end_and_sse_queue() -> None:
    settings = _live_settings()
    engine = FlatVectorRAGEngine(settings)
    sse_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    emitter = RecordingStageEmitter(sse_queue=sse_q)

    fake_bq = MagicMock()
    fake_bq.query.side_effect = [
        _FakeBQJob([], bytes_processed=1024),  # dry-run
        _FakeBQJob(
            [
                {
                    "chunk_id": "CHK_1",
                    "doc_name": "form_sd.htm",
                    "doc_type": "sec_cmr",
                    "page_number": 1,
                    "content": "Apex Silicon sources tantalum via Tier-1 partners.",
                    "data_origin": "REAL_PUBLIC",
                    "source_license": "US_GOV_PUBLIC_DOMAIN",
                    "source_url": "https://www.sec.gov/",
                    "distance": 0.14,
                }
            ],
            bytes_processed=2048,
        ),
    ]

    req = QueryRequest(
        pack_id="supply_chain_subtier_risk",
        query="Which smelters expose Apex Silicon?",
        synthesis_model="gemini-2.5-flash",
    )

    with (
        patch("app.engines.flat_vector_rag.embed_single_text", return_value=[0.1, 0.2, 0.3]),
        patch("app.engines.flat_vector_rag.get_bigquery_client", return_value=fake_bq),
        patch(
            "app.engines.flat_vector_rag.generate_grounded_text",
            return_value=(
                "Based on [CHUNK:CHK_1], Apex Silicon uses Tier-1 partners; downstream smelter hops are missing.",
                TokenUsage(input_tokens=400, output_tokens=80),
            ),
        ),
    ):
        res = asyncio.run(engine.run_query(req, emitter=emitter))

    assert res.engine_id == "flat_vector_rag"
    assert res.paths == ()
    assert res.retrieved_entity_ids == ()
    assert res.retrieved_chunk_ids == ("CHK_1",)
    assert res.flags.graph_backend_used == "none"
    assert res.flags.bytes_processed == 2048
    assert res.provenance.is_fully_verified is True
    assert res.engine_cost_usd > 0.0

    completed_stages = [e.stage for e in emitter.events if isinstance(e, StageCompletedEvent)]
    assert completed_stages == ["query_prep", "embed_query", "retrieve_chunks", "synthesize", "verify_provenance"]
    assert sse_q.qsize() == len(emitter.events)


def test_flat_vector_engine_enforces_bq_byte_cap_and_fails_loud() -> None:
    settings = _live_settings()
    engine = FlatVectorRAGEngine(settings)
    emitter = RecordingStageEmitter()

    fake_bq = MagicMock()
    fake_bq.query.return_value = _FakeBQJob([], bytes_processed=settings.max_bq_bytes_billed + 1)

    req = QueryRequest(
        pack_id="supply_chain_subtier_risk",
        query="Test byte cap guard",
        synthesis_model="gemini-2.5-flash",
    )
    with (
        patch("app.engines.flat_vector_rag.embed_single_text", return_value=[0.1, 0.2]),
        patch("app.engines.flat_vector_rag.get_bigquery_client", return_value=fake_bq),
    ):
        with pytest.raises(RuntimeError, match="exceeds|cap|>"):
            asyncio.run(engine.run_query(req, emitter=emitter))

    failed = [e for e in emitter.events if isinstance(e, StageFailedEvent)]
    assert len(failed) == 1
    assert failed[0].stage == "retrieve_chunks"


def test_bq_graphrag_engine_full_pipeline_rerank_and_hallucination_detection() -> None:
    settings = _live_settings()
    engine = BigQueryGraphRAGEngine(settings)
    emitter = RecordingStageEmitter()

    fake_bq = MagicMock()
    fake_bq.query.side_effect = [
        _FakeBQJob([], bytes_processed=512),  # seed dry-run
        _FakeBQJob([{"entity_id": "ENT_OEM", "canonical_name": "Apex Silicon"}], bytes_processed=1024),
        _FakeBQJob(_sample_gql_rows(), bytes_processed=4096),  # ISO GQL traversal
        _FakeBQJob(
            [
                {
                    "chunk_id": "CHK_1",
                    "doc_name": "form_sd.htm",
                    "doc_type": "sec_cmr",
                    "page_number": 1,
                    "content": "SEC Form SD filing cites Tier-1 supplier.",
                    "data_origin": "REAL_PUBLIC",
                    "source_license": "US_GOV_PUBLIC_DOMAIN",
                    "source_url": "https://www.sec.gov/",
                },
                {
                    "chunk_id": "CHK_2",
                    "doc_name": "smelter_annex.htm",
                    "doc_type": "sec_cmr",
                    "page_number": 2,
                    "content": "Smelter annex lists Refinery X.",
                    "data_origin": "REAL_PUBLIC",
                    "source_license": "US_GOV_PUBLIC_DOMAIN",
                    "source_url": "https://www.sec.gov/",
                },
            ],
            bytes_processed=512,
        ),
    ]

    req = QueryRequest(
        pack_id="supply_chain_subtier_risk",
        query="Trace 2-hop smelter exposure for Apex Silicon",
        synthesis_model="gemini-2.5-flash",
        max_hops=3,
        direction=TraversalDirection.OUTGOING,
        enable_rerank=True,
    )

    with (
        patch("app.engines.bq_graphrag.embed_single_text", return_value=[0.25, 0.75]),
        patch("app.engines.bq_graphrag.get_bigquery_client", return_value=fake_bq),
        patch("app.engines.bq_graphrag.rerank_records", return_value=[("path-001", 0.94)]),
        patch(
            "app.engines.bq_graphrag.generate_grounded_text",
            return_value=(
                "[ENTITY:ENT_OEM] reaches [ENTITY:ENT_SMELT] via [EDGE:EDG_1], [EDGE:EDG_2], "
                "and hallucinated [EDGE:EDG_999] [CHUNK:CHK_1].",
                TokenUsage(input_tokens=900, output_tokens=120),
            ),
        ),
    ):
        res = asyncio.run(engine.run_query(req, emitter=emitter))

    assert res.engine_id == "bq_graphrag"
    assert len(res.paths) == 1
    assert res.flags.graph_backend_used == "bigquery_property_graph"
    assert res.flags.reranker_used is True
    assert set(res.retrieved_entity_ids) == {"ENT_OEM", "ENT_FAB", "ENT_SMELT"}
    assert set(res.retrieved_edge_ids) == {"EDG_1", "EDG_2"}
    assert set(res.retrieved_chunk_ids) == {"CHK_1", "CHK_2"}
    assert res.provenance.is_fully_verified is False
    assert res.provenance.unverified_edge_ids == ("EDG_999",)

    # Verify save_recording -> replay_recording roundtrip with ZERO GCP calls
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        saved = save_recording(root, result=res, stage_events=emitter.events, question_id="sc_q01", git_sha="abc1234")
        assert saved.is_file()

        replay_emitter = RecordingStageEmitter()
        replayed = asyncio.run(
            replay_recording(
                root,
                engine_id="bq_graphrag",
                pack_id="supply_chain_subtier_risk",
                synthesis_model="gemini-2.5-flash",
                question_id="sc_q01",
                emitter=replay_emitter,
            )
        )
        assert replayed.answer == res.answer
        assert replayed.paths == res.paths
        assert replayed.provenance == res.provenance
        assert replayed.engine_cost_usd == res.engine_cost_usd
        assert any(isinstance(e, StageStartedEvent) for e in replay_emitter.events)
        assert any(
            isinstance(e, StageCompletedEvent) and "recorded_duration_ms" in e.payload
            for e in replay_emitter.events
        )


def test_bq_graphrag_emits_skipped_when_rerank_disabled_and_fails_loud_on_chunk_error() -> None:
    settings = _live_settings()
    engine = BigQueryGraphRAGEngine(settings)
    emitter = RecordingStageEmitter()

    fake_bq = MagicMock()
    fake_bq.query.side_effect = [
        _FakeBQJob([], bytes_processed=512),
        _FakeBQJob([{"entity_id": "ENT_OEM", "canonical_name": "Apex Silicon"}], bytes_processed=512),
        _FakeBQJob(_sample_gql_rows(), bytes_processed=1024),
        RuntimeError("Simulated BigQuery chunk read outage"),
    ]
    req = QueryRequest(
        pack_id="supply_chain_subtier_risk",
        query="Check fail loud during gql_traverse chunk hydration",
        synthesis_model="gemini-2.5-flash",
        enable_rerank=False,
    )
    with (
        patch("app.engines.bq_graphrag.embed_single_text", return_value=[0.1, 0.2]),
        patch("app.engines.bq_graphrag.get_bigquery_client", return_value=fake_bq),
    ):
        with pytest.raises(RuntimeError, match="chunk read outage"):
            asyncio.run(engine.run_query(req, emitter=emitter))

    failed = [e for e in emitter.events if isinstance(e, StageFailedEvent)]
    assert len(failed) == 1
    assert failed[0].stage == "gql_traverse"

    # Also verify rerank skip path when enable_rerank=False succeeds cleanly
    emitter2 = RecordingStageEmitter()
    fake_bq2 = MagicMock()
    fake_bq2.query.side_effect = [
        _FakeBQJob([], bytes_processed=512),
        _FakeBQJob([{"entity_id": "ENT_OEM", "canonical_name": "Apex Silicon"}], bytes_processed=512),
        _FakeBQJob(_sample_gql_rows(), bytes_processed=1024),
        _FakeBQJob([], bytes_processed=128),
    ]
    with (
        patch("app.engines.bq_graphrag.embed_single_text", return_value=[0.1, 0.2]),
        patch("app.engines.bq_graphrag.get_bigquery_client", return_value=fake_bq2),
        patch(
            "app.engines.bq_graphrag.generate_grounded_text",
            return_value=("Verified [ENTITY:ENT_OEM] [EDGE:EDG_1].", TokenUsage(100, 20)),
        ),
    ):
        res2 = asyncio.run(engine.run_query(req, emitter=emitter2))
    assert res2.flags.reranker_used is False
    assert any(isinstance(e, StageSkippedEvent) and e.stage == "rerank" for e in emitter2.events)


def test_spanner_engine_fails_loud_when_parked_and_runs_when_configured() -> None:
    parked_engine = SpannerGraphRAGEngine(_live_settings(spanner_instance_id=""))
    emitter = RecordingStageEmitter()
    req = QueryRequest(
        pack_id="supply_chain_subtier_risk",
        query="Trace exposure",
        synthesis_model="gemini-2.5-flash",
    )
    with pytest.raises(RuntimeError, match="SPANNER_INSTANCE_ID"):
        asyncio.run(parked_engine.run_query(req, emitter=emitter))
    assert any(isinstance(e, StageFailedEvent) for e in emitter.events)

    # Configured Spanner Graph adapter path
    active_engine = SpannerGraphRAGEngine(_live_settings(spanner_instance_id="graphrag-spanner"))
    emitter2 = RecordingStageEmitter()

    class _FakeSpannerResults:
        def __init__(self, cols: list[str], rows: list[list[Any]]) -> None:
            self.fields = [type("F", (), {"name": c})() for c in cols]
            self._rows = rows

        def __iter__(self) -> Any:
            return iter(self._rows)

    gql_row = _sample_gql_rows()[0]
    snapshot = MagicMock()
    snapshot.execute_sql.side_effect = [
        _FakeSpannerResults(["entity_id", "canonical_name"], [["ENT_OEM", "Apex Silicon"]]),
        _FakeSpannerResults(list(gql_row.keys()), [list(gql_row.values())]),
        _FakeSpannerResults(
            ["chunk_id", "doc_name", "doc_type", "page_number", "content", "data_origin", "source_license", "source_url"],
            [["CHK_1", "form_sd.htm", "sec_cmr", 1, "Evidence text.", "REAL_PUBLIC", "US_GOV_PUBLIC_DOMAIN", ""]],
        ),
    ]
    fake_db = MagicMock()
    fake_db.snapshot.return_value.__enter__.return_value = snapshot

    with (
        patch("app.engines.spanner_graphrag.embed_single_text", return_value=[0.1, 0.2]),
        patch("app.engines.spanner_graphrag.get_spanner_database", return_value=fake_db),
        patch("app.engines.spanner_graphrag.rerank_records", return_value=[("path-001", 0.9)]),
        patch(
            "app.engines.spanner_graphrag.generate_grounded_text",
            return_value=("[ENTITY:ENT_OEM] [EDGE:EDG_1] [CHUNK:CHK_1]", TokenUsage(200, 40)),
        ),
    ):
        res = asyncio.run(active_engine.run_query(req, emitter=emitter2))

    assert res.engine_id == "spanner_graphrag"
    assert res.flags.graph_backend_used == "spanner_graph"
    assert res.flags.reranker_used is True
    assert res.provenance.is_fully_verified is True


def test_replay_blocks_path_traversal_and_missing_fixtures() -> None:
    emitter = RecordingStageEmitter()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(FileNotFoundError):
            asyncio.run(
                replay_recording(
                    root,
                    engine_id="bq_graphrag",
                    pack_id="supply_chain_subtier_risk",
                    synthesis_model="gemini-2.5-flash",
                    question_id="missing_q99",
                    emitter=emitter,
                )
            )
        with pytest.raises(ValueError, match="Unsafe recording path segment"):
            asyncio.run(
                replay_recording(
                    root,
                    engine_id="bq_graphrag",
                    pack_id="../etc",
                    synthesis_model="gemini-2.5-flash",
                    question_id="passwd",
                    emitter=emitter,
                )
            )
