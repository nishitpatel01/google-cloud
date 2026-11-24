"""Cloud DLP PII inspection & redaction with deterministic offline preview for Replay mode."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import Settings
from app.core.gcp_clients import GCPClientPool

_DLP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL_ADDRESS", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("US_SOCIAL_SECURITY_NUMBER", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD_NUMBER", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("PHONE_NUMBER", re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
)


@dataclass(frozen=True)
class DLPInspectionResult:
    engine: str  # "cloud_dlp_live" | "deterministic_preview"
    findings_by_type: dict[str, int]
    total_findings: int
    redacted_text: str


def _deterministic_redact(text: str) -> DLPInspectionResult:
    counts: dict[str, int] = {}
    redacted = text
    for info_type, pattern in _DLP_PATTERNS:
        matches = pattern.findall(redacted)
        if matches:
            counts[info_type] = len(matches)
            redacted = pattern.sub(f"[{info_type}_REDACTED]", redacted)
    return DLPInspectionResult(
        engine="deterministic_preview",
        findings_by_type=counts,
        total_findings=sum(counts.values()),
        redacted_text=redacted,
    )


def inspect_and_redact_pii(
    text: str,
    *,
    settings: Settings,
    gcp_clients: GCPClientPool | None = None,
    use_live_dlp: bool = False,
) -> DLPInspectionResult:
    """Redact PII via Cloud DLP API when live, or deterministic preview in zero-credential Replay."""
    if not use_live_dlp or gcp_clients is None or not settings.live_capable:
        return _deterministic_redact(text)

    dlp = gcp_clients.dlp()
    parent = f"projects/{gcp_clients.project_id}/locations/global"
    info_types = [
        {"name": "EMAIL_ADDRESS"},
        {"name": "PHONE_NUMBER"},
        {"name": "CREDIT_CARD_NUMBER"},
        {"name": "US_SOCIAL_SECURITY_NUMBER"},
    ]
    inspect_config = {"info_types": info_types, "include_quote": False}
    deidentify_config = {
        "info_type_transformations": {
            "transformations": [
                {
                    "primitive_transformation": {
                        "replace_with_info_type_config": {}
                    }
                }
            ]
        }
    }
    resp = dlp.deidentify_content(
        request={
            "parent": parent,
            "inspect_config": inspect_config,
            "deidentify_config": deidentify_config,
            "item": {"value": text[:120_000]},
        }
    )
    counts: dict[str, int] = {}
    for summary in resp.overview.transformation_summaries:
        name = summary.info_type.name if summary.info_type else "PII"
        cnt = sum(r.count for r in summary.results)
        if cnt:
            counts[name] = counts.get(name, 0) + cnt
    return DLPInspectionResult(
        engine="cloud_dlp_live",
        findings_by_type=counts,
        total_findings=sum(counts.values()),
        redacted_text=resp.item.value or text,
    )
