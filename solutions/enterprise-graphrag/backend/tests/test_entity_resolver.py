"""Boundary, invariant, and scaling tests for canonical entity resolution."""

from __future__ import annotations

import time

from app.core.models import DataOrigin, EntityNode, GraphEdge, RiskTier, SourceLicense
from app.packs.entity_resolver import (
    cosine_similarity,
    normalize_entity_name,
    remap_edges,
    resolve_entities,
)


def _node(
    eid: str,
    name: str,
    etype: str = "Supplier",
    origin: DataOrigin = DataOrigin.REAL_PUBLIC,
    risk: RiskTier = RiskTier.LOW,
    exposure: float = 0.0,
    chunks: tuple[str, ...] = ("chk-1",),
) -> EntityNode:
    lic = SourceLicense.US_GOV_PUBLIC_DOMAIN if origin == DataOrigin.REAL_PUBLIC else SourceLicense.MODELED
    return EntityNode(
        pack_id="supply_chain_subtier_risk",
        entity_id=eid,
        canonical_name=name,
        entity_type=etype,
        risk_tier=risk,
        exposure_usd=exposure,
        data_origin=origin,
        source_license=lic,
        source_chunk_ids=chunks,
    )


def test_normalize_entity_name_strips_legal_suffixes() -> None:
    assert normalize_entity_name("ACME Semiconductor, Inc.") == "acme semiconductor"
    assert normalize_entity_name("Acme Semiconductor Corporation LLC") == "acme semiconductor"
    assert normalize_entity_name("Intel & Co. Ltd.") == "intel and"
    assert normalize_entity_name("Inc.") == "inc"  # single token preserved


def test_resolve_entities_empty_and_single_boundary() -> None:
    empty = resolve_entities([])
    assert empty.canonical_entities == ()
    assert empty.merged_count == 0

    one = resolve_entities([_node("e1", "Intel Corporation")])
    assert len(one.canonical_entities) == 1
    assert one.merged_count == 0
    assert one.alias_map == {"e1": "e1"}


def test_resolve_entities_merges_legal_variants_and_preserves_highest_risk() -> None:
    n1 = _node("e_b", "Yunnan Tin Co., Ltd.", risk=RiskTier.MEDIUM, exposure=100.0, chunks=("c1",))
    n2 = _node("e_a", "YUNNAN TIN CORPORATION", risk=RiskTier.CRITICAL, exposure=250.0, chunks=("c2",))
    res = resolve_entities([n1, n2])
    assert res.merged_count == 1
    assert len(res.canonical_entities) == 1
    canon = res.canonical_entities[0]
    assert canon.entity_id == "e_a"
    assert canon.risk_tier == RiskTier.CRITICAL
    assert canon.exposure_usd == 250.0
    assert canon.source_chunk_ids == ("c1", "c2")
    assert res.alias_map == {"e_a": "e_a", "e_b": "e_a"}


def test_resolve_entities_never_merges_across_different_entity_types() -> None:
    n1 = _node("e1", "Taiwan", etype="CountryOrHub")
    n2 = _node("e2", "Taiwan Inc.", etype="SmelterOrSupplier")
    res = resolve_entities([n1, n2])
    assert res.merged_count == 0
    assert len(res.canonical_entities) == 2


def test_resolve_entities_semantic_threshold_boundary() -> None:
    n1 = _node("e1", "Jiangxi Tungsten Mining")
    n2 = _node("e2", "Jiangxi Tungsten Smelting")
    n3 = _node("e3", "Jiangxi Copper Processing")
    # cos(e1, e2) = 0.96 >= 0.92 -> merge; cos(e1, e3) = 0.80 < 0.92 -> separate
    embs = {
        "e1": [1.0, 0.0, 0.0],
        "e2": [0.96, 0.28, 0.0],
        "e3": [0.80, 0.60, 0.0],
    }
    assert cosine_similarity(embs["e1"], embs["e2"]) >= 0.92
    assert cosine_similarity(embs["e1"], embs["e3"]) < 0.92
    res = resolve_entities([n1, n2, n3], embeddings=embs, cosine_threshold=0.92)
    assert res.merged_count == 1
    assert res.alias_map["e2"] == "e1"
    assert res.alias_map["e3"] == "e3"


def test_remap_edges_drops_collapsed_self_loops_and_deduplicates_parallel() -> None:
    e_self = GraphEdge(
        pack_id="supply_chain_subtier_risk",
        edge_id="ed-self",
        source_entity_id="a1",
        target_entity_id="a2",
        relation_type="SOURCED_FROM_SMELTER",
        confidence=0.9,
        evidence_excerpt="Alias self-loop after merge.",
        source_chunk_id="c1",
        source_doc_name="doc1",
        data_origin=DataOrigin.REAL_PUBLIC,
        source_license=SourceLicense.US_GOV_PUBLIC_DOMAIN,
    )
    e_low = e_self.model_copy(
        update={"edge_id": "ed-low", "source_entity_id": "a1", "target_entity_id": "b1", "confidence": 0.7}
    )
    e_high = e_self.model_copy(
        update={"edge_id": "ed-high", "source_entity_id": "a2", "target_entity_id": "b1", "confidence": 0.95}
    )
    remapped = remap_edges([e_self, e_low, e_high], {"a1": "a1", "a2": "a1", "b1": "b1"})
    assert len(remapped) == 1
    assert remapped[0].edge_id == "ed-high"
    assert remapped[0].source_entity_id == "a1"


def test_resolve_entities_linear_scale_n2000() -> None:
    nodes = [_node(f"e-{i:04d}", f"Supplier Alpha {i % 1000} Inc.") for i in range(2000)]
    t0 = time.perf_counter()
    res = resolve_entities(nodes)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert len(res.canonical_entities) == 1000
    assert res.merged_count == 1000
    assert elapsed_ms < 500.0
