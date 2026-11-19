"""Unit tests for parameterized ISO GQL builders, provenance verification, and GQL parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core.models import TraversalDirection
from app.engines.base import parse_gql_path_rows
from app.engines.gql_builder import (
    build_bq_chunks_by_ids_sql,
    build_bq_flat_vector_chunks_sql,
    build_bq_gql_traversal,
    build_bq_vector_seed_entities_sql,
    build_spanner_gql_traversal,
    build_spanner_vector_seed_sql,
)
from app.engines.provenance_verifier import compute_multihop_recall, verify_citations

SOLUTION_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = SOLUTION_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from seed_data import render_bigquery_ddl, validate_gcp_identifiers  # noqa: E402


def test_bq_gql_builder_outgoing_incoming_and_both_directions() -> None:
    out_q = build_bq_gql_traversal(
        dataset="enterprise_graphrag",
        direction=TraversalDirection.OUTGOING,
        max_hops=3,
        limit=50,
    )
    assert out_q.dialect == "bigquery_iso_gql"
    assert "GRAPH enterprise_graphrag.EnterpriseGraph" in out_q.statement
    assert "MATCH p = ANY SHORTEST (src:Entity)-[hops:Rel]->{1,3}(dst:Entity)" in out_q.statement
    assert "src.entity_id IN UNNEST(@seed_ids)" in out_q.statement
    assert "FILTER min_confidence >= @min_confidence" in out_q.statement
    assert "LIMIT 50" in out_q.statement
    assert "-(hops" not in out_q.statement
    assert "MERGE (" not in out_q.statement

    in_q = build_bq_gql_traversal(
        dataset="enterprise_graphrag",
        direction=TraversalDirection.INCOMING,
        max_hops=4,
        limit=100,
    )
    assert "dst.entity_id IN UNNEST(@seed_ids)" in in_q.statement
    assert "->{" in in_q.statement

    both_q = build_bq_gql_traversal(
        dataset="enterprise_graphrag",
        direction=TraversalDirection.BOTH,
        max_hops=3,
        limit=120,
    )
    assert "UNION ALL" in both_q.statement
    assert both_q.statement.count("GRAPH_TABLE(enterprise_graphrag.EnterpriseGraph") == 2
    assert "src.entity_id IN UNNEST(@seed_ids)" in both_q.statement
    assert "dst.entity_id IN UNNEST(@seed_ids)" in both_q.statement
    assert "LIMIT 120" in both_q.statement


def test_spanner_and_vector_builders_are_strictly_parameterized() -> None:
    flat_sql = build_bq_flat_vector_chunks_sql(dataset="graphrag", top_k=6)
    assert "VECTOR_SEARCH(" in flat_sql.statement
    assert "`graphrag.source_chunks`" in flat_sql.statement
    assert "@pack_id" in flat_sql.statement
    assert "@query_embedding" in flat_sql.statement

    seed_sql = build_bq_vector_seed_entities_sql(dataset="graphrag", top_k=5)
    assert "`graphrag.entities`" in seed_sql.statement
    assert "top_k => 5" in seed_sql.statement
    assert "@query_text" in seed_sql.statement
    assert "lexical_hits" in seed_sql.statement

    chunks_sql = build_bq_chunks_by_ids_sql(dataset="graphrag")
    assert "chunk_id IN UNNEST(@chunk_ids)" in chunks_sql.statement

    sp_seed = build_spanner_vector_seed_sql(top_k=4)
    assert "COSINE_DISTANCE(embedding, @query_embedding)" in sp_seed.statement
    assert "LIMIT 4" in sp_seed.statement

    sp_gql = build_spanner_gql_traversal(direction=TraversalDirection.BOTH, max_hops=4, limit=80)
    assert sp_gql.dialect == "spanner_iso_gql"
    assert "GRAPH EnterpriseGraph" in sp_gql.statement
    assert "MATCH p = ANY SHORTEST (src:Entity)-[hops:Rel]->{1,4}(dst:Entity)" in sp_gql.statement
    assert "LIMIT 80" in sp_gql.statement


def test_gql_builders_reject_injection_and_out_of_bounds_inputs() -> None:
    with pytest.raises(ValueError):
        build_bq_gql_traversal(dataset="graphrag; DROP TABLE entities")
    with pytest.raises(ValueError):
        build_bq_gql_traversal(dataset="graphrag", graph_name="Graph; --")
    with pytest.raises(ValueError):
        build_bq_gql_traversal(dataset="graphrag", max_hops=0)
    with pytest.raises(ValueError):
        build_bq_gql_traversal(dataset="graphrag", max_hops=5)
    with pytest.raises(ValueError):
        build_bq_gql_traversal(dataset="graphrag", limit=0)
    with pytest.raises(ValueError):
        build_bq_gql_traversal(dataset="graphrag", limit=201)
    with pytest.raises(ValueError):
        build_bq_flat_vector_chunks_sql(dataset="graphrag", top_k=26)
    with pytest.raises(ValueError):
        build_bq_vector_seed_entities_sql(dataset="graphrag", top_k=0)
    with pytest.raises(ValueError):
        build_spanner_vector_seed_sql(top_k=21)


def test_verify_citations_and_multihop_recall() -> None:
    answer = (
        "Risk flows from [ENTITY:ENT_A] via [EDGE:EDG_1] to [ENTITY:ENT_B] "
        "(duplicate [ENTITY:ENT_A]) backed by [CHUNK:CHK_1], plus a hallucinated "
        "[ENTITY:ENT_FAKE], [EDGE:EDG_FAKE], and [CHUNK:CHK_FAKE]."
    )
    report = verify_citations(
        answer,
        allowed_entity_ids={"ENT_A", "ENT_B"},
        allowed_edge_ids={"EDG_1"},
        allowed_chunk_ids={"CHK_1"},
    )
    assert report.cited_entity_ids == ("ENT_A", "ENT_B", "ENT_FAKE")
    assert report.verified_entity_ids == ("ENT_A", "ENT_B")
    assert report.unverified_entity_ids == ("ENT_FAKE",)
    assert report.verified_edge_ids == ("EDG_1",)
    assert report.unverified_edge_ids == ("EDG_FAKE",)
    assert report.verified_chunk_ids == ("CHK_1",)
    assert report.unverified_chunk_ids == ("CHK_FAKE",)
    assert report.is_fully_verified is False

    clean_report = verify_citations(
        "Verified [ENTITY:ENT_A] --[EDGE:EDG_1]--> [ENTITY:ENT_B] [CHUNK:CHK_1].",
        allowed_entity_ids={"ENT_A", "ENT_B"},
        allowed_edge_ids={"EDG_1"},
        allowed_chunk_ids={"CHK_1"},
    )
    assert clean_report.is_fully_verified is True

    recall = compute_multihop_recall(
        expected_entity_ids=("ENT_A", "ENT_B", "ENT_C"),
        expected_edge_ids=("EDG_1", "EDG_2"),
        retrieved_entity_ids={"ENT_A", "ENT_B"},
        retrieved_edge_ids={"EDG_1", "EDG_2"},
    )
    assert recall["entity_recall"] == pytest.approx(0.6667, abs=1e-4)
    assert recall["edge_recall"] == 1.0
    assert recall["path_recall"] == pytest.approx(0.8333, abs=1e-4)


def test_parse_gql_path_rows_unwraps_json_properties_and_deduplicates() -> None:
    nodes_payload = [
        {"identifier": "id1", "labels": ["Entity"], "properties": {"entity_id": "E1", "canonical_name": "Alpha"}},
        {"identifier": "id2", "labels": ["Entity"], "properties": {"entity_id": "E2", "canonical_name": "Beta"}},
        {"identifier": "id3", "labels": ["Entity"], "properties": {"entity_id": "E3", "canonical_name": "Gamma"}},
    ]
    edges_payload = [
        {
            "identifier": "r1",
            "labels": ["Rel"],
            "properties": {
                "edge_id": "R1",
                "source_entity_id": "E1",
                "target_entity_id": "E2",
                "relation_type": "SUPPLIES",
                "source_chunk_id": "C1",
            },
        },
        {
            "identifier": "r2",
            "labels": ["Rel"],
            "properties": {
                "edge_id": "R2",
                "source_entity_id": "E2",
                "target_entity_id": "E3",
                "relation_type": "REFINES",
                "source_chunk_id": "C2",
            },
        },
    ]
    rows = [
        {
            "hop_count": 2,
            "path_exposure_usd": 1_250_000.0,
            "min_confidence": 0.92,
            "path_nodes_json": json.dumps(nodes_payload),
            "path_edges_json": json.dumps(edges_payload),
        },
        # Duplicate signature using pre-parsed lists (must deduplicate)
        {
            "hop_count": 2,
            "path_exposure_usd": 1_250_000.0,
            "min_confidence": 0.92,
            "path_nodes_json": nodes_payload,
            "path_edges_json": edges_payload,
        },
        # Malformed row (must be skipped safely)
        {
            "hop_count": 1,
            "path_nodes_json": "[]",
            "path_edges_json": None,
        },
    ]

    paths, nodes_by_id, edges_by_id = parse_gql_path_rows(rows)
    assert len(paths) == 1
    assert paths[0].node_ids == ("E1", "E2", "E3")
    assert paths[0].edge_ids == ("R1", "R2")
    assert paths[0].hop_count == 2
    assert paths[0].path_exposure_usd == 1_250_000.0
    assert paths[0].min_confidence == 0.92
    assert set(nodes_by_id.keys()) == {"E1", "E2", "E3"}
    assert set(edges_by_id.keys()) == {"R1", "R2"}


def test_seed_ddl_renderer_and_identifier_validation() -> None:
    ddl_path = SOLUTION_ROOT / "infra" / "sql" / "bigquery_schema.sql"
    table_ddls, graph_ddl = render_bigquery_ddl(
        ddl_path,
        project_id="demo-project-123",
        dataset="enterprise_graphrag",
        location="US",
        reservation_id="graphrag-enterprise-0slot",
    )
    assert len(table_ddls) == 6  # schema + 5 tables
    assert "CREATE SCHEMA IF NOT EXISTS `demo-project-123.enterprise_graphrag`" in table_ddls[0]
    assert "CREATE OR REPLACE PROPERTY GRAPH enterprise_graphrag.EnterpriseGraph" in graph_ddl
    assert "${PROJECT_ID}" not in graph_ddl
    assert "${BQ_DATASET}" not in graph_ddl

    with pytest.raises(ValueError):
        validate_gcp_identifiers(
            project_id="bad_project; DROP",
            dataset="graphrag",
            location="US",
            reservation_id="res-1",
        )
