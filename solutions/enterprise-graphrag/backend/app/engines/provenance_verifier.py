"""Deterministic post-synthesis provenance verifier and multi-hop recall calculator."""

from __future__ import annotations

import re
from typing import AbstractSet, Iterable

from app.core.models import ProvenanceReport

_ENTITY_CITE_RE = re.compile(r"\[ENTITY:([A-Za-z0-9][A-Za-z0-9._:-]{1,95})\]")
_EDGE_CITE_RE = re.compile(r"\[EDGE:([A-Za-z0-9][A-Za-z0-9._:-]{1,95})\]")
_CHUNK_CITE_RE = re.compile(r"\[CHUNK:([A-Za-z0-9][A-Za-z0-9._:-]{1,95})\]")


def _unique_ordered(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def verify_citations(
    answer_text: str,
    *,
    allowed_entity_ids: AbstractSet[str],
    allowed_edge_ids: AbstractSet[str],
    allowed_chunk_ids: AbstractSet[str],
) -> ProvenanceReport:
    """Deterministically verify every cited entity/edge/chunk ID against retrieved ground truth."""
    cited_entities = _unique_ordered(_ENTITY_CITE_RE.findall(answer_text))
    cited_edges = _unique_ordered(_EDGE_CITE_RE.findall(answer_text))
    cited_chunks = _unique_ordered(_CHUNK_CITE_RE.findall(answer_text))

    return ProvenanceReport(
        cited_entity_ids=cited_entities,
        verified_entity_ids=tuple(i for i in cited_entities if i in allowed_entity_ids),
        unverified_entity_ids=tuple(i for i in cited_entities if i not in allowed_entity_ids),
        cited_edge_ids=cited_edges,
        verified_edge_ids=tuple(i for i in cited_edges if i in allowed_edge_ids),
        unverified_edge_ids=tuple(i for i in cited_edges if i not in allowed_edge_ids),
        cited_chunk_ids=cited_chunks,
        verified_chunk_ids=tuple(i for i in cited_chunks if i in allowed_chunk_ids),
        unverified_chunk_ids=tuple(i for i in cited_chunks if i not in allowed_chunk_ids),
    )


def compute_multihop_recall(
    *,
    expected_entity_ids: Iterable[str],
    expected_edge_ids: Iterable[str],
    retrieved_entity_ids: AbstractSet[str],
    retrieved_edge_ids: AbstractSet[str],
) -> dict[str, float]:
    """Compute exact entity recall, edge recall, and combined multi-hop path recall."""
    exp_ent = set(expected_entity_ids)
    exp_edge = set(expected_edge_ids)
    ent_recall = (len(exp_ent & retrieved_entity_ids) / len(exp_ent)) if exp_ent else 1.0
    edge_recall = (len(exp_edge & retrieved_edge_ids) / len(exp_edge)) if exp_edge else 1.0
    combined = (ent_recall + edge_recall) / 2.0
    return {
        "entity_recall": round(ent_recall, 4),
        "edge_recall": round(edge_recall, 4),
        "path_recall": round(combined, 4),
    }
