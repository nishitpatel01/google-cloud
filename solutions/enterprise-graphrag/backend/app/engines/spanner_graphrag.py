"""Tier-2 Enterprise Spanner Graph (ISO GQL) GraphRAG engine adapter.

Disabled by default ($0/mo standing cost). Activated when GRAPH_BACKEND=spanner
and SPANNER_INSTANCE_ID is provisioned via Terraform enable_enterprise_scale=true
(or during an optional ephemeral Spanner benchmark pass).
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import Settings
from app.core.gcp_clients import (
    embed_single_text,
    generate_grounded_text,
    get_spanner_database,
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
from app.engines.bq_graphrag import GRAPHRAG_SYSTEM_INSTRUCTION, _format_path_text
from app.engines.gql_builder import build_spanner_gql_traversal, build_spanner_vector_seed_sql
from app.engines.provenance_verifier import verify_citations


class SpannerGraphRAGEngine(BaseGraphEngine):
    """Tier-2 Enterprise Spanner Graph adapter with identical stage & provenance contracts."""

    engine_id = "spanner_graphrag"
    engine_label = "Hybrid GraphRAG (Tier-2 Spanner Graph ISO GQL)"
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

    def _run_spanner_seeds(
        self,
        *,
        pack_id: str,
        query_embedding: list[float],
        top_k: int,
    ) -> tuple[list[dict[str, Any]], str]:
        from google.cloud.spanner_v1 import param_types

        pq = build_spanner_vector_seed_sql(top_k=top_k)
        db = get_spanner_database(
            self._settings.gcp_project_id,
            self._settings.spanner_instance_id,
            self._settings.spanner_database_id,
        )
        with db.snapshot() as snapshot:
            results = snapshot.execute_sql(
                pq.statement,
                params={"pack_id": pack_id, "query_embedding": query_embedding},
                param_types={
                    "pack_id": param_types.STRING,
                    "query_embedding": param_types.Array(param_types.FLOAT32),
                },
            )
            cols = [f.name for f in results.fields]
            rows = [dict(zip(cols, row)) for row in results]
        return rows, pq.statement

    def _run_spanner_gql(
        self,
        *,
        pack_id: str,
        seed_ids: list[str],
        req: QueryRequest,
    ) -> tuple[list[dict[str, Any]], str]:
        from google.cloud.spanner_v1 import param_types

        pq = build_spanner_gql_traversal(
            direction=req.direction,
            max_hops=req.max_hops,
            limit=120,
        )
        db = get_spanner_database(
            self._settings.gcp_project_id,
            self._settings.spanner_instance_id,
            self._settings.spanner_database_id,
        )
        with db.snapshot() as snapshot:
            results = snapshot.execute_sql(
                pq.statement,
                params={"pack_id": pack_id, "seed_ids": seed_ids},
                param_types={
                    "pack_id": param_types.STRING,
                    "seed_ids": param_types.Array(param_types.STRING),
                },
            )
            cols = [f.name for f in results.fields]
            rows = [dict(zip(cols, row)) for row in results]
        return rows, pq.statement

    def _fetch_spanner_chunks(self, *, pack_id: str, chunk_ids: list[str]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        from google.cloud.spanner_v1 import param_types

        db = get_spanner_database(
            self._settings.gcp_project_id,
            self._settings.spanner_instance_id,
            self._settings.spanner_database_id,
        )
        sql = (
            "SELECT chunk_id, doc_name, doc_type, page_number, content, data_origin, "
            "source_license, source_url FROM source_chunks "
            "WHERE pack_id = @pack_id AND chunk_id IN UNNEST(@chunk_ids)"
        )
        with db.snapshot() as snapshot:
            results = snapshot.execute_sql(
                sql,
                params={"pack_id": pack_id, "chunk_ids": chunk_ids},
                param_types={
                    "pack_id": param_types.STRING,
                    "chunk_ids": param_types.Array(param_types.STRING),
                },
            )
            cols = [f.name for f in results.fields]
            return [dict(zip(cols, row)) for row in results]

    async def run_query(
        self,
        req: QueryRequest,
        *,
        emitter: StageEmitter,
    ) -> EngineResult:
        t0 = time.perf_counter()
        if not self._settings.spanner_instance_id:
            err = RuntimeError("SPANNER_INSTANCE_ID is not configured (Tier-2 Spanner Graph is parked at $0/mo)")
            h_err = await emitter.started("query_prep", label="Check Spanner Graph config", service="Spanner Graph")
            await emitter.failed(h_err, error=err)
            raise err

        h_prep = await emitter.started(
            "query_prep",
            label="Prepare Spanner Graph multi-hop query",
            service="local",
            request={"pack_id": req.pack_id, "max_hops": req.max_hops},
        )
        clean_query = " ".join(req.query.strip().split())
        if not clean_query:
            err = ValueError("Query must not be empty")
            await emitter.failed(h_prep, error=err)
            raise err
        await emitter.completed(h_prep, service_used="local", payload={"prepared_query": clean_query})

        h_emb = await emitter.started(
            "embed_query",
            label="Embed query (gemini-embedding-2)",
            service=f"Vertex AI {self._settings.vertex_embedding_model}",
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
        await emitter.completed(h_emb, payload={"dimensions": len(q_vec)})

        h_seed = await emitter.started(
            "vector_seed",
            label="Resolve seed entities (Spanner COSINE_DISTANCE)",
            service="Spanner Graph SQL",
        )
        try:
            seed_rows, seed_sql = await run_blocking(
                self._run_spanner_seeds,
                pack_id=req.pack_id,
                query_embedding=q_vec,
                top_k=req.top_k_seeds,
            )
        except Exception as exc:
            await emitter.failed(h_seed, error=exc)
            raise
        seed_ids = [str(r["entity_id"]) for r in seed_rows]
        await emitter.completed(
            h_seed,
            service_used="Spanner Graph SQL",
            payload={"sql": seed_sql, "seed_ids": seed_ids, "rows_returned": len(seed_rows)},
        )

        h_gql = await emitter.started(
            "gql_traverse",
            label=f"Traverse Spanner Graph (ISO GQL, up to {req.max_hops} hops)",
            service="Spanner Graph (ISO GQL)",
        )
        try:
            gql_rows, gql_text = await run_blocking(
                self._run_spanner_gql,
                pack_id=req.pack_id,
                seed_ids=seed_ids,
                req=req,
            )
            paths, nodes_by_id, edges_by_id = parse_gql_path_rows(gql_rows)
            for r in seed_rows:
                nodes_by_id.setdefault(str(r["entity_id"]), r)
            linked_chunk_ids = sorted(
                {str(e["source_chunk_id"]) for e in edges_by_id.values() if e.get("source_chunk_id")}
            )
            linked_chunks = await run_blocking(
                self._fetch_spanner_chunks,
                pack_id=req.pack_id,
                chunk_ids=linked_chunk_ids[:20],
            )
        except Exception as exc:
            await emitter.failed(h_gql, error=exc)
            raise
        chunks_by_id = {str(c["chunk_id"]): c for c in linked_chunks}
        await emitter.completed(
            h_gql,
            service_used="Spanner Graph (ISO GQL)",
            payload={"gql": gql_text, "paths_found": len(paths), "unique_edges": len(edges_by_id)},
        )

        reranker_used = False
        ranked_paths = list(paths)
        if not req.enable_rerank or not paths:
            await emitter.skipped("rerank", reason="disabled or no paths")
        else:
            path_records = [
                {"id": p.path_id, "title": p.path_id, "content": _format_path_text(p, nodes_by_id, edges_by_id)}
                for p in paths[:40]
            ]
            h_rk = await emitter.started("rerank", label="Rerank candidate paths", service="Discovery Engine Ranking API")
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
            lookup = {p.path_id: p for p in paths}
            ranked_paths = [lookup[pid] for pid, _ in ranked_ids if pid in lookup]
            reranker_used = True
            await emitter.completed(h_rk, payload={"candidates_out": len(ranked_paths)})

        context_paths = [_format_path_text(p, nodes_by_id, edges_by_id) for p in ranked_paths[:15]]
        context_chunks = [
            f"[CHUNK:{cid}] ({c['doc_name']} | origin={c['data_origin']})\n{c['content'][:900]}"
            for cid, c in list(chunks_by_id.items())[:10]
        ]
        prompt = (
            f"Question: {clean_query}\n\nVerified Spanner Graph ISO GQL Paths:\n"
            + ("\n\n".join(context_paths) if context_paths else "(No paths.)")
            + "\n\nLinked Source Document Excerpts:\n"
            + ("\n\n".join(context_chunks) if context_chunks else "(No linked chunks.)")
        )
        h_syn = await emitter.started("synthesize", label=f"Synthesize ({req.synthesis_model})", service=f"Vertex AI {req.synthesis_model}")
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
        await emitter.completed(h_syn, payload={"engine_cost_usd": engine_cost})

        h_ver = await emitter.started("verify_provenance", label="Verify Edge-ID provenance", service="Deterministic Provenance Verifier")
        provenance = verify_citations(
            answer,
            allowed_entity_ids=frozenset(nodes_by_id.keys()),
            allowed_edge_ids=frozenset(edges_by_id.keys()),
            allowed_chunk_ids=frozenset(chunks_by_id.keys()),
        )
        await emitter.completed(h_ver, payload={"is_fully_verified": provenance.is_fully_verified})

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
            total_duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            flags=EngineFlags(graph_backend_used="spanner_graph", reranker_used=reranker_used),
        )
