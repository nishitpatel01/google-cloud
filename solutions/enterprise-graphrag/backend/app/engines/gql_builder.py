"""Strictly parameterized ISO/IEC 39075 GQL & vector search query builders.

Enforces BigQuery Property Graph & Spanner Graph best practices:
  - ISO GQL only (never Cypher)
  - Explicit directed traversals (-> / <- + UNION ALL for both; never undirected scans)
  - Low-cardinality seed anchoring with ANY SHORTEST + bounded hop depth (1..4) + LIMIT <= 200
  - Strict identifier allowlist validation and query parameterization (zero SQL/GQL injection)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.models import TraversalDirection

_SAFE_DATASET_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_SAFE_GRAPH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")


@dataclass(frozen=True)
class ParameterizedQuery:
    """SQL or ISO GQL text paired with explicit bound parameter names (never string-interpolated)."""

    dialect: str
    statement: str
    required_params: tuple[str, ...]


def _validate_dataset(dataset: str) -> str:
    if not _SAFE_DATASET_RE.match(dataset):
        raise ValueError(f"Unsafe BigQuery dataset identifier: {dataset!r}")
    return dataset


def _validate_graph_name(graph_name: str) -> str:
    if not _SAFE_GRAPH_RE.match(graph_name):
        raise ValueError(f"Unsafe graph identifier: {graph_name!r}")
    return graph_name


def _validate_bounds(max_hops: int, limit: int) -> tuple[int, int]:
    if not (1 <= max_hops <= 4):
        raise ValueError(f"max_hops must be between 1 and 4, got {max_hops}")
    if not (1 <= limit <= 200):
        raise ValueError(f"limit must be between 1 and 200, got {limit}")
    return max_hops, limit


def build_bq_flat_vector_chunks_sql(*, dataset: str, top_k: int = 6) -> ParameterizedQuery:
    """Flat Vector RAG baseline SQL querying ONLY source_chunks via BigQuery VECTOR_SEARCH."""
    ds = _validate_dataset(dataset)
    if not (1 <= top_k <= 25):
        raise ValueError(f"top_k must be between 1 and 25, got {top_k}")
    sql = f"""
SELECT
  base.chunk_id,
  base.doc_name,
  base.doc_type,
  base.page_number,
  base.content,
  base.data_origin,
  base.source_license,
  base.source_url,
  distance
FROM VECTOR_SEARCH(
  (SELECT * FROM `{ds}.source_chunks` WHERE pack_id = @pack_id AND ARRAY_LENGTH(embedding) > 0),
  'embedding',
  (SELECT @query_embedding AS embedding),
  top_k => {top_k},
  distance_type => 'COSINE'
)
ORDER BY distance ASC
""".strip()
    return ParameterizedQuery(
        dialect="bigquery_sql",
        statement=sql,
        required_params=("pack_id", "query_embedding"),
    )


def build_bq_vector_seed_entities_sql(*, dataset: str, top_k: int = 6) -> ParameterizedQuery:
    """GraphRAG Stage 3: hybrid vector + canonical-name anchor Entity retrieval in BigQuery."""
    ds = _validate_dataset(dataset)
    if not (1 <= top_k <= 20):
        raise ValueError(f"top_k must be between 1 and 20, got {top_k}")
    sql = f"""
WITH vector_hits AS (
  SELECT
    base.entity_id,
    base.canonical_name,
    base.entity_type,
    base.jurisdiction,
    base.risk_tier,
    base.exposure_usd,
    base.data_origin,
    base.source_license,
    distance
  FROM VECTOR_SEARCH(
    (SELECT * FROM `{ds}.entities` WHERE pack_id = @pack_id AND ARRAY_LENGTH(embedding) > 0),
    'embedding',
    (SELECT @query_embedding AS embedding),
    top_k => {top_k},
    distance_type => 'COSINE'
  )
),
lexical_hits AS (
  SELECT
    entity_id,
    canonical_name,
    entity_type,
    jurisdiction,
    risk_tier,
    exposure_usd,
    data_origin,
    source_license,
    0.05 AS distance
  FROM `{ds}.entities`
  WHERE pack_id = @pack_id
    AND (
      (LENGTH(canonical_name) >= 3 AND STRPOS(LOWER(@query_text), LOWER(canonical_name)) > 0)
      OR (
        LENGTH(REGEXP_REPLACE(LOWER(canonical_name), r'\\s+(corporation|corp|inc|ltd|llc|co|limited|group|holdings)\\.?$', '')) >= 4
        AND STRPOS(
          LOWER(@query_text),
          REGEXP_REPLACE(LOWER(canonical_name), r'\\s+(corporation|corp|inc|ltd|llc|co|limited|group|holdings)\\.?$', '')
        ) > 0
      )
    )
  LIMIT {top_k}
)
SELECT
  entity_id,
  canonical_name,
  entity_type,
  jurisdiction,
  risk_tier,
  exposure_usd,
  data_origin,
  source_license,
  distance
FROM (
  SELECT * FROM vector_hits
  UNION ALL
  SELECT * FROM lexical_hits
)
QUALIFY ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY distance ASC) = 1
ORDER BY distance ASC
LIMIT {top_k}
""".strip()
    return ParameterizedQuery(
        dialect="bigquery_sql",
        statement=sql,
        required_params=("pack_id", "query_embedding", "query_text"),
    )


def build_bq_chunks_by_ids_sql(*, dataset: str) -> ParameterizedQuery:
    """Fetch source document chunks linked by graph edge provenance IDs."""
    ds = _validate_dataset(dataset)
    sql = f"""
SELECT
  chunk_id,
  doc_name,
  doc_type,
  page_number,
  content,
  data_origin,
  source_license,
  source_url
FROM `{ds}.source_chunks`
WHERE pack_id = @pack_id AND chunk_id IN UNNEST(@chunk_ids)
""".strip()
    return ParameterizedQuery(
        dialect="bigquery_sql",
        statement=sql,
        required_params=("pack_id", "chunk_ids"),
    )


def _gql_branch(
    *,
    graph_ref: str,
    direction: TraversalDirection,
    max_hops: int,
    limit: int,
    use_unnest: bool = True,
) -> str:
    seed_pred = "IN UNNEST(@seed_ids)" if use_unnest else "IN UNNEST(@seed_ids)"
    if direction == TraversalDirection.OUTGOING:
        match_line = f"MATCH p = ANY SHORTEST (src:Entity)-[hops:Rel]->{{1,{max_hops}}}(dst:Entity)"
        where_line = (
            f"WHERE src.pack_id = @pack_id AND dst.pack_id = @pack_id "
            f"AND src.entity_id {seed_pred}"
        )
    else:
        match_line = f"MATCH p = ANY SHORTEST (src:Entity)-[hops:Rel]->{{1,{max_hops}}}(dst:Entity)"
        where_line = (
            f"WHERE src.pack_id = @pack_id AND dst.pack_id = @pack_id "
            f"AND dst.entity_id {seed_pred}"
        )

    return f"""
GRAPH {graph_ref}
{match_line}
{where_line}
LET hop_count = PATH_LENGTH(p),
    min_confidence = MIN(hops.confidence),
    path_exposure_usd = MAX(hops.exposure_usd)
FILTER min_confidence >= @min_confidence
RETURN
  src.entity_id AS start_id,
  dst.entity_id AS end_id,
  dst.entity_type AS end_type,
  dst.risk_tier AS end_risk_tier,
  hop_count,
  path_exposure_usd,
  min_confidence,
  TO_JSON(NODES(p)) AS path_nodes_json,
  TO_JSON(EDGES(p)) AS path_edges_json
ORDER BY path_exposure_usd DESC, hop_count ASC
LIMIT {limit}
""".strip()


def build_bq_gql_traversal(
    *,
    dataset: str,
    graph_name: str = "EnterpriseGraph",
    direction: TraversalDirection = TraversalDirection.BOTH,
    max_hops: int = 4,
    limit: int = 120,
) -> ParameterizedQuery:
    """Build parameterized BigQuery Property Graph ISO GQL multi-hop traversal query."""
    ds = _validate_dataset(dataset)
    gn = _validate_graph_name(graph_name)
    hops, lim = _validate_bounds(max_hops, limit)
    graph_ref = f"{ds}.{gn}"

    if direction == TraversalDirection.BOTH:
        stmt = (
            f"SELECT * FROM (\n"
            f"  SELECT * FROM GRAPH_TABLE({graph_ref}\n"
            f"    MATCH p = ANY SHORTEST (src:Entity)-[hops:Rel]->{{1,{hops}}}(dst:Entity)\n"
            f"    WHERE src.pack_id = @pack_id AND dst.pack_id = @pack_id AND src.entity_id IN UNNEST(@seed_ids)\n"
            f"    COLUMNS (\n"
            f"      src.entity_id AS start_id,\n"
            f"      dst.entity_id AS end_id,\n"
            f"      dst.entity_type AS end_type,\n"
            f"      dst.risk_tier AS end_risk_tier,\n"
            f"      PATH_LENGTH(p) AS hop_count,\n"
            f"      MAX(hops.exposure_usd) AS path_exposure_usd,\n"
            f"      MIN(hops.confidence) AS min_confidence,\n"
            f"      TO_JSON(NODES(p)) AS path_nodes_json,\n"
            f"      TO_JSON(EDGES(p)) AS path_edges_json\n"
            f"    )\n"
            f"  ) WHERE min_confidence >= @min_confidence\n"
            f"  UNION ALL\n"
            f"  SELECT * FROM GRAPH_TABLE({graph_ref}\n"
            f"    MATCH p = ANY SHORTEST (src:Entity)-[hops:Rel]->{{1,{hops}}}(dst:Entity)\n"
            f"    WHERE src.pack_id = @pack_id AND dst.pack_id = @pack_id AND dst.entity_id IN UNNEST(@seed_ids)\n"
            f"    COLUMNS (\n"
            f"      src.entity_id AS start_id,\n"
            f"      dst.entity_id AS end_id,\n"
            f"      dst.entity_type AS end_type,\n"
            f"      dst.risk_tier AS end_risk_tier,\n"
            f"      PATH_LENGTH(p) AS hop_count,\n"
            f"      MAX(hops.exposure_usd) AS path_exposure_usd,\n"
            f"      MIN(hops.confidence) AS min_confidence,\n"
            f"      TO_JSON(NODES(p)) AS path_nodes_json,\n"
            f"      TO_JSON(EDGES(p)) AS path_edges_json\n"
            f"    )\n"
            f"  ) WHERE min_confidence >= @min_confidence\n"
            f")\nORDER BY path_exposure_usd DESC, hop_count ASC\nLIMIT {lim}"
        )
    else:
        stmt = _gql_branch(
            graph_ref=graph_ref,
            direction=direction,
            max_hops=hops,
            limit=lim,
        )

    return ParameterizedQuery(
        dialect="bigquery_iso_gql",
        statement=stmt,
        required_params=("pack_id", "seed_ids", "min_confidence"),
    )


def build_spanner_vector_seed_sql(*, top_k: int = 6) -> ParameterizedQuery:
    """Tier-2 Spanner Graph cosine-distance seed entity retrieval."""
    if not (1 <= top_k <= 20):
        raise ValueError(f"top_k must be between 1 and 20, got {top_k}")
    sql = f"""
SELECT
  entity_id,
  canonical_name,
  entity_type,
  jurisdiction,
  risk_tier,
  exposure_usd,
  data_origin,
  source_license,
  COSINE_DISTANCE(embedding, @query_embedding) AS distance
FROM entities
WHERE pack_id = @pack_id AND embedding IS NOT NULL
ORDER BY distance ASC
LIMIT {top_k}
""".strip()
    return ParameterizedQuery(
        dialect="spanner_sql",
        statement=sql,
        required_params=("pack_id", "query_embedding"),
    )


def build_spanner_gql_traversal(
    *,
    graph_name: str = "EnterpriseGraph",
    direction: TraversalDirection = TraversalDirection.BOTH,
    max_hops: int = 4,
    limit: int = 120,
) -> ParameterizedQuery:
    """Tier-2 Spanner Graph parameterized ISO GQL multi-hop traversal."""
    gn = _validate_graph_name(graph_name)
    hops, lim = _validate_bounds(max_hops, limit)

    if direction == TraversalDirection.OUTGOING:
        where_clause = "src.pack_id = @pack_id AND dst.pack_id = @pack_id AND src.entity_id IN UNNEST(@seed_ids)"
    elif direction == TraversalDirection.INCOMING:
        where_clause = "src.pack_id = @pack_id AND dst.pack_id = @pack_id AND dst.entity_id IN UNNEST(@seed_ids)"
    else:
        where_clause = (
            "src.pack_id = @pack_id AND dst.pack_id = @pack_id "
            "AND (src.entity_id IN UNNEST(@seed_ids) OR dst.entity_id IN UNNEST(@seed_ids))"
        )

    stmt = f"""
GRAPH {gn}
MATCH p = ANY SHORTEST (src:Entity)-[hops:Rel]->{{1,{hops}}}(dst:Entity)
WHERE {where_clause}
RETURN
  src.entity_id AS start_id,
  dst.entity_id AS end_id,
  dst.entity_type AS end_type,
  dst.risk_tier AS end_risk_tier,
  PATH_LENGTH(p) AS hop_count,
  TO_JSON(NODES(p)) AS path_nodes_json,
  TO_JSON(EDGES(p)) AS path_edges_json
ORDER BY hop_count ASC
LIMIT {lim}
""".strip()
    return ParameterizedQuery(
        dialect="spanner_iso_gql",
        statement=stmt,
        required_params=("pack_id", "seed_ids"),
    )
