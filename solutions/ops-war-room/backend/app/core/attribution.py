"""4-Layer GCP Resource Naming & Mandatory 6-Key FinOps Attribution for ACC-04."""

from __future__ import annotations

import re

SOLUTION_ID = "acc04"
SOLUTION_SLUG = "ops_war_room"
SOLUTION_HYPHEN = "ops-war-room"
PORTFOLIO_LABEL = "gcp_ai_accelerators"

# Deterministic resource names for ACC-04
DEFAULT_BQ_DATASET = "acc04_ops_war_room"
DEFAULT_FIRESTORE_DATABASE = "acc04-ops-war-room"
DEFAULT_MODEL_ARMOR_TEMPLATE = "acc04-ops-war-room-guardrail"
DEFAULT_AR_REPO = "acc04-ops-war-room"
DEFAULT_UI_SERVICE = "acc04-warroom-ui"
DEFAULT_REPLAY_SERVICE = "acc04-warroom-replay"
DEFAULT_WORKFLOW_O2C = "acc04-o2c-saga"
DEFAULT_WORKFLOW_SECOPS = "acc04-secops-contain"
DEFAULT_WORKFLOW_SRE = "acc04-sre-rollback"

MANDATORY_LABEL_KEYS: tuple[str, ...] = (
    "portfolio",
    "solution_id",
    "solution",
    "tier",
    "managed_by",
    "env",
)

_GCP_LABEL_VAL_RE = re.compile(r"^[a-z0-9_-]{1,63}$")


def sanitize_gcp_label_value(raw: str, *, fallback: str = "unknown") -> str:
    """Normalize any string into a valid lowercase GCP label value (max 63 chars)."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", (raw or "").strip().lower()).strip("_-")
    if not cleaned:
        return fallback
    return cleaned[:63]


def build_gcp_labels(
    *,
    tier: str = "tier1_serverless",
    managed_by: str = "app_runtime",
    env: str = "dev",
    component: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build mandatory 6-key GCP FinOps attribution labels (+ optional component)."""
    labels: dict[str, str] = {
        "portfolio": PORTFOLIO_LABEL,
        "solution_id": SOLUTION_ID,
        "solution": SOLUTION_SLUG,
        "tier": sanitize_gcp_label_value(tier, fallback="tier1_serverless"),
        "managed_by": sanitize_gcp_label_value(managed_by, fallback="app_runtime"),
        "env": sanitize_gcp_label_value(env, fallback="dev"),
    }
    if component:
        labels["component"] = sanitize_gcp_label_value(component, fallback="core")
    if extra:
        for k, v in extra.items():
            key = sanitize_gcp_label_value(k, fallback="meta")
            if key in MANDATORY_LABEL_KEYS:
                continue
            labels[key] = sanitize_gcp_label_value(v)
    return labels


def validate_gcp_labels(labels: dict[str, str]) -> list[str]:
    """Return list of schema violations for a GCP label dict (empty list = valid)."""
    errors: list[str] = []
    for key in MANDATORY_LABEL_KEYS:
        if key not in labels:
            errors.append(f"Missing mandatory label key: {key}")
    if labels.get("portfolio") != PORTFOLIO_LABEL:
        errors.append(f"portfolio must equal {PORTFOLIO_LABEL!r}")
    if labels.get("solution_id") != SOLUTION_ID:
        errors.append(f"solution_id must equal {SOLUTION_ID!r}")
    if labels.get("solution") != SOLUTION_SLUG:
        errors.append(f"solution must equal {SOLUTION_SLUG!r}")
    for k, v in labels.items():
        if not _GCP_LABEL_VAL_RE.match(k):
            errors.append(f"Invalid label key format: {k!r}")
        if not _GCP_LABEL_VAL_RE.match(v):
            errors.append(f"Invalid label value format for {k!r}: {v!r}")
    return errors


def default_gcs_bucket_name(project_id: str) -> str:
    """Deterministic GCS bucket name tied to project and acc04 prefix."""
    safe_proj = re.sub(r"[^a-z0-9-]+", "-", project_id.strip().lower()).strip("-")
    return f"{safe_proj}-acc04-ops-war-room"[:63]
