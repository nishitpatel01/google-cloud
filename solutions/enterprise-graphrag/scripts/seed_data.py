#!/usr/bin/env python3
"""Idempotent Tier-1 BigQuery & BigQuery Property Graph (ISO GQL) seeder.

Loads validated public industry packs from packs/<pack_id>/ into BigQuery via
load jobs + transactional staging swaps (never streaming inserts), generates
embeddings via Vertex AI gemini-embedding-2 (1 content per call, threaded),
ensures a $0/mo idle BigQuery Enterprise 0-slot autoscale reservation exists
(reusing any existing project QUERY assignment without modifying it), and
creates/refreshes the ISO/IEC 39075 Property Graph `EnterpriseGraph`.

Usage:
  PYTHONPATH=backend .venv/bin/python scripts/seed_data.py --dry-run
  PYTHONPATH=backend .venv/bin/python scripts/seed_data.py --pack all --smoke-gql
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = SOLUTION_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings  # noqa: E402
from app.core.gcp_clients import embed_texts_threaded, get_bigquery_client  # noqa: E402
from app.core.models import EntityNode, GraphEdge, SourceChunk  # noqa: E402
from app.core.stage_emitter import RecordingStageEmitter  # noqa: E402
from app.engines.base import QueryRequest  # noqa: E402
from app.engines.bq_graphrag import BigQueryGraphRAGEngine  # noqa: E402
from app.engines.flat_vector_rag import FlatVectorRAGEngine  # noqa: E402
from app.engines.provenance_verifier import compute_multihop_recall  # noqa: E402
from app.packs.loader import LoadedPack, load_all_packs  # noqa: E402

_SAFE_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,29}$")
_SAFE_DATASET_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_SAFE_LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
_SAFE_RESERVATION_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def validate_gcp_identifiers(
    *,
    project_id: str,
    dataset: str,
    location: str,
    reservation_id: str,
) -> None:
    """Validate SQL-substituted identifiers against strict allowlists."""
    if not _SAFE_PROJECT_RE.match(project_id):
        raise ValueError(f"Invalid GCP_PROJECT_ID: {project_id!r}")
    if not _SAFE_DATASET_RE.match(dataset):
        raise ValueError(f"Invalid BQ_DATASET: {dataset!r}")
    if not _SAFE_LOCATION_RE.match(location):
        raise ValueError(f"Invalid BQ_LOCATION: {location!r}")
    if not _SAFE_RESERVATION_RE.match(reservation_id):
        raise ValueError(f"Invalid BQ_RESERVATION_ID: {reservation_id!r}")


def render_bigquery_ddl(
    ddl_path: Path,
    *,
    project_id: str,
    dataset: str,
    location: str,
    reservation_id: str,
) -> tuple[list[str], str]:
    """Render infra/sql/bigquery_schema.sql into (table_ddls, property_graph_ddl)."""
    validate_gcp_identifiers(
        project_id=project_id,
        dataset=dataset,
        location=location,
        reservation_id=reservation_id,
    )
    raw = ddl_path.read_text(encoding="utf-8")
    rendered = (
        raw.replace("${PROJECT_ID}", project_id)
        .replace("${BQ_DATASET}", dataset)
        .replace("${BQ_LOCATION}", location)
        .replace("${BQ_LOCATION_LOWER}", location.lower())
        .replace("${BQ_RESERVATION_ID}", reservation_id)
    )
    # Strip full-line SQL comments before splitting on ';'
    cleaned_lines = [line for line in rendered.splitlines() if not line.lstrip().startswith("--")]
    cleaned = "\n".join(cleaned_lines)
    statements = [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]

    table_ddls: list[str] = []
    graph_ddl = ""
    for stmt in statements:
        if "CREATE OR REPLACE PROPERTY GRAPH" in stmt.upper():
            graph_ddl = stmt
        else:
            table_ddls.append(stmt)
    if not graph_ddl:
        raise ValueError(f"No CREATE OR REPLACE PROPERTY GRAPH statement found in {ddl_path}")
    return table_ddls, graph_ddl


def entity_embedding_text(ent: EntityNode) -> str:
    """Build deterministic semantic embedding text for an entity vertex."""
    attrs = " | ".join(
        f"{k}={v}" for k, v in sorted(ent.attributes.items()) if v is not None and str(v).strip()
    )
    parts = [
        ent.canonical_name,
        f"type={ent.entity_type}",
        f"jurisdiction={ent.jurisdiction}",
        f"risk_tier={ent.risk_tier.value}",
        f"single_source={ent.single_source}",
        f"origin={ent.data_origin.value}",
    ]
    if attrs:
        parts.append(attrs)
    return " | ".join(parts)[:3000]


def chunk_embedding_text(chunk: SourceChunk) -> str:
    """Build deterministic semantic embedding text for a source document chunk."""
    header = f"{chunk.doc_name} ({chunk.doc_type}, page {chunk.page_number}, origin={chunk.data_origin.value})"
    return f"{header}\n{chunk.content}"[:5000]


def _entity_schema() -> list[Any]:
    from google.cloud import bigquery

    return [
        bigquery.SchemaField("pack_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("entity_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("canonical_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("entity_type", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("jurisdiction", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("risk_tier", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("exposure_usd", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("single_source", "BOOL", mode="NULLABLE"),
        bigquery.SchemaField("data_origin", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_license", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_url", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("attributes_json", "JSON", mode="NULLABLE"),
        bigquery.SchemaField("source_chunk_ids", "STRING", mode="REPEATED"),
        bigquery.SchemaField("embedding", "FLOAT64", mode="REPEATED"),
        bigquery.SchemaField("ingest_run_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("updated_at", "TIMESTAMP", mode="REQUIRED"),
    ]


def _edge_schema() -> list[Any]:
    from google.cloud import bigquery

    return [
        bigquery.SchemaField("pack_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("edge_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_entity_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("target_entity_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("relation_type", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("weight", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("exposure_usd", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("confidence", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("evidence_excerpt", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_chunk_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_doc_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("data_origin", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_license", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_url", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("ingest_run_id", "STRING", mode="REQUIRED"),
    ]


def _chunk_schema() -> list[Any]:
    from google.cloud import bigquery

    return [
        bigquery.SchemaField("pack_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("chunk_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("doc_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("doc_type", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("page_number", "INT64", mode="REQUIRED"),
        bigquery.SchemaField("content", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("data_origin", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_license", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("source_url", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("embedding", "FLOAT64", mode="REPEATED"),
        bigquery.SchemaField("ingest_run_id", "STRING", mode="REQUIRED"),
    ]


def _ingest_run_schema() -> list[Any]:
    from google.cloud import bigquery

    return [
        bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("pack_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("finished_at", "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("docs_count", "INT64", mode="NULLABLE"),
        bigquery.SchemaField("entities_count", "INT64", mode="NULLABLE"),
        bigquery.SchemaField("edges_count", "INT64", mode="NULLABLE"),
        bigquery.SchemaField("chunks_count", "INT64", mode="NULLABLE"),
        bigquery.SchemaField("embedding_model", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("stage_log", "JSON", mode="NULLABLE"),
    ]


def ensure_enterprise_reservation(
    client: Any,
    *,
    project_id: str,
    location: str,
    reservation_id: str,
    dry_run: bool = False,
) -> str:
    """Reuse an existing QUERY reservation assignment or create a $0-idle 0-slot Enterprise one."""
    from google.cloud import bigquery

    loc_lower = location.lower()
    check_sql = (
        f"SELECT reservation_name FROM `region-{loc_lower}`.INFORMATION_SCHEMA.ASSIGNMENTS "
        "WHERE job_type = 'QUERY' AND assignee_id = @project_id LIMIT 1"
    )
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("project_id", "STRING", project_id)],
        labels={"solution": "enterprise_graphrag"},
    )
    rows = list(client.query(check_sql, job_config=cfg, location=location).result())
    if rows:
        existing = str(rows[0]["reservation_name"])
        print(f"[reservation] Reusing existing BigQuery QUERY assignment: {existing} (0 changes made)")
        return existing

    if dry_run:
        print(
            f"[reservation] [dry-run] Would create 0-slot Enterprise reservation "
            f"`{project_id}.region-{loc_lower}.{reservation_id}` and QUERY assignment."
        )
        return reservation_id

    create_res_sql = (
        f"CREATE RESERVATION `{project_id}.region-{loc_lower}.{reservation_id}` "
        "OPTIONS (edition = 'ENTERPRISE', slot_capacity = 0, autoscale_max_slots = 50)"
    )
    try:
        client.query(
            create_res_sql,
            job_config=bigquery.QueryJobConfig(labels={"solution": "enterprise_graphrag"}),
            location=location,
        ).result()
        print(f"[reservation] Created 0-slot Enterprise reservation: {reservation_id}")
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise
        print(f"[reservation] Reservation already exists: {reservation_id}")

    create_asgn_sql = (
        f"CREATE ASSIGNMENT `{project_id}.region-{loc_lower}.{reservation_id}.{reservation_id}-query` "
        f"OPTIONS (assignee = 'projects/{project_id}', job_type = 'QUERY')"
    )
    try:
        client.query(
            create_asgn_sql,
            job_config=bigquery.QueryJobConfig(labels={"solution": "enterprise_graphrag"}),
            location=location,
        ).result()
        print(f"[reservation] Assigned QUERY jobs for project to {reservation_id}")
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise
        print(f"[reservation] QUERY assignment already exists for {reservation_id}")
    return reservation_id


def _load_and_swap_table(
    client: Any,
    *,
    project_id: str,
    dataset: str,
    location: str,
    table_name: str,
    pack_id: str,
    run_id: str,
    rows: list[dict[str, Any]],
    schema: list[Any],
) -> None:
    """Load rows into an ephemeral staging table and swap atomically scoped to pack_id."""
    from google.cloud import bigquery

    stg_name = f"_stg_{table_name}_{run_id.replace('-', '_')[:12]}"
    stg_ref = f"{project_id}.{dataset}.{stg_name}"
    target_ref = f"{project_id}.{dataset}.{table_name}"

    load_cfg = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        labels={"solution": "enterprise_graphrag"},
    )
    try:
        load_job = client.load_table_from_json(rows, stg_ref, job_config=load_cfg, location=location)
        load_job.result()

        swap_sql = f"""
BEGIN TRANSACTION;
DELETE FROM `{target_ref}` WHERE pack_id = @pack_id;
INSERT INTO `{target_ref}` SELECT * FROM `{stg_ref}`;
COMMIT TRANSACTION;
""".strip()
        swap_cfg = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("pack_id", "STRING", pack_id)],
            labels={"solution": "enterprise_graphrag"},
        )
        client.query(swap_sql, job_config=swap_cfg, location=location).result()
    finally:
        client.delete_table(stg_ref, not_found_ok=True)


def seed_pack(
    client: Any,
    *,
    settings: Settings,
    pack: LoadedPack,
) -> dict[str, Any]:
    """Embed and atomically load one validated pack into BigQuery."""
    run_id = f"seed-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    ent_texts = [entity_embedding_text(e) for e in pack.entities]
    chunk_texts = [chunk_embedding_text(c) for c in pack.chunks]

    t_emb = time.perf_counter()
    ent_embeddings = embed_texts_threaded(
        project_id=settings.gcp_project_id,
        model_id=settings.vertex_embedding_model,
        texts=ent_texts,
        task_type="RETRIEVAL_DOCUMENT",
        location=settings.vertex_location,
    )
    chunk_embeddings = embed_texts_threaded(
        project_id=settings.gcp_project_id,
        model_id=settings.vertex_embedding_model,
        texts=chunk_texts,
        task_type="RETRIEVAL_DOCUMENT",
        location=settings.vertex_location,
    )
    emb_ms = round((time.perf_counter() - t_emb) * 1000.0, 2)
    now_iso = datetime.now(timezone.utc).isoformat()

    entity_rows = [
        {
            "pack_id": ent.pack_id,
            "entity_id": ent.entity_id,
            "canonical_name": ent.canonical_name,
            "entity_type": ent.entity_type,
            "jurisdiction": ent.jurisdiction,
            "risk_tier": ent.risk_tier.value,
            "exposure_usd": float(ent.exposure_usd),
            "single_source": bool(ent.single_source),
            "data_origin": ent.data_origin.value,
            "source_license": ent.source_license.value,
            "source_url": ent.source_url,
            "attributes_json": ent.attributes,
            "source_chunk_ids": list(ent.source_chunk_ids),
            "embedding": vec,
            "ingest_run_id": run_id,
            "updated_at": now_iso,
        }
        for ent, vec in zip(pack.entities, ent_embeddings, strict=True)
    ]

    edge_rows = [
        {
            "pack_id": edge.pack_id,
            "edge_id": edge.edge_id,
            "source_entity_id": edge.source_entity_id,
            "target_entity_id": edge.target_entity_id,
            "relation_type": edge.relation_type,
            "weight": float(edge.weight),
            "exposure_usd": float(edge.exposure_usd),
            "confidence": float(edge.confidence),
            "evidence_excerpt": edge.evidence_excerpt,
            "source_chunk_id": edge.source_chunk_id,
            "source_doc_name": edge.source_doc_name,
            "data_origin": edge.data_origin.value,
            "source_license": edge.source_license.value,
            "source_url": edge.source_url,
            "ingest_run_id": run_id,
        }
        for edge in pack.edges
    ]

    chunk_rows = [
        {
            "pack_id": ch.pack_id,
            "chunk_id": ch.chunk_id,
            "doc_name": ch.doc_name,
            "doc_type": ch.doc_type,
            "page_number": int(ch.page_number),
            "content": ch.content,
            "data_origin": ch.data_origin.value,
            "source_license": ch.source_license.value,
            "source_url": ch.source_url,
            "embedding": vec,
            "ingest_run_id": run_id,
        }
        for ch, vec in zip(pack.chunks, chunk_embeddings, strict=True)
    ]

    t_load = time.perf_counter()
    _load_and_swap_table(
        client,
        project_id=settings.gcp_project_id,
        dataset=settings.bq_dataset,
        location=settings.bq_location,
        table_name="entities",
        pack_id=pack.pack_id,
        run_id=run_id,
        rows=entity_rows,
        schema=_entity_schema(),
    )
    _load_and_swap_table(
        client,
        project_id=settings.gcp_project_id,
        dataset=settings.bq_dataset,
        location=settings.bq_location,
        table_name="edges",
        pack_id=pack.pack_id,
        run_id=run_id,
        rows=edge_rows,
        schema=_edge_schema(),
    )
    _load_and_swap_table(
        client,
        project_id=settings.gcp_project_id,
        dataset=settings.bq_dataset,
        location=settings.bq_location,
        table_name="source_chunks",
        pack_id=pack.pack_id,
        run_id=run_id,
        rows=chunk_rows,
        schema=_chunk_schema(),
    )
    load_ms = round((time.perf_counter() - t_load) * 1000.0, 2)

    # Record ingest_runs audit entry via load job (append)
    from google.cloud import bigquery

    finished_at = datetime.now(timezone.utc)
    docs_count = len({c.doc_name for c in pack.chunks})
    summary = {
        "run_id": run_id,
        "pack_id": pack.pack_id,
        "entities": len(entity_rows),
        "edges": len(edge_rows),
        "chunks": len(chunk_rows),
        "docs": docs_count,
        "embedding_dims": len(ent_embeddings[0]) if ent_embeddings else 0,
        "embed_ms": emb_ms,
        "bq_load_swap_ms": load_ms,
        "total_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "real_public_ratio": round(pack.real_public_overall_ratio, 4),
    }
    audit_row = [
        {
            "run_id": run_id,
            "pack_id": pack.pack_id,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "status": "SUCCEEDED",
            "docs_count": docs_count,
            "entities_count": len(entity_rows),
            "edges_count": len(edge_rows),
            "chunks_count": len(chunk_rows),
            "embedding_model": settings.vertex_embedding_model,
            "stage_log": summary,
        }
    ]
    client.load_table_from_json(
        audit_row,
        f"{settings.gcp_project_id}.{settings.bq_dataset}.ingest_runs",
        job_config=bigquery.LoadJobConfig(
            schema=_ingest_run_schema(),
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            labels={"solution": "enterprise_graphrag"},
        ),
        location=settings.bq_location,
    ).result()

    # Verify post-swap row counts in BigQuery match LoadedPack exactly
    verify_sql = f"""
SELECT
  (SELECT COUNT(*) FROM `{settings.gcp_project_id}.{settings.bq_dataset}.entities` WHERE pack_id = @pack_id) AS entities_cnt,
  (SELECT COUNT(*) FROM `{settings.gcp_project_id}.{settings.bq_dataset}.edges` WHERE pack_id = @pack_id) AS edges_cnt,
  (SELECT COUNT(*) FROM `{settings.gcp_project_id}.{settings.bq_dataset}.source_chunks` WHERE pack_id = @pack_id) AS chunks_cnt
""".strip()
    verify_cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("pack_id", "STRING", pack.pack_id)],
        labels={"solution": "enterprise_graphrag"},
    )
    counts = dict(next(iter(client.query(verify_sql, job_config=verify_cfg, location=settings.bq_location).result())))
    if (
        counts["entities_cnt"] != len(pack.entities)
        or counts["edges_cnt"] != len(pack.edges)
        or counts["chunks_cnt"] != len(pack.chunks)
    ):
        raise RuntimeError(f"Post-load count mismatch for {pack.pack_id}: {counts}")

    print(
        f"[seed] {pack.pack_id}: entities={summary['entities']}, edges={summary['edges']}, "
        f"chunks={summary['chunks']}, dims={summary['embedding_dims']}, "
        f"embed={emb_ms:.0f}ms, load_swap={load_ms:.0f}ms"
    )
    return summary


async def run_live_smoke(settings: Settings, pack: LoadedPack, *, enable_rerank: bool) -> dict[str, Any]:
    """Run live Flat Vector RAG vs BigQuery Property Graph ISO GQL on the first golden question."""
    q = pack.benchmark.questions[0]
    req = QueryRequest(
        pack_id=pack.pack_id,
        query=q.question,
        synthesis_model=settings.vertex_model_synthesis,
        question_id=q.question_id,
        max_hops=q.hop_depth,
        direction=q.direction,
        min_confidence=0.5,
        top_k_seeds=6,
        enable_rerank=enable_rerank,
    )

    flat_engine = FlatVectorRAGEngine(settings)
    graph_engine = BigQueryGraphRAGEngine(settings)

    flat_emitter = RecordingStageEmitter()
    graph_emitter = RecordingStageEmitter()

    flat_res = await flat_engine.run_query(req, emitter=flat_emitter)
    graph_res = await graph_engine.run_query(req, emitter=graph_emitter)

    flat_recall = compute_multihop_recall(
        expected_entity_ids=q.expected_entity_ids,
        expected_edge_ids=q.expected_edge_ids,
        retrieved_entity_ids=frozenset(flat_res.retrieved_entity_ids),
        retrieved_edge_ids=frozenset(flat_res.retrieved_edge_ids),
    )
    graph_recall = compute_multihop_recall(
        expected_entity_ids=q.expected_entity_ids,
        expected_edge_ids=q.expected_edge_ids,
        retrieved_entity_ids=frozenset(graph_res.retrieved_entity_ids),
        retrieved_edge_ids=frozenset(graph_res.retrieved_edge_ids),
    )

    report = {
        "pack_id": pack.pack_id,
        "question_id": q.question_id,
        "hop_depth": q.hop_depth,
        "direction": q.direction.value,
        "flat_vector_rag": {
            "duration_ms": flat_res.total_duration_ms,
            "chunks_retrieved": len(flat_res.retrieved_chunk_ids),
            "bytes_processed": flat_res.flags.bytes_processed,
            "engine_cost_usd": flat_res.engine_cost_usd,
            "provenance_verified": flat_res.provenance.is_fully_verified,
            "recall": flat_recall,
        },
        "bq_graphrag": {
            "duration_ms": graph_res.total_duration_ms,
            "paths_found": len(graph_res.paths),
            "entities_retrieved": len(graph_res.retrieved_entity_ids),
            "edges_retrieved": len(graph_res.retrieved_edge_ids),
            "chunks_linked": len(graph_res.retrieved_chunk_ids),
            "reranker_used": graph_res.flags.reranker_used,
            "bytes_processed": graph_res.flags.bytes_processed,
            "engine_cost_usd": graph_res.engine_cost_usd,
            "provenance_verified": graph_res.provenance.is_fully_verified,
            "unverified_entities": list(graph_res.provenance.unverified_entity_ids),
            "unverified_edges": list(graph_res.provenance.unverified_edge_ids),
            "recall": graph_recall,
        },
    }
    print("[smoke-gql] Live comparison summary:")
    print(json.dumps(report, indent=2))
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed BigQuery Property Graph packs for Enterprise GraphRAG.")
    parser.add_argument(
        "--pack",
        default="all",
        help="Pack ID to seed or 'all' (default: all).",
    )
    parser.add_argument(
        "--skip-reservation",
        action="store_true",
        help="Skip checking/creating BigQuery Enterprise 0-slot reservation.",
    )
    parser.add_argument(
        "--smoke-gql",
        action="store_true",
        help="Run a live Flat Vector vs BigQuery Property Graph ISO GQL smoke query after seeding.",
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="Disable Discovery Engine Ranking API during --smoke-gql.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate packs, render DDL, and check reservation status without mutating BigQuery.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings()
    if not settings.gcp_project_id:
        raise SystemExit("ERROR: GCP_PROJECT_ID must be set in .env or environment before seeding BigQuery.")

    all_packs = load_all_packs(settings.packs_dir)
    if not all_packs:
        raise SystemExit(f"ERROR: No valid packs found under {settings.packs_dir}")

    if args.pack == "all":
        selected = list(all_packs.values())
    elif args.pack in all_packs:
        selected = [all_packs[args.pack]]
    else:
        raise SystemExit(f"ERROR: Unknown pack {args.pack!r}. Available: {sorted(all_packs.keys())}")

    ddl_path = SOLUTION_ROOT / "infra" / "sql" / "bigquery_schema.sql"
    table_ddls, graph_ddl = render_bigquery_ddl(
        ddl_path,
        project_id=settings.gcp_project_id,
        dataset=settings.bq_dataset,
        location=settings.bq_location,
        reservation_id=settings.bq_reservation_id,
    )

    print(
        f"[init] dataset={settings.bq_dataset} location={settings.bq_location} "
        f"packs={[p.pack_id for p in selected]} table_ddls={len(table_ddls)}"
    )

    client = get_bigquery_client(settings.gcp_project_id)

    if not args.skip_reservation:
        ensure_enterprise_reservation(
            client,
            project_id=settings.gcp_project_id,
            location=settings.bq_location,
            reservation_id=settings.bq_reservation_id,
            dry_run=args.dry_run,
        )

    if args.dry_run:
        print("[dry-run] Validated all selected packs and rendered BigQuery DDL cleanly. Zero mutations performed.")
        return 0

    from google.cloud import bigquery

    ddl_cfg = bigquery.QueryJobConfig(labels={"solution": "enterprise_graphrag"})
    for stmt in table_ddls:
        client.query(stmt, job_config=ddl_cfg, location=settings.bq_location).result()
    print(f"[ddl] Ensured BigQuery dataset & {len(table_ddls) - 1} tables in {settings.bq_dataset}")

    for pack in selected:
        seed_pack(client, settings=settings, pack=pack)

    # Create or refresh ISO/IEC 39075 Property Graph
    client.query(graph_ddl, job_config=ddl_cfg, location=settings.bq_location).result()
    print(f"[graph] Created/refreshed BigQuery Property Graph {settings.bq_dataset}.EnterpriseGraph")

    if args.smoke_gql:
        asyncio.run(run_live_smoke(settings, selected[0], enable_rerank=not args.no_rerank))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
