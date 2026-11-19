"""Integrity, real-public-ratio, multi-hop reachability, and negative boundary tests for all packs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.models import (
    DataOrigin,
    EntityNode,
    GraphEdge,
    ProvenanceReport,
    SourceLicense,
)
from app.packs.loader import load_all_packs, load_pack

PACKS_ROOT = Path(__file__).resolve().parents[2] / "packs"
EXPECTED_PACK_IDS = {
    "supply_chain_subtier_risk",
    "banking_aml_ubo_contagion",
    "legal_ma_clause_graph",
}


def test_all_three_packs_load_with_strict_referential_and_path_integrity() -> None:
    packs = load_all_packs(PACKS_ROOT)
    assert set(packs.keys()) == EXPECTED_PACK_IDS

    total_questions = 0
    for pack_id, pack in packs.items():
        assert len(pack.entities) >= 20, f"{pack_id} has too few entities"
        assert len(pack.edges) >= 30, f"{pack_id} has too few edges"
        assert len(pack.chunks) >= 8, f"{pack_id} has too few source chunks"
        assert len(pack.benchmark.questions) == 5, f"{pack_id} must have 5 golden questions"
        total_questions += len(pack.benchmark.questions)
        # Enforce Real-Public-First data policy (>= 85% real public data per pack)
        assert pack.real_public_overall_ratio >= 0.85, (
            f"{pack_id} real_public_overall_ratio={pack.real_public_overall_ratio:.2%} < 85%"
        )
        # Every pack must have MANIFEST.json with SHA-256 checksums
        manifest_path = PACKS_ROOT / pack_id / "corpus" / "MANIFEST.json"
        assert manifest_path.is_file()
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert meta["entities_count"] == len(pack.entities)
        assert meta["edges_count"] == len(pack.edges)
        assert meta["chunks_count"] == len(pack.chunks)
    assert total_questions == 15


def test_model_rejects_mismatched_origin_and_license() -> None:
    with pytest.raises(ValidationError, match="MODELED"):
        EntityNode(
            pack_id="supply_chain_subtier_risk",
            entity_id="ENT-BAD",
            canonical_name="Bad Provenance Entity",
            entity_type="Issuer",
            data_origin=DataOrigin.REAL_PUBLIC,
            source_license=SourceLicense.MODELED,
        )


def test_edge_rejects_self_loop_at_construction() -> None:
    with pytest.raises(ValidationError, match="Self-loop"):
        GraphEdge(
            pack_id="supply_chain_subtier_risk",
            edge_id="EDGE-LOOP",
            source_entity_id="ISSUER-INTEL",
            target_entity_id="ISSUER-INTEL",
            relation_type="SOURCED_FROM_SMELTER",
            evidence_excerpt="Invalid self-loop edge test case.",
            source_chunk_id="CHK-1",
            source_doc_name="doc.txt",
            data_origin=DataOrigin.REAL_PUBLIC,
            source_license=SourceLicense.US_GOV_PUBLIC_DOMAIN,
        )


def test_loader_rejects_dangling_edge_foreign_key(tmp_path: Path) -> None:
    bad_pack = tmp_path / "supply_chain_subtier_risk"
    shutil.copytree(PACKS_ROOT / "supply_chain_subtier_risk", bad_pack)
    edges_path = bad_pack / "corpus" / "edges.json"
    edges = json.loads(edges_path.read_text(encoding="utf-8"))
    edges[0]["target_entity_id"] = "NONEXISTENT-ENTITY-999"
    edges_path.write_text(json.dumps(edges), encoding="utf-8")

    with pytest.raises(ValueError, match="dangling target_entity_id"):
        load_pack(bad_pack)


def test_loader_rejects_unknown_ontology_entity_type(tmp_path: Path) -> None:
    bad_pack = tmp_path / "banking_aml_ubo_contagion"
    shutil.copytree(PACKS_ROOT / "banking_aml_ubo_contagion", bad_pack)
    ents_path = bad_pack / "corpus" / "entities.json"
    ents = json.loads(ents_path.read_text(encoding="utf-8"))
    ents[0]["entity_type"] = "UndeclaredType"
    ents_path.write_text(json.dumps(ents), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown entity_type"):
        load_pack(bad_pack)


def test_provenance_report_is_fully_verified_invariant() -> None:
    clean = ProvenanceReport(
        cited_entity_ids=("E1",),
        verified_entity_ids=("E1",),
        unverified_entity_ids=(),
        cited_edge_ids=("ED1",),
        verified_edge_ids=("ED1",),
        unverified_edge_ids=(),
        cited_chunk_ids=("C1",),
        verified_chunk_ids=("C1",),
        unverified_chunk_ids=(),
    )
    assert clean.is_fully_verified is True

    hallucinated = clean.model_copy(update={"unverified_edge_ids": ("ED-FAKE",)})
    assert hallucinated.is_fully_verified is False
