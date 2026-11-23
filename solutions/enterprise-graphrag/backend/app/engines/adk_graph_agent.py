"""Google ADK 2.15 Graph Coordinator Engine (Gemini LlmAgent + BigQuery ISO GQL tools).

Orchestrates multi-hop contagion investigations using a real Google ADK LlmAgent
and InMemoryRunner with bounded LLM turns, wall-clock timeout, per-tool telemetry,
and deterministic Edge-ID/Entity-ID/Chunk-ID provenance verification.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.attribution import gcp_job_labels
from app.core.config import MODEL_CATALOG, Settings
from app.core.gcp_clients import (
    embed_single_text,
    get_bigquery_client,
    rerank_records,
    run_blocking,
)
from app.core.models import GraphPath, TraversalDirection
from app.core.pricing import TokenUsage, compute_llm_cost_usd
from app.core.security import screen_with_model_armor
from app.core.stage_emitter import StageEmitter
from app.engines.base import (
    BaseGraphEngine,
    EngineFlags,
    EngineResult,
    QueryRequest,
    parse_gql_path_rows,
)
from app.engines.bq_graphrag import _format_path_text
from app.engines.gql_builder import (
    build_bq_chunks_by_ids_sql,
    build_bq_flat_vector_chunks_sql,
    build_bq_gql_traversal,
    build_bq_vector_seed_entities_sql,
)
from app.engines.provenance_verifier import verify_citations
from app.packs.loader import LoadedPack

ADK_APP_NAME = "enterprise_graphrag_adk"
ADK_AGENT_NAME = "graphrag_contagion_coordinator"
ADK_USER_ID = "analyst"
DEFAULT_MAX_LLM_CALLS = 6
DEFAULT_BUDGET_SECONDS = 90.0
MAX_RUNNER_EVENTS = 250

ADK_SYSTEM_INSTRUCTION = (
    "You are an enterprise GraphRAG contagion coordinator backed by BigQuery Property Graph (ISO GQL). "
    "Answer ONLY from tool outputs — never invent entities, edges, or chunks.\n"
    "Workflow:\n"
    "1. Call traverse_knowledge_graph with a targeted search query, direction ('outgoing', 'incoming', or 'both'), "
    "and max_hops (1..4) to retrieve multi-hop ISO GQL contagion paths and linked source document chunks.\n"
    "2. Optionally call inspect_pack_ontology or search_document_chunks only if additional domain metadata or "
    "source wording is needed.\n"
    "3. Draft a concise executive contagion brief citing EVERY entity, hop, and document claim inline with exact "
    "[ENTITY:<entity_id>], [EDGE:<edge_id>], and [CHUNK:<chunk_id>] tags copied character-for-character from tool "
    "output (never paraphrase or truncate hex suffixes). Clearly distinguish REAL_PUBLIC facts from MODELED_BRIDGE scenarios.\n"
    "4. MANDATORY: You MUST call verify_draft_citations(draft_answer=<your full draft>) BEFORE producing your final "
    "response. Never skip verify_draft_citations. If it returns is_fully_verified=false, fix every unverified ID using "
    "the exact IDs from traverse_knowledge_graph and return the repaired brief."
)


class ADKCoordinatorError(RuntimeError):
    """Raised when the ADK coordinator violates turn, timeout, or protocol bounds."""


@dataclass
class ADKToolLedger:
    """Request-scoped ground-truth provenance ledger populated strictly by executed tools."""

    paths: list[GraphPath] = field(default_factory=list)
    seen_path_edges: set[tuple[str, ...]] = field(default_factory=set)
    nodes_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    chunks_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    bytes_processed: int = 0
    cache_hit: bool = False
    reranker_used: bool = False
    graph_queried: bool = False

    def record_paths(
        self,
        paths: tuple[GraphPath, ...],
        nodes: dict[str, dict[str, Any]],
        edges: dict[str, dict[str, Any]],
    ) -> None:
        self.nodes_by_id.update(nodes)
        self.edges_by_id.update(edges)
        for p in paths:
            if p.edge_ids not in self.seen_path_edges:
                self.seen_path_edges.add(p.edge_ids)
                self.paths.append(p)


def _extract_non_thought_text(event: Any) -> str:
    content = getattr(event, "content", None)
    parts = list(getattr(content, "parts", None) or [])
    return "".join(
        str(p.text) for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)
    ).strip()


def _parse_direction(raw: str, fallback: TraversalDirection) -> TraversalDirection:
    cleaned = (raw or "").strip().lower()
    if cleaned == "outgoing":
        return TraversalDirection.OUTGOING
    if cleaned == "incoming":
        return TraversalDirection.INCOMING
    if cleaned == "both":
        return TraversalDirection.BOTH
    return fallback


class ADKGraphCoordinatorEngine(BaseGraphEngine):
    """Google ADK 2.15 multi-tool GraphRAG coordinator over BigQuery Property Graph."""

    engine_id = "adk_graph_coordinator"
    engine_label = "Google ADK Graph Coordinator (ADK 2.15 + ISO GQL Tools)"
    stages = (
        "query_prep",
        "model_armor_input",
        "adk_coordinate",
        "verify_provenance",
    )

    def __init__(
        self,
        settings: Settings,
        *,
        packs: dict[str, LoadedPack] | None = None,
        max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        runner_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._packs = packs or {}
        self._max_llm_calls = max(2, min(12, max_llm_calls))
        self._budget_seconds = max(5.0, min(300.0, budget_seconds))
        self._runner_factory = runner_factory

    def _build_tools(self, req: QueryRequest, ledger: ADKToolLedger) -> list[Callable[..., Any]]:
        """Create request-scoped plain Python tools auto-inspected by Google ADK 2.15."""
        settings = self._settings
        loaded_pack = self._packs.get(req.pack_id)

        def inspect_pack_ontology() -> dict[str, Any]:
            """Inspect the active industry pack's ontology node types, edge types, and metadata."""
            t0 = time.perf_counter()
            if loaded_pack is None:
                out: dict[str, Any] = {
                    "status": "success",
                    "pack_id": req.pack_id,
                    "note": "Pack manifest not preloaded; use traverse_knowledge_graph directly.",
                }
            else:
                m = loaded_pack.manifest
                out = {
                    "status": "success",
                    "pack_id": m.pack_id,
                    "title": m.title,
                    "industry": m.industry,
                    "default_max_hops": m.default_max_hops,
                    "node_types": [nt.type for nt in m.ontology.node_types],
                    "edge_types": [et.type for et in m.ontology.edge_types],
                }
            ledger.tool_trace.append(
                {
                    "tool": "inspect_pack_ontology",
                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                    "status": "success",
                }
            )
            return out

        def traverse_knowledge_graph(
            search_query: str,
            direction: str = "both",
            max_hops: int = 3,
        ) -> dict[str, Any]:
            """Traverse the BigQuery Property Graph (ISO GQL) for multi-hop contagion paths.

            Args:
                search_query: Focused entity names and risk concepts to anchor seed retrieval.
                direction: Traversal direction — 'outgoing', 'incoming', or 'both'.
                max_hops: Maximum relationship hops to traverse (1 to 4).
            """
            from google.cloud import bigquery

            t0 = time.perf_counter()
            clean_q = " ".join((search_query or req.query).strip().split())
            eff_dir = _parse_direction(direction, req.direction)
            eff_hops = max(1, min(4, int(max_hops)))

            q_vec = embed_single_text(
                project_id=settings.gcp_project_id,
                model_id=settings.vertex_embedding_model,
                text=clean_q,
                task_type="RETRIEVAL_QUERY",
                location=settings.vertex_location,
            )

            client = get_bigquery_client(settings.gcp_project_id)
            seed_pq = build_bq_vector_seed_entities_sql(
                dataset=settings.bq_dataset,
                top_k=req.top_k_seeds,
            )
            seed_params = [
                bigquery.ScalarQueryParameter("pack_id", "STRING", req.pack_id),
                bigquery.ScalarQueryParameter("query_text", "STRING", clean_q),
                bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", q_vec),
            ]
            seed_labels = gcp_job_labels(settings, component="adk_vector_seed")
            dry_job = client.query(
                seed_pq.statement,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=seed_params, dry_run=True, use_query_cache=False, labels=seed_labels
                ),
                location=settings.bq_location,
            )
            if int(dry_job.total_bytes_processed or 0) > settings.max_bq_bytes_billed:
                raise RuntimeError("BigQuery vector seed query exceeded max_bq_bytes_billed cap")

            seed_job = client.query(
                seed_pq.statement,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=seed_params,
                    maximum_bytes_billed=settings.max_bq_bytes_billed,
                    labels=seed_labels,
                ),
                location=settings.bq_location,
            )
            seed_rows = [dict(r) for r in seed_job.result()]
            ledger.bytes_processed += int(seed_job.total_bytes_processed or 0)
            ledger.cache_hit = ledger.cache_hit or bool(seed_job.cache_hit)
            seed_ids = [str(r["entity_id"]) for r in seed_rows]

            gql_pq = build_bq_gql_traversal(
                dataset=settings.bq_dataset,
                direction=eff_dir,
                max_hops=eff_hops,
                limit=120,
            )
            gql_job = client.query(
                gql_pq.statement,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pack_id", "STRING", req.pack_id),
                        bigquery.ArrayQueryParameter("seed_ids", "STRING", seed_ids),
                        bigquery.ScalarQueryParameter("min_confidence", "FLOAT64", req.min_confidence),
                    ],
                    maximum_bytes_billed=settings.max_bq_bytes_billed,
                    labels=gcp_job_labels(settings, component="adk_gql_traverse"),
                ),
                location=settings.bq_location,
            )
            gql_rows = [dict(r) for r in gql_job.result()]
            ledger.bytes_processed += int(gql_job.total_bytes_processed or 0)
            ledger.cache_hit = ledger.cache_hit or bool(gql_job.cache_hit)
            ledger.graph_queried = True

            paths, nodes_by_id, edges_by_id = parse_gql_path_rows(gql_rows)
            for r in seed_rows:
                nodes_by_id.setdefault(str(r["entity_id"]), r)

            ranked_paths = list(paths)
            if req.enable_rerank and paths:
                records = [
                    {
                        "id": p.path_id,
                        "title": f"{p.path_id} ({p.hop_count} hops)",
                        "content": _format_path_text(p, nodes_by_id, edges_by_id),
                    }
                    for p in paths[:30]
                ]
                ranked_ids = rerank_records(
                    project_id=settings.gcp_project_id,
                    query=clean_q,
                    records=records,
                    top_n=min(10, len(records)),
                )
                lookup = {p.path_id: p for p in paths}
                ranked_paths = [lookup[pid] for pid, _ in ranked_ids if pid in lookup]
                ledger.reranker_used = True

            ledger.record_paths(tuple(ranked_paths), nodes_by_id, edges_by_id)

            chunk_ids = sorted(
                {str(e["source_chunk_id"]) for e in edges_by_id.values() if e.get("source_chunk_id")}
            )[:15]
            if chunk_ids:
                ch_pq = build_bq_chunks_by_ids_sql(dataset=settings.bq_dataset)
                ch_job = client.query(
                    ch_pq.statement,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("pack_id", "STRING", req.pack_id),
                            bigquery.ArrayQueryParameter("chunk_ids", "STRING", chunk_ids),
                        ],
                        maximum_bytes_billed=settings.max_bq_bytes_billed,
                        labels=gcp_job_labels(settings, component="adk_chunk_fetch"),
                    ),
                    location=settings.bq_location,
                )
                for c in ch_job.result():
                    cd = dict(c)
                    ledger.chunks_by_id[str(cd["chunk_id"])] = cd

            formatted_paths = [
                _format_path_text(p, nodes_by_id, edges_by_id) for p in ranked_paths[:10]
            ]
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            ledger.tool_trace.append(
                {
                    "tool": "traverse_knowledge_graph",
                    "direction": eff_dir.value,
                    "max_hops": eff_hops,
                    "seed_count": len(seed_ids),
                    "paths_found": len(ranked_paths),
                    "duration_ms": elapsed_ms,
                    "status": "success",
                }
            )
            return {
                "status": "success",
                "seed_ids": seed_ids,
                "paths_found": len(ranked_paths),
                "unique_nodes": len(nodes_by_id),
                "unique_edges": len(edges_by_id),
                "paths_text": formatted_paths,
                "linked_chunks": [
                    {
                        "chunk_id": cid,
                        "doc_name": ledger.chunks_by_id[cid].get("doc_name", ""),
                        "data_origin": ledger.chunks_by_id[cid].get("data_origin", ""),
                        "excerpt": str(ledger.chunks_by_id[cid].get("content", ""))[:500],
                    }
                    for cid in chunk_ids
                    if cid in ledger.chunks_by_id
                ][:8],
            }

        def search_document_chunks(search_query: str, top_k: int = 5) -> dict[str, Any]:
            """Search raw source document chunks via BigQuery VECTOR_SEARCH for supporting wording.

            Args:
                search_query: Natural-language search query.
                top_k: Maximum chunks to retrieve (1 to 10).
            """
            from google.cloud import bigquery

            t0 = time.perf_counter()
            clean_q = " ".join((search_query or req.query).strip().split())
            k = max(1, min(10, int(top_k)))
            q_vec = embed_single_text(
                project_id=settings.gcp_project_id,
                model_id=settings.vertex_embedding_model,
                text=clean_q,
                task_type="RETRIEVAL_QUERY",
                location=settings.vertex_location,
            )
            pq = build_bq_flat_vector_chunks_sql(dataset=settings.bq_dataset, top_k=k)
            client = get_bigquery_client(settings.gcp_project_id)
            params = [
                bigquery.ScalarQueryParameter("pack_id", "STRING", req.pack_id),
                bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", q_vec),
            ]
            chunk_labels = gcp_job_labels(settings, component="adk_chunk_search")
            dry_job = client.query(
                pq.statement,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=params, dry_run=True, use_query_cache=False, labels=chunk_labels
                ),
                location=settings.bq_location,
            )
            if int(dry_job.total_bytes_processed or 0) > settings.max_bq_bytes_billed:
                raise RuntimeError("BigQuery chunk search exceeded max_bq_bytes_billed cap")

            job = client.query(
                pq.statement,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=params,
                    maximum_bytes_billed=settings.max_bq_bytes_billed,
                    labels=chunk_labels,
                ),
                location=settings.bq_location,
            )
            rows = [dict(r) for r in job.result()]
            ledger.bytes_processed += int(job.total_bytes_processed or 0)
            for row in rows:
                ledger.chunks_by_id[str(row["chunk_id"])] = row

            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            ledger.tool_trace.append(
                {
                    "tool": "search_document_chunks",
                    "rows_returned": len(rows),
                    "duration_ms": elapsed_ms,
                    "status": "success",
                }
            )
            return {
                "status": "success",
                "chunks": [
                    {
                        "chunk_id": r["chunk_id"],
                        "doc_name": r["doc_name"],
                        "data_origin": r["data_origin"],
                        "content": str(r["content"])[:700],
                    }
                    for r in rows
                ],
            }

        def verify_draft_citations(draft_answer: str) -> dict[str, Any]:
            """Deterministically verify [ENTITY:], [EDGE:], and [CHUNK:] citations in a draft brief.

            Args:
                draft_answer: Draft executive brief containing inline provenance tags.
            """
            t0 = time.perf_counter()
            report = verify_citations(
                draft_answer or "",
                allowed_entity_ids=frozenset(ledger.nodes_by_id.keys()),
                allowed_edge_ids=frozenset(ledger.edges_by_id.keys()),
                allowed_chunk_ids=frozenset(ledger.chunks_by_id.keys()),
            )
            ledger.tool_trace.append(
                {
                    "tool": "verify_draft_citations",
                    "is_fully_verified": report.is_fully_verified,
                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                    "status": "success",
                }
            )
            return {
                "status": "success",
                "is_fully_verified": report.is_fully_verified,
                "unverified_entity_ids": list(report.unverified_entity_ids),
                "unverified_edge_ids": list(report.unverified_edge_ids),
                "unverified_chunk_ids": list(report.unverified_chunk_ids),
            }

        return [
            inspect_pack_ontology,
            traverse_knowledge_graph,
            search_document_chunks,
            verify_draft_citations,
        ]

    def _build_adk_agent(self, req: QueryRequest, tools: list[Callable[..., Any]]) -> Any:
        from google.adk.agents import LlmAgent
        from google.adk.models.google_llm import Gemini
        from google.adk.planners import BuiltInPlanner
        from google.genai import types

        spec = MODEL_CATALOG[req.synthesis_model]
        model = Gemini(
            model=spec.model_id,
            client_kwargs={
                "vertexai": True,
                "project": self._settings.gcp_project_id,
                "location": self._settings.vertex_location,
            },
        )
        if spec.model_id.startswith("gemini-4"):
            thinking_cfg = types.ThinkingConfig(thinking_level="LOW")
        else:
            thinking_cfg = types.ThinkingConfig(thinking_budget=1024)

        return LlmAgent(
            name=ADK_AGENT_NAME,
            model=model,
            description="Enterprise GraphRAG coordinator over BigQuery Property Graph ISO GQL tools.",
            instruction=ADK_SYSTEM_INSTRUCTION,
            tools=tools,
            planner=BuiltInPlanner(thinking_config=thinking_cfg),
            generate_content_config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=2048,
                labels=gcp_job_labels(self._settings, component="adk_coordinator"),
            ),
        )

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
            label="Validate ADK coordinator model & query parameters",
            service="Google ADK 2.15",
            request={"pack_id": req.pack_id, "model": req.synthesis_model, "max_hops": req.max_hops},
        )
        clean_query = " ".join(req.query.strip().split())
        if not clean_query:
            err = ValueError("Query must not be empty")
            await emitter.failed(h_prep, error=err)
            raise err
        spec = MODEL_CATALOG.get(req.synthesis_model)
        if spec is None or not spec.supports_adk:
            err = ValueError(
                f"Model {req.synthesis_model!r} is not supported by Google ADK Graph Coordinator "
                "(select a Vertex AI Gemini model)."
            )
            await emitter.failed(h_prep, error=err)
            raise err
        await emitter.completed(
            h_prep,
            service_used="Google ADK 2.15",
            payload={"prepared_query": clean_query, "model": req.synthesis_model},
        )

        # Stage 2: model_armor_input
        model_armor_checked = False
        if not self._settings.model_armor_template_id.strip():
            await emitter.skipped("model_armor_input", reason="MODEL_ARMOR_TEMPLATE_ID not configured")
        else:
            h_ma = await emitter.started(
                "model_armor_input",
                label="Screen prompt via Google Cloud Model Armor",
                service="Google Cloud Model Armor",
                request={"location": self._settings.model_armor_location},
            )
            try:
                verdict = await run_blocking(
                    screen_with_model_armor,
                    settings=self._settings,
                    text=clean_query,
                    mode="user_prompt",
                )
            except Exception as exc:
                await emitter.failed(h_ma, error=exc)
                raise
            model_armor_checked = verdict.checked
            await emitter.completed(
                h_ma,
                service_used="Google Cloud Model Armor",
                payload={"match_state": verdict.match_state, "checked": verdict.checked},
            )

        # Stage 3: adk_coordinate
        ledger = ADKToolLedger()
        h_adk = await emitter.started(
            "adk_coordinate",
            label=f"Execute Google ADK 2.15 coordinator ({req.synthesis_model})",
            service=f"Google ADK 2.15 + Vertex AI {req.synthesis_model}",
            request={
                "pack_id": req.pack_id,
                "max_hops": req.max_hops,
                "direction": req.direction.value,
                "max_llm_calls": self._max_llm_calls,
            },
        )
        prompt_text = (
            f"Pack: {req.pack_id}\n"
            f"Preferred traversal direction: {req.direction.value}\n"
            f"Max hops: {req.max_hops}\n"
            f"Analyst Question: {clean_query}"
        )
        in_tokens = 0
        out_tokens = 0
        turns = 0
        final_answer = ""

        try:
            from google.adk.agents.run_config import RunConfig
            from google.adk.runners import InMemoryRunner
            from google.genai import types

            tools = self._build_tools(req, ledger)
            agent = self._build_adk_agent(req, tools)
            runner = (self._runner_factory or InMemoryRunner)(agent=agent, app_name=ADK_APP_NAME)
            session = await runner.session_service.create_session(
                app_name=ADK_APP_NAME, user_id=ADK_USER_ID
            )
            user_msg = types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
            stream = runner.run_async(
                user_id=ADK_USER_ID,
                session_id=session.id,
                new_message=user_msg,
                run_config=RunConfig(max_llm_calls=self._max_llm_calls),
            )
            events_seen = 0
            async with asyncio.timeout(self._budget_seconds):
                async with aclosing(stream) as events:
                    async for event in events:
                        events_seen += 1
                        if events_seen > MAX_RUNNER_EVENTS:
                            raise ADKCoordinatorError(
                                f"ADK runner exceeded {MAX_RUNNER_EVENTS} event limit"
                            )
                        if getattr(event, "partial", None):
                            continue
                        um = getattr(event, "usage_metadata", None)
                        if um is not None:
                            in_tokens += int(getattr(um, "prompt_token_count", 0) or 0)
                            out_tokens += int(getattr(um, "candidates_token_count", 0) or 0)
                        if getattr(event, "author", "") == ADK_AGENT_NAME:
                            turns += 1
                            text = _extract_non_thought_text(event)
                            calls = list(event.get_function_calls() or [])
                            if not calls and text and event.is_final_response():
                                final_answer = text

            if not final_answer:
                raise ADKCoordinatorError(
                    f"ADK coordinator ended after {turns} turn(s) without a final response"
                )
        except TimeoutError as exc:
            err = ADKCoordinatorError(
                f"ADK coordinator exceeded {self._budget_seconds:.0f}s time budget"
            )
            await emitter.failed(h_adk, error=err)
            raise err from exc
        except Exception as exc:
            await emitter.failed(h_adk, error=exc)
            raise

        usage = TokenUsage(input_tokens=in_tokens, output_tokens=out_tokens)
        engine_cost = compute_llm_cost_usd(req.synthesis_model, usage)
        await emitter.completed(
            h_adk,
            service_used=f"Google ADK 2.15 + Vertex AI {req.synthesis_model}",
            payload={
                "turns": turns,
                "tool_calls": ledger.tool_trace,
                "paths_found": len(ledger.paths),
                "unique_nodes": len(ledger.nodes_by_id),
                "unique_edges": len(ledger.edges_by_id),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "engine_cost_usd": engine_cost,
                "bytes_processed": ledger.bytes_processed,
            },
        )

        # Stage 4: verify_provenance
        h_ver = await emitter.started(
            "verify_provenance",
            label="Deterministic Edge-ID & Entity-ID Provenance Audit",
            service="Deterministic Provenance Verifier",
            request={
                "allowed_entities": len(ledger.nodes_by_id),
                "allowed_edges": len(ledger.edges_by_id),
                "allowed_chunks": len(ledger.chunks_by_id),
            },
        )
        provenance = verify_citations(
            final_answer,
            allowed_entity_ids=frozenset(ledger.nodes_by_id.keys()),
            allowed_edge_ids=frozenset(ledger.edges_by_id.keys()),
            allowed_chunk_ids=frozenset(ledger.chunks_by_id.keys()),
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
            answer=final_answer,
            paths=tuple(ledger.paths),
            retrieved_entity_ids=tuple(sorted(ledger.nodes_by_id.keys())),
            retrieved_edge_ids=tuple(sorted(ledger.edges_by_id.keys())),
            retrieved_chunk_ids=tuple(sorted(ledger.chunks_by_id.keys())),
            retrieved_nodes=tuple(ledger.nodes_by_id.values()),
            retrieved_edges=tuple(ledger.edges_by_id.values()),
            retrieved_chunks=tuple(ledger.chunks_by_id.values()),
            provenance=provenance,
            usage=usage,
            engine_cost_usd=engine_cost,
            total_duration_ms=total_ms,
            flags=EngineFlags(
                graph_backend_used="bigquery_property_graph" if ledger.graph_queried else "none",
                reranker_used=ledger.reranker_used,
                model_armor_checked=model_armor_checked,
                bq_cache_hit=ledger.cache_hit,
                bytes_processed=ledger.bytes_processed,
            ),
        )
