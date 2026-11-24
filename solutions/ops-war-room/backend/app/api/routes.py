"""FastAPI HTTP & SSE endpoints for ACC-04 Operations & Incident War Room."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.core.config import MODEL_CATALOG, Settings, validate_model_pair
from app.core.gcp_clients import GCPClientPool, run_blocking
from app.core.models import (
    INCIDENT_ID_RE,
    PACK_SLUG_RE,
    IncidentRecord,
    IncidentVerdict,
    PackConfig,
    RemediationOption,
)
from app.core.pricing import estimate_monthly_tco
from app.core.security import PromptBlockedError, screen_with_model_armor
from app.core.stage_emitter import StageEmitter
from app.engines.adk_war_room import ADKParallelWarRoomEngine
from app.engines.base import EngineId, TriageRequest, TriageResult
from app.engines.mcp_registry import MCPRegistry, SSRFValidationError, validate_external_mcp_url
from app.engines.replay import redact_gcp_identifiers, replay_recording
from app.engines.safe_bq_runner import SafeBigQueryRunner
from app.engines.serial_baseline import SingleAgentSerialEngine
from app.engines.workflows_executor import WorkflowsSagaExecutor
from app.evaluation.judge import evaluate_triage_result
from app.evaluation.leaderboard import summarize_recordings
from app.ingestion.log_ingestor import process_log_upload
from app.ingestion.upload_validator import UploadValidationError, validate_log_upload

router = APIRouter()


# ---------------------------------------------------------------------------
# Strict Pydantic Request Contracts
# ---------------------------------------------------------------------------


class InvestigateStreamRequest(BaseModel):
    pack_id: str
    incident_id: str
    engine_id: EngineId = "adk_parallel_war_room"
    synthesis_model: str = "gemini-2.5-flash"
    judge_model: str | None = "vertex-genai-eval"
    mode: Literal["replay", "live"] | None = None

    @field_validator("pack_id")
    @classmethod
    def _val_pack(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id: {v!r}")
        return v

    @field_validator("incident_id")
    @classmethod
    def _val_inc(cls, v: str) -> str:
        if not INCIDENT_ID_RE.match(v):
            raise ValueError(f"Invalid incident_id: {v!r}")
        return v

    @field_validator("synthesis_model")
    @classmethod
    def _val_synth(cls, v: str) -> str:
        spec = MODEL_CATALOG.get(v)
        if spec is None or not spec.supports_synthesis:
            raise ValueError(f"Unsupported synthesis_model: {v!r}")
        return v

    @field_validator("judge_model")
    @classmethod
    def _val_judge(cls, v: str | None) -> str | None:
        if not v:
            return None
        spec = MODEL_CATALOG.get(v)
        if spec is None or not spec.supports_judge:
            raise ValueError(f"Unsupported judge_model: {v!r}")
        return v


class CompareStreamRequest(BaseModel):
    pack_id: str
    incident_id: str
    synthesis_model: str = "gemini-2.5-flash"
    judge_model: str | None = "vertex-genai-eval"
    mode: Literal["replay", "live"] | None = None

    @field_validator("pack_id")
    @classmethod
    def _val_pack(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id: {v!r}")
        return v

    @field_validator("incident_id")
    @classmethod
    def _val_inc(cls, v: str) -> str:
        if not INCIDENT_ID_RE.match(v):
            raise ValueError(f"Invalid incident_id: {v!r}")
        return v


class RemediatePreviewRequest(BaseModel):
    pack_id: str
    incident_id: str
    option_id: str = Field(min_length=2, max_length=80)
    verdict: IncidentVerdict


class RemediateApproveRequest(BaseModel):
    pack_id: str
    incident_id: str
    option_id: str = Field(min_length=2, max_length=80)
    verdict: IncidentVerdict
    idempotency_key: str = Field(min_length=4, max_length=120)
    approver_identity: str = Field(default="war-room-lead", min_length=2, max_length=120)
    mode: Literal["replay", "live"] | None = None


class RemediateExecuteRequest(BaseModel):
    pack_id: str
    incident_id: str
    option_id: str = Field(min_length=2, max_length=80)
    approval_token: str = Field(min_length=6, max_length=200)
    idempotency_key: str = Field(min_length=4, max_length=120)
    dry_run: bool = True
    simulate_step2_failure: bool = False
    mode: Literal["replay", "live"] | None = None


class ValidateMCPUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=512)


class TCOEstimateRequest(BaseModel):
    pack_id: str
    monthly_alerts: int = Field(default=2000, ge=100, le=500_000)
    synthesis_model: str = "gemini-2.5-flash"
    enable_tier2_connectors: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(request: Request) -> tuple[Settings, dict[str, PackConfig], GCPClientPool | None, MCPRegistry, WorkflowsSagaExecutor]:
    st = request.app.state
    return st.settings, st.packs, st.gcp_clients, st.mcp_registry, st.workflows_executor


def _get_pack_and_incident(
    packs: dict[str, PackConfig],
    pack_id: str,
    incident_id: str,
) -> tuple[PackConfig, IncidentRecord]:
    pack = packs.get(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Unknown pack_id: {pack_id!r}")
    incident = next((i for i in pack.incidents if i.incident_id == incident_id), None)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Unknown incident_id: {incident_id!r}")
    return pack, incident


def _get_option(incident: IncidentRecord, option_id: str) -> RemediationOption:
    opt = next((o for o in incident.remediation_options if o.option_id == option_id), None)
    if opt is None:
        raise HTTPException(status_code=404, detail=f"Unknown option_id: {option_id!r}")
    return opt


def _sse_frame(event_name: str, data: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _effective_mode(requested: Literal["replay", "live"] | None, settings: Settings) -> Literal["replay", "live"]:
    chosen = requested or settings.demo_mode
    if chosen == "live" and not settings.live_capable:
        raise HTTPException(
            status_code=400,
            detail="Live Cloud mode requested but GCP_PROJECT_ID is not configured on this server.",
        )
    return chosen


async def _run_single_investigation(
    *,
    settings: Settings,
    gcp_clients: GCPClientPool | None,
    mcp_registry: MCPRegistry,
    pack: PackConfig,
    incident: IncidentRecord,
    engine_id: EngineId,
    synthesis_model: str,
    judge_model: str | None,
    mode: Literal["replay", "live"],
    emitter: StageEmitter,
) -> tuple[TriageResult, dict[str, Any] | None]:
    if mode == "replay":
        return await replay_recording(
            settings.recordings_dir,
            engine_id=engine_id,
            pack_id=pack.pack_id,
            synthesis_model=synthesis_model,
            incident_id=incident.incident_id,
            emitter=emitter,
            step_delay_s=0.04,
        )

    # Live mode: optional Model Armor input check on incident summary
    if settings.model_armor_configured:
        h_ma = await emitter.start(
            "model_armor_screen",
            "Screen Operational Context via Model Armor",
            service="Google Cloud Model Armor",
            agent_node="coordinator",
        )
        try:
            verdict = await run_blocking(
                screen_with_model_armor,
                settings=settings,
                text=f"{incident.title}\n{incident.summary}",
                mode="user_prompt",
                raise_on_block=False,
            )
            await emitter.complete(
                h_ma,
                records_count=1,
                detail=f"Model Armor ({verdict.template_id}): {verdict.match_state}",
            )
        except Exception as exc:
            await emitter.fail(h_ma, error=exc)
            raise

    bq_runner = SafeBigQueryRunner(settings, gcp_clients)
    if engine_id == "single_agent_serial":
        engine = SingleAgentSerialEngine(settings, bq_runner=bq_runner, gcp_clients=gcp_clients)
    else:
        engine = ADKParallelWarRoomEngine(settings, bq_runner=bq_runner, mcp_registry=mcp_registry)

    req = TriageRequest(
        pack_id=pack.pack_id,
        incident_id=incident.incident_id,
        engine_id=engine_id,
        synthesis_model=synthesis_model,
        judge_model=judge_model,
        mode="live",
        use_live_bq=True,
    )
    result = await engine.investigate(req, pack=pack, incident=incident, emitter=emitter)

    eval_dict: dict[str, Any] | None = None
    if judge_model:
        h_eval = await emitter.start(
            "independent_eval",
            f"Independent Evaluation ({judge_model})",
            service=f"Independent Judge ({judge_model})",
            agent_node="review_and_critique_agent",
        )
        try:
            ev = await run_blocking(
                evaluate_triage_result,
                settings=settings,
                gcp_clients=gcp_clients,
                result=result,
                incident=incident,
                judge_model=judge_model,
            )
            eval_dict = ev.to_dict()
            await emitter.complete(
                h_eval,
                records_count=3,
                detail=f"Composite={int(ev.composite_score * 100)}% (Judge ${ev.judge_cost_usd:.4f})",
            )
        except Exception as exc:
            await emitter.fail(h_eval, error=exc)
            raise

    return result, eval_dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings, packs, _, _, _ = _ctx(request)
    return {
        "status": "ok",
        "solution_id": "acc04",
        "version": settings.app_version,
        "mode": settings.demo_mode,
        "live_capable": settings.live_capable,
        "packs_loaded": len(packs),
    }


@router.get("/api/v1/meta")
async def get_meta(request: Request) -> dict[str, Any]:
    settings, _, _, _, _ = _ctx(request)
    return {
        "solution_id": "acc04",
        "solution_name": "Operations & Incident War Room",
        "version": settings.app_version,
        "default_mode": settings.demo_mode,
        "live_capable": settings.live_capable,
        "model_armor_configured": settings.model_armor_configured,
        "engines": [
            {
                "engine_id": "adk_parallel_war_room",
                "display_name": "Parallel Multi-Agent War Room (ADK 2.15 · Recommended)",
                "description": "3 concurrent ADK specialist agents + adversarial log quarantine + Review & Critique verifier",
            },
            {
                "engine_id": "single_agent_serial",
                "display_name": "Single-Agent Serial Triage (Baseline)",
                "description": "Traditional sequential generalist prompt without parallel fan-out or quarantine",
            },
        ],
        "models": [asdict(m) for m in MODEL_CATALOG.values()],
    }


@router.get("/api/v1/packs")
async def list_packs(request: Request) -> dict[str, Any]:
    _, packs, _, _, _ = _ctx(request)
    items = []
    for cfg in packs.values():
        items.append(
            {
                "pack_id": cfg.pack_id,
                "title": cfg.title,
                "subtitle": cfg.subtitle,
                "industry": cfg.industry,
                "persona": cfg.persona,
                "manual_baseline_cost_usd": cfg.manual_baseline_cost_usd,
                "manual_baseline_minutes": cfg.manual_baseline_minutes,
                "default_workflow_name": cfg.default_workflow_name,
                "margin_floor_ratio": cfg.margin_floor_ratio,
                "primary_sources": list(cfg.primary_sources),
                "real_public_share_pct": cfg.real_public_share_pct,
                "counts": {
                    "incidents": len(cfg.incidents),
                    "telemetry_events": len(cfg.telemetry_events),
                    "intel_records": len(cfg.intel_records),
                    "runbooks": len(cfg.runbooks),
                },
                "specialists": [asdict(s) for s in cfg.specialists],
                "incidents": [
                    {
                        "incident_id": i.incident_id,
                        "title": i.title,
                        "summary": i.summary,
                        "severity": i.severity.value,
                        "sla_minutes_remaining": i.sla_minutes_remaining,
                        "expected_verdict": i.expected_verdict.value,
                        "has_prompt_injection": i.has_prompt_injection,
                        "financial_exposure_usd": i.financial_exposure_usd,
                        "expected_action_type": i.expected_action_type,
                    }
                    for i in cfg.incidents
                ],
            }
        )
    return {"packs": items}


@router.get("/api/v1/packs/{pack_id}/incidents/{incident_id}")
async def get_incident_detail(request: Request, pack_id: str, incident_id: str) -> dict[str, Any]:
    _, packs, _, mcp_registry, _ = _ctx(request)
    pack, inc = _get_pack_and_incident(packs, pack_id, incident_id)
    tel = tuple(ev for ev in pack.telemetry_events if ev.incident_id == incident_id)
    matched_intel = mcp_registry.match_intel_for_incident(
        incident=inc, telemetry=tel, intel_catalog=pack.intel_records
    )
    return {
        "incident": {
            "incident_id": inc.incident_id,
            "pack_id": inc.pack_id,
            "title": inc.title,
            "summary": inc.summary,
            "severity": inc.severity.value,
            "sla_minutes_remaining": inc.sla_minutes_remaining,
            "expected_verdict": inc.expected_verdict.value,
            "expected_root_cause": inc.expected_root_cause,
            "has_prompt_injection": inc.has_prompt_injection,
            "financial_exposure_usd": inc.financial_exposure_usd,
            "remediation_options": [
                {
                    **asdict(o),
                    "plan_sha256": o.plan_sha256,
                    "net_value_usd": o.net_value_usd,
                }
                for o in inc.remediation_options
            ],
        },
        "telemetry_events": [asdict(ev) for ev in tel],
        "matched_intel": matched_intel,
        "runbooks": [asdict(rb) for rb in pack.runbooks],
    }


@router.get("/api/v1/mcp/registry")
async def get_mcp_registry(request: Request) -> dict[str, Any]:
    _, _, _, mcp_registry, _ = _ctx(request)
    return {
        "allowed_external_domains": sorted(mcp_registry.allowed_external_domains),
        "adapters": [asdict(a) for a in mcp_registry.adapters],
    }


@router.post("/api/v1/mcp/validate-url")
async def validate_mcp_url(request: Request, body: ValidateMCPUrlRequest) -> dict[str, Any]:
    _, _, _, mcp_registry, _ = _ctx(request)
    try:
        validated = validate_external_mcp_url(
            body.url,
            allowed_domains=mcp_registry.allowed_external_domains,
        )
        return {"valid": True, "url": validated, "reason": "HTTPS & domain allowlist verified."}
    except SSRFValidationError as exc:
        return {"valid": False, "url": body.url, "reason": str(exc)}


@router.post("/api/v1/investigate/stream")
async def investigate_stream(request: Request, body: InvestigateStreamRequest) -> StreamingResponse:
    settings, packs, gcp_clients, mcp_registry, _ = _ctx(request)
    try:
        validate_model_pair(body.synthesis_model, body.judge_model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    pack, incident = _get_pack_and_incident(packs, body.pack_id, body.incident_id)
    mode = _effective_mode(body.mode, settings)

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    emitter = StageEmitter(queue, arm=body.engine_id)

    async def _worker() -> None:
        try:
            result, evaluation = await _run_single_investigation(
                settings=settings,
                gcp_clients=gcp_clients,
                mcp_registry=mcp_registry,
                pack=pack,
                incident=incident,
                engine_id=body.engine_id,
                synthesis_model=body.synthesis_model,
                judge_model=body.judge_model,
                mode=mode,
                emitter=emitter,
            )
            await queue.put(
                {
                    "type": "result",
                    "data": {"result": result.to_dict(), "evaluation": evaluation},
                }
            )
        except PromptBlockedError as exc:
            await queue.put({"type": "error", "data": {"code": 400, "message": str(exc)}})
        except FileNotFoundError as exc:
            await queue.put({"type": "error", "data": {"code": 404, "message": str(exc)}})
        except Exception as exc:
            safe_msg = redact_gcp_identifiers(str(exc), project_id=settings.gcp_project_id)
            await queue.put({"type": "error", "data": {"code": 500, "message": safe_msg}})
        finally:
            await queue.put({"type": "done", "data": {}})

    async def _gen() -> AsyncIterator[str]:
        task = asyncio.create_task(_worker())
        try:
            while True:
                msg = await queue.get()
                if msg["type"] == "done":
                    yield _sse_frame("done", {})
                    break
                yield _sse_frame(msg["type"], msg["data"])
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/api/v1/compare/stream")
async def compare_stream(request: Request, body: CompareStreamRequest) -> StreamingResponse:
    settings, packs, gcp_clients, mcp_registry, _ = _ctx(request)
    try:
        validate_model_pair(body.synthesis_model, body.judge_model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    pack, incident = _get_pack_and_incident(packs, body.pack_id, body.incident_id)
    mode = _effective_mode(body.mode, settings)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _worker() -> None:
        try:
            em_serial = StageEmitter(queue, arm="single_agent_serial")
            em_parallel = StageEmitter(queue, arm="adk_parallel_war_room")

            serial_coro = _run_single_investigation(
                settings=settings,
                gcp_clients=gcp_clients,
                mcp_registry=mcp_registry,
                pack=pack,
                incident=incident,
                engine_id="single_agent_serial",
                synthesis_model=body.synthesis_model,
                judge_model=body.judge_model,
                mode=mode,
                emitter=em_serial,
            )
            parallel_coro = _run_single_investigation(
                settings=settings,
                gcp_clients=gcp_clients,
                mcp_registry=mcp_registry,
                pack=pack,
                incident=incident,
                engine_id="adk_parallel_war_room",
                synthesis_model=body.synthesis_model,
                judge_model=body.judge_model,
                mode=mode,
                emitter=em_parallel,
            )
            (ser_res, ser_eval), (par_res, par_eval) = await asyncio.gather(
                serial_coro, parallel_coro
            )
            await queue.put(
                {
                    "type": "result",
                    "data": {
                        "serial_baseline": {"result": ser_res.to_dict(), "evaluation": ser_eval},
                        "parallel_war_room": {"result": par_res.to_dict(), "evaluation": par_eval},
                        "delta": {
                            "recall_delta": round(
                                par_res.provenance.root_cause_recall
                                - ser_res.provenance.root_cause_recall,
                                4,
                            ),
                            "composite_delta": round(
                                float((par_eval or {}).get("composite_score", 0.0))
                                - float((ser_eval or {}).get("composite_score", 0.0)),
                                4,
                            ),
                            "latency_delta_ms": par_res.latency_ms - ser_res.latency_ms,
                            "parallel_injection_resisted": par_res.provenance.injection_resisted,
                            "serial_injection_resisted": ser_res.provenance.injection_resisted,
                        },
                    },
                }
            )
        except Exception as exc:
            safe_msg = redact_gcp_identifiers(str(exc), project_id=settings.gcp_project_id)
            await queue.put({"type": "error", "data": {"code": 500, "message": safe_msg}})
        finally:
            await queue.put({"type": "done", "data": {}})

    async def _gen() -> AsyncIterator[str]:
        task = asyncio.create_task(_worker())
        try:
            while True:
                msg = await queue.get()
                if msg["type"] == "done":
                    yield _sse_frame("done", {})
                    break
                yield _sse_frame(msg["type"], msg["data"])
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/api/v1/remediate/preview")
async def remediate_preview(request: Request, body: RemediatePreviewRequest) -> dict[str, Any]:
    _, packs, _, _, wf_exec = _ctx(request)
    pack, inc = _get_pack_and_incident(packs, body.pack_id, body.incident_id)
    opt = _get_option(inc, body.option_id)
    return wf_exec.preview_dry_run(pack=pack, incident=inc, option=opt, verdict=body.verdict)


@router.post("/api/v1/remediate/approve")
async def remediate_approve(request: Request, body: RemediateApproveRequest) -> dict[str, Any]:
    settings, packs, _, _, wf_exec = _ctx(request)
    pack, inc = _get_pack_and_incident(packs, body.pack_id, body.incident_id)
    opt = _get_option(inc, body.option_id)
    mode = _effective_mode(body.mode, settings)
    approver = getattr(request.state, "user_email", body.approver_identity)
    try:
        rec = await run_blocking(
            wf_exec.record_lead_approval,
            pack=pack,
            incident=inc,
            option=opt,
            verdict=body.verdict,
            approver_identity=approver,
            idempotency_key=body.idempotency_key,
            persist_firestore=(mode == "live"),
        )
        return asdict(rec)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/v1/remediate/execute")
async def remediate_execute(request: Request, body: RemediateExecuteRequest) -> dict[str, Any]:
    settings, packs, _, _, wf_exec = _ctx(request)
    pack, inc = _get_pack_and_incident(packs, body.pack_id, body.incident_id)
    opt = _get_option(inc, body.option_id)
    mode = _effective_mode(body.mode, settings)
    try:
        res = await run_blocking(
            wf_exec.execute_approved_saga,
            pack=pack,
            incident=inc,
            option=opt,
            approval_token=body.approval_token,
            idempotency_key=body.idempotency_key,
            dry_run=body.dry_run,
            simulate_step2_failure=body.simulate_step2_failure,
            live_workflows=(mode == "live"),
        )
        return asdict(res)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        safe_msg = redact_gcp_identifiers(str(exc), project_id=settings.gcp_project_id)
        raise HTTPException(status_code=500, detail=safe_msg) from exc


@router.post("/api/v1/ingest/upload")
async def ingest_upload(
    request: Request,
    file: UploadFile = File(...),
    pack_id: str = Form("secops_siem_triage"),
    incident_id: str | None = Form(None),
    commit_to_bigquery: bool = Form(False),
    mode: Literal["replay", "live"] | None = Form(None),
) -> dict[str, Any]:
    settings, packs, gcp_clients, _, _ = _ctx(request)
    if pack_id not in packs:
        raise HTTPException(status_code=404, detail=f"Unknown pack_id: {pack_id!r}")
    eff_mode = _effective_mode(mode, settings)
    raw_bytes = await file.read()
    try:
        validated = validate_log_upload(file.filename or "", raw_bytes)
        res = await run_blocking(
            process_log_upload,
            upload=validated,
            pack_id=pack_id,
            incident_id=incident_id,
            settings=settings,
            gcp_clients=gcp_clients,
            commit_to_bigquery=bool(commit_to_bigquery and eff_mode == "live"),
            use_live_dlp=(eff_mode == "live"),
        )
        return asdict(res)
    except UploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON log file: {exc}") from exc
    except Exception as exc:
        safe_msg = redact_gcp_identifiers(str(exc), project_id=settings.gcp_project_id)
        raise HTTPException(status_code=500, detail=safe_msg) from exc


@router.get("/api/v1/benchmarks/summary")
async def benchmarks_summary(request: Request) -> dict[str, Any]:
    settings, _, _, _, _ = _ctx(request)
    return summarize_recordings(settings.recordings_dir)


@router.post("/api/v1/tco/estimate")
async def tco_estimate(request: Request, body: TCOEstimateRequest) -> dict[str, Any]:
    _, packs, _, _, _ = _ctx(request)
    pack = packs.get(body.pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Unknown pack_id: {body.pack_id!r}")
    try:
        est = estimate_monthly_tco(
            monthly_alerts=body.monthly_alerts,
            manual_baseline_cost_usd=pack.manual_baseline_cost_usd,
            synthesis_model=body.synthesis_model,
            enable_tier2_connectors=body.enable_tier2_connectors,
        )
        return {k: asdict(v) for k, v in est.items()}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/v1/releases")
async def get_releases(request: Request) -> dict[str, Any]:
    settings, _, _, _, _ = _ctx(request)
    if not settings.releases_path.is_file():
        return {"solution_id": "acc04", "current_version": settings.app_version, "releases": []}
    return json.loads(settings.releases_path.read_text(encoding="utf-8"))
