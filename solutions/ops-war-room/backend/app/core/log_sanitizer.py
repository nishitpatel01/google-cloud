"""Deterministic adversarial log prompt-injection scanner and quarantine sanitizer."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from app.core.models import TelemetryEvent

_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(ignore\s+(all\s+)?(previous|prior|above)\s+instructions?|"
            r"disregard\s+(all\s+)?(previous|prior|above)|"
            r"forget\s+(everything|all\s+prior)|"
            r"override\s+(system|security|policy)\s+(instructions?|verdict|rules?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "forced_benign_verdict",
        re.compile(
            r"\b(mark\s+(this\s+)?(alert|incident|event)\s+as\s+(benign|safe|false\s+positive)|"
            r"classify\s+as\s+(benign|authorized|false\s+positive)|"
            r"close\s+(this\s+)?(alert|ticket|incident)\s+(immediately|without\s+action)|"
            r"skip\s+(containment|remediation|escalation)|"
            r"approve\s+automatically)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_delimiter_spoofing",
        re.compile(
            r"(<\|?\s*(system|assistant|developer)\s*\|?>|"
            r"\[/?\s*(SYSTEM|INST|ASSISTANT)\s*\]|"
            r"###\s*(SYSTEM|INSTRUCTION|OVERRIDE))",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_hijack_directive",
        re.compile(
            r"\b(call\s+tool|execute\s+workflow|transfer_to_agent|return\s+json\s+verdict\s*[:=]\s*benign)\b",
            re.IGNORECASE,
        ),
    ),
)

_BASE64_CANDIDATE_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class SanitizationReport:
    event_id: str
    injection_detected: bool
    matched_rules: tuple[str, ...]
    quarantined_message: str
    preserved_structured_fields: dict[str, object]


def _scan_text_for_rules(text: str) -> list[str]:
    hits: list[str] = []
    for name, pattern in _INJECTION_RULES:
        if pattern.search(text):
            hits.append(name)

    # Also inspect base64-encoded blobs embedded inside headers/user-agents
    for cand in _BASE64_CANDIDATE_RE.findall(text):
        if len(cand) > 512:
            continue
        try:
            padded = cand + "=" * ((4 - len(cand) % 4) % 4)
            decoded = base64.b64decode(padded, validate=True).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if len(decoded) >= 12:
            for name, pattern in _INJECTION_RULES:
                tag = f"base64_{name}"
                if pattern.search(decoded) and tag not in hits:
                    hits.append(tag)
    return hits


def sanitize_untrusted_text(raw_text: str, *, event_id: str = "inline") -> SanitizationReport:
    """Detect prompt-injection payloads in untrusted text and wrap safely as inert data."""
    cleaned = _CONTROL_CHARS_RE.sub(" ", raw_text or "")
    hits = _scan_text_for_rules(cleaned)

    quarantined = cleaned
    if hits:
        for _, pattern in _INJECTION_RULES:
            quarantined = pattern.sub("[REDACTED_INJECTION_PAYLOAD]", quarantined)
        for cand in _BASE64_CANDIDATE_RE.findall(quarantined):
            try:
                padded = cand + "=" * ((4 - len(cand) % 4) % 4)
                decoded = base64.b64decode(padded, validate=True).decode("utf-8", errors="ignore")
                if any(p.search(decoded) for _, p in _INJECTION_RULES):
                    quarantined = quarantined.replace(cand, "[REDACTED_BASE64_INJECTION]")
            except Exception:
                continue

    wrapped = f"[UNTRUSTED_TELEMETRY_DATA event_id={event_id}]{quarantined[:1200]}[/UNTRUSTED_TELEMETRY_DATA]"
    return SanitizationReport(
        event_id=event_id,
        injection_detected=bool(hits),
        matched_rules=tuple(sorted(set(hits))),
        quarantined_message=wrapped,
        preserved_structured_fields={},
    )


def sanitize_telemetry_event(event: TelemetryEvent) -> SanitizationReport:
    """Inspect raw message and string-valued structured metadata while preserving core columns."""
    combined_parts = [event.raw_message]
    for k, v in event.structured_fields.items():
        if isinstance(v, str):
            combined_parts.append(f"{k}={v}")
    combined = " | ".join(combined_parts)
    report = sanitize_untrusted_text(combined, event_id=event.event_id)

    # Deterministic structured columns remain authoritative for ReviewAndCritiqueAgent
    preserved: dict[str, object] = {
        "event_id": event.event_id,
        "timestamp_iso": event.timestamp_iso,
        "source_table": event.source_table,
        "actor": event.actor,
        "resource": event.resource,
        "action": event.action,
        "status": event.status,
        "severity": event.severity,
        "data_origin": event.data_origin.value,
    }
    return SanitizationReport(
        event_id=event.event_id,
        injection_detected=report.injection_detected,
        matched_rules=report.matched_rules,
        quarantined_message=report.quarantined_message,
        preserved_structured_fields=preserved,
    )


def sanitize_telemetry_batch(events: list[TelemetryEvent] | tuple[TelemetryEvent, ...]) -> list[SanitizationReport]:
    """Sanitize a batch of telemetry events in O(N) single pass."""
    return [sanitize_telemetry_event(ev) for ev in events]
