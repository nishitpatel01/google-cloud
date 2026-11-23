"""Cross-family LLM judge and Vertex AI GenAI Evaluation Service (:evaluateInstances).

Strictly enforces:
  - judge_model != synthesis_model (zero self-evaluation bias)
  - Split Engine Cost vs Judge Cost accounting
  - Fail-loud error reporting (zero hardcoded fallback scores)
  - Project ID & project number redaction on all HTTP error diagnostics
  - 6-key GCP FinOps labels attached on Gemini judge calls
"""

from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.attribution import gcp_job_labels
from app.core.config import MODEL_CATALOG, Settings, validate_model_pair
from app.core.gcp_clients import get_genai_client, retry_with_backoff, run_blocking
from app.core.pricing import TokenUsage, compute_llm_cost_usd
from app.core.stage_emitter import StageEmitter
from app.engines.base import EngineResult

ANTHROPIC_VERTEX_VERSION = "vertex-2023-10-16"
MAX_RATIONALE_CHARS = 600
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PROJECT_NUMBER_RE = re.compile(r"projects/\d{6,}")
_RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_EMPTY_CONTEXT_PLACEHOLDER = "[no retrieved evidence]"

_JUDGE_SYSTEM_PROMPT = (
    "You are an independent enterprise risk & compliance evaluator. "
    "Evaluate the candidate answer strictly against the retrieved evidence and question. "
    "Respond ONLY with a single valid JSON object containing exactly four keys: "
    '"groundedness" (float 0.0-1.0), "qa_relevance" (float 0.0-1.0), '
    '"context_precision" (float 0.0-1.0), and "rationale" (concise string <= 400 chars). '
    "Penalize unverified claims or missed multi-hop links that are absent from the retrieved evidence."
)


class JudgeResponseError(ValueError):
    """Raised when an evaluation provider returns malformed or out-of-range scores."""


class JudgeHTTPError(RuntimeError):
    """Raised when an evaluation REST endpoint returns a non-2xx HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class JudgeEvaluation(BaseModel):
    """Immutable measured evaluation output attached to recorded or live query results."""

    model_config = ConfigDict(frozen=True)

    judge_model: str
    judge_provider: Literal["vertex_genai_eval", "anthropic_vertex", "vertex_gemini"]
    groundedness: float = Field(ge=0.0, le=1.0)
    qa_relevance: float = Field(ge=0.0, le=1.0)
    context_precision: float = Field(ge=0.0, le=1.0)
    path_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    provenance_verified: bool = True
    composite_score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=MAX_RATIONALE_CHARS)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    judge_cost_usd: float = Field(default=0.0, ge=0.0)
    duration_ms: float = Field(default=0.0, ge=0.0)


# ---------------------------------------------------------------------------
# Pure Functional Core: URL Builders, Redaction, Normalization & Parsing
# ---------------------------------------------------------------------------


def redact_gcp_identifiers(message: str, project_id: str) -> str:
    """Scrub project_id and 6+ digit project numbers from diagnostic messages."""
    cleaned = message
    if project_id and project_id.strip():
        cleaned = cleaned.replace(project_id.strip(), "<redacted-project>")
    return _PROJECT_NUMBER_RE.sub("projects/<redacted-project-num>", cleaned)


def evaluate_instances_url(project_id: str, location: str) -> str:
    """Build the official Vertex AI v1beta1 :evaluateInstances REST endpoint URL."""
    for field_name, val in (("project_id", project_id), ("location", location)):
        if not val or not _SAFE_SEGMENT_RE.fullmatch(val.strip()):
            raise ValueError(f"{field_name} must be a safe GCP identifier, got {val!r}")
    proj = project_id.strip()
    loc = location.strip()
    host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    return f"https://{host}/v1beta1/projects/{proj}/locations/{loc}:evaluateInstances"


def claude_raw_predict_url(project_id: str, model_id: str, location: str = "global") -> str:
    """Build the Vertex AI Anthropic :rawPredict REST endpoint URL."""
    for field_name, val in (("project_id", project_id), ("model_id", model_id), ("location", location)):
        if not val or not _SAFE_SEGMENT_RE.fullmatch(val.strip()):
            raise ValueError(f"{field_name} must be a safe identifier, got {val!r}")
    proj, mod, loc = project_id.strip(), model_id.strip(), location.strip()
    host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    return (
        f"https://{host}/v1/projects/{proj}/locations/{loc}"
        f"/publishers/anthropic/models/{mod}:rawPredict"
    )


def normalize_unit_score(raw: Any, metric_name: str) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise JudgeResponseError(f"{metric_name} must be numeric, got {raw!r}")
    val = float(raw)
    if not (0.0 <= val <= 1.0):
        raise JudgeResponseError(f"{metric_name} must be within [0.0, 1.0], got {val}")
    return round(val, 3)


def normalize_likert_score(raw: Any, metric_name: str) -> float:
    """Normalize Vertex AI Likert [1.0, 5.0] score linearly onto [0.0, 1.0]."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise JudgeResponseError(f"{metric_name} must be numeric, got {raw!r}")
    val = float(raw)
    if not (1.0 <= val <= 5.0):
        raise JudgeResponseError(f"{metric_name} must be in Likert [1.0, 5.0], got {val}")
    return round((val - 1.0) / 4.0, 3)


def compute_composite_score(
    *,
    groundedness: float,
    qa_relevance: float,
    context_precision: float,
    path_recall: float | None = None,
) -> float:
    """Deterministic weighted quality composite incorporating multi-hop structural recall."""
    if path_recall is not None:
        score = (
            0.35 * path_recall
            + 0.30 * groundedness
            + 0.20 * qa_relevance
            + 0.15 * context_precision
        )
    else:
        score = 0.45 * groundedness + 0.35 * qa_relevance + 0.20 * context_precision
    return round(min(1.0, max(0.0, score)), 3)


def build_evidence_context(result: EngineResult, *, max_chars: int = 28_000) -> str:
    """Assemble retrieved paths, entities, edges, and document chunks for judging.

    Places verified cited entities, edges, and chunks first within each section so
    truncation never drops evidence that the candidate answer explicitly cited.
    """
    verified_e = set(result.provenance.verified_entity_ids)
    verified_ed = set(result.provenance.verified_edge_ids)
    verified_c = set(result.provenance.verified_chunk_ids)

    lines: list[str] = []
    if result.paths:
        lines.append("=== RETRIEVED GRAPH PATHS ===")
        ordered_paths = sorted(
            result.paths,
            key=lambda p: (
                0 if any(eid in verified_ed for eid in p.edge_ids) else 1,
                -p.hop_count,
                -p.path_exposure_usd,
            ),
        )
        for p in ordered_paths[:20]:
            lines.append(
                f"Path {p.path_id} ({p.hop_count} hops, exposure_usd=${p.path_exposure_usd:,.2f}, "
                f"min_confidence={p.min_confidence:.2f}): "
                f"nodes={' -> '.join(p.node_ids)} | edges={', '.join(p.edge_ids)}"
            )
    if result.retrieved_chunks:
        lines.append("=== RETRIEVED SOURCE CHUNKS ===")
        ordered_chunks = sorted(
            result.retrieved_chunks,
            key=lambda ch: 0 if str(ch.get("chunk_id")) in verified_c else 1,
        )
        for ch in ordered_chunks[:12]:
            lines.append(
                f"[CHUNK:{ch.get('chunk_id')}] ({ch.get('doc_name', 'doc')} p.{ch.get('page_number', 1)}, "
                f"origin={ch.get('data_origin')}): "
                f"{str(ch.get('content', ''))[:1400]}"
            )
    if result.retrieved_edges:
        lines.append("=== RETRIEVED GRAPH EDGES ===")
        ordered_edges = sorted(
            result.retrieved_edges,
            key=lambda ed: 0 if str(ed.get("edge_id")) in verified_ed else 1,
        )
        for ed in ordered_edges[:35]:
            lines.append(
                f"[EDGE:{ed.get('edge_id')}] {ed.get('source_entity_id')} "
                f"--{ed.get('relation_type')}--> {ed.get('target_entity_id')} "
                f"(origin={ed.get('data_origin')}, exposure_usd=${float(ed.get('exposure_usd') or 0.0):,.2f}, "
                f"doc={ed.get('source_doc_name')}): "
                f"{str(ed.get('evidence_excerpt', ''))[:320]}"
            )
    if result.retrieved_nodes:
        lines.append("=== RETRIEVED ENTITIES ===")
        ordered_nodes = sorted(
            result.retrieved_nodes,
            key=lambda nd: 0 if str(nd.get("entity_id")) in verified_e else 1,
        )
        for nd in ordered_nodes[:35]:
            lines.append(
                f"[ENTITY:{nd.get('entity_id')}] {nd.get('canonical_name')} "
                f"(type={nd.get('entity_type')}, jurisdiction={nd.get('jurisdiction')}, "
                f"risk={nd.get('risk_tier')}, origin={nd.get('data_origin')}, "
                f"exposure_usd=${float(nd.get('exposure_usd') or 0.0):,.2f})"
            )
    text = "\n".join(lines).strip()
    return text[:max_chars] if text else _EMPTY_CONTEXT_PLACEHOLDER


def build_evaluate_instance_requests(
    *,
    query: str,
    context: str,
    candidate_answer: str,
    reference_answer: str | None = None,
    autorater_model_resource: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build three Vertex AI :evaluateInstances PointwiseMetric rubrics (1-5 Likert)."""
    q = query.strip()
    ans = candidate_answer.strip()
    if not q:
        raise ValueError("query must be non-empty")
    if not ans:
        raise ValueError("candidate_answer must be non-empty")
    ctx = context.strip() or _EMPTY_CONTEXT_PLACEHOLDER
    ref = reference_answer.strip() if reference_answer and reference_answer.strip() else "N/A"

    templates_and_instances: dict[str, tuple[str, dict[str, str]]] = {
        "GROUNDEDNESS": (
            "Rate Groundedness from 1 (hallucinated / unsupported) to 5 (every factual claim is strictly "
            "supported by the Retrieved Context). Penalize claims not backed by the context.\n"
            "Retrieved Context: {context}\nCandidate Response: {response}",
            {"context": ctx, "response": ans},
        ),
        "QUESTION_ANSWERING_QUALITY": (
            "Rate Multi-Hop Answer Completeness & Relevance from 1 (misses key entities/paths or refuses) "
            "to 5 (accurately answers every multi-hop aspect of the Question and Reference Key Points).\n"
            "Question: {question}\nReference Key Points: {reference}\nCandidate Response: {response}",
            {"question": q, "reference": ref, "response": ans},
        ),
        "FULFILLMENT": (
            "Rate Evidence Retrieval Precision from 1 (irrelevant or missing multi-hop links) to 5 "
            "(retrieved graph paths and filing excerpts directly supply the exact multi-hop evidence needed).\n"
            "Question: {question}\nRetrieved Context: {context}",
            {"question": q, "context": ctx},
        ),
    }

    requests_map: dict[str, dict[str, Any]] = {}
    for metric_name, (template, instance_obj) in templates_and_instances.items():
        body: dict[str, Any] = {
            "pointwiseMetricInput": {
                "metricSpec": {"metricPromptTemplate": template},
                "instance": {"jsonInstance": json.dumps(instance_obj, ensure_ascii=False)},
            }
        }
        if autorater_model_resource:
            body["autoraterConfig"] = {"autoraterModel": autorater_model_resource}
        requests_map[metric_name] = body
    return requests_map


def parse_judge_json(text: str) -> tuple[float, float, float, str]:
    """Extract and validate {groundedness, qa_relevance, context_precision, rationale}."""
    if not isinstance(text, str) or not text.strip():
        raise JudgeResponseError("Judge returned empty response text")
    decoder = json.JSONDecoder()
    pos = text.find("{")
    parsed: dict[str, Any] | None = None
    for _ in range(30):
        if pos < 0:
            break
        try:
            obj, _ = decoder.raw_decode(text, pos)
        except ValueError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            parsed = obj
            break
        pos = text.find("{", pos + 1)
    if parsed is None:
        raise JudgeResponseError(f"Judge output contains no JSON object: {text[:200]!r}")

    groundedness = normalize_unit_score(parsed.get("groundedness"), "groundedness")
    qa_relevance = normalize_unit_score(parsed.get("qa_relevance"), "qa_relevance")
    context_precision = normalize_unit_score(parsed.get("context_precision"), "context_precision")
    rationale = parsed.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise JudgeResponseError("Judge 'rationale' must be a non-empty string")
    return groundedness, qa_relevance, context_precision, rationale.strip()[:MAX_RATIONALE_CHARS]


def _estimate_tokens(text: str) -> int:
    cleaned = text.strip()
    return max(1, math.ceil(len(cleaned) / 4)) if cleaned else 0


def _get_adc_bearer_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    if not creds.token:
        raise RuntimeError("Failed to obtain Google Cloud ADC bearer token")
    return str(creds.token)


def _extract_eval_metric(
    doc: dict[str, Any],
    *,
    metric_name: str,
) -> tuple[float, str]:
    block = doc.get("pointwiseMetricResult") or doc.get("pointwise_metric_result")
    if not isinstance(block, dict):
        raise JudgeResponseError(f"evaluateInstances ({metric_name}) missing 'pointwiseMetricResult'")
    raw_score = block.get("score")
    score = normalize_likert_score(raw_score, metric_name)
    return score, str(block.get("explanation") or "").strip()


# ---------------------------------------------------------------------------
# Live Evaluation Providers (Vertex GenAI Eval, Claude rawPredict, Gemini Judge)
# ---------------------------------------------------------------------------


def evaluate_with_vertex_genai_eval(
    *,
    settings: Settings,
    query: str,
    context: str,
    candidate_answer: str,
    reference_answer: str | None = None,
    path_recall: float | None = None,
    provenance_verified: bool = True,
    autorater_model_id: str = "gemini-2.5-pro",
    token_provider: Callable[[], str] | None = None,
    http_post: Callable[..., httpx.Response] | None = None,
) -> JudgeEvaluation:
    """Invoke Vertex AI GenAI Evaluation Service (:evaluateInstances) across 3 PointwiseMetric rubrics."""
    t0 = time.perf_counter()
    loc = settings.gcp_region or "us-central1"
    url = evaluate_instances_url(settings.gcp_project_id, loc)
    token = (token_provider or _get_adc_bearer_token)()
    post_fn = http_post or httpx.post
    autorater_resource = (
        f"projects/{settings.gcp_project_id.strip()}/locations/{loc}"
        f"/publishers/google/models/{autorater_model_id}"
    )

    requests_map = build_evaluate_instance_requests(
        query=query,
        context=context,
        candidate_answer=candidate_answer,
        reference_answer=reference_answer,
        autorater_model_resource=autorater_resource,
    )

    metric_names = ("GROUNDEDNESS", "QUESTION_ANSWERING_QUALITY", "FULFILLMENT")

    scores: dict[str, float] = {}
    explanations: list[str] = []
    total_in = 0
    total_out = 0

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    def _score_one(metric_name: str) -> tuple[str, float, str, int, int]:
        body = requests_map[metric_name]

        def _do_post() -> dict[str, Any]:
            resp = post_fn(url, headers=headers, json=body, timeout=httpx.Timeout(60.0))
            if resp.status_code == 200:
                parsed = resp.json()
                if not isinstance(parsed, dict):
                    raise JudgeResponseError(f"evaluateInstances ({metric_name}) returned non-object JSON")
                return parsed
            redacted = redact_gcp_identifiers(resp.text[:400], settings.gcp_project_id)
            err = JudgeHTTPError(
                resp.status_code,
                f"Vertex evaluateInstances ({metric_name}) HTTP {resp.status_code}: {redacted}",
            )
            if resp.status_code in _RETRYABLE_HTTP_STATUS:
                raise RuntimeError(f"429/503 retryable: {err}") from err
            raise err

        doc = retry_with_backoff(_do_post)
        score, explanation = _extract_eval_metric(doc, metric_name=metric_name)
        usage = doc.get("usageMetadata") or doc.get("usage_metadata")
        if isinstance(usage, dict) and (usage.get("promptTokenCount") or usage.get("candidatesTokenCount")):
            in_tok = int(usage.get("promptTokenCount") or 0)
            out_tok = int(usage.get("candidatesTokenCount") or 0)
        else:
            in_tok = _estimate_tokens(json.dumps(body))
            out_tok = _estimate_tokens(explanation)
        return metric_name, score, explanation, in_tok, out_tok

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(_score_one, metric_names))

    for metric_name, score, explanation, in_tok, out_tok in results:
        scores[metric_name] = score
        if explanation:
            explanations.append(f"{metric_name}: {explanation[:170]}")
        total_in += in_tok
        total_out += out_tok

    groundedness = scores["GROUNDEDNESS"]
    qa_relevance = scores["QUESTION_ANSWERING_QUALITY"]
    context_precision = scores["FULFILLMENT"]
    usage_obj = TokenUsage(input_tokens=total_in, output_tokens=total_out)
    cost = compute_llm_cost_usd(autorater_model_id, usage_obj)
    rationale = " | ".join(explanations)[:MAX_RATIONALE_CHARS] or "Scored via Vertex AI GenAI Evaluation Service."

    return JudgeEvaluation(
        judge_model="vertex-genai-eval",
        judge_provider="vertex_genai_eval",
        groundedness=groundedness,
        qa_relevance=qa_relevance,
        context_precision=context_precision,
        path_recall=path_recall,
        provenance_verified=provenance_verified,
        composite_score=compute_composite_score(
            groundedness=groundedness,
            qa_relevance=qa_relevance,
            context_precision=context_precision,
            path_recall=path_recall,
        ),
        rationale=rationale,
        input_tokens=total_in,
        output_tokens=total_out,
        judge_cost_usd=cost,
        duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
    )


def _build_llm_judge_prompt(
    *,
    query: str,
    context: str,
    candidate_answer: str,
    reference_answer: str | None = None,
) -> str:
    ref_block = f"\nExpected Key Points:\n{reference_answer.strip()}\n" if reference_answer else ""
    return (
        f"Question:\n{query.strip()}\n"
        f"{ref_block}\n"
        f"Retrieved Evidence:\n<retrieved_evidence>\n{context.strip()}\n</retrieved_evidence>\n\n"
        f"Candidate Answer:\n<candidate_answer>\n{candidate_answer.strip()}\n</candidate_answer>\n\n"
        "Return ONLY the JSON object with groundedness, qa_relevance, context_precision, and rationale."
    )


def evaluate_with_claude_vertex(
    *,
    settings: Settings,
    judge_model: str,
    query: str,
    context: str,
    candidate_answer: str,
    reference_answer: str | None = None,
    path_recall: float | None = None,
    provenance_verified: bool = True,
    token_provider: Callable[[], str] | None = None,
    http_post: Callable[..., httpx.Response] | None = None,
) -> JudgeEvaluation:
    """Invoke a Vertex AI Claude partner model via global :rawPredict as cross-family judge."""
    t0 = time.perf_counter()
    spec = MODEL_CATALOG[judge_model]
    url = claude_raw_predict_url(settings.gcp_project_id, judge_model, location=spec.location)
    token = (token_provider or _get_adc_bearer_token)()
    post_fn = http_post or httpx.post

    user_prompt = _build_llm_judge_prompt(
        query=query,
        context=context,
        candidate_answer=candidate_answer,
        reference_answer=reference_answer,
    )
    body: dict[str, Any] = {
        "anthropic_version": ANTHROPIC_VERTEX_VERSION,
        "max_tokens": 512,
        "system": _JUDGE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if spec.supports_temperature:
        body["temperature"] = 0.0

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    def _call() -> dict[str, Any]:
        resp = post_fn(url, headers=headers, json=body, timeout=httpx.Timeout(60.0))
        if resp.status_code == 200:
            return resp.json()
        redacted = redact_gcp_identifiers(resp.text[:400], settings.gcp_project_id)
        err = JudgeHTTPError(
            resp.status_code,
            f"Vertex Claude rawPredict ({judge_model}) HTTP {resp.status_code}: {redacted}",
        )
        if resp.status_code in _RETRYABLE_HTTP_STATUS:
            raise RuntimeError(f"429/503 retryable: {err}") from err
        raise err

    doc = retry_with_backoff(_call)
    content_blocks = doc.get("content") or []
    text = "".join(
        str(b.get("text", ""))
        for b in content_blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()
    groundedness, qa_relevance, context_precision, rationale = parse_judge_json(text)

    usage_raw = doc.get("usage") or {}
    usage = TokenUsage(
        input_tokens=int(usage_raw.get("input_tokens") or 0),
        output_tokens=int(usage_raw.get("output_tokens") or 0),
    )
    cost = compute_llm_cost_usd(judge_model, usage)

    return JudgeEvaluation(
        judge_model=judge_model,
        judge_provider="anthropic_vertex",
        groundedness=groundedness,
        qa_relevance=qa_relevance,
        context_precision=context_precision,
        path_recall=path_recall,
        provenance_verified=provenance_verified,
        composite_score=compute_composite_score(
            groundedness=groundedness,
            qa_relevance=qa_relevance,
            context_precision=context_precision,
            path_recall=path_recall,
        ),
        rationale=rationale,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        judge_cost_usd=cost,
        duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
    )


def evaluate_with_gemini_judge(
    *,
    settings: Settings,
    judge_model: str,
    query: str,
    context: str,
    candidate_answer: str,
    reference_answer: str | None = None,
    path_recall: float | None = None,
    provenance_verified: bool = True,
) -> JudgeEvaluation:
    """Invoke a distinct Gemini judge model with structured JSON output and 6-key GCP labels."""
    from google.genai import types

    t0 = time.perf_counter()
    spec = MODEL_CATALOG[judge_model]
    client = get_genai_client(settings.gcp_project_id, location=spec.location)
    prompt = _build_llm_judge_prompt(
        query=query,
        context=context,
        candidate_answer=candidate_answer,
        reference_answer=reference_answer,
    )
    cfg_kwargs: dict[str, Any] = {
        "system_instruction": _JUDGE_SYSTEM_PROMPT,
        "response_mime_type": "application/json",
        "labels": gcp_job_labels(settings, component="judge_eval"),
    }
    if spec.supports_temperature:
        cfg_kwargs["temperature"] = 0.0

    def _call() -> tuple[str, TokenUsage]:
        resp = client.models.generate_content(
            model=judge_model,
            contents=prompt,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        text = (resp.text or "").strip()
        if not text:
            raise JudgeResponseError(f"Empty judge response from {judge_model}")
        um = getattr(resp, "usage_metadata", None)
        return text, TokenUsage(
            input_tokens=int(getattr(um, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(um, "candidates_token_count", 0) or 0),
        )

    text, usage = retry_with_backoff(_call)
    groundedness, qa_relevance, context_precision, rationale = parse_judge_json(text)
    cost = compute_llm_cost_usd(judge_model, usage)

    return JudgeEvaluation(
        judge_model=judge_model,
        judge_provider="vertex_gemini",
        groundedness=groundedness,
        qa_relevance=qa_relevance,
        context_precision=context_precision,
        path_recall=path_recall,
        provenance_verified=provenance_verified,
        composite_score=compute_composite_score(
            groundedness=groundedness,
            qa_relevance=qa_relevance,
            context_precision=context_precision,
            path_recall=path_recall,
        ),
        rationale=rationale,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        judge_cost_usd=cost,
        duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
    )


async def evaluate_engine_result(
    *,
    settings: Settings,
    result: EngineResult,
    judge_model: str,
    reference_answer: str | None = None,
    path_recall: float | None = None,
    emitter: StageEmitter | None = None,
) -> JudgeEvaluation:
    """Validate judge != synthesis, emit StageEmitter telemetry, and score an EngineResult."""
    _synth_spec, judge_spec = validate_model_pair(result.synthesis_model, judge_model)
    context = build_evidence_context(result)

    handle = None
    if emitter is not None:
        handle = await emitter.started(
            "evaluate",
            label=f"Evaluate Answer ({judge_spec.display_name})",
            service=judge_spec.family,
            request={"judge_model": judge_model},
        )

    try:
        if judge_spec.family == "vertex_eval":
            autorater_id = (
                "gemini-2.5-flash" if result.synthesis_model == "gemini-2.5-pro" else "gemini-2.5-pro"
            )
            ev = await run_blocking(
                evaluate_with_vertex_genai_eval,
                settings=settings,
                query=result.query,
                context=context,
                candidate_answer=result.answer,
                reference_answer=reference_answer,
                path_recall=path_recall,
                provenance_verified=result.provenance.is_fully_verified,
                autorater_model_id=autorater_id,
            )
        elif judge_spec.family == "claude":
            ev = await run_blocking(
                evaluate_with_claude_vertex,
                settings=settings,
                judge_model=judge_model,
                query=result.query,
                context=context,
                candidate_answer=result.answer,
                reference_answer=reference_answer,
                path_recall=path_recall,
                provenance_verified=result.provenance.is_fully_verified,
            )
        else:
            ev = await run_blocking(
                evaluate_with_gemini_judge,
                settings=settings,
                judge_model=judge_model,
                query=result.query,
                context=context,
                candidate_answer=result.answer,
                reference_answer=reference_answer,
                path_recall=path_recall,
                provenance_verified=result.provenance.is_fully_verified,
            )
    except Exception as exc:
        if emitter is not None and handle is not None:
            safe_exc = RuntimeError(redact_gcp_identifiers(str(exc), settings.gcp_project_id))
            await emitter.failed(handle, error=safe_exc)
        raise

    if emitter is not None and handle is not None:
        await emitter.completed(
            handle,
            service_used=ev.judge_provider,
            payload={
                "judge_model": ev.judge_model,
                "groundedness": ev.groundedness,
                "qa_relevance": ev.qa_relevance,
                "context_precision": ev.context_precision,
                "composite_score": ev.composite_score,
                "judge_cost_usd": ev.judge_cost_usd,
            },
        )
    return ev
