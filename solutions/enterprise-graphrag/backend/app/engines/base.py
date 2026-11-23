"""Shared engine contracts, immutable EngineResult, and GQL path parser."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.models import (
    GraphPath,
    ProvenanceReport,
    StageEvent,
    TraversalDirection,
)
from app.core.pricing import TokenUsage
from app.core.stage_emitter import StageEmitter


@dataclass(frozen=True)
class EngineFlags:
    """Honest runtime service attribution flags surfaced directly in the UI."""

    graph_backend_used: str  # "none" | "bigquery_property_graph" | "spanner_graph"
    reranker_used: bool = False
    model_armor_checked: bool = False
    bq_cache_hit: bool = False
    bytes_processed: int = 0


@dataclass(frozen=True)
class QueryRequest:
    """Validated query parameters passed into every engine."""

    pack_id: str
    query: str
    synthesis_model: str
    question_id: str | None = None
    max_hops: int = 4
    direction: TraversalDirection = TraversalDirection.BOTH
    min_confidence: float = 0.5
    top_k_seeds: int = 6
    enable_rerank: bool = True


@dataclass(frozen=True)
class EngineResult:
    """Pure, immutable output of an engine execution — never holds hardcoded eval metrics."""

    engine_id: str
    engine_label: str
    pack_id: str
    query: str
    synthesis_model: str
    answer: str
    paths: tuple[GraphPath, ...]
    retrieved_entity_ids: tuple[str, ...]
    retrieved_edge_ids: tuple[str, ...]
    retrieved_chunk_ids: tuple[str, ...]
    retrieved_nodes: tuple[dict[str, Any], ...]
    retrieved_edges: tuple[dict[str, Any], ...]
    retrieved_chunks: tuple[dict[str, Any], ...]
    provenance: ProvenanceReport
    usage: TokenUsage
    engine_cost_usd: float
    total_duration_ms: float
    flags: EngineFlags
    stage_log: tuple[StageEvent, ...] = field(default_factory=tuple)


class BaseGraphEngine(ABC):
    """Contract enforced by flat_vector_rag, bq_graphrag, and spanner_graphrag."""

    engine_id: str
    engine_label: str
    stages: tuple[str, ...]

    @abstractmethod
    async def run_query(
        self,
        req: QueryRequest,
        *,
        emitter: StageEmitter,
    ) -> EngineResult: ...


def _unwrap_json_array(raw: Any) -> list[dict[str, Any]]:
    """Decode BigQuery/Spanner TO_JSON(...) column safely into a list of dicts."""
    if raw is None:
        return []
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            props = item.get("properties")
            out.append(props if isinstance(props, dict) else item)
    return out


def parse_gql_path_rows(
    rows: list[dict[str, Any]],
) -> tuple[
    tuple[GraphPath, ...],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Normalize ISO GQL rows containing path_nodes_json & path_edges_json into typed GraphPaths."""
    paths: list[GraphPath] = []
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges_by_id: dict[str, dict[str, Any]] = {}
    seen_path_sigs: set[tuple[str, ...]] = set()

    for idx, row in enumerate(rows):
        raw_nodes = _unwrap_json_array(row.get("path_nodes_json"))
        raw_edges = _unwrap_json_array(row.get("path_edges_json"))
        node_ids: list[str] = []
        edge_ids: list[str] = []

        for n in raw_nodes:
            nid = str(n.get("entity_id", "")).strip()
            if nid:
                node_ids.append(nid)
                nodes_by_id.setdefault(nid, n)

        for e in raw_edges:
            eid = str(e.get("edge_id", "")).strip()
            if eid:
                edge_ids.append(eid)
                edges_by_id.setdefault(eid, e)

        if len(node_ids) < 2 or not edge_ids:
            continue
        sig = tuple(edge_ids)
        if sig in seen_path_sigs:
            continue
        seen_path_sigs.add(sig)

        hop_count = min(4, max(1, int(row.get("hop_count") or len(edge_ids))))
        exposure = float(row.get("path_exposure_usd") or 0.0)
        min_conf = float(row.get("min_confidence") if row.get("min_confidence") is not None else 1.0)
        paths.append(
            GraphPath(
                path_id=f"path-{idx + 1:03d}",
                hop_count=hop_count,
                node_ids=tuple(node_ids),
                edge_ids=tuple(edge_ids),
                path_exposure_usd=max(0.0, exposure),
                min_confidence=min(1.0, max(0.0, min_conf)),
            )
        )

    return tuple(paths), nodes_by_id, edges_by_id
