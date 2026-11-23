"""Thin FastAPI HTTP & SSE routes delegating strictly to engines, ingestion, and security core."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.core.config import MODEL_CATALOG, Settings
from app.core.gcp_clients import run_blocking
from app.core.models import PACK_SLUG_RE, TraversalDirection
from app.core.pricing import estimate_monthly_tco
from app.core.security import PromptBlockedError, screen_with_model_armor
from app.core.stage_emitter import RecordingStageEmitter
from app.engines.adk_graph_agent import ADKGraphCoordinatorEngine
from app.engines.base import BaseGraphEngine, EngineResult, QueryRequest
from app.engines.bq_graphrag import BigQueryGraphRAGEngine
from app.engines.flat_vector_rag import FlatVectorRAGEngine
from app.engines.provenance_verifier import compute_multihop_recall
from app.engines.replay import replay_recording, save_recording
from app.engines.spanner_graphrag import SpannerGraphRAGEngine
from app.ingestion.bq_loader import persist_extracted_graph
from app.ingestion.dlp_guard import inspect_text_with_dlp
from app.ingestion.multimodal_extractor import (
    extract_document_chunks,
    extract_graph_from_upload,
    parse_custom_ontology,
)
from app.ingestion.upload_validator import UploadValidationError, validate_upload
from app.packs.loader import LoadedPack

router = APIRouter()

MAX_GRAPH_NODES_RESPONSE = 250
MAX_GRAPH_EDGES_RESPONSE = 500


# ---------------------------------------------------------------------------
# Strict Pydantic Request Contracts
# ---------------------------------------------------------------------------


class QueryStreamRequest(BaseModel):
    pack_id: str
    query: str = Field(min_length=3, max_length=1500)
    engine_id: Literal[
        "bq_graphrag",
        "flat_vector_rag",
        "adk_graph_coordinator",
        "spanner_graphrag",
    ] = "bq_graphrag"
    synthesis_model: str = "gemini-2.5-flash"
    question_id: str | None = Field(default=None, max_length=80)
    max_hops: int = Field(default=3, ge=1, le=4)
    direction: TraversalDirection = TraversalDirection.BOTH
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    top_k_seeds: int = Field(default=6, ge=1, le=15)
    enable_rerank: bool = True
    mode: Literal["replay", "live"] | None = None

    @field_validator("pack_id")
    @classmethod
    def _validate_pack(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id format: {v!r}")
        return v

    @field_validator("synthesis_model")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        if v not in MODEL_CATALOG:
            raise ValueError(f"Unsupported synthesis_model: {v!r}")
        return v


class CompareStreamRequest(BaseModel):
    pack_id: str
    query: str = Field(min_length=3, max_length=1500)
    graph_engine_id: Literal[
        "bq_graphrag",
        "adk_graph_coordinator",
        "spanner_graphrag",
    ] = "bq_graphrag"
    synthesis_model: str = "gemini-2.5-flash"
    question_id: str | None = Field(default=None, max_length=80)
    max_hops: int = Field(default=3, ge=1, le=4)
    direction: TraversalDirection = TraversalDirection.BOTH
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    top_k_seeds: int = Field(default=6, ge=1, le=15)
    enable_rerank: bool = True
    mode: Literal["replay", "live"] | None = None

    @field_validator("pack_id")
    @classmethod
    def _validate_pack(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id format: {v!r}")
        return v

    @field_validator("synthesis_model")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        if v not in MODEL_CATALOG:
            raise ValueError(f"Unsupported synthesis_model: {v!r}")
        return v


class TcoEstimateRequest(BaseModel):
    pack_id: str | None = None
    monthly_queries: int = Field(default=5000, ge=0, le=10_000_000)
    synthesis_model: str = "gemini-2.5-flash"
    manual_baseline_cost_usd: float | None = Field(default=None, gt=0.0, le=100_000.0)
    tier: Literal["tier1_bigquery_serverless", "tier2_spanner_enterprise"] = "tier1_bigquery_serverless"
    spanner_processing_units: int = Field(default=100, ge=100, le=10_000)

    @field_validator("synthesis_model")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        if v not in MODEL_CATALOG:
            raise ValueError(f"Unsupported synthesis_model: {v!r}")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _packs(request: Request) -> dict[str, LoadedPack]:
    return request.app.state.packs


def _require_pack(request: Request, pack_id: str) -> LoadedPack:
    if not PACK_SLUG_RE.match(pack_id):
        raise HTTPException(status_code=422, detail="Invalid pack_id format")
    pack = _packs(request).get(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Unknown pack: {pack_id}")
    return pack


def _serialize_engine_result(res: EngineResult, *, recall: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "engine_id": res.engine_id,
        "engine_label": res.engine_label,
        "pack_id": res.pack_id,
        "query": res.query,
        "synthesis_model": res.synthesis_model,
        "answer": res.answer,
        "paths": [p.model_dump(mode="json") for p in res.paths],
        "retrieved_entity_ids": list(res.retrieved_entity_ids),
        "retrieved_edge_ids": list(res.retrieved_edge_ids),
        "retrieved_chunk_ids": list(res.retrieved_chunk_ids),
        "retrieved_nodes": list(res.retrieved_nodes),
        "retrieved_edges": list(res.retrieved_edges),
        "retrieved_chunks": list(res.retrieved_chunks),
        "provenance": res.provenance.model_dump(mode="json"),
        "usage": asdict(res.usage),
        "engine_cost_usd": res.engine_cost_usd,
        "total_duration_ms": res.total_duration_ms,
        "flags": asdict(res.flags),
        "recall": recall,
    }


def _sse_frame(event_name: str, data: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, default=str)}\n\n"


def _build_engine(
    request: Request,
    engine_id: str,
) -> BaseGraphEngine:
    overrides: dict[str, BaseGraphEngine] = getattr(request.app.state, "engine_overrides", {})
    if engine_id in overrides:
        return overrides[engine_id]
    settings = _settings(request)
    packs = _packs(request)
    if engine_id == "flat_vector_rag":
        return FlatVectorRAGEngine(settings)
    if engine_id == "bq_graphrag":
        return BigQueryGraphRAGEngine(settings)
    if engine_id == "adk_graph_coordinator":
        return ADKGraphCoordinatorEngine(settings, packs=packs)
    if engine_id == "spanner_graphrag":
        return SpannerGraphRAGEngine(settings)
    raise HTTPException(status_code=422, detail=f"Unsupported engine_id: {engine_id}")


def _golden_recall(pack: LoadedPack, question_id: str | None, res: EngineResult) -> dict[str, float] | None:
    if not question_id:
        return None
    for q in pack.benchmark.questions:
        if q.question_id == question_id:
            return compute_multihop_recall(
                expected_entity_ids=q.expected_entity_ids,
                expected_edge_ids=q.expected_edge_ids,
                retrieved_entity_ids=frozenset(res.retrieved_entity_ids),
                retrieved_edge_ids=frozenset(res.retrieved_edge_ids),
            )
    return None


async def _execute_arm(
    *,
    request: Request,
    settings: Settings,
    engine_id: str,
    query_req: QueryRequest,
    mode: str,
    emitter: RecordingStageEmitter,
) -> EngineResult:
    if mode == "replay":
        if not query_req.question_id:
            raise ValueError("question_id is required in replay mode")
        return await replay_recording(
            settings.recordings_dir,
            engine_id=engine_id,
            pack_id=query_req.pack_id,
            synthesis_model=query_req.synthesis_model,
            question_id=query_req.question_id,
            emitter=emitter,
        )

    if not settings.gcp_project_id and engine_id not in getattr(request.app.state, "engine_overrides", {}):
        raise ValueError("GCP_PROJECT_ID must be configured for live execution")

    # Optional Model Armor screening on analyst prompt for non-ADK engines (ADK runs its own stage)
    ma_checked = False
    if engine_id != "adk_graph_coordinator" and settings.model_armor_template_id.strip():
        verdict = await run_blocking(
            screen_with_model_armor,
            settings=settings,
            text=query_req.query,
            mode="user_prompt",
        )
        ma_checked = verdict.checked

    engine = _build_engine(request, engine_id)
    result = await engine.run_query(query_req, emitter=emitter)
    if ma_checked and not result.flags.model_armor_checked:
        result = replace(result, flags=replace(result.flags, model_armor_checked=True))

    if settings.demo_mode == "record" and query_req.question_id:
        save_recording(
            settings.recordings_dir,
            result=result,
            stage_events=emitter.events,
            question_id=query_req.question_id,
        )
    return result


# ---------------------------------------------------------------------------
# Read-Only Health, Metadata, and Pack Endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Zero-dependency liveness probe (< 10 ms on 1-vCPU Cloud Run cold starts)."""
    s = _settings(request)
    return {
        "status": "ok",
        "app_version": s.app_version,
        "demo_mode": s.demo_mode,
        "graph_backend": s.graph_backend,
        "packs_loaded": len(_packs(request)),
    }


@router.get("/api/v1/meta")
async def metadata(request: Request) -> dict[str, Any]:
    s = _settings(request)
    return {
        "app_version": s.app_version,
        "demo_mode": s.demo_mode,
        "graph_backend": s.graph_backend,
        "default_synthesis_model": s.vertex_model_synthesis,
        "default_judge_model": s.vertex_model_judge,
        "embedding_model": s.vertex_embedding_model,
        "model_armor_configured": bool(s.model_armor_template_id.strip()),
        "dlp_enabled": s.enable_cloud_dlp,
        "iap_required": s.require_iap_jwt,
        "spanner_configured": bool(s.spanner_instance_id.strip()),
        "live_capable": bool(s.gcp_project_id.strip()),
        "models": [
            {
                "model_id": spec.model_id,
                "display_name": spec.display_name,
                "family": spec.family,
                "tier": spec.tier,
                "input_per_1m_usd": spec.input_per_1m_usd,
                "output_per_1m_usd": spec.output_per_1m_usd,
                "supports_adk": spec.supports_adk,
            }
            for spec in MODEL_CATALOG.values()
        ],
    }


@router.get("/api/v1/packs")
async def list_packs(request: Request) -> dict[str, Any]:
    packs_out = []
    for pack in _packs(request).values():
        m = pack.manifest
        packs_out.append(
            {
                "pack_id": m.pack_id,
                "title": m.title,
                "subtitle": m.subtitle,
                "industry": m.industry,
                "persona": m.persona,
                "manual_baseline_cost_usd": m.manual_baseline_cost_usd,
                "manual_baseline_minutes": m.manual_baseline_minutes,
                "default_max_hops": m.default_max_hops,
                "primary_sources": list(m.primary_sources),
                "real_public_overall_ratio": round(pack.real_public_overall_ratio, 4),
                "counts": {
                    "entities": len(pack.entities),
                    "edges": len(pack.edges),
                    "chunks": len(pack.chunks),
                    "questions": len(pack.benchmark.questions),
                },
                "ontology": m.ontology.model_dump(mode="json"),
                "questions": [q.model_dump(mode="json") for q in pack.benchmark.questions],
            }
        )
    return {"packs": packs_out}


@router.get("/api/v1/packs/{pack_id}/graph")
async def get_pack_graph(request: Request, pack_id: str) -> dict[str, Any]:
    pack = _require_pack(request, pack_id)
    nodes = [e.model_dump(mode="json") for e in pack.entities[:MAX_GRAPH_NODES_RESPONSE]]
    allowed_node_ids = {n["entity_id"] for n in nodes}
    edges = [
        ed.model_dump(mode="json")
        for ed in pack.edges
        if ed.source_entity_id in allowed_node_ids and ed.target_entity_id in allowed_node_ids
    ][:MAX_GRAPH_EDGES_RESPONSE]
    chunks = [c.model_dump(mode="json") for c in pack.chunks]
    return {
        "pack_id": pack.pack_id,
        "ontology": pack.manifest.ontology.model_dump(mode="json"),
        "nodes": nodes,
        "edges": edges,
        "chunks": chunks,
        "truncated": (
            len(pack.entities) > MAX_GRAPH_NODES_RESPONSE
            or len(pack.edges) > MAX_GRAPH_EDGES_RESPONSE
        ),
    }


# ---------------------------------------------------------------------------
# Streaming Query & Side-by-Side Comparison Endpoints (SSE)
# ---------------------------------------------------------------------------


@router.post("/api/v1/query/stream")
async def query_stream(request: Request, body: QueryStreamRequest) -> StreamingResponse:
    pack = _require_pack(request, body.pack_id)
    s = _settings(request)
    eff_mode = body.mode or ("replay" if s.demo_mode == "replay" else "live")
    if eff_mode == "live" and not s.gcp_project_id and body.engine_id not in getattr(request.app.state, "engine_overrides", {}):
        raise HTTPException(status_code=400, detail="Live mode requires GCP_PROJECT_ID to be configured.")
    if body.engine_id == "adk_graph_coordinator" and not MODEL_CATALOG[body.synthesis_model].supports_adk:
        raise HTTPException(status_code=422, detail="ADK Graph Coordinator requires a Gemini model.")

    query_req = QueryRequest(
        pack_id=body.pack_id,
        query=body.query,
        synthesis_model=body.synthesis_model,
        question_id=body.question_id,
        max_hops=body.max_hops,
        direction=body.direction,
        min_confidence=body.min_confidence,
        top_k_seeds=body.top_k_seeds,
        enable_rerank=body.enable_rerank,
    )

    async def event_gen() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        emitter = RecordingStageEmitter(sse_queue=queue)
        sentinel = object()
        result_box: dict[str, Any] = {}

        async def _runner() -> None:
            try:
                res = await _execute_arm(
                    request=request,
                    settings=s,
                    engine_id=body.engine_id,
                    query_req=query_req,
                    mode=eff_mode,
                    emitter=emitter,
                )
                result_box["result"] = res
            except PromptBlockedError as exc:
                result_box["error"] = {"error_type": "PromptBlockedError", "message": str(exc), "status": 400}
            except FileNotFoundError as exc:
                result_box["error"] = {"error_type": "RecordingNotFound", "message": str(exc), "status": 404}
            except ValueError as exc:
                result_box["error"] = {"error_type": "ValidationError", "message": str(exc), "status": 422}
            except Exception as exc:
                result_box["error"] = {
                    "error_type": type(exc).__name__,
                    "message": "Query execution failed; inspect server logs for diagnostics.",
                    "status": 500,
                }
            finally:
                await queue.put({"__done__": sentinel})

        task = asyncio.create_task(_runner())
        try:
            while True:
                item = await queue.get()
                if item.get("__done__") is sentinel:
                    break
                yield _sse_frame("stage", item)
            await task
            if "error" in result_box:
                yield _sse_frame("error", result_box["error"])
            else:
                res: EngineResult = result_box["result"]
                recall = _golden_recall(pack, body.question_id, res)
                yield _sse_frame("result", _serialize_engine_result(res, recall=recall))
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/compare/stream")
async def compare_stream(request: Request, body: CompareStreamRequest) -> StreamingResponse:
    """Side-by-side SSE comparison: Flat Vector RAG vs Hybrid GraphRAG on the exact same prompt."""
    pack = _require_pack(request, body.pack_id)
    s = _settings(request)
    eff_mode = body.mode or ("replay" if s.demo_mode == "replay" else "live")
    if eff_mode == "live" and not s.gcp_project_id and not getattr(request.app.state, "engine_overrides", {}):
        raise HTTPException(status_code=400, detail="Live mode requires GCP_PROJECT_ID to be configured.")

    query_req = QueryRequest(
        pack_id=body.pack_id,
        query=body.query,
        synthesis_model=body.synthesis_model,
        question_id=body.question_id,
        max_hops=body.max_hops,
        direction=body.direction,
        min_confidence=body.min_confidence,
        top_k_seeds=body.top_k_seeds,
        enable_rerank=body.enable_rerank,
    )

    async def event_gen() -> AsyncIterator[str]:
        arms = [("flat_vector_rag", "flat_vector_rag"), ("graphrag", body.graph_engine_id)]
        arm_results: dict[str, dict[str, Any]] = {}

        for arm_label, eng_id in arms:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            emitter = RecordingStageEmitter(sse_queue=queue)
            sentinel = object()
            box: dict[str, Any] = {}

            async def _run_one(eid: str = eng_id, em: RecordingStageEmitter = emitter, b: dict[str, Any] = box) -> None:
                try:
                    b["result"] = await _execute_arm(
                        request=request,
                        settings=s,
                        engine_id=eid,
                        query_req=query_req,
                        mode=eff_mode,
                        emitter=em,
                    )
                except PromptBlockedError as exc:
                    b["error"] = {"arm": arm_label, "error_type": "PromptBlockedError", "message": str(exc)}
                except FileNotFoundError as exc:
                    b["error"] = {"arm": arm_label, "error_type": "RecordingNotFound", "message": str(exc)}
                except Exception as exc:
                    b["error"] = {
                        "arm": arm_label,
                        "error_type": type(exc).__name__,
                        "message": "Comparison arm failed; inspect server logs for diagnostics.",
                    }
                finally:
                    await queue.put({"__done__": sentinel})

            task = asyncio.create_task(_run_one())
            try:
                while True:
                    item = await queue.get()
                    if item.get("__done__") is sentinel:
                        break
                    yield _sse_frame("stage", {"arm": arm_label, "engine_id": eng_id, **item})
                await task
                if "error" in box:
                    yield _sse_frame("error", box["error"])
                    return
                res: EngineResult = box["result"]
                recall = _golden_recall(pack, body.question_id, res)
                arm_results[arm_label] = _serialize_engine_result(res, recall=recall)
            finally:
                if not task.done():
                    task.cancel()

        flat_rec = (arm_results["flat_vector_rag"].get("recall") or {}).get("path_recall")
        graph_rec = (arm_results["graphrag"].get("recall") or {}).get("path_recall")
        yield _sse_frame(
            "comparison",
            {
                "pack_id": body.pack_id,
                "question_id": body.question_id,
                "flat_vector_rag": arm_results["flat_vector_rag"],
                "graphrag": arm_results["graphrag"],
                "path_recall_delta": (
                    round(float(graph_rec) - float(flat_rec), 4)
                    if flat_rec is not None and graph_rec is not None
                    else None
                ),
            },
        )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Multimodal BYO-Ontology Upload & Ingestion Endpoint
# ---------------------------------------------------------------------------


@router.post("/api/v1/ingest/upload")
async def ingest_upload(
    request: Request,
    pack_id: str = Form(...),
    file: UploadFile = File(...),
    custom_ontology_yaml: str = Form(default=""),
    persist_to_bigquery: bool = Form(default=False),
    extraction_model: str = Form(default=""),
) -> dict[str, Any]:
    """Validate uploaded document, preview DLP, extract ontology-guided graph, and optionally persist."""
    pack = _require_pack(request, pack_id)
    s = _settings(request)

    # Bounded streaming read so oversized uploads fail fast without exhausting RAM
    chunks_buf = bytearray()
    cap = s.max_upload_bytes
    while True:
        part = await file.read(65_536)
        if not part:
            break
        chunks_buf.extend(part)
        if len(chunks_buf) > cap:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds maximum allowed size of {cap} bytes.",
            )

    try:
        validated = validate_upload(
            pack_id=pack_id,
            filename=file.filename or "",
            content_type=file.content_type,
            content=bytes(chunks_buf),
            max_upload_bytes=cap,
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        ontology = parse_custom_ontology(custom_ontology_yaml, pack.manifest.ontology)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid custom ontology YAML: {exc}") from exc

    if extraction_model and extraction_model not in MODEL_CATALOG:
        raise HTTPException(status_code=422, detail=f"Unsupported extraction_model: {extraction_model!r}")

    doc_chunks, is_image = extract_document_chunks(validated)
    combined_text = "\n\n".join(c.content for c in doc_chunks)
    dlp_summary = await run_blocking(
        inspect_text_with_dlp,
        settings=s,
        text=combined_text,
        info_types=ontology.dlp_info_types,
    )

    extractor_override = getattr(request.app.state, "extractor_override", None)
    can_extract_live = bool(s.gcp_project_id.strip() and s.demo_mode != "replay") or extractor_override is not None

    if not can_extract_live:
        return {
            "status": "validated_preview",
            "extraction_mode": "replay_preview_only",
            "message": "File validated and chunked cleanly. Set DEMO_MODE=live and GCP_PROJECT_ID for Gemini ontology extraction.",
            "upload": {
                "pack_id": validated.pack_id,
                "display_name": validated.display_name,
                "canonical_mime": validated.canonical_mime,
                "gcs_object_key": validated.gcs_object_key,
                "size_bytes": validated.size_bytes,
                "sha256": validated.sha256,
                "is_image": is_image,
            },
            "dlp": dlp_summary.to_dict(),
            "chunks_count": len(doc_chunks),
            "entities": [],
            "edges": [],
            "persisted": None,
        }

    try:
        outcome = await run_blocking(
            extract_graph_from_upload,
            settings=s,
            upload=validated,
            ontology=ontology,
            existing_entities=pack.entities,
            extraction_model=extraction_model or s.vertex_model_synthesis,
            extractor_fn=extractor_override,
        )
    except PromptBlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    persisted: dict[str, Any] | None = None
    if persist_to_bigquery:
        persist_fn = getattr(request.app.state, "persist_override", None) or persist_extracted_graph
        persisted = await run_blocking(
            persist_fn,
            settings=s,
            upload=validated,
            outcome=outcome,
        )

    return {
        "status": "extracted",
        "extraction_mode": "live_gemini",
        "upload": {
            "pack_id": validated.pack_id,
            "display_name": validated.display_name,
            "canonical_mime": validated.canonical_mime,
            "gcs_object_key": validated.gcs_object_key,
            "size_bytes": validated.size_bytes,
            "sha256": validated.sha256,
            "is_image": is_image,
        },
        "dlp": dlp_summary.to_dict(),
        "model_armor_checked": outcome.model_armor_checked,
        "chunks_count": len(outcome.chunks),
        "merged_entities_count": outcome.merged_entities_count,
        "dropped_invalid_count": outcome.dropped_invalid_count,
        "extraction_cost_usd": outcome.extraction_cost_usd,
        "entities": [e.model_dump(mode="json") for e in outcome.entities],
        "edges": [ed.model_dump(mode="json") for ed in outcome.edges],
        "persisted": persisted,
    }


# ---------------------------------------------------------------------------
# Monthly GCP TCO & Customer ROI Estimator
# ---------------------------------------------------------------------------


@router.post("/api/v1/tco/estimate")
async def tco_estimate(request: Request, body: TcoEstimateRequest) -> dict[str, Any]:
    manual_cost = body.manual_baseline_cost_usd
    if manual_cost is None:
        if body.pack_id:
            pack = _require_pack(request, body.pack_id)
            manual_cost = pack.manifest.manual_baseline_cost_usd
        else:
            manual_cost = 4500.0

    est = estimate_monthly_tco(
        monthly_queries=body.monthly_queries,
        synthesis_model=body.synthesis_model,
        manual_baseline_cost_usd=manual_cost,
        tier=body.tier,
        spanner_processing_units=body.spanner_processing_units,
    )
    return asdict(est)
