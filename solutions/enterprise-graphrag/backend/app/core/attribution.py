"""Canonical GCP resource naming and 6-key FinOps/discovery label attribution (ACC-05).

Every BigQuery dataset, table, query/load job, GCS object, Vertex AI request, and
Terraform resource stamps the exact same 6-key label schema so Cloud Asset Inventory,
Cloud Billing, and INFORMATION_SCHEMA.JOBS isolate all resources for a solution in one query.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.config import Settings

PORTFOLIO_ID = "gcp_ai_accelerators"
SOLUTION_ID = "acc05"
SOLUTION_SLUG = "enterprise_graphrag"

_GCP_LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_GCP_LABEL_VAL_RE = re.compile(r"^[a-z0-9_-]{0,63}$")


def sanitize_label_value(raw: str) -> str:
    """Normalize an arbitrary identifier into a valid GCP label value ([a-z0-9_-], <=63)."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", (raw or "").strip().lower()).strip("_")
    return cleaned[:63] or "unknown"


def validate_gcp_labels(labels: dict[str, str]) -> dict[str, str]:
    """Enforce strict GCP label key/value constraints before passing to any GCP API."""
    if len(labels) > 64:
        raise ValueError(f"GCP supports at most 64 labels per resource, got {len(labels)}")
    out: dict[str, str] = {}
    for k, v in labels.items():
        if not _GCP_LABEL_KEY_RE.fullmatch(k):
            raise ValueError(f"Invalid GCP label key: {k!r}")
        if not _GCP_LABEL_VAL_RE.fullmatch(v):
            raise ValueError(f"Invalid GCP label value for {k!r}: {v!r}")
        out[k] = v
    return out


def gcp_resource_labels(
    settings: Settings,
    *,
    managed_by: str = "app_runtime",
) -> dict[str, str]:
    """Return the mandatory 6-key GCP label schema for static resources (datasets, tables, buckets)."""
    tier = (
        "tier2_enterprise"
        if settings.graph_backend == "spanner" or bool(settings.spanner_instance_id.strip())
        else "tier1_serverless"
    )
    return validate_gcp_labels(
        {
            "portfolio": sanitize_label_value(settings.portfolio_id),
            "solution_id": sanitize_label_value(settings.solution_id),
            "solution": sanitize_label_value(settings.solution_slug),
            "tier": tier,
            "managed_by": sanitize_label_value(managed_by),
            "env": sanitize_label_value(settings.app_env),
        }
    )


def gcp_job_labels(
    settings: Settings,
    *,
    component: str,
    managed_by: str = "app_runtime",
    extra: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return full 6-key labels + component tag for BigQuery jobs and Vertex AI requests."""
    base = gcp_resource_labels(settings, managed_by=managed_by)
    base["component"] = sanitize_label_value(component)
    if extra:
        for k, v in extra.items():
            key = sanitize_label_value(str(k))
            if key and key[0].isalpha():
                base[key] = sanitize_label_value(str(v))
    return validate_gcp_labels(base)
