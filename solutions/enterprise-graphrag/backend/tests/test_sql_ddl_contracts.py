"""Static contract tests for BigQuery Property Graph & Spanner Graph ISO GQL DDL."""

from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[2] / "infra" / "sql"


def test_bigquery_ddl_is_valid_iso_gql_and_excludes_embeddings_from_graph() -> None:
    sql = (SQL_DIR / "bigquery_schema.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE PROPERTY GRAPH" in sql
    assert "NODE TABLES" in sql
    assert "EDGE TABLES" in sql
    assert "data_origin STRING NOT NULL" in sql
    assert "source_license STRING NOT NULL" in sql
    # Must never contain Cypher-only syntax
    assert not re.search(r"\bMERGE\s*\(", sql, re.IGNORECASE)
    # Embedding vector column must NOT appear inside CREATE OR REPLACE PROPERTY GRAPH block
    graph_block = sql.split("CREATE OR REPLACE PROPERTY GRAPH", 1)[1]
    assert "embedding" not in graph_block


def test_spanner_ddl_matches_homogeneous_entity_rel_contract() -> None:
    sql = (SQL_DIR / "spanner_schema.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE PROPERTY GRAPH EnterpriseGraph" in sql
    assert "PRIMARY KEY (pack_id, entity_id)" in sql
    assert "PRIMARY KEY (pack_id, edge_id)" in sql
    assert "CONSTRAINT fk_edge_source FOREIGN KEY" in sql
    assert "data_origin STRING(32) NOT NULL" in sql
    graph_block = sql.split("CREATE OR REPLACE PROPERTY GRAPH", 1)[1]
    assert "embedding" not in graph_block
