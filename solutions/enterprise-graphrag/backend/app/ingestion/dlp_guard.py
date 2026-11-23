"""Google Cloud DLP (Sensitive Data Protection) inspection preview for /ingest uploads.

Never fabricates findings locally: when ENABLE_CLOUD_DLP=false, reports enabled=False honestly.
Masks any finding quotes so raw PII/PHI is never echoed back to API responses or logs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Sequence

from app.core.config import Settings

DEFAULT_DLP_INFO_TYPES: tuple[str, ...] = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "PERSON_NAME",
    "US_SOCIAL_SECURITY_NUMBER",
)
_MAX_DLP_CHARS = 50_000

_DLP_LOCK = threading.Lock()
_DLP_CLIENT: Any = None


def _get_dlp_client() -> Any:
    global _DLP_CLIENT
    with _DLP_LOCK:
        if _DLP_CLIENT is None:
            from google.cloud import dlp_v2

            _DLP_CLIENT = dlp_v2.DlpServiceClient()
        return _DLP_CLIENT


@dataclass(frozen=True)
class DlpInspectionSummary:
    """PII-safe Cloud DLP inspection outcome for an uploaded document."""

    enabled: bool
    inspected_chars: int
    findings_count: int
    info_types_found: tuple[str, ...]
    counts_by_type: dict[str, int]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "inspected_chars": self.inspected_chars,
            "findings_count": self.findings_count,
            "info_types_found": list(self.info_types_found),
            "counts_by_type": dict(self.counts_by_type),
            "reason": self.reason,
        }


def inspect_text_with_dlp(
    *,
    settings: Settings,
    text: str,
    info_types: Sequence[str] = (),
    dlp_client: Any | None = None,
) -> DlpInspectionSummary:
    """Inspect extracted document text via Google Cloud DLP without echoing raw PII."""
    if not settings.enable_cloud_dlp:
        return DlpInspectionSummary(
            enabled=False,
            inspected_chars=0,
            findings_count=0,
            info_types_found=(),
            counts_by_type={},
            reason="ENABLE_CLOUD_DLP=false (opt-in Cloud DLP inspection disabled)",
        )
    if not settings.gcp_project_id:
        raise RuntimeError("GCP_PROJECT_ID is required when ENABLE_CLOUD_DLP=true")

    snippet = (text or "")[:_MAX_DLP_CHARS]
    if not snippet.strip():
        return DlpInspectionSummary(
            enabled=True,
            inspected_chars=0,
            findings_count=0,
            info_types_found=(),
            counts_by_type={},
            reason="No text content to inspect",
        )

    active_types = tuple(info_types) if info_types else DEFAULT_DLP_INFO_TYPES
    client = dlp_client or _get_dlp_client()
    parent = f"projects/{settings.gcp_project_id}/locations/{settings.gcp_region}"
    request = {
        "parent": parent,
        "inspect_config": {
            "info_types": [{"name": it} for it in active_types],
            "include_quote": False,  # Never pull raw PII quotes into memory or responses
            "limits": {"max_findings_per_request": 100},
        },
        "item": {"value": snippet},
    }
    response = client.inspect_content(request=request)
    counts: dict[str, int] = {}
    findings = getattr(getattr(response, "result", None), "findings", None) or []
    for f in findings:
        it_name = str(getattr(getattr(f, "info_type", None), "name", "") or "UNKNOWN")
        counts[it_name] = counts.get(it_name, 0) + 1

    return DlpInspectionSummary(
        enabled=True,
        inspected_chars=len(snippet),
        findings_count=sum(counts.values()),
        info_types_found=tuple(sorted(counts.keys())),
        counts_by_type=counts,
        reason="Inspected via Google Cloud DLP (quotes omitted for PII safety)",
    )
