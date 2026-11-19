"""Pack manifest, ontology, corpus, and benchmark loader with strict graph invariants."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from app.core.models import (
    BenchmarkSuite,
    DataOrigin,
    EntityNode,
    GoldenQuestion,
    GraphEdge,
    PackManifest,
    SourceChunk,
    TraversalDirection,
)


@dataclass(frozen=True)
class LoadedPack:
    """Validated, immutable, in-memory representation of a single industry pack."""

    manifest: PackManifest
    entities: tuple[EntityNode, ...]
    edges: tuple[GraphEdge, ...]
    chunks: tuple[SourceChunk, ...]
    benchmark: BenchmarkSuite
    entities_by_id: dict[str, EntityNode]
    edges_by_id: dict[str, GraphEdge]
    chunks_by_id: dict[str, SourceChunk]

    @property
    def pack_id(self) -> str:
        return self.manifest.pack_id

    @property
    def real_public_entity_ratio(self) -> float:
        if not self.entities:
            return 0.0
        real = sum(1 for e in self.entities if e.data_origin == DataOrigin.REAL_PUBLIC)
        return real / len(self.entities)

    @property
    def real_public_edge_ratio(self) -> float:
        if not self.edges:
            return 0.0
        real = sum(1 for e in self.edges if e.data_origin == DataOrigin.REAL_PUBLIC)
        return real / len(self.edges)

    @property
    def real_public_chunk_ratio(self) -> float:
        if not self.chunks:
            return 0.0
        real = sum(1 for c in self.chunks if c.data_origin == DataOrigin.REAL_PUBLIC)
        return real / len(self.chunks)

    @property
    def real_public_overall_ratio(self) -> float:
        total = len(self.entities) + len(self.edges) + len(self.chunks)
        if total == 0:
            return 0.0
        real = (
            sum(1 for e in self.entities if e.data_origin == DataOrigin.REAL_PUBLIC)
            + sum(1 for e in self.edges if e.data_origin == DataOrigin.REAL_PUBLIC)
            + sum(1 for c in self.chunks if c.data_origin == DataOrigin.REAL_PUBLIC)
        )
        return real / total


def find_reachable_from_seeds(
    edges: Iterable[GraphEdge],
    seed_ids: Iterable[str],
    *,
    max_hops: int = 4,
    direction: TraversalDirection = TraversalDirection.OUTGOING,
) -> tuple[set[str], set[str]]:
    """Bounded BFS returning (reachable_node_ids, traversed_edge_ids) within max_hops."""
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in edges:
        if direction in (TraversalDirection.OUTGOING, TraversalDirection.BOTH):
            adj[edge.source_entity_id].append((edge.target_entity_id, edge.edge_id))
        if direction in (TraversalDirection.INCOMING, TraversalDirection.BOTH):
            adj[edge.target_entity_id].append((edge.source_entity_id, edge.edge_id))

    visited_depth: dict[str, int] = {}
    traversed_edges: set[str] = set()
    queue: deque[tuple[str, int]] = deque()

    for sid in seed_ids:
        visited_depth[sid] = 0
        queue.append((sid, 0))

    while queue:
        node, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for nxt, eid in adj.get(node, ()):
            traversed_edges.add(eid)
            next_depth = depth + 1
            if nxt not in visited_depth or next_depth < visited_depth[nxt]:
                visited_depth[nxt] = next_depth
                queue.append((nxt, next_depth))

    return set(visited_depth.keys()), traversed_edges


def _validate_pack_integrity(
    manifest: PackManifest,
    entities: tuple[EntityNode, ...],
    edges: tuple[GraphEdge, ...],
    chunks: tuple[SourceChunk, ...],
    benchmark: BenchmarkSuite,
) -> tuple[dict[str, EntityNode], dict[str, GraphEdge], dict[str, SourceChunk]]:
    """Enforce zero duplicate keys, strict ontology membership, FK & path validity."""
    pack_id = manifest.pack_id
    allowed_nodes = manifest.ontology.allowed_node_types
    allowed_edges = manifest.ontology.allowed_edge_types

    chunks_by_id: dict[str, SourceChunk] = {}
    for ch in chunks:
        if ch.pack_id != pack_id:
            raise ValueError(f"Chunk {ch.chunk_id} has pack_id={ch.pack_id!r}, expected {pack_id!r}")
        if ch.chunk_id in chunks_by_id:
            raise ValueError(f"Duplicate chunk_id {ch.chunk_id!r} in pack {pack_id!r}")
        chunks_by_id[ch.chunk_id] = ch

    entities_by_id: dict[str, EntityNode] = {}
    for ent in entities:
        if ent.pack_id != pack_id:
            raise ValueError(f"Entity {ent.entity_id} has pack_id={ent.pack_id!r}, expected {pack_id!r}")
        if ent.entity_id in entities_by_id:
            raise ValueError(f"Duplicate entity_id {ent.entity_id!r} in pack {pack_id!r}")
        if ent.entity_type not in allowed_nodes:
            raise ValueError(
                f"Entity {ent.entity_id} has unknown entity_type={ent.entity_type!r}; "
                f"allowed={sorted(allowed_nodes)}"
            )
        for cid in ent.source_chunk_ids:
            if cid not in chunks_by_id:
                raise ValueError(f"Entity {ent.entity_id} references missing chunk_id={cid!r}")
        entities_by_id[ent.entity_id] = ent

    edges_by_id: dict[str, GraphEdge] = {}
    for edge in edges:
        if edge.pack_id != pack_id:
            raise ValueError(f"Edge {edge.edge_id} has pack_id={edge.pack_id!r}, expected {pack_id!r}")
        if edge.edge_id in edges_by_id:
            raise ValueError(f"Duplicate edge_id {edge.edge_id!r} in pack {pack_id!r}")
        if edge.relation_type not in allowed_edges:
            raise ValueError(
                f"Edge {edge.edge_id} has unknown relation_type={edge.relation_type!r}; "
                f"allowed={sorted(allowed_edges)}"
            )
        if edge.source_entity_id not in entities_by_id:
            raise ValueError(f"Edge {edge.edge_id} has dangling source_entity_id={edge.source_entity_id!r}")
        if edge.target_entity_id not in entities_by_id:
            raise ValueError(f"Edge {edge.edge_id} has dangling target_entity_id={edge.target_entity_id!r}")
        if edge.source_chunk_id not in chunks_by_id:
            raise ValueError(f"Edge {edge.edge_id} has dangling source_chunk_id={edge.source_chunk_id!r}")
        edges_by_id[edge.edge_id] = edge

    if benchmark.pack_id != pack_id:
        raise ValueError(f"Benchmark pack_id={benchmark.pack_id!r} != {pack_id!r}")

    seen_qids: set[str] = set()
    for q in benchmark.questions:
        if q.question_id in seen_qids:
            raise ValueError(f"Duplicate question_id={q.question_id!r} in pack {pack_id!r}")
        seen_qids.add(q.question_id)
        _verify_golden_question(q, entities_by_id, edges_by_id, edges)

    return entities_by_id, edges_by_id, chunks_by_id


def _verify_golden_question(
    q: GoldenQuestion,
    entities_by_id: dict[str, EntityNode],
    edges_by_id: dict[str, GraphEdge],
    all_edges: tuple[GraphEdge, ...],
) -> None:
    for sid in q.seed_entity_ids:
        if sid not in entities_by_id:
            raise ValueError(f"Question {q.question_id} references unknown seed_entity_id={sid!r}")
    for eid in q.expected_entity_ids:
        if eid not in entities_by_id:
            raise ValueError(f"Question {q.question_id} references unknown expected_entity_id={eid!r}")
    for edge_id in q.expected_edge_ids:
        if edge_id not in edges_by_id:
            raise ValueError(f"Question {q.question_id} references unknown expected_edge_id={edge_id!r}")

    reachable_nodes, reachable_edges = find_reachable_from_seeds(
        all_edges,
        q.seed_entity_ids,
        max_hops=q.hop_depth,
        direction=q.direction,
    )
    missing_nodes = set(q.expected_entity_ids) - reachable_nodes
    if missing_nodes:
        raise ValueError(
            f"Question {q.question_id} expected nodes {sorted(missing_nodes)} not reachable "
            f"within {q.hop_depth} hops ({q.direction.value}) from {q.seed_entity_ids}"
        )
    missing_edges = set(q.expected_edge_ids) - reachable_edges
    if missing_edges:
        raise ValueError(
            f"Question {q.question_id} expected edges {sorted(missing_edges)} not traversed "
            f"within {q.hop_depth} hops ({q.direction.value}) from {q.seed_entity_ids}"
        )


def load_pack(pack_dir: Path) -> LoadedPack:
    """Load and rigorously validate one pack directory."""
    manifest_raw = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    manifest = PackManifest.model_validate(manifest_raw)

    corpus_dir = pack_dir / "corpus"
    entities_raw = json.loads((corpus_dir / "entities.json").read_text(encoding="utf-8"))
    edges_raw = json.loads((corpus_dir / "edges.json").read_text(encoding="utf-8"))
    chunks_raw = json.loads((corpus_dir / "chunks.json").read_text(encoding="utf-8"))
    bench_raw = json.loads((pack_dir / "benchmark.json").read_text(encoding="utf-8"))

    entities = tuple(EntityNode.model_validate(item) for item in entities_raw)
    edges = tuple(GraphEdge.model_validate(item) for item in edges_raw)
    chunks = tuple(SourceChunk.model_validate(item) for item in chunks_raw)
    benchmark = BenchmarkSuite.model_validate(bench_raw)

    entities_by_id, edges_by_id, chunks_by_id = _validate_pack_integrity(
        manifest, entities, edges, chunks, benchmark
    )
    return LoadedPack(
        manifest=manifest,
        entities=entities,
        edges=edges,
        chunks=chunks,
        benchmark=benchmark,
        entities_by_id=entities_by_id,
        edges_by_id=edges_by_id,
        chunks_by_id=chunks_by_id,
    )


def load_all_packs(packs_root: Path) -> dict[str, LoadedPack]:
    """Discover and validate all pack directories containing pack.yaml."""
    loaded: dict[str, LoadedPack] = {}
    if not packs_root.is_dir():
        return loaded
    for child in sorted(packs_root.iterdir()):
        if child.is_dir() and (child / "pack.yaml").is_file():
            pack = load_pack(child)
            loaded[pack.pack_id] = pack
    return loaded
