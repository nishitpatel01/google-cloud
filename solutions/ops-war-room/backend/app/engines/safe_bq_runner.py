"""Read-only, strictly parameterized BigQuery telemetry & intel runner with FinOps labeling."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.gcp_clients import GCPClientPool
from app.core.models import (
    INCIDENT_ID_RE,
    PACK_SLUG_RE,
    DataOrigin,
    IntelRecord,
    PackConfig,
    RunbookRecord,
    TelemetryEvent,
)

MAX_BYTES_BILLED_DEFAULT = 100 * 1024 * 1024  # 100 MiB guardrail per read-only query
_SAFE_IDENT_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXECUTE)\b|;",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryAuditResult:
    rows: tuple[Any, ...]
    bytes_billed: int
    parameterized_sql: str
    source: str  # "bigquery_live" | "local_pack_corpus"


def assert_read_only_sql(sql: str) -> None:
    """Fail-closed SQL guardrail ensuring only single-statement SELECT queries execute."""
    stripped = (sql or "").strip()
    if not stripped.upper().startswith("SELECT"):
        raise ValueError("Only read-only SELECT queries are permitted in SafeBigQueryRunner.")
    if _FORBIDDEN_SQL_RE.search(stripped):
        raise ValueError("Forbidden DDL/DML keyword or stacked statement detected in SQL.")


class SafeBigQueryRunner:
    """Executes strictly parameterized BigQuery queries with FinOps labels and byte caps."""

    def __init__(
        self,
        settings: Settings,
        gcp_clients: GCPClientPool | None = None,
        *,
        max_bytes_billed: int = MAX_BYTES_BILLED_DEFAULT,
    ) -> None:
        self._settings = settings
        self._gcp_clients = gcp_clients
        self._max_bytes_billed = max_bytes_billed

    def _table_ref(self, table_name: str) -> str:
        if not _SAFE_IDENT_RE.match(table_name):
            raise ValueError(f"Invalid table identifier: {table_name!r}")
        dataset = self._settings.bq_dataset
        if not _SAFE_IDENT_RE.match(dataset):
            raise ValueError(f"Invalid dataset identifier: {dataset!r}")
        project = self._gcp_clients.project_id if self._gcp_clients else "PROJECT_ID"
        return f"`{project}.{dataset}.{table_name}`"

    def fetch_incident_telemetry(
        self,
        pack: PackConfig,
        incident_id: str,
        *,
        use_live_bq: bool = False,
    ) -> QueryAuditResult:
        if not PACK_SLUG_RE.match(pack.pack_id):
            raise ValueError(f"Invalid pack_id: {pack.pack_id!r}")
        if not INCIDENT_ID_RE.match(incident_id):
            raise ValueError(f"Invalid incident_id: {incident_id!r}")

        table_ref = self._table_ref("telemetry_events")
        sql = (
            f"SELECT event_id, pack_id, incident_id, CAST(event_ts AS STRING) AS timestamp_iso, "
            f"source_table, actor, resource, action, status, severity, raw_message, "
            f"structured_fields_json, contains_prompt_injection, data_origin "
            f"FROM {table_ref} "
            f"WHERE pack_id = @pack_id AND incident_id = @incident_id "
            f"ORDER BY event_ts ASC LIMIT 100"
        )
        assert_read_only_sql(sql)

        if not use_live_bq:
            matched = tuple(
                ev for ev in pack.telemetry_events if ev.incident_id == incident_id
            )
            return QueryAuditResult(
                rows=matched,
                bytes_billed=0,
                parameterized_sql=sql,
                source="local_pack_corpus",
            )

        if self._gcp_clients is None:
            raise RuntimeError("GCPClientPool required for live BigQuery telemetry query.")

        from google.cloud import bigquery

        bq = self._gcp_clients.bigquery()
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pack_id", "STRING", pack.pack_id),
                bigquery.ScalarQueryParameter("incident_id", "STRING", incident_id),
            ],
            maximum_bytes_billed=self._max_bytes_billed,
            labels=self._settings.gcp_labels(component="telemetry_sql_agent"),
        )
        job = bq.query(sql, job_config=job_config)
        rows = list(job.result())
        events: list[TelemetryEvent] = []
        for r in rows:
            fields = json.loads(r["structured_fields_json"]) if r["structured_fields_json"] else {}
            events.append(
                TelemetryEvent(
                    event_id=str(r["event_id"]),
                    pack_id=str(r["pack_id"]),
                    incident_id=str(r["incident_id"]),
                    timestamp_iso=str(r["timestamp_iso"]),
                    source_table=str(r["source_table"]),
                    actor=str(r["actor"]),
                    resource=str(r["resource"]),
                    action=str(r["action"]),
                    status=str(r["status"]),
                    severity=str(r["severity"]),
                    raw_message=str(r["raw_message"]),
                    structured_fields=fields,
                    contains_prompt_injection=bool(r["contains_prompt_injection"]),
                    data_origin=DataOrigin(r["data_origin"]),
                )
            )
        return QueryAuditResult(
            rows=tuple(events),
            bytes_billed=int(job.total_bytes_billed or 0),
            parameterized_sql=sql,
            source="bigquery_live",
        )

    def fetch_pack_intel(
        self,
        pack: PackConfig,
        *,
        use_live_bq: bool = False,
    ) -> QueryAuditResult:
        if not PACK_SLUG_RE.match(pack.pack_id):
            raise ValueError(f"Invalid pack_id: {pack.pack_id!r}")

        table_ref = self._table_ref("intel_catalog")
        sql = (
            f"SELECT intel_id, pack_id, intel_type, title, summary, source_name, "
            f"source_url, license_name, metadata_json, data_origin "
            f"FROM {table_ref} WHERE pack_id = @pack_id ORDER BY intel_id ASC LIMIT 100"
        )
        assert_read_only_sql(sql)

        if not use_live_bq:
            return QueryAuditResult(
                rows=pack.intel_records,
                bytes_billed=0,
                parameterized_sql=sql,
                source="local_pack_corpus",
            )

        if self._gcp_clients is None:
            raise RuntimeError("GCPClientPool required for live BigQuery intel query.")

        from google.cloud import bigquery

        bq = self._gcp_clients.bigquery()
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pack_id", "STRING", pack.pack_id),
            ],
            maximum_bytes_billed=self._max_bytes_billed,
            labels=self._settings.gcp_labels(component="intel_mcp_agent"),
        )
        job = bq.query(sql, job_config=job_config)
        records = tuple(
            IntelRecord(
                intel_id=str(r["intel_id"]),
                pack_id=str(r["pack_id"]),
                intel_type=r["intel_type"],
                title=str(r["title"]),
                summary=str(r["summary"]),
                source_name=str(r["source_name"]),
                source_url=str(r["source_url"]),
                license_name=str(r["license_name"]),
                metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                data_origin=DataOrigin(r["data_origin"]),
            )
            for r in job.result()
        )
        return QueryAuditResult(
            rows=records,
            bytes_billed=int(job.total_bytes_billed or 0),
            parameterized_sql=sql,
            source="bigquery_live",
        )

    def fetch_pack_runbooks(
        self,
        pack: PackConfig,
        *,
        use_live_bq: bool = False,
    ) -> QueryAuditResult:
        if not PACK_SLUG_RE.match(pack.pack_id):
            raise ValueError(f"Invalid pack_id: {pack.pack_id!r}")

        table_ref = self._table_ref("runbooks")
        sql = (
            f"SELECT runbook_id, pack_id, title, category, content, safe_commands, data_origin "
            f"FROM {table_ref} WHERE pack_id = @pack_id ORDER BY runbook_id ASC LIMIT 50"
        )
        assert_read_only_sql(sql)

        if not use_live_bq:
            return QueryAuditResult(
                rows=pack.runbooks,
                bytes_billed=0,
                parameterized_sql=sql,
                source="local_pack_corpus",
            )

        if self._gcp_clients is None:
            raise RuntimeError("GCPClientPool required for live BigQuery runbooks query.")

        from google.cloud import bigquery

        bq = self._gcp_clients.bigquery()
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pack_id", "STRING", pack.pack_id),
            ],
            maximum_bytes_billed=self._max_bytes_billed,
            labels=self._settings.gcp_labels(component="blast_radius_agent"),
        )
        job = bq.query(sql, job_config=job_config)
        records = tuple(
            RunbookRecord(
                runbook_id=str(r["runbook_id"]),
                pack_id=str(r["pack_id"]),
                title=str(r["title"]),
                category=str(r["category"]),
                content=str(r["content"]),
                safe_commands=tuple(r["safe_commands"] or []),
                data_origin=DataOrigin(r["data_origin"]),
            )
            for r in job.result()
        )
        return QueryAuditResult(
            rows=records,
            bytes_billed=int(job.total_bytes_billed or 0),
            parameterized_sql=sql,
            source="bigquery_live",
        )
