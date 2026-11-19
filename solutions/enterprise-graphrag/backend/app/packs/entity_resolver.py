"""Deterministic O(N) canonical entity resolution and edge remapping.

Prevents graph fragmentation (e.g. 'Acme Corp.' vs 'ACME Corporation LLC')
during both seeded ingestion and customer BYO-document /ingest uploads.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping, Sequence

from app.core.models import DataOrigin, EntityNode, GraphEdge, RiskTier

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_MULTI_SPACE_RE = re.compile(r"\s+")

LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "llc",
        "llp",
        "ltd",
        "limited",
        "plc",
        "sa",
        "sas",
        "sarl",
        "gmbh",
        "ag",
        "co",
        "company",
        "lp",
        "nv",
        "bv",
        "pte",
        "pty",
        "holdings",
        "holding",
        "group",
        "international",
        "intl",
    }
)

_RISK_RANK: dict[RiskTier, int] = {
    RiskTier.LOW: 0,
    RiskTier.MEDIUM: 1,
    RiskTier.HIGH: 2,
    RiskTier.CRITICAL: 3,
}


def normalize_entity_name(name: str) -> str:
    """Normalize an entity name by stripping punctuation and trailing legal suffixes."""
    cleaned = unicodedata.normalize("NFKC", name).casefold()
    cleaned = cleaned.replace("&", " and ")
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned)
    tokens = [t for t in _MULTI_SPACE_RE.split(cleaned.strip()) if t]
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """Safe cosine similarity returning 0.0 on empty or zero-norm vectors."""
    if not vec_a or len(vec_a) != len(vec_b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom == 0.0:
        return 0.0
    return dot / denom


@dataclass(frozen=True)
class EntityResolutionResult:
    canonical_entities: tuple[EntityNode, ...]
    alias_map: dict[str, str]
    merged_count: int


def _merge_entity_group(group: list[EntityNode]) -> EntityNode:
    """Deterministically merge duplicate entity records preserving highest risk & provenance."""
    # Prefer REAL_PUBLIC over MODELED_BRIDGE, then lexicographically first entity_id
    ordered = sorted(
        group,
        key=lambda e: (0 if e.data_origin == DataOrigin.REAL_PUBLIC else 1, e.entity_id),
    )
    primary = ordered[0]
    if len(ordered) == 1:
        return primary

    highest_risk = max((e.risk_tier for e in ordered), key=lambda r: _RISK_RANK[r])
    max_exposure = max(e.exposure_usd for e in ordered)
    any_single_source = any(e.single_source for e in ordered)
    merged_chunks = tuple(sorted({cid for e in ordered for cid in e.source_chunk_ids}))
    merged_attrs = dict(primary.attributes)
    aliases = sorted({e.canonical_name for e in ordered if e.canonical_name != primary.canonical_name})
    if aliases:
        merged_attrs["resolved_aliases"] = aliases

    return primary.model_copy(
        update={
            "risk_tier": highest_risk,
            "exposure_usd": max_exposure,
            "single_source": any_single_source,
            "source_chunk_ids": merged_chunks,
            "attributes": merged_attrs,
        }
    )


def resolve_entities(
    entities: Sequence[EntityNode],
    *,
    embeddings: Mapping[str, Sequence[float]] | None = None,
    cosine_threshold: float = 0.92,
) -> EntityResolutionResult:
    """Deduplicate entities in O(N) via normalized buckets + optional intra-block cosine merge."""
    if not entities:
        return EntityResolutionResult(canonical_entities=(), alias_map={}, merged_count=0)

    # Phase 1: O(N) exact bucket grouping by (pack_id, entity_type, normalized_name)
    buckets: dict[tuple[str, str, str], list[EntityNode]] = defaultdict(list)
    for ent in entities:
        norm = normalize_entity_name(ent.canonical_name)
        key = (ent.pack_id, ent.entity_type, norm or ent.entity_id.casefold())
        buckets[key].append(ent)

    alias_map: dict[str, str] = {}
    phase1_entities: list[EntityNode] = []
    for key in sorted(buckets.keys()):
        group = buckets[key]
        merged = _merge_entity_group(group)
        phase1_entities.append(merged)
        for item in group:
            alias_map[item.entity_id] = merged.entity_id

    if not embeddings:
        merged_count = len(entities) - len(phase1_entities)
        return EntityResolutionResult(
            canonical_entities=tuple(phase1_entities),
            alias_map=alias_map,
            merged_count=merged_count,
        )

    # Phase 2: Bounded semantic merge strictly within same (pack_id, entity_type, first_token)
    # Keeps candidate comparisons bounded even at scale N.
    blocks: dict[tuple[str, str, str], list[EntityNode]] = defaultdict(list)
    for ent in phase1_entities:
        norm = normalize_entity_name(ent.canonical_name)
        first_tok = norm.split(" ", 1)[0] if norm else ""
        blocks[(ent.pack_id, ent.entity_type, first_tok)].append(ent)

    final_entities: list[EntityNode] = []
    phase2_remap: dict[str, str] = {}
    for bkey in sorted(blocks.keys()):
        block_items = sorted(blocks[bkey], key=lambda e: e.entity_id)
        clusters: list[list[EntityNode]] = []
        cluster_vecs: list[Sequence[float] | None] = []
        for item in block_items:
            vec = embeddings.get(item.entity_id)
            placed = False
            if vec:
                for idx, rep_vec in enumerate(cluster_vecs):
                    if rep_vec and cosine_similarity(vec, rep_vec) >= cosine_threshold:
                        clusters[idx].append(item)
                        placed = True
                        break
            if not placed:
                clusters.append([item])
                cluster_vecs.append(vec)
        for cl in clusters:
            merged = _merge_entity_group(cl)
            final_entities.append(merged)
            for item in cl:
                phase2_remap[item.entity_id] = merged.entity_id

    final_alias_map = {orig: phase2_remap.get(mid, mid) for orig, mid in alias_map.items()}
    final_entities.sort(key=lambda e: (e.pack_id, e.entity_id))
    return EntityResolutionResult(
        canonical_entities=tuple(final_entities),
        alias_map=final_alias_map,
        merged_count=len(entities) - len(final_entities),
    )


def remap_edges(
    edges: Sequence[GraphEdge],
    alias_map: Mapping[str, str],
) -> tuple[GraphEdge, ...]:
    """Remap edge endpoints via alias_map, drop collapsed self-loops, and dedup parallel edges."""
    best_by_triple: dict[tuple[str, str, str, str], GraphEdge] = {}
    for edge in edges:
        src = alias_map.get(edge.source_entity_id, edge.source_entity_id)
        dst = alias_map.get(edge.target_entity_id, edge.target_entity_id)
        if src == dst:
            continue
        remapped = edge.model_copy(
            update={"source_entity_id": src, "target_entity_id": dst}
        )
        triple = (remapped.pack_id, src, dst, remapped.relation_type)
        prev = best_by_triple.get(triple)
        if prev is None or remapped.confidence > prev.confidence:
            best_by_triple[triple] = remapped
    return tuple(sorted(best_by_triple.values(), key=lambda e: (e.pack_id, e.edge_id)))
