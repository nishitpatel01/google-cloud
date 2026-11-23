"""Integration tests for FastAPI SSE endpoints, Google ADK 2.15 Coordinator, and /ingest pipeline."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.models import (
    DataOrigin,
    EntityNode,
    GraphEdge,
    GraphPath,
    ProvenanceReport,
    RiskTier,
    SourceChunk,
    SourceLicense,
    StageFailedEvent,
    TraversalDirection,
)
from app.core.pricing import TokenUsage
from app.core.stage_emitter import RecordingStageEmitter, StageEmitter
from app.engines.adk_graph_agent import ADKCoordinatorError, ADK_AGENT_NAME, ADKGraphCoordinatorEngine
from app.engines.base import BaseGraphEngine, EngineFlags, EngineResult, QueryRequest
from app.engines.replay import save_recording
from app.ingestion.bq_loader import persist_extracted_graph, upload_raw_to_gcs
from app.ingestion.dlp_guard import inspect_text_with_dlp
from app.ingestion.multimodal_extractor import ExtractionOutcome, parse_custom_ontology
from app.ingestion.upload_validator import validate_upload
from app.main import create_app
from app.packs.loader import load_all_packs


def _parse_sse(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    cur_event = ""
    cur_data = ""
    for line in raw_text.splitlines():
        if line.startswith("event: "):
            cur_event = line[7:].strip()
        elif line.startswith("data: "):
            cur_data = line[6:].strip()
        elif line == "" and cur_event and cur_data:
            events.append((cur_event, json.loads(cur_data)))
            cur_event, cur_data = "", ""
    return events


class _StubEngine(BaseGraphEngine):
    def __init__(self, engine_id: str, retrieved_entities: tuple[str, ...], retrieved_edges: tuple[str, ...]) -> None:
        self.engine_id = engine_id
        self.engine_label = f"Stub {engine_id}"
        self.stages = ("query_prep", "synthesize")
        self._ent = retrieved_entities
        self._edg = retrieved_edges

    async def run_query(self, req: QueryRequest, *, emitter: StageEmitter) -> EngineResult:
        h = await emitter.started("query_prep", label="Stub prep", service="stub")
        await emitter.completed(h, payload={"ok": True})
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            pack_id=req.pack_id,
            query=req.query,
            synthesis_model=req.synthesis_model,
            answer="Stub answer citing verified path.",
            paths=(),
            retrieved_entity_ids=self._ent,
            retrieved_edge_ids=self._edg,
            retrieved_chunk_ids=("CHK-1",),
            retrieved_nodes=(),
            retrieved_edges=(),
            retrieved_chunks=(),
            provenance=ProvenanceReport(
                cited_entity_ids=(),
                verified_entity_ids=(),
                unverified_entity_ids=(),
                cited_edge_ids=(),
                verified_edge_ids=(),
                unverified_edge_ids=(),
                cited_chunk_ids=(),
                verified_chunk_ids=(),
                unverified_chunk_ids=(),
            ),
            usage=TokenUsage(input_tokens=100, output_tokens=20),
            engine_cost_usd=0.0001,
            total_duration_ms=12.5,
            flags=EngineFlags(graph_backend_used="bigquery_property_graph" if self._edg else "none"),
        )


def test_read_endpoints_packs_graph_and_tco() -> None:
    app = create_app(settings=Settings(demo_mode="replay"))
    client = TestClient(app)

    meta = client.get("/api/v1/meta").json()
    assert meta["demo_mode"] == "replay"
    assert len(meta["models"]) >= 5
    assert any(m["model_id"] == "gemini-2.5-flash" and m["supports_adk"] is True for m in meta["models"])

    packs_resp = client.get("/api/v1/packs").json()
    assert len(packs_resp["packs"]) == 3
    for p in packs_resp["packs"]:
        assert p["real_public_overall_ratio"] >= 0.90
        assert p["counts"]["questions"] == 5

    g = client.get("/api/v1/packs/supply_chain_subtier_risk/graph").json()
    assert g["pack_id"] == "supply_chain_subtier_risk"
    assert len(g["nodes"]) == 34
    assert len(g["edges"]) == 95
    assert g["truncated"] is False

    assert client.get("/api/v1/packs/nonexistent_pack/graph").status_code == 404

    t1 = client.post(
        "/api/v1/tco/estimate",
        json={"pack_id": "supply_chain_subtier_risk", "monthly_queries": 1000, "tier": "tier1_bigquery_serverless"},
    ).json()
    t2 = client.post(
        "/api/v1/tco/estimate",
        json={"pack_id": "supply_chain_subtier_risk", "monthly_queries": 1000, "tier": "tier2_spanner_enterprise"},
    ).json()
    assert t1["total_gcp_monthly_usd"] < t2["total_gcp_monthly_usd"]
    assert t1["roi_multiple"] > 1.0


def test_sse_query_replay_and_compare_stream_endpoints() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rec_root = Path(tmp)
        settings = Settings(demo_mode="replay")
        # Save one genuine recording into temp recordings dir
        stub = _StubEngine("bq_graphrag", ("ISSUER-INTEL",), ("EDGE-1",))
        em = RecordingStageEmitter()
        res = asyncio.run(
            stub.run_query(
                QueryRequest(pack_id="supply_chain_subtier_risk", query="Trace Intel", synthesis_model="gemini-2.5-flash"),
                emitter=em,
            )
        )
        save_recording(rec_root, result=res, stage_events=em.events, question_id="sc_q01")

        app = create_app(settings=settings)
        client = TestClient(app)

        with patch.object(Settings, "recordings_dir", property(lambda _self: rec_root)):
            resp = client.post(
                "/api/v1/query/stream",
                json={
                    "pack_id": "supply_chain_subtier_risk",
                    "query": "Trace Intel tungsten risk",
                    "engine_id": "bq_graphrag",
                    "question_id": "sc_q01",
                    "mode": "replay",
                },
            )
            assert resp.status_code == 200
            frames = _parse_sse(resp.text)
            assert any(name == "stage" for name, _ in frames)
            result_frames = [data for name, data in frames if name == "result"]
            assert len(result_frames) == 1
            assert result_frames[0]["answer"] == "Stub answer citing verified path."

    # Side-by-side /compare/stream with injected live stub arms
    app2 = create_app(settings=Settings(demo_mode="live", gcp_project_id="demo-proj-123"))
    pack = app2.state.packs["supply_chain_subtier_risk"]
    q0 = pack.benchmark.questions[0]
    app2.state.engine_overrides = {
        "flat_vector_rag": _StubEngine("flat_vector_rag", (), ()),
        "bq_graphrag": _StubEngine("bq_graphrag", q0.expected_entity_ids, q0.expected_edge_ids),
    }
    client2 = TestClient(app2)
    cmp_resp = client2.post(
        "/api/v1/compare/stream",
        json={
            "pack_id": "supply_chain_subtier_risk",
            "query": q0.question,
            "question_id": q0.question_id,
            "graph_engine_id": "bq_graphrag",
            "mode": "live",
        },
    )
    assert cmp_resp.status_code == 200
    cmp_frames = _parse_sse(cmp_resp.text)
    comp = [d for n, d in cmp_frames if n == "comparison"]
    assert len(comp) == 1
    assert comp[0]["flat_vector_rag"]["recall"]["path_recall"] == 0.0
    assert comp[0]["graphrag"]["recall"]["path_recall"] == 1.0
    assert comp[0]["path_recall_delta"] == 1.0


def test_adk_graph_coordinator_rejects_non_gemini_and_runs_with_tools() -> None:
    settings = Settings(demo_mode="live", gcp_project_id="demo-proj-123")
    packs = load_all_packs(settings.packs_dir)

    # 1. Rejects Claude model cleanly with StageFailedEvent
    eng_bad = ADKGraphCoordinatorEngine(settings, packs=packs)
    em_bad = RecordingStageEmitter()
    with pytest.raises(ValueError, match="not supported by Google ADK"):
        asyncio.run(
            eng_bad.run_query(
                QueryRequest(
                    pack_id="supply_chain_subtier_risk",
                    query="Trace exposure",
                    synthesis_model="claude-sonnet-4-6",
                ),
                emitter=em_bad,
            )
        )
    assert any(isinstance(e, StageFailedEvent) for e in em_bad.events)

    # 2. Runs full ADK 2.15 coordinator invoking real tool callables via fake runner
    class _FakeSessionService:
        async def create_session(self, **_kwargs: Any) -> Any:
            return type("S", (), {"id": "sess-1"})()

    class _FakeEvent:
        def __init__(self, text: str) -> None:
            self.partial = False
            self.author = ADK_AGENT_NAME
            part = type("P", (), {"text": text, "thought": False})()
            self.content = type("C", (), {"parts": [part]})()
            self.usage_metadata = type("U", (), {"prompt_token_count": 650, "candidates_token_count": 140})()

        def get_function_calls(self) -> list[Any]:
            return []

        def is_final_response(self) -> bool:
            return True

    class _FakeRunner:
        def __init__(self, *, agent: Any, app_name: str) -> None:
            self.agent = agent
            self.app_name = app_name
            self.session_service = _FakeSessionService()

        async def run_async(self, **_kwargs: Any) -> Any:
            # Execute real registered plain-function ADK tools inside runner!
            tool_map = {fn.__name__: fn for fn in self.agent.tools}
            onto = tool_map["inspect_pack_ontology"]()
            assert onto["pack_id"] == "supply_chain_subtier_risk"
            g_res = tool_map["traverse_knowledge_graph"](
                search_query="Intel tungsten China", direction="outgoing", max_hops=3
            )
            assert g_res["paths_found"] == 1
            draft = "Verified [ENTITY:ENT_A] via [EDGE:EDG_1] to [ENTITY:ENT_B] [CHUNK:CHK_1]."
            ver = tool_map["verify_draft_citations"](draft_answer=draft)
            assert ver["is_fully_verified"] is True
            yield _FakeEvent(draft)

    class _FakeBQJob:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows
            self.total_bytes_processed = 1024
            self.cache_hit = False

        def result(self) -> list[dict[str, Any]]:
            return self._rows

    fake_bq = MagicMock()
    fake_bq.query.side_effect = [
        _FakeBQJob([]),  # seed dry-run
        _FakeBQJob([{"entity_id": "ENT_A", "canonical_name": "Intel Corporation"}]),
        _FakeBQJob(
            [
                {
                    "hop_count": 1,
                    "path_exposure_usd": 500_000.0,
                    "min_confidence": 0.95,
                    "path_nodes_json": json.dumps(
                        [
                            {"properties": {"entity_id": "ENT_A", "canonical_name": "Intel Corporation"}},
                            {"properties": {"entity_id": "ENT_B", "canonical_name": "Tungsten Smelter"}},
                        ]
                    ),
                    "path_edges_json": json.dumps(
                        [
                            {
                                "properties": {
                                    "edge_id": "EDG_1",
                                    "source_entity_id": "ENT_A",
                                    "target_entity_id": "ENT_B",
                                    "relation_type": "DISCLOSES_SMELTER",
                                    "source_chunk_id": "CHK_1",
                                    "evidence_excerpt": "SEC Form SD filing cites Tungsten Smelter.",
                                    "data_origin": "REAL_PUBLIC",
                                }
                            }
                        ]
                    ),
                }
            ]
        ),
        _FakeBQJob(
            [
                {
                    "chunk_id": "CHK_1",
                    "doc_name": "intel_form_sd.htm",
                    "data_origin": "REAL_PUBLIC",
                    "content": "SEC Form SD filing cites Tungsten Smelter.",
                }
            ]
        ),
    ]

    eng = ADKGraphCoordinatorEngine(settings, packs=packs, runner_factory=_FakeRunner)
    em = RecordingStageEmitter()
    with (
        patch("app.engines.adk_graph_agent.embed_single_text", return_value=[0.1, 0.2]),
        patch("app.engines.adk_graph_agent.get_bigquery_client", return_value=fake_bq),
        patch("app.engines.adk_graph_agent.rerank_records", return_value=[("path-001", 0.92)]),
    ):
        res = asyncio.run(
            eng.run_query(
                QueryRequest(
                    pack_id="supply_chain_subtier_risk",
                    query="Trace Intel tungsten exposure",
                    synthesis_model="gemini-2.5-flash",
                ),
                emitter=em,
            )
        )

    assert res.engine_id == "adk_graph_coordinator"
    assert res.provenance.is_fully_verified is True
    assert res.flags.graph_backend_used == "bigquery_property_graph"
    assert res.flags.reranker_used is True
    assert res.engine_cost_usd > 0.0
    assert len(res.paths) == 1

    # 3. Fail-loud when runner yields no final response
    class _EmptyRunner(_FakeRunner):
        async def run_async(self, **_kwargs: Any) -> Any:
            if False:
                yield None

    eng_empty = ADKGraphCoordinatorEngine(settings, packs=packs, runner_factory=_EmptyRunner)
    em_empty = RecordingStageEmitter()
    with pytest.raises(ADKCoordinatorError, match="without a final response"):
        asyncio.run(
            eng_empty.run_query(
                QueryRequest(
                    pack_id="supply_chain_subtier_risk",
                    query="Trace exposure",
                    synthesis_model="gemini-2.5-flash",
                ),
                emitter=em_empty,
            )
        )
    assert any(isinstance(e, StageFailedEvent) and e.stage == "adk_coordinate" for e in em_empty.events)


def test_multimodal_ingest_pdf_custom_ontology_dlp_and_bq_loader() -> None:
    settings = Settings(demo_mode="live", gcp_project_id="demo-proj-123", gcs_bucket="demo-bucket")
    app = create_app(settings=settings)
    pack = app.state.packs["supply_chain_subtier_risk"]

    # Create a real in-memory PDF via PyMuPDF
    pdf_doc = pymupdf.open()
    page = pdf_doc.new_page()
    page.insert_text((72, 72), "Intel Corp. disclosed new sourcing link to Nordic Cobalt Refinery in Finland.")
    pdf_bytes = pdf_doc.tobytes()
    pdf_doc.close()

    def _fake_extractor(**_kwargs: Any) -> tuple[dict[str, Any], TokenUsage]:
        return (
            {
                "entities": [
                    {
                        "canonical_name": "Intel Corp.",  # must merge with existing Intel Corporation!
                        "entity_type": "Issuer",
                        "jurisdiction": "United States",
                        "risk_tier": "MEDIUM",
                    },
                    {
                        "canonical_name": "Nordic Cobalt Refinery",
                        "entity_type": "SmelterOrSupplier",
                        "jurisdiction": "Finland",
                        "risk_tier": "HIGH",
                    },
                    {
                        "canonical_name": "Bogus Hallucinated Type",
                        "entity_type": "UnknownFakeNodeType",  # must be dropped cleanly!
                    },
                ],
                "edges": [
                    {
                        "source_name": "Intel Corp.",
                        "target_name": "Nordic Cobalt Refinery",
                        "relation_type": "SOURCED_FROM_SMELTER",
                        "confidence": 0.91,
                        "evidence_excerpt": "Intel Corp. disclosed new sourcing link to Nordic Cobalt Refinery.",
                    }
                ],
            },
            TokenUsage(input_tokens=420, output_tokens=180),
        )

    app.state.extractor_override = _fake_extractor
    app.state.persist_override = lambda **_kw: {
        "ingest_run_id": "upl-test123",
        "gcs_uri": "gs://demo-bucket/uploads/test.pdf",
        "persisted_entities": 2,
        "persisted_edges": 1,
        "persisted_chunks": 1,
    }

    client = TestClient(app)
    resp = client.post(
        "/api/v1/ingest/upload",
        data={"pack_id": "supply_chain_subtier_risk", "persist_to_bigquery": "true"},
        files={"file": ("supplier_update.pdf", pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "extracted"
    assert body["dropped_invalid_count"] == 1
    assert body["merged_entities_count"] >= 1
    assert len(body["edges"]) == 1
    assert body["edges"][0]["source_entity_id"] == "ISSUER-INTEL"  # resolved canonically!
    assert body["edges"][0]["data_origin"] == "CUSTOMER_UPLOAD"
    assert body["persisted"]["ingest_run_id"] == "upl-test123"

    # Custom ontology YAML parser rejects non-object YAML
    with pytest.raises(ValueError):
        parse_custom_ontology("- just a list", pack.manifest.ontology)

    # Cloud DLP guard: enabled=False honest summary + mocked enabled=True with include_quote=False
    dlp_off = inspect_text_with_dlp(settings=settings, text="Contact alice@example.com")
    assert dlp_off.enabled is False
    assert dlp_off.findings_count == 0

    fake_dlp = MagicMock()
    finding = type("F", (), {"info_type": type("IT", (), {"name": "EMAIL_ADDRESS"})()})()
    fake_dlp.inspect_content.return_value = type("R", (), {"result": type("Res", (), {"findings": [finding]})()})()
    s_dlp = Settings(demo_mode="live", gcp_project_id="demo-proj-123", enable_cloud_dlp=True)
    dlp_on = inspect_text_with_dlp(settings=s_dlp, text="Contact alice@example.com", dlp_client=fake_dlp)
    assert dlp_on.enabled is True
    assert dlp_on.findings_count == 1
    assert dlp_on.info_types_found == ("EMAIL_ADDRESS",)
    assert fake_dlp.inspect_content.call_args.kwargs["request"]["inspect_config"]["include_quote"] is False

    # GCS attachment disposition + BigQuery staging MERGE & cleanup
    validated = validate_upload(
        pack_id="supply_chain_subtier_risk",
        filename="memo.txt",
        content_type="text/plain",
        content=b"Intel Corp. sources tungsten from Nordic Refinery.",
    )
    fake_storage = MagicMock()
    fake_blob = MagicMock()
    fake_storage.bucket.return_value.blob.return_value = fake_blob
    gcs_uri = upload_raw_to_gcs(settings=settings, upload=validated, storage_client=fake_storage)
    assert gcs_uri.startswith("gs://demo-bucket/uploads/supply_chain_subtier_risk/")
    assert fake_blob.content_disposition.startswith("attachment;")

    outcome = ExtractionOutcome(
        chunks=(
            SourceChunk(
                pack_id="supply_chain_subtier_risk",
                chunk_id="UPL-CHK-1",
                doc_name="memo.txt",
                doc_type="uploaded_txt",
                page_number=1,
                content="Intel Corp. sources tungsten from Nordic Refinery.",
                data_origin=DataOrigin.CUSTOMER_UPLOAD,
                source_license=SourceLicense.CUSTOMER_PRIVATE,
            ),
        ),
        entities=(
            EntityNode(
                pack_id="supply_chain_subtier_risk",
                entity_id="UPL-ENT-1",
                canonical_name="Nordic Refinery",
                entity_type="Smelter",
                data_origin=DataOrigin.CUSTOMER_UPLOAD,
                source_license=SourceLicense.CUSTOMER_PRIVATE,
            ),
        ),
        edges=(
            GraphEdge(
                pack_id="supply_chain_subtier_risk",
                edge_id="UPL-EDG-1",
                source_entity_id="ISSUER-INTEL",
                target_entity_id="UPL-ENT-1",
                relation_type="DISCLOSES_SMELTER",
                evidence_excerpt="Intel Corp. sources tungsten from Nordic Refinery.",
                source_chunk_id="UPL-CHK-1",
                source_doc_name="memo.txt",
                data_origin=DataOrigin.CUSTOMER_UPLOAD,
                source_license=SourceLicense.CUSTOMER_PRIVATE,
            ),
        ),
        merged_entities_count=0,
        dropped_invalid_count=0,
        model_armor_checked=False,
        usage=TokenUsage(100, 40),
        extraction_cost_usd=0.0001,
    )
    fake_bq = MagicMock()
    with patch("app.ingestion.bq_loader.embed_texts_threaded", return_value=[[0.1, 0.2]]):
        persisted = persist_extracted_graph(
            settings=settings,
            upload=validated,
            outcome=outcome,
            bq_client=fake_bq,
            storage_client=fake_storage,
        )
    assert persisted["persisted_entities"] == 1
    assert persisted["persisted_edges"] == 1
    assert persisted["persisted_chunks"] == 1
    assert fake_bq.delete_table.call_count == 3  # all 3 staging tables cleaned up!
