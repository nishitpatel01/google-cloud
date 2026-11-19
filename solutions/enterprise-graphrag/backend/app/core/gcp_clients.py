"""Threadpool-safe lazy Google Cloud client factories and retry helpers.

All heavy SDK imports and ADC metadata calls execute lazily under an RLock
inside asyncio.to_thread so 1-vCPU Cloud Run cold starts never block /health.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence, TypeVar

from app.core.config import MODEL_CATALOG
from app.core.pricing import TokenUsage

T = TypeVar("T")

_CLIENT_LOCK = threading.RLock()
_BQ_CLIENTS: dict[str, Any] = {}
_GENAI_CLIENTS: dict[tuple[str, str], Any] = {}
_RANK_CLIENT: Any = None
_SPANNER_DBS: dict[tuple[str, str, str], Any] = {}


def _is_transient_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "429",
            "503",
            "resource exhausted",
            "rate limit",
            "temporarily unavailable",
            "deadline exceeded",
            "connection reset",
        )
    )


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 0.6,
) -> T:
    """Bounded jittered exponential backoff for transient Vertex/BQ/Spanner RPCs."""
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_transient_error(exc):
                raise
            delay = base_delay_s * (2 ** (attempt - 1)) + random.uniform(0.05, 0.35)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def get_bigquery_client(project_id: str) -> Any:
    """Lazily initialize and cache a BigQuery client per project."""
    with _CLIENT_LOCK:
        client = _BQ_CLIENTS.get(project_id)
        if client is None:
            from google.cloud import bigquery

            client = bigquery.Client(project=project_id)
            _BQ_CLIENTS[project_id] = client
        return client


def get_genai_client(project_id: str, location: str = "global") -> Any:
    """Lazily initialize and cache a Vertex AI google-genai Client."""
    key = (project_id, location)
    with _CLIENT_LOCK:
        client = _GENAI_CLIENTS.get(key)
        if client is None:
            from google import genai

            client = genai.Client(vertexai=True, project=project_id, location=location)
            _GENAI_CLIENTS[key] = client
        return client


def get_ranking_client() -> Any:
    """Lazily initialize Discovery Engine RankServiceClient."""
    global _RANK_CLIENT
    with _CLIENT_LOCK:
        if _RANK_CLIENT is None:
            from google.cloud import discoveryengine_v1 as discoveryengine

            _RANK_CLIENT = discoveryengine.RankServiceClient()
        return _RANK_CLIENT


def get_spanner_database(project_id: str, instance_id: str, database_id: str) -> Any:
    """Lazily initialize Cloud Spanner Database handle for Tier-2 Spanner Graph."""
    key = (project_id, instance_id, database_id)
    with _CLIENT_LOCK:
        db = _SPANNER_DBS.get(key)
        if db is None:
            from google.cloud import spanner

            client = spanner.Client(project=project_id)
            instance = client.instance(instance_id)
            db = instance.database(database_id)
            _SPANNER_DBS[key] = db
        return db


def embed_single_text(
    *,
    project_id: str,
    model_id: str,
    text: str,
    task_type: str = "RETRIEVAL_QUERY",
    location: str = "global",
) -> list[float]:
    """Embed ONE text item per call (gemini-embedding-2 collapses list inputs into one vector)."""
    from google.genai import types

    client = get_genai_client(project_id, location=location)

    def _call() -> list[float]:
        resp = client.models.embed_content(
            model=model_id,
            contents=text,
            config=types.EmbedContentConfig(task_type=task_type),
        )
        if not resp.embeddings or not resp.embeddings[0].values:
            raise RuntimeError(f"Empty embedding returned by {model_id}")
        return [float(v) for v in resp.embeddings[0].values]

    return retry_with_backoff(_call)


def embed_texts_threaded(
    *,
    project_id: str,
    model_id: str,
    texts: Sequence[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
    location: str = "global",
    max_workers: int = 8,
) -> list[list[float]]:
    """Embed multiple texts safely using 1-content-per-call across a bounded thread pool."""
    if not texts:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(texts))) as pool:
        futures = [
            pool.submit(
                embed_single_text,
                project_id=project_id,
                model_id=model_id,
                text=t,
                task_type=task_type,
                location=location,
            )
            for t in texts
        ]
        return [f.result() for f in futures]


def generate_grounded_text(
    *,
    project_id: str,
    model_id: str,
    prompt: str,
    system_instruction: str,
    location: str = "global",
    temperature: float = 0.1,
) -> tuple[str, TokenUsage]:
    """Invoke a verified Gemini model on Vertex AI and return (answer, TokenUsage)."""
    spec = MODEL_CATALOG.get(model_id)
    if spec is None:
        raise ValueError(f"Unsupported model_id: {model_id!r}")
    if spec.family != "gemini":
        raise ValueError(
            f"Direct google-genai generation only supports Gemini models; got {model_id!r} "
            "(Claude models route through Vertex rawPredict in the evaluation judge)."
        )

    from google.genai import types

    client = get_genai_client(project_id, location=location)
    cfg_kwargs: dict[str, Any] = {"system_instruction": system_instruction}
    if spec.supports_temperature:
        cfg_kwargs["temperature"] = temperature

    def _call() -> tuple[str, TokenUsage]:
        resp = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError(f"Empty generation response from {model_id}")
        um = getattr(resp, "usage_metadata", None)
        usage = TokenUsage(
            input_tokens=int(getattr(um, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(um, "candidates_token_count", 0) or 0),
        )
        return text, usage

    return retry_with_backoff(_call)


def rerank_records(
    *,
    project_id: str,
    query: str,
    records: Sequence[dict[str, str]],
    top_n: int = 8,
    ranking_model: str = "semantic-ranker-default@latest",
) -> list[tuple[str, float]]:
    """Rerank retrieved path/chunk snippets via Discovery Engine Ranking API."""
    if not records:
        return []
    from google.cloud import discoveryengine_v1 as discoveryengine

    client = get_ranking_client()
    ranking_config = client.ranking_config_path(
        project=project_id, location="global", ranking_config="default_ranking_config"
    )
    req_records = [
        discoveryengine.RankingRecord(
            id=r["id"],
            title=r.get("title", r["id"])[:250],
            content=r["content"][:4000],
        )
        for r in records
    ]

    def _call() -> list[tuple[str, float]]:
        resp = client.rank(
            request=discoveryengine.RankRequest(
                ranking_config=ranking_config,
                model=ranking_model,
                top_n=min(top_n, len(req_records)),
                query=query,
                records=req_records,
            )
        )
        return [(rec.id, float(rec.score)) for rec in resp.records]

    return retry_with_backoff(_call)


async def run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run synchronous GCP SDK calls off the FastAPI event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)
