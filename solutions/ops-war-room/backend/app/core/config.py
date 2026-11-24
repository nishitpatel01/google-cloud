"""Runtime configuration, model catalog, and judge!=synthesis enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.attribution import (
    DEFAULT_BQ_DATASET,
    DEFAULT_FIRESTORE_DATABASE,
    DEFAULT_WORKFLOW_O2C,
    DEFAULT_WORKFLOW_SECOPS,
    DEFAULT_WORKFLOW_SRE,
    build_gcp_labels,
)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    display_name: str
    provider: Literal["vertex_gemini", "vertex_eval", "vertex_anthropic"]
    location: str
    input_cost_per_1m_usd: float
    output_cost_per_1m_usd: float
    supports_synthesis: bool = True
    supports_judge: bool = True
    notes: str = ""


MODEL_CATALOG: dict[str, ModelSpec] = {
    "gemini-2.5-flash": ModelSpec(
        model_id="gemini-2.5-flash",
        display_name="Gemini 2.5 Flash (GA Default)",
        provider="vertex_gemini",
        location="us-central1",
        input_cost_per_1m_usd=0.30,
        output_cost_per_1m_usd=2.50,
        supports_synthesis=True,
        supports_judge=True,
        notes="Low-latency parallel specialist & coordinator default",
    ),
    "gemini-2.5-pro": ModelSpec(
        model_id="gemini-2.5-pro",
        display_name="Gemini 2.5 Pro (Deep Reasoning / Judge)",
        provider="vertex_gemini",
        location="us-central1",
        input_cost_per_1m_usd=1.25,
        output_cost_per_1m_usd=10.00,
        supports_synthesis=True,
        supports_judge=True,
        notes="High-precision critique & independent judge",
    ),
    "vertex-genai-eval": ModelSpec(
        model_id="vertex-genai-eval",
        display_name="Vertex AI GenAI Evaluation (GA)",
        provider="vertex_eval",
        location="us-central1",
        input_cost_per_1m_usd=1.25,
        output_cost_per_1m_usd=10.00,
        supports_synthesis=False,
        supports_judge=True,
        notes="Managed :evaluateInstances PointwiseMetric autorater",
    ),
    "claude-opus-5-5": ModelSpec(
        model_id="claude-opus-5-5",
        display_name="Anthropic Claude Opus 5.5 (Cross-Family Judge)",
        provider="vertex_anthropic",
        location="global",
        input_cost_per_1m_usd=5.00,
        output_cost_per_1m_usd=25.00,
        supports_synthesis=False,
        supports_judge=True,
        notes="Independent cross-family Vertex Model Garden judge",
    ),
}


def validate_model_pair(synthesis_model: str, judge_model: str | None) -> tuple[str, str | None]:
    """Validate synthesis and judge models, strictly enforcing judge != synthesis."""
    synth = MODEL_CATALOG.get(synthesis_model)
    if synth is None or not synth.supports_synthesis:
        raise ValueError(f"Unsupported synthesis model: {synthesis_model!r}")
    if judge_model is None or judge_model == "":
        return synthesis_model, None
    judge = MODEL_CATALOG.get(judge_model)
    if judge is None or not judge.supports_judge:
        raise ValueError(f"Unsupported judge model: {judge_model!r}")
    if judge_model == synthesis_model:
        raise ValueError(
            f"Independent evaluation requires judge_model != synthesis_model (both set to {synthesis_model!r})"
        )
    return synthesis_model, judge_model


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_version: str = "0.1.0"
    demo_mode: Literal["replay", "live"] = "replay"
    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    bq_location: str = "US"
    bq_dataset: str = DEFAULT_BQ_DATASET
    firestore_database: str = DEFAULT_FIRESTORE_DATABASE
    gcs_bucket: str = ""
    model_armor_template_id: str = ""
    model_armor_location: str = "us-central1"
    workflow_location: str = "us-central1"
    workflow_o2c: str = DEFAULT_WORKFLOW_O2C
    workflow_secops: str = DEFAULT_WORKFLOW_SECOPS
    workflow_sre: str = DEFAULT_WORKFLOW_SRE

    gcp_env: str = "dev"
    gcp_tier: Literal["tier1_serverless", "tier2_enterprise"] = "tier1_serverless"
    gcp_managed_by: str = "app_runtime"

    require_iap_auth: bool = False
    iap_expected_audience: str = ""
    allowed_origins: str = (
        "http://127.0.0.1:5173,http://localhost:5173,"
        "http://127.0.0.1:8000,http://localhost:8000"
    )
    rate_limit_per_min: int = Field(default=60, ge=5, le=1000)

    solution_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[3]
    )

    @field_validator("demo_mode", mode="before")
    @classmethod
    def _normalize_mode(cls, v: str) -> str:
        return (v or "replay").strip().lower()

    @property
    def live_capable(self) -> bool:
        return bool(self.gcp_project_id.strip())

    @property
    def model_armor_configured(self) -> bool:
        return bool(self.gcp_project_id.strip() and self.model_armor_template_id.strip())

    @property
    def packs_dir(self) -> Path:
        return self.solution_root / "packs"

    @property
    def recordings_dir(self) -> Path:
        return self.solution_root / "data" / "recordings"

    @property
    def releases_path(self) -> Path:
        return self.solution_root / "data" / "releases.json"

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        if any(o == "*" for o in origins):
            raise ValueError("Wildcard CORS origin '*' is forbidden")
        return origins

    def gcp_labels(self, *, component: str | None = None) -> dict[str, str]:
        return build_gcp_labels(
            tier=self.gcp_tier,
            managed_by=self.gcp_managed_by,
            env=self.gcp_env,
            component=component,
        )

    def workflow_name_for_pack(self, pack_id: str) -> str:
        mapping = {
            "supply_chain_o2c_saga": self.workflow_o2c,
            "secops_siem_triage": self.workflow_secops,
            "sre_outage_commander": self.workflow_sre,
        }
        return mapping.get(pack_id, self.workflow_o2c)
