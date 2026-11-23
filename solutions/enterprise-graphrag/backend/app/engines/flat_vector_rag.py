"""Flat Vector RAG baseline engine (BigQuery VECTOR_SEARCH over source_chunks only).

Used as the honest side-by-side baseline arm on /compare to prove empirically
why single-hop chunk similarity misses 2-to-4 hop risk and contagion chains.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.attribution import gcp_job_labels
from app.core.config import Settings
from app.core.gcp_clients import (
    embed_single_text,
    generate_grounded_text,
    get_bigquery_client,
    run_blocking,
)
from app.core.pricing import compute_llm_cost_usd
from app.core.stage_emitter import StageEmitter
from app.engines.base import BaseGraphEngine, EngineFlags, EngineResult, QueryRequest
from app.engines.gql_builder import build_bq_flat_vector_chunks_sql
from app.engines.provenance_verifier import verify_citations

FLAT_SYSTEM_INSTRUCTION = (
    "You are an enterprise risk analyst answering strictly from the provided document chunks. "
    "Cite every factual claim inline using exact [CHUNK:<chunk_id>] tags. "
    "Do NOT invent relationships, entities, or dollar amounts that are not explicitly stated "
    "in the provided chunks. If intermediate hops or connections are missing from the chunks, "
    "state clearly what is missing."
)


class FlatVectorRAGEngine(BaseGraphEngine):
    """Single-hop BigQuery VECTOR_SEARCH baseline with zero graph traversal."""

    engine_id = "flat_vector_rag"
    engine_label = "Flat Vector RAG (Baseline — Chunks Only)"
    stages = ("query_prep", "embed_query", "retrieve_chunks", "synthesize", "verify_provenance")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _run_bq_chunk_search(
        self,
        *,
        pack_id: str,
        query_embedding: list[float],
        top_k: int,
    ) -> tuple[list[dict[str, Any]], int, bool, str]:
        from google.cloud import bigquery

        pq = build_bq_flat_vector_chunks_sql(dataset=self._settings.bq_dataset, top_k=top_k)
        client = get_bigquery_client(self._settings.gcp_project_id)
        params = [
            bigquery.ScalarQueryParameter("pack_id", "STRING", pack_id),
            bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", query_embedding),
        ]

        labels = gcp_job_labels(self._settings, component="flat_vector_search")
        # Mandatory dry-run byte cap guard
        dry_cfg = bigquery.QueryJobConfig(
            query_parameters=params, dry_run=True, use_query_cache=False, labels=labels
        )
        dry_job = client.query(pq.statement, job_config=dry_cfg, location=self._settings.bq_location)
        est_bytes = int(dry_job.total_bytes_processed or 0)
        if est_bytes > self._settings.max_bq_bytes_billed:
            raise RuntimeError(
                f"BigQuery dry-run estimated {est_bytes} bytes > cap {self._settings.max_bq_bytes_billed}"
            )

        run_cfg = bigquery.QueryJobConfig(
            query_parameters=params,
            maximum_bytes_billed=self._settings.max_bq_bytes_billed,
            labels=labels,
        )
        job = client.query(pq.statement, job_config=run_cfg, location=self._settings.bq_location)
        rows = [dict(r) for r in job.result()]
        bytes_processed = int(job.total_bytes_processed or 0)
        cache_hit = bool(job.cache_hit)
        return rows, bytes_processed, cache_hit, pq.statement

    async def run_query(
        self,
        req: QueryRequest,
        *,
        emitter: StageEmitter,
    ) -> EngineResult:
        t0 = time.perf_counter()

        # Stage 1: query_prep
        h_prep = await emitter.started(
            "query_prep",
            label="Prepare query & pack scope",
            service="local",
            request={"pack_id": req.pack_id, "query": req.query},
        )
        clean_query = " ".join(req.query.strip().split())
        if not clean_query:
            err = ValueError("Query must not be empty")
            await emitter.failed(h_prep, error=err)
            raise err
        await emitter.completed(
            h_prep,
            service_used="local",
            payload={"prepared_query": clean_query, "pack_id": req.pack_id},
        )

        # Stage 2: embed_query
        h_emb = await emitter.started(
            "embed_query",
            label="Embed query (gemini-embedding-2)",
            service=f"Vertex AI {self._settings.vertex_embedding_model}",
            request={"model": self._settings.vertex_embedding_model, "task_type": "RETRIEVAL_QUERY"},
        )
        try:
            q_vec = await run_blocking(
                embed_single_text,
                project_id=self._settings.gcp_project_id,
                model_id=self._settings.vertex_embedding_model,
                text=clean_query,
                task_type="RETRIEVAL_QUERY",
                location=self._settings.vertex_location,
            )
        except Exception as exc:
            await emitter.failed(h_emb, error=exc)
            raise
        await emitter.completed(
            h_emb,
            service_used=f"Vertex AI {self._settings.vertex_embedding_model}",
            payload={"dimensions": len(q_vec)},
        )

        # Stage 3: retrieve_chunks (NO graph traversal)
        h_ret = await emitter.started(
            "retrieve_chunks",
            label="BigQuery VECTOR_SEARCH (chunks only — no graph hops)",
            service="BigQuery VECTOR_SEARCH",
            request={"pack_id": req.pack_id, "top_k": req.top_k_seeds},
        )
        try:
            chunks, bytes_processed, cache_hit, sql_text = await run_blocking(
                self._run_bq_chunk_search,
                pack_id=req.pack_id,
                query_embedding=q_vec,
                top_k=req.top_k_seeds,
            )
        except Exception as exc:
            await emitter.failed(h_ret, error=exc)
            raise
        chunk_ids = tuple(str(c["chunk_id"]) for c in chunks)
        await emitter.completed(
            h_ret,
            service_used="BigQuery VECTOR_SEARCH",
            payload={
                "sql": sql_text,
                "rows_returned": len(chunks),
                "chunk_ids": list(chunk_ids),
                "bytes_processed": bytes_processed,
                "cache_hit": cache_hit,
            },
        )

        # Stage 4: synthesize
        context_blocks = [
            f"[CHUNK:{c['chunk_id']}] ({c['doc_name']} | origin={c['data_origin']} | distance={float(c.get('distance', 0.0)):.4f})\n"
            f"{c['content']}"
            for c in chunks
        ]
        prompt = (
            f"Question: {clean_query}\n\n"
            f"Retrieved Document Chunks (Top-{len(chunks)} vector similarity only):\n\n"
            + "\n\n---\n\n".join(context_blocks)
            + "\n\nAnswer concisely and cite exact [CHUNK:<id>] identifiers."
        )
        h_syn = await emitter.started(
            "synthesize",
            label=f"Synthesize answer ({req.synthesis_model})",
            service=f"Vertex AI {req.synthesis_model}",
            request={"model": req.synthesis_model, "chunks_in_context": len(chunks)},
        )
        try:
            answer, usage = await run_blocking(
                generate_grounded_text,
                project_id=self._settings.gcp_project_id,
                model_id=req.synthesis_model,
                prompt=prompt,
                system_instruction=FLAT_SYSTEM_INSTRUCTION,
                location=self._settings.vertex_location,
                labels=gcp_job_labels(self._settings, component="flat_vector_synthesis"),
            )
        except Exception as exc:
            await emitter.failed(h_syn, error=exc)
            raise
        engine_cost = compute_llm_cost_usd(req.synthesis_model, usage)
        await emitter.completed(
            h_syn,
            service_used=f"Vertex AI {req.synthesis_model}",
            payload={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "engine_cost_usd": engine_cost,
            },
        )

        # Stage 5: verify_provenance
        h_ver = await emitter.started(
            "verify_provenance",
            label="Verify chunk citation provenance",
            service="Deterministic Provenance Verifier",
            request={"allowed_chunks": len(chunk_ids)},
        )
        provenance = verify_citations(
            answer,
            allowed_entity_ids=frozenset(),
            allowed_edge_ids=frozenset(),
            allowed_chunk_ids=frozenset(chunk_ids),
        )
        await emitter.completed(
            h_ver,
            service_used="Deterministic Provenance Verifier",
            payload={
                "is_fully_verified": provenance.is_fully_verified,
                "verified_chunks": len(provenance.verified_chunk_ids),
                "unverified_chunks": len(provenance.unverified_chunk_ids),
            },
        )

        total_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            pack_id=req.pack_id,
            query=clean_query,
            synthesis_model=req.synthesis_model,
            answer=answer,
            paths=(),
            retrieved_entity_ids=(),
            retrieved_edge_ids=(),
            retrieved_chunk_ids=chunk_ids,
            retrieved_nodes=(),
            retrieved_edges=(),
            retrieved_chunks=tuple(chunks),
            provenance=provenance,
            usage=usage,
            engine_cost_usd=engine_cost,
            total_duration_ms=total_ms,
            flags=EngineFlags(
                graph_backend_used="none",
                reranker_used=False,
                bq_cache_hit=cache_hit,
                bytes_processed=bytes_processed,
            ),
        )
