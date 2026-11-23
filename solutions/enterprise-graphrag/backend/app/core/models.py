"""Canonical domain models and strict invariants for Enterprise GraphRAG.

Follows Functional Core principles: parse untrusted data at module boundaries
into immutable, validated domain objects where illegal states are unrepresentable.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,95}$")
PACK_SLUG_RE = re.compile(r"^[a-z0-9_]{2,40}$")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class DataOrigin(str, Enum):
    """Explicit provenance classification surfaced in BigQuery and UI badges."""

    REAL_PUBLIC = "REAL_PUBLIC"
    MODELED_BRIDGE = "MODELED_BRIDGE"
    CUSTOMER_UPLOAD = "CUSTOMER_UPLOAD"


class SourceLicense(str, Enum):
    """Permissive licenses verified for MIT-repo redistribution (plus private BYO uploads)."""

    US_GOV_PUBLIC_DOMAIN = "US_GOV_PUBLIC_DOMAIN"
    CC0_1_0 = "CC0_1_0"
    CC_BY_4_0 = "CC_BY_4_0"
    MODELED = "MODELED"
    CUSTOMER_PRIVATE = "CUSTOMER_PRIVATE"


class RiskTier(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TraversalDirection(str, Enum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BOTH = "both"


class EntityNode(BaseModel):
    """A homogeneous graph vertex scoped to a pack_id."""

    model_config = ConfigDict(frozen=True)

    pack_id: str
    entity_id: str
    canonical_name: str = Field(min_length=1, max_length=240)
    entity_type: str = Field(min_length=1, max_length=64)
    jurisdiction: str = Field(default="UNKNOWN", max_length=64)
    risk_tier: RiskTier = RiskTier.LOW
    exposure_usd: float = Field(default=0.0, ge=0.0)
    single_source: bool = False
    data_origin: DataOrigin
    source_license: SourceLicense
    source_url: str = Field(default="", max_length=512)
    source_chunk_ids: tuple[str, ...] = ()
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("pack_id")
    @classmethod
    def _check_pack_id(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id: {v!r}")
        return v

    @field_validator("entity_id")
    @classmethod
    def _check_entity_id(cls, v: str) -> str:
        if not SAFE_ID_RE.match(v):
            raise ValueError(f"Invalid entity_id: {v!r}")
        return v

    @model_validator(mode="after")
    def _check_origin_license_consistency(self) -> "EntityNode":
        if self.data_origin == DataOrigin.MODELED_BRIDGE and self.source_license != SourceLicense.MODELED:
            raise ValueError("MODELED_BRIDGE records must use source_license=MODELED")
        if self.data_origin == DataOrigin.REAL_PUBLIC and self.source_license in (
            SourceLicense.MODELED,
            SourceLicense.CUSTOMER_PRIVATE,
        ):
            raise ValueError("REAL_PUBLIC records cannot use MODELED or CUSTOMER_PRIVATE licenses")
        if self.data_origin == DataOrigin.CUSTOMER_UPLOAD and self.source_license != SourceLicense.CUSTOMER_PRIVATE:
            raise ValueError("CUSTOMER_UPLOAD records must use source_license=CUSTOMER_PRIVATE")
        return self


class GraphEdge(BaseModel):
    """A directed relationship carrying explicit chunk provenance."""

    model_config = ConfigDict(frozen=True)

    pack_id: str
    edge_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str = Field(min_length=1, max_length=64)
    weight: float = Field(default=1.0, gt=0.0)
    exposure_usd: float = Field(default=0.0, ge=0.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_excerpt: str = Field(min_length=10, max_length=1500)
    source_chunk_id: str
    source_doc_name: str = Field(min_length=1, max_length=240)
    data_origin: DataOrigin
    source_license: SourceLicense
    source_url: str = Field(default="", max_length=512)

    @field_validator("pack_id")
    @classmethod
    def _check_pack_id(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id: {v!r}")
        return v

    @field_validator("edge_id", "source_entity_id", "target_entity_id", "source_chunk_id")
    @classmethod
    def _check_ids(cls, v: str) -> str:
        if not SAFE_ID_RE.match(v):
            raise ValueError(f"Invalid identifier: {v!r}")
        return v

    @model_validator(mode="after")
    def _check_no_self_loop_and_license(self) -> "GraphEdge":
        if self.source_entity_id == self.target_entity_id:
            raise ValueError(f"Self-loop edge rejected: {self.edge_id} -> {self.source_entity_id}")
        if self.data_origin == DataOrigin.MODELED_BRIDGE and self.source_license != SourceLicense.MODELED:
            raise ValueError("MODELED_BRIDGE edges must use source_license=MODELED")
        if self.data_origin == DataOrigin.REAL_PUBLIC and self.source_license in (
            SourceLicense.MODELED,
            SourceLicense.CUSTOMER_PRIVATE,
        ):
            raise ValueError("REAL_PUBLIC edges cannot use MODELED or CUSTOMER_PRIVATE licenses")
        if self.data_origin == DataOrigin.CUSTOMER_UPLOAD and self.source_license != SourceLicense.CUSTOMER_PRIVATE:
            raise ValueError("CUSTOMER_UPLOAD edges must use source_license=CUSTOMER_PRIVATE")
        return self


class SourceChunk(BaseModel):
    """A document passage shared by Flat Vector RAG and GraphRAG evidence inspection."""

    model_config = ConfigDict(frozen=True)

    pack_id: str
    chunk_id: str
    doc_name: str = Field(min_length=1, max_length=240)
    doc_type: str = Field(min_length=1, max_length=64)
    page_number: int = Field(default=1, ge=1)
    content: str = Field(min_length=20, max_length=8000)
    data_origin: DataOrigin
    source_license: SourceLicense
    source_url: str = Field(default="", max_length=512)

    @field_validator("pack_id")
    @classmethod
    def _check_pack_id(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id: {v!r}")
        return v

    @field_validator("chunk_id")
    @classmethod
    def _check_chunk_id(cls, v: str) -> str:
        if not SAFE_ID_RE.match(v):
            raise ValueError(f"Invalid chunk_id: {v!r}")
        return v


class OntologyNodeType(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    color: str
    shape: Literal["circle", "hexagon", "square", "diamond"] = "circle"
    is_revenue_bearing: bool = False

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str) -> str:
        if not HEX_COLOR_RE.match(v):
            raise ValueError(f"Color must be #RRGGBB, got {v!r}")
        return v


class OntologyEdgeType(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    directed: bool = True
    propagates_risk: bool = True


class OntologySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_types: tuple[OntologyNodeType, ...] = Field(min_length=2)
    edge_types: tuple[OntologyEdgeType, ...] = Field(min_length=1)
    dlp_info_types: tuple[str, ...] = ()

    @property
    def allowed_node_types(self) -> frozenset[str]:
        return frozenset(nt.type for nt in self.node_types)

    @property
    def allowed_edge_types(self) -> frozenset[str]:
        return frozenset(et.type for et in self.edge_types)


class PackManifest(BaseModel):
    """Declarative industry pack definition loaded from packs/<id>/pack.yaml."""

    model_config = ConfigDict(frozen=True)

    pack_id: str
    title: str = Field(min_length=3, max_length=120)
    subtitle: str = Field(min_length=5, max_length=240)
    industry: str = Field(min_length=2, max_length=80)
    persona: str = Field(min_length=2, max_length=100)
    manual_baseline_cost_usd: float = Field(gt=0.0)
    manual_baseline_minutes: int = Field(gt=0)
    default_max_hops: int = Field(default=4, ge=1, le=4)
    primary_sources: tuple[str, ...] = Field(min_length=1)
    ontology: OntologySpec

    @field_validator("pack_id")
    @classmethod
    def _check_pack_id(cls, v: str) -> str:
        if not PACK_SLUG_RE.match(v):
            raise ValueError(f"Invalid pack_id: {v!r}")
        return v


class GoldenQuestion(BaseModel):
    """Verified multi-hop benchmark question anchored to real pack entities/edges."""

    model_config = ConfigDict(frozen=True)

    question_id: str
    question: str = Field(min_length=10, max_length=600)
    hop_depth: int = Field(ge=2, le=4)
    direction: TraversalDirection = TraversalDirection.OUTGOING
    seed_entity_ids: tuple[str, ...] = Field(min_length=1)
    expected_entity_ids: tuple[str, ...] = Field(min_length=2)
    expected_edge_ids: tuple[str, ...] = Field(min_length=2)
    expected_answer_points: tuple[str, ...] = Field(min_length=2)
    why_flat_vector_misses: str = Field(min_length=15, max_length=500)


class BenchmarkSuite(BaseModel):
    model_config = ConfigDict(frozen=True)

    pack_id: str
    questions: tuple[GoldenQuestion, ...] = Field(min_length=5)


class GraphPath(BaseModel):
    """A single traversed multi-hop path returned by BigQuery or Spanner ISO GQL."""

    model_config = ConfigDict(frozen=True)

    path_id: str
    hop_count: int = Field(ge=1, le=4)
    node_ids: tuple[str, ...] = Field(min_length=2)
    edge_ids: tuple[str, ...] = Field(min_length=1)
    path_exposure_usd: float = Field(default=0.0, ge=0.0)
    min_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ProvenanceReport(BaseModel):
    """Deterministic post-synthesis audit of cited entity/edge/chunk IDs."""

    model_config = ConfigDict(frozen=True)

    cited_entity_ids: tuple[str, ...]
    verified_entity_ids: tuple[str, ...]
    unverified_entity_ids: tuple[str, ...]
    cited_edge_ids: tuple[str, ...]
    verified_edge_ids: tuple[str, ...]
    unverified_edge_ids: tuple[str, ...]
    cited_chunk_ids: tuple[str, ...]
    verified_chunk_ids: tuple[str, ...]
    unverified_chunk_ids: tuple[str, ...]

    @property
    def is_fully_verified(self) -> bool:
        return (
            len(self.unverified_entity_ids) == 0
            and len(self.unverified_edge_ids) == 0
            and len(self.unverified_chunk_ids) == 0
        )


# Tagged union for StageEmitter SSE events (illegal combinations unrepresentable)
class StageStartedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_type: Literal["stage_started"] = "stage_started"
    stage: str
    label: str
    service: str
    request: dict[str, Any] = Field(default_factory=dict)


class StageCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_type: Literal["stage_completed"] = "stage_completed"
    stage: str
    service_used: str
    duration_ms: float = Field(ge=0.0)
    payload: dict[str, Any] = Field(default_factory=dict)


class StageFailedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_type: Literal["stage_failed"] = "stage_failed"
    stage: str
    error_type: str
    error_message: str
    duration_ms: float = Field(ge=0.0)


class StageSkippedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_type: Literal["stage_skipped"] = "stage_skipped"
    stage: str
    reason: str


StageEvent = Annotated[
    Union[StageStartedEvent, StageCompletedEvent, StageFailedEvent, StageSkippedEvent],
    Field(discriminator="event_type"),
]
