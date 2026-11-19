"""Tier-1 Serverless BigQuery Property Graph (ISO GQL) + Hybrid Vector GraphRAG engine."""

from __future__ import annotations

import time
from typing import Any

from app.core.config import Settings
from app.core.gcp_clients import (
    embed_single_text,
    generate_grounded_text,
    get_bigquery_client,
    rerank_records,
    run_blocking,
)
from app.core.pricing import compute_llm_cost_usd
from app.core.stage_emitter import StageEmitter
from app.engines.base import (
    BaseGraphEngine,
    EngineFlags,
    EngineResult,
    QueryRequest,
    parse_gql_path_rows,
)
from app.engines.gql_builder import (
    build_bq_chunks_by_ids_sql,
    build_bq_gql_traversal,
    build_bq_vector_seed_entities_sql,
)
from app.engines.provenance_verifier import verify_citations

GRAPHRAG_SYSTEM_INSTRUCTION = (
    "You are an enterprise GraphRAG risk & contagion analyst. Answer strictly from the "
    "verified ISO GQL graph paths and linked source document chunks provided below. "
    "For EVERY entity, relationship hop, and evidence claim, include exact inline tags: "
    "[ENTITY:<entity_id>], [EDGE:<edge_id>], and [CHUNK:<chunk_id>]. "
    "Clearly distinguish REAL_PUBLIC records from MODELED_BRIDGE scenarios. "
    "NEVER invent entity IDs, edge IDs, chunk IDs, or links not present in the provided context."
)


class BigQueryGraphRAGEngine(BaseGraphEngine):
    """Tier-1 Serverless GraphRAG: Vector Seed -> BigQuery Property Graph ISO GQL -> Rerank -> Synthesis."""

    engine_id = "bq_graphrag"
    engine_label = "Hybrid GraphRAG (Tier-1 BigQuery Property Graph ISO GQL)"
    stages = (
        "query_prep",
        "embed_query",
        "vector_seed",
        "gql_traverse",
        "rerank",
        "synthesize",
        "verify_provenance",
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _run_bq_vector_seeds(
        self,
        *,
        pack_id: str,
        query_text: str,
        query_embedding: list[float],
        top_k: int,
    ) -> tuple[list[dict[str, Any]], int, bool, str]:
        from google.cloud import bigquery

        pq = build_bq_vector_seed_entities_sql(dataset=self._settings.bq_dataset, top_k=top_k)
        client = get_bigquery_client(self._settings.gcp_project_id)
        params = [
            bigquery.ScalarQueryParameter("pack_id", "STRING", pack_id),
            bigquery.ScalarQueryParameter("query_text", "STRING", query_text),
            bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", query_embedding),
        ]
        dry_cfg = bigquery.QueryJobConfig(query_parameters=params, dry_run=True, use_query_cache=False)
        dry_job = client.query(pq.statement, job_config=dry_cfg, location=self._settings.bq_location)
        est_bytes = int(dry_job.total_bytes_processed or 0)
        if est_bytes > self._settings.max_bq_bytes_billed:
            raise RuntimeError(
                f"BigQuery seed search dry-run {est_bytes} bytes > cap {self._settings.max_bq_bytes_billed}"
            )
        run_cfg = bigquery.QueryJobConfig(
            query_parameters=params,
            maximum_bytes_billed=self._settings.max_bq_bytes_billed,
        )
        job = client.query(pq.statement, job_config=run_cfg, location=self._settings.bq_location)
        return [dict(r) for r in job.result()], int(job.total_bytes_processed or 0), bool(job.cache_hit), pq.statement

    def _run_bq_gql(
        self,
        *,
        pack_id: str,
        seed_ids: list[str],
        req: QueryRequest,
    ) -> tuple[list[dict[str, Any]], int, bool, str]:
        from google.cloud import bigquery

        pq = build_bq_gql_traversal(
            dataset=self._settings.bq_dataset,
            direction=req.direction,
            max_hops=req.max_hops,
            limit=120,
        )
        client = get_bigquery_client(self._settings.gcp_project_id)
        params = [
            bigquery.ScalarQueryParameter("pack_id", "STRING", pack_id),
            bigquery.ArrayQueryParameter("seed_ids", "STRING", seed_ids),
            bigquery.ScalarQueryParameter("min_confidence", "FLOAT64", req.min_confidence),
        ]
        run_cfg = bigquery.QueryJobConfig(
            query_parameters=params,
            maximum_bytes_billed=self._settings.max_bq_bytes_billed,
        )
        job = client.query(pq.statement, job_config=run_cfg, location=self._settings.bq_location)
        return [dict(r) for r in job.result()], int(job.total_bytes_processed or 0), bool(job.cache_hit), pq.statement

    def _fetch_linked_chunks(
        self,
        *,
        pack_id: str,
        chunk_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        from google.cloud import bigquery

        pq = build_bq_chunks_by_ids_sql(dataset=self._settings.bq_dataset)
        client = get_bigquery_client(self._settings.gcp_project_id)
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pack_id", "STRING", pack_id),
                bigquery.ArrayQueryParameter("chunk_ids", "STRING", chunk_ids),
            ],
            maximum_bytes_billed=self._settings.max_bq_bytes_billed,
        )
        return [
            dict(r)
            for r in client.query(pq.statement, job_config=cfg, location=self._settings.bq_location).result()
        ]

    async def run_query(
        self,
        req: QueryRequest,
        *,
        emitter: StageEmitter,
    ) -> EngineResult:
        t0 = time.perf_counter()
        total_bytes = 0
        any_cache_hit = False

        # Stage 1: query_prep
        h_prep = await emitter.started(
            "query_prep",
            label="Prepare multi-hop graph query",
            service="local",
            request={"pack_id": req.pack_id, "max_hops": req.max_hops, "direction": req.direction.value},
        )
        clean_query = " ".join(req.query.strip().split())
        if not clean_query:
            err = ValueError("Query must not be empty")
            await emitter.failed(h_prep, error=err)
            raise err
        await emitter.completed(
            h_prep,
            service_used="local",
            payload={"prepared_query": clean_query, "max_hops": req.max_hops, "direction": req.direction.value},
        )

        # Stage 2: embed_query
        h_emb = await emitter.started(
            "embed_query",
            label="Embed query (gemini-embedding-2)",
            service=f"Vertex AI {self._settings.vertex_embedding_model}",
            request={"model": self._settings.vertex_embedding_model},
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

        # Stage 3: vector_seed (BigQuery VECTOR_SEARCH on entities)
        h_seed = await emitter.started(
            "vector_seed",
            label="Resolve anchor seed entities (BigQuery VECTOR_SEARCH)",
            service="BigQuery VECTOR_SEARCH",
            request={"pack_id": req.pack_id, "top_k_seeds": req.top_k_seeds},
        )
        try:
            seed_rows, seed_bytes, seed_cache, seed_sql = await run_blocking(
                self._run_bq_vector_seeds,
                pack_id=req.pack_id,
                query_text=clean_query,
                query_embedding=q_vec,
                top_k=req.top_k_seeds,
            )
        except Exception as exc:
            await emitter.failed(h_seed, error=exc)
            raise
        total_bytes += seed_bytes
        any_cache_hit = any_cache_hit or seed_cache
        seed_ids = [str(r["entity_id"]) for r in seed_rows]
        await emitter.completed(
            h_seed,
            service_used="BigQuery VECTOR_SEARCH",
            payload={
                "sql": seed_sql,
                "seed_ids": seed_ids,
                "rows_returned": len(seed_rows),
                "bytes_processed": seed_bytes,
                "cache_hit": seed_cache,
            },
        )

        # Stage 4: gql_traverse (BigQuery Property Graph ISO GQL)
        h_gql = await emitter.started(
            "gql_traverse",
            label=f"Traverse BigQuery Property Graph (ISO GQL, up to {req.max_hops} hops)",
            service="BigQuery Property Graph (ISO GQL)",
            request={"seed_ids": seed_ids, "max_hops": req.max_hops, "direction": req.direction.value},
        )
        try:
            gql_rows, gql_bytes, gql_cache, gql_text = await run_blocking(
                self._run_bq_gql,
                pack_id=req.pack_id,
                seed_ids=seed_ids,
                req=req,
            )
            paths, nodes_by_id, edges_by_id = parse_gql_path_rows(gql_rows)
            # Ensure seed entities themselves are preserved even if isolated
            for r in seed_rows:
                nodes_by_id.setdefault(str(r["entity_id"]), r)

            linked_chunk_ids = sorted(
                {str(e["source_chunk_id"]) for e in edges_by_id.values() if e.get("source_chunk_id")}
            )
            linked_chunks = await run_blocking(
                self._fetch_linked_chunks,
                pack_id=req.pack_id,
                chunk_ids=linked_chunk_ids[:20],
            )
        except Exception as exc:
            await emitter.failed(h_gql, error=exc)
            raise
        total_bytes += gql_bytes
        any_cache_hit = any_cache_hit or gql_cache
        chunks_by_id = {str(c["chunk_id"]): c for c in linked_chunks}

        await emitter.completed(
            h_gql,
            service_used="BigQuery Property Graph (ISO GQL)",
            payload={
                "gql": gql_text,
                "paths_found": len(paths),
                "unique_nodes": len(nodes_by_id),
                "unique_edges": len(edges_by_id),
                "linked_chunks": len(chunks_by_id),
                "bytes_processed": gql_bytes,
                "cache_hit": gql_cache,
            },
        )

        # Stage 5: rerank (Discovery Engine Ranking API over path summaries)
        reranker_used = False
        ranked_paths = list(paths)
        if not req.enable_rerank or not paths:
            await emitter.skipped(
                "rerank",
                reason="reranking disabled by config" if not req.enable_rerank else "no graph paths returned",
            )
        else:
            path_records = [
                {
                    "id": p.path_id,
                    "title": f"{p.path_id} ({p.hop_count} hops)",
                    "content": _format_path_text(p, nodes_by_id, edges_by_id),
                }
                for p in paths[:40]
            ]
            h_rk = await emitter.started(
                "rerank",
                label="Rerank candidate paths (Discovery Engine Ranking API)",
                service="Discovery Engine Ranking API",
                request={"candidates_in": len(path_records), "top_n": min(12, len(path_records))},
            )
            try:
                ranked_ids = await run_blocking(
                    rerank_records,
                    project_id=self._settings.gcp_project_id,
                    query=clean_query,
                    records=path_records,
                    top_n=min(12, len(path_records)),
                )
            except Exception as exc:
                await emitter.failed(h_rk, error=exc)
                raise
            path_lookup = {p.path_id: p for p in paths}
            ranked_paths = [path_lookup[pid] for pid, _ in ranked_ids if pid in path_lookup]
            reranker_used = True
            await emitter.completed(
                h_rk,
                service_used="Discovery Engine Ranking API",
                payload={
                    "candidates_in": len(path_records),
                    "candidates_out": len(ranked_paths),
                    "top_scores": [round(sc, 4) for _, sc in ranked_ids[:5]],
                },
            )

        # Stage 6: synthesize
        context_paths = [
            _format_path_text(p, nodes_by_id, edges_by_id) for p in ranked_paths[:15]
        ]
        context_chunks = [
            f"[CHUNK:{cid}] ({c['doc_name']} | origin={c['data_origin']})\n{c['content'][:900]}"
            for cid, c in list(chunks_by_id.items())[:10]
        ]
        prompt = (
            f"Question: {clean_query}\n\n"
            f"Verified ISO GQL Multi-Hop Paths ({len(context_paths)} shown of {len(paths)}):\n"
            + ("\n\n".join(context_paths) if context_paths else "(No paths found within hop limit.)")
            + "\n\nLinked Source Document Excerpts:\n"
            + ("\n\n".join(context_chunks) if context_chunks else "(No linked chunks.)")
            + "\n\nSynthesize an executive contagion brief. Cite exact [ENTITY:<id>], [EDGE:<id>], and [CHUNK:<id>] tags."
        )
        h_syn = await emitter.started(
            "synthesize",
            label=f"Synthesize grounded contagion brief ({req.synthesis_model})",
            service=f"Vertex AI {req.synthesis_model}",
            request={"model": req.synthesis_model, "paths_in_context": len(context_paths)},
        )
        try:
            answer, usage = await run_blocking(
                generate_grounded_text,
                project_id=self._settings.gcp_project_id,
                model_id=req.synthesis_model,
                prompt=prompt,
                system_instruction=GRAPHRAG_SYSTEM_INSTRUCTION,
                location=self._settings.vertex_location,
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

        # Stage 7: verify_provenance
        h_ver = await emitter.started(
            "verify_provenance",
            label="Deterministic Edge-ID & Entity-ID Provenance Audit",
            service="Deterministic Provenance Verifier",
            request={
                "allowed_entities": len(nodes_by_id),
                "allowed_edges": len(edges_by_id),
                "allowed_chunks": len(chunks_by_id),
            },
        )
        provenance = verify_citations(
            answer,
            allowed_entity_ids=frozenset(nodes_by_id.keys()),
            allowed_edge_ids=frozenset(edges_by_id.keys()),
            allowed_chunk_ids=frozenset(chunks_by_id.keys()),
        )
        await emitter.completed(
            h_ver,
            service_used="Deterministic Provenance Verifier",
            payload={
                "is_fully_verified": provenance.is_fully_verified,
                "verified_entities": len(provenance.verified_entity_ids),
                "unverified_entities": len(provenance.unverified_entity_ids),
                "verified_edges": len(provenance.verified_edge_ids),
                "unverified_edges": len(provenance.unverified_edge_ids),
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
            paths=tuple(ranked_paths),
            retrieved_entity_ids=tuple(sorted(nodes_by_id.keys())),
            retrieved_edge_ids=tuple(sorted(edges_by_id.keys())),
            retrieved_chunk_ids=tuple(sorted(chunks_by_id.keys())),
            retrieved_nodes=tuple(nodes_by_id.values()),
            retrieved_edges=tuple(edges_by_id.values()),
            retrieved_chunks=tuple(chunks_by_id.values()),
            provenance=provenance,
            usage=usage,
            engine_cost_usd=engine_cost,
            total_duration_ms=total_ms,
            flags=EngineFlags(
                graph_backend_used="bigquery_property_graph",
                reranker_used=reranker_used,
                bq_cache_hit=any_cache_hit,
                bytes_processed=total_bytes,
            ),
        )


def _format_path_text(
    path: Any,
    nodes_by_id: dict[str, dict[str, Any]],
    edges_by_id: dict[str, dict[str, Any]],
) -> str:
    lines = [f"Path {path.path_id} ({path.hop_count} hops, exposure_usd=${path.path_exposure_usd:,.0f}):"]
    for eid in path.edge_ids:
        e = edges_by_id.get(eid, {})
        src_id = str(e.get("source_entity_id", ""))
        dst_id = str(e.get("target_entity_id", ""))
        src_name = nodes_by_id.get(src_id, {}).get("canonical_name", src_id)
        dst_name = nodes_by_id.get(dst_id, {}).get("canonical_name", dst_id)
        lines.append(
            f"  - [ENTITY:{src_id}] ({src_name}) --[{e.get('relation_type','REL')} | EDGE:{eid}]--> "
            f"[ENTITY:{dst_id}] ({dst_name}) | origin={e.get('data_origin','')} | "
            f"evidence={str(e.get('evidence_excerpt',''))[:240]} [CHUNK:{e.get('source_chunk_id','')}]"
        )
    return "\n".join(lines)
