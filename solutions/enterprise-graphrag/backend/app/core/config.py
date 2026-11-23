"""Typed runtime settings and verified model catalog for Enterprise GraphRAG."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACK_ID_PATTERN = re.compile(r"^[a-z0-9_]{2,40}$")


@dataclass(frozen=True)
class ModelSpec:
    """Verified Vertex AI model metadata and published reference pricing."""

    model_id: str
    display_name: str
    family: Literal["gemini", "claude"]
    tier: Literal["ga_default", "frontier_opt_in"]
    input_per_1m_usd: float
    output_per_1m_usd: float
    location: str = "global"
    supports_adk: bool = True
    supports_temperature: bool = True


MODEL_CATALOG: dict[str, ModelSpec] = {
    "gemini-2.5-flash": ModelSpec(
        model_id="gemini-2.5-flash",
        display_name="Gemini 2.5 Flash (GA Default)",
        family="gemini",
        tier="ga_default",
        input_per_1m_usd=0.30,
        output_per_1m_usd=2.50,
    ),
    "gemini-2.5-pro": ModelSpec(
        model_id="gemini-2.5-pro",
        display_name="Gemini 2.5 Pro (GA Reasoning / Judge)",
        family="gemini",
        tier="ga_default",
        input_per_1m_usd=1.25,
        output_per_1m_usd=10.00,
    ),
    "gemini-4-hydrogen": ModelSpec(
        model_id="gemini-4-hydrogen",
        display_name="Gemini 4 Hydrogen (Fast)",
        family="gemini",
        tier="frontier_opt_in",
        input_per_1m_usd=1.50,
        output_per_1m_usd=7.50,
    ),
    "gemini-4-carbon": ModelSpec(
        model_id="gemini-4-carbon",
        display_name="Gemini 4 Carbon",
        family="gemini",
        tier="frontier_opt_in",
        input_per_1m_usd=2.00,
        output_per_1m_usd=10.00,
    ),
    "gemini-4-argon": ModelSpec(
        model_id="gemini-4-argon",
        display_name="Gemini 4 Argon (Frontier)",
        family="gemini",
        tier="frontier_opt_in",
        input_per_1m_usd=2.00,
        output_per_1m_usd=10.00,
    ),
    "claude-sonnet-4-6": ModelSpec(
        model_id="claude-sonnet-4-6",
        display_name="Claude Sonnet 4.6 (Vertex)",
        family="claude",
        tier="frontier_opt_in",
        input_per_1m_usd=3.00,
        output_per_1m_usd=15.00,
        supports_adk=False,
    ),
    "claude-opus-5-5": ModelSpec(
        model_id="claude-opus-5-5",
        display_name="Claude Opus 5.5 (Cross-Family Judge)",
        family="claude",
        tier="frontier_opt_in",
        input_per_1m_usd=4.00,
        output_per_1m_usd=20.00,
        supports_adk=False,
        supports_temperature=False,
    ),
}


def validate_model_pair(synthesis_model: str, judge_model: str) -> tuple[ModelSpec, ModelSpec]:
    """Validate both models exist in catalog and enforce judge != synthesis."""
    if synthesis_model not in MODEL_CATALOG:
        raise ValueError(f"Unsupported synthesis model: {synthesis_model!r}")
    if judge_model not in MODEL_CATALOG:
        raise ValueError(f"Unsupported judge model: {judge_model!r}")
    if synthesis_model == judge_model:
        raise ValueError(
            "Judge model must differ from synthesis model to avoid self-evaluation bias."
        )
    return MODEL_CATALOG[synthesis_model], MODEL_CATALOG[judge_model]


class Settings(BaseSettings):
    """Application configuration parsed strictly from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_version: str = "0.4.0"
    portfolio_id: str = "gcp_ai_accelerators"
    solution_id: str = "acc05"
    solution_slug: str = "enterprise_graphrag"
    app_env: str = "demo"

    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    bq_dataset: str = "acc05_enterprise_graphrag"
    bq_location: str = "US"
    bq_reservation_id: str = "acc05-graphrag-0slot"
    gcs_bucket: str = ""

    demo_mode: Literal["replay", "record", "live"] = "replay"
    replay_speed: float = Field(default=1.0, ge=0.0, le=100.0)

    graph_backend: Literal["bigquery", "spanner"] = "bigquery"
    spanner_instance_id: str = ""
    spanner_database_id: str = "acc05-graphrag-db"

    vertex_location: str = "global"
    vertex_model_synthesis: str = "gemini-2.5-flash"
    vertex_model_judge: str = "gemini-2.5-pro"
    vertex_embedding_model: str = "gemini-embedding-2"

    model_armor_location: str = "us-central1"
    model_armor_template_id: str = ""
    enable_cloud_dlp: bool = False
    require_iap_jwt: bool = False
    iap_jwt_audience: str = ""
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_bq_bytes_billed: int = Field(default=5_368_709_120, ge=10_000_000)
    max_upload_bytes: int = Field(default=10_485_760, ge=1024, le=26_214_400)

    @property
    def solution_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def packs_dir(self) -> Path:
        return self.solution_root / "packs"

    @property
    def recordings_dir(self) -> Path:
        return self.solution_root / "data" / "recordings"

    @property
    def allowed_origins_list(self) -> list[str]:
        origins = [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]
        if "*" in origins:
            raise ValueError("Wildcard '*' CORS origin is prohibited.")
        return origins

    @field_validator("bq_dataset")
    @classmethod
    def _validate_dataset_name(cls, v: str) -> str:
        if not PACK_ID_PATTERN.match(v):
            raise ValueError("bq_dataset must match ^[a-z0-9_]{2,40}$")
        return v

    @model_validator(mode="after")
    def _validate_invariants(self) -> "Settings":
        validate_model_pair(self.vertex_model_synthesis, self.vertex_model_judge)
        if self.require_iap_jwt and not self.iap_jwt_audience:
            raise ValueError("IAP_JWT_AUDIENCE is required when REQUIRE_IAP_JWT=true")
        if self.demo_mode in ("live", "record") and not self.gcp_project_id:
            raise ValueError("GCP_PROJECT_ID is required when DEMO_MODE is live or record")
        if self.graph_backend == "spanner" and self.demo_mode != "replay" and not self.spanner_instance_id:
            raise ValueError("SPANNER_INSTANCE_ID is required when GRAPH_BACKEND=spanner")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
