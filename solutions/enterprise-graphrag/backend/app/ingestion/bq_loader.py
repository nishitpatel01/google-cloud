"""Atomic GCS upload and BigQuery staging-table MERGE loader for /ingest uploads.

Never uses BigQuery streaming inserts (avoids 90-minute DML streaming-buffer locks).
Every uploaded object uses a server-generated UUID key and Content-Disposition: attachment.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.attribution import gcp_job_labels, gcp_resource_labels
from app.core.config import Settings
from app.core.gcp_clients import embed_texts_threaded, get_bigquery_client
from app.ingestion.multimodal_extractor import ExtractionOutcome
from app.ingestion.upload_validator import ValidatedUpload


def upload_raw_to_gcs(
    *,
    settings: Settings,
    upload: ValidatedUpload,
    storage_client: Any | None = None,
) -> str:
    """Persist validated raw upload bytes to GCS under an opaque UUID key (attachment disposition)."""
    bucket_name = settings.gcs_bucket.strip()
    if not bucket_name:
        return ""
    if storage_client is None:
        from google.cloud import storage

        storage_client = storage.Client(project=settings.gcp_project_id)
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(upload.gcs_object_key)
    blob.content_disposition = f'attachment; filename="{upload.display_name}"'
    blob.metadata = {
        **gcp_resource_labels(settings, managed_by="app_runtime"),
        "pack_id": upload.pack_id,
        "sha256": upload.sha256,
        "display_name": upload.display_name,
    }
    blob.upload_from_string(upload.content, content_type=upload.canonical_mime)
    return f"gs://{bucket_name}/{upload.gcs_object_key}"


def _merge_via_staging(
    client: Any,
    *,
    settings: Settings,
    table_name: str,
    key_col: str,
    run_id: str,
    rows: list[dict[str, Any]],
    schema: list[Any],
) -> None:
    """Load rows into an ephemeral staging table and MERGE atomically into target BigQuery table."""
    if not rows:
        return
    from google.cloud import bigquery

    project_id = settings.gcp_project_id
    dataset = settings.bq_dataset
    location = settings.bq_location
    labels = gcp_job_labels(settings, component="ingest_merge")

    stg_ref = f"{project_id}.{dataset}._stg_upl_{table_name}_{run_id[:10]}"
    target_ref = f"{project_id}.{dataset}.{table_name}"
    cols = [field.name for field in schema]
    update_assigns = ", ".join(f"T.{c} = S.{c}" for c in cols if c not in ("pack_id", key_col))
    insert_cols = ", ".join(cols)
    insert_vals = ", ".join(f"S.{c}" for c in cols)

    load_cfg = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        labels=labels,
    )
    try:
        client.load_table_from_json(rows, stg_ref, job_config=load_cfg, location=location).result()
        merge_sql = f"""
MERGE `{target_ref}` AS T
USING `{stg_ref}` AS S
ON T.pack_id = S.pack_id AND T.{key_col} = S.{key_col}
WHEN MATCHED THEN UPDATE SET {update_assigns}
WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
""".strip()
        client.query(
            merge_sql,
            job_config=bigquery.QueryJobConfig(labels=labels),
            location=location,
        ).result()
    finally:
        client.delete_table(stg_ref, not_found_ok=True)


def persist_extracted_graph(
    *,
    settings: Settings,
    upload: ValidatedUpload,
    outcome: ExtractionOutcome,
    bq_client: Any | None = None,
    storage_client: Any | None = None,
) -> dict[str, Any]:
    """Embed extracted entities/chunks, store raw bytes in GCS, and MERGE atomically into BigQuery."""
    if not settings.gcp_project_id:
        raise RuntimeError("GCP_PROJECT_ID is required to persist ingested graph records to BigQuery")

    from google.cloud import bigquery

    run_id = f"upl-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc)
    gcs_uri = upload_raw_to_gcs(settings=settings, upload=upload, storage_client=storage_client)

    ent_texts = [
        f"{e.canonical_name} | type={e.entity_type} | jurisdiction={e.jurisdiction} | risk={e.risk_tier.value}"
        for e in outcome.entities
    ]
    chunk_texts = [f"{c.doc_name} (p.{c.page_number})\n{c.content}"[:5000] for c in outcome.chunks]

    ent_vecs = (
        embed_texts_threaded(
            project_id=settings.gcp_project_id,
            model_id=settings.vertex_embedding_model,
            texts=ent_texts,
            task_type="RETRIEVAL_DOCUMENT",
            location=settings.vertex_location,
        )
        if ent_texts
        else []
    )
    chunk_vecs = (
        embed_texts_threaded(
            project_id=settings.gcp_project_id,
            model_id=settings.vertex_embedding_model,
            texts=chunk_texts,
            task_type="RETRIEVAL_DOCUMENT",
            location=settings.vertex_location,
        )
        if chunk_texts
        else []
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    entity_rows = [
        {
            "pack_id": e.pack_id,
            "entity_id": e.entity_id,
            "canonical_name": e.canonical_name,
            "entity_type": e.entity_type,
            "jurisdiction": e.jurisdiction,
            "risk_tier": e.risk_tier.value,
            "exposure_usd": float(e.exposure_usd),
            "single_source": bool(e.single_source),
            "data_origin": e.data_origin.value,
            "source_license": e.source_license.value,
            "source_url": gcs_uri or e.source_url,
            "attributes_json": e.attributes,
            "source_chunk_ids": list(e.source_chunk_ids),
            "embedding": vec,
            "ingest_run_id": run_id,
            "updated_at": now_iso,
        }
        for e, vec in zip(outcome.entities, ent_vecs, strict=True)
    ]
    edge_rows = [
        {
            "pack_id": ed.pack_id,
            "edge_id": ed.edge_id,
            "source_entity_id": ed.source_entity_id,
            "target_entity_id": ed.target_entity_id,
            "relation_type": ed.relation_type,
            "weight": float(ed.weight),
            "exposure_usd": float(ed.exposure_usd),
            "confidence": float(ed.confidence),
            "evidence_excerpt": ed.evidence_excerpt,
            "source_chunk_id": ed.source_chunk_id,
            "source_doc_name": ed.source_doc_name,
            "data_origin": ed.data_origin.value,
            "source_license": ed.source_license.value,
            "source_url": gcs_uri or ed.source_url,
            "ingest_run_id": run_id,
        }
        for ed in outcome.edges
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
            "source_url": gcs_uri or ch.source_url,
            "embedding": vec,
            "ingest_run_id": run_id,
        }
        for ch, vec in zip(outcome.chunks, chunk_vecs, strict=True)
    ]

    entity_schema = [
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
    edge_schema = [
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
    chunk_schema = [
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

    client = bq_client or get_bigquery_client(settings.gcp_project_id)
    _merge_via_staging(
        client,
        settings=settings,
        table_name="entities",
        key_col="entity_id",
        run_id=run_id,
        rows=entity_rows,
        schema=entity_schema,
    )
    _merge_via_staging(
        client,
        settings=settings,
        table_name="edges",
        key_col="edge_id",
        run_id=run_id,
        rows=edge_rows,
        schema=edge_schema,
    )
    _merge_via_staging(
        client,
        settings=settings,
        table_name="source_chunks",
        key_col="chunk_id",
        run_id=run_id,
        rows=chunk_rows,
        schema=chunk_schema,
    )

    # Record audit log row
    finished_at = datetime.now(timezone.utc)
    audit_schema = [
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
    client.load_table_from_json(
        [
            {
                "run_id": run_id,
                "pack_id": upload.pack_id,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "status": "SUCCEEDED",
                "docs_count": 1,
                "entities_count": len(entity_rows),
                "edges_count": len(edge_rows),
                "chunks_count": len(chunk_rows),
                "embedding_model": settings.vertex_embedding_model,
                "stage_log": {
                    "doc": upload.display_name,
                    "gcs_key": upload.gcs_object_key,
                    "merged_entities": outcome.merged_entities_count,
                },
            }
        ],
        f"{settings.gcp_project_id}.{settings.bq_dataset}.ingest_runs",
        job_config=bigquery.LoadJobConfig(
            schema=audit_schema,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            labels=gcp_job_labels(settings, component="ingest_audit"),
        ),
        location=settings.bq_location,
    ).result()

    return {
        "ingest_run_id": run_id,
        "gcs_uri": gcs_uri,
        "persisted_entities": len(entity_rows),
        "persisted_edges": len(edge_rows),
        "persisted_chunks": len(chunk_rows),
    }
