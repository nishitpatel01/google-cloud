"""Declarative MCP Registry loader, SSRF URL validator, and Tier-1 deterministic sandbox adapters."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from app.core.evidence_verifier import check_remediation_policy
from app.core.models import IncidentRecord, IntelRecord, PackConfig, TelemetryEvent

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
    }
)


class SSRFValidationError(ValueError):
    """Raised when an external MCP endpoint URL violates HTTPS or private-network guardrails."""


def validate_external_mcp_url(
    url: str,
    *,
    allowed_domains: frozenset[str] | set[str],
) -> str:
    """Strictly validate Tier-2 external MCP URLs against SSRF and allowlist rules."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        raise SSRFValidationError(f"External MCP URL must use https:// (got {parsed.scheme!r})")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SSRFValidationError("External MCP URL missing hostname")
    if host in _BLOCKED_HOSTNAMES or host.endswith(".internal") or host.endswith(".local"):
        raise SSRFValidationError(f"Blocked internal/metadata hostname: {host!r}")

    # Reject raw IP addresses (IPv4 or IPv6), especially private/loopback/link-local ranges
    try:
        ip = ipaddress.ip_address(host)
        raise SSRFValidationError(f"Raw IP addresses are forbidden for external MCP endpoints: {ip}")
    except ValueError:
        pass

    if allowed_domains:
        permitted = any(
            host == dom.lower() or host.endswith("." + dom.lower())
            for dom in allowed_domains
        )
        if not permitted:
            raise SSRFValidationError(
                f"Hostname {host!r} is not in allowed_external_domains allowlist"
            )
    return raw


@dataclass(frozen=True)
class MCPAdapterSpec:
    adapter_id: str
    pack_id: str
    display_name: str
    mode: str
    read_only: bool
    description: str
    tier2_endpoint_template: str


class MCPRegistry:
    """Loads mcp_registry.yaml and executes deterministic Tier-1 sandbox tool queries."""

    def __init__(self, registry_path: Path) -> None:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        self.allowed_external_domains: frozenset[str] = frozenset(
            str(d).strip().lower() for d in raw.get("allowed_external_domains", [])
        )
        self.adapters: tuple[MCPAdapterSpec, ...] = tuple(
            MCPAdapterSpec(
                adapter_id=str(a["adapter_id"]),
                pack_id=str(a["pack_id"]),
                display_name=str(a["display_name"]),
                mode=str(a.get("mode", "tier1_sandbox")),
                read_only=bool(a.get("read_only", True)),
                description=str(a.get("description", "")),
                tier2_endpoint_template=str(a.get("tier2_endpoint_template", "")),
            )
            for a in raw.get("adapters", [])
        )

    def adapters_for_pack(self, pack_id: str) -> list[MCPAdapterSpec]:
        return [a for a in self.adapters if a.pack_id == pack_id]

    def match_intel_for_incident(
        self,
        *,
        incident: IncidentRecord,
        telemetry: tuple[TelemetryEvent, ...],
        intel_catalog: tuple[IntelRecord, ...],
    ) -> list[dict[str, Any]]:
        """Correlate incident telemetry against public intel records in O(I + E) time."""
        intel_by_id = {r.intel_id: r for r in intel_catalog}
        haystack = " ".join(
            [incident.title, incident.summary]
            + [f"{ev.action} {ev.resource} {ev.raw_message}" for ev in telemetry]
        ).lower()

        selected_ids: list[str] = []
        # 1. Exact ID mentions in telemetry or incident context
        for r in intel_catalog:
            if r.intel_id.lower() in haystack and r.intel_id not in selected_ids:
                selected_ids.append(r.intel_id)

        # 2. Domain action -> MITRE/OTel/LOCODE deterministic correlation signals
        keyword_map = {
            "createserviceaccountkey": ["T1078.004", "T1098.001"],
            "setiampolicy": ["T1098.001", "T1078.004"],
            "storage.objects.get": ["T1530"],
            "storage.objects.list": ["T1530"],
            "egress_transfer": ["T1537"],
            "exploit_attempt_match": ["T1190"],
            "metadata_token_read": ["T1552.005", "T1059.004"],
            "testiampermissions": ["T1078"],
            "http_500": ["OTEL-HTTP_SERVER_REQUEST_DURATION"],
            "http_504": ["OTEL-HTTP_SERVER_REQUEST_DURATION"],
            "http_502": ["OTEL-HTTP_SERVER_REQUEST_DURATION"],
            "synthetic_load_spike": ["OTEL-HTTP_SERVER_REQUEST_DURATION"],
            "oom_crashloop": ["OTEL-HTTP_SERVER_REQUEST_DURATION"],
        }
        for needle, mapped_ids in keyword_map.items():
            if needle in haystack:
                for mid in mapped_ids:
                    if mid in intel_by_id and mid not in selected_ids:
                        selected_ids.append(mid)

        # 3. Include matched CVE / NWS / GCP status if referenced in structured fields
        for ev in telemetry:
            for k in ("matched_cve", "port_code", "hub", "primary_hub", "backup_hub", "alt_hub"):
                val = str(ev.structured_fields.get(k, "")).strip()
                if not val:
                    continue
                for candidate in (val, f"LOCODE-{val}"):
                    if candidate in intel_by_id and candidate not in selected_ids:
                        selected_ids.append(candidate)

        # 4. Ensure at least top relevant public intel entries for the pack are surfaced
        if incident.pack_id == "sre_outage_commander" and "504" in haystack:
            first_gcp = next(
                (r.intel_id for r in intel_catalog if r.intel_type == "gcp_status"), None
            )
            if first_gcp and first_gcp not in selected_ids:
                selected_ids.append(first_gcp)

        if not selected_ids:
            selected_ids = [r.intel_id for r in intel_catalog[:4]]

        out: list[dict[str, Any]] = []
        for iid in selected_ids[:8]:
            rec = intel_by_id[iid]
            out.append(
                {
                    "intel_id": rec.intel_id,
                    "intel_type": rec.intel_type,
                    "title": rec.title,
                    "summary": rec.summary[:260],
                    "source_name": rec.source_name,
                    "license_name": rec.license_name,
                    "data_origin": rec.data_origin.value,
                }
            )
        return out

    def evaluate_blast_radius_and_sla(
        self,
        *,
        pack: PackConfig,
        incident: IncidentRecord,
        telemetry: tuple[TelemetryEvent, ...],
    ) -> dict[str, Any]:
        """Deterministic impact, authorized-change check, and margin-floor policy audit."""
        approved_change_tickets: list[str] = []
        has_signed_pod = False
        is_shadow_load_test = False
        for ev in telemetry:
            sf = ev.structured_fields
            if sf.get("approved_window") is True and sf.get("change_ticket"):
                approved_change_tickets.append(str(sf["change_ticket"]))
            if ev.action == "AUTHORIZED_REDTEAM_WINDOW" and sf.get("ticket_id"):
                approved_change_tickets.append(str(sf["ticket_id"]))
            if ev.action == "PROOF_OF_DELIVERY_SIGNED" or sf.get("order_status") == "DELIVERED":
                has_signed_pod = True
            if ev.action == "SYNTHETIC_LOAD_SPIKE" and sf.get("customer_impact") is False:
                is_shadow_load_test = True

        option_audits = []
        for opt in incident.remediation_options:
            # Evaluate policy assuming confirmed incident first so margin floor triggers transparently
            decision = check_remediation_policy(
                verdict=incident.expected_verdict,
                option=opt,
                margin_floor_ratio=pack.margin_floor_ratio,
            )
            option_audits.append(
                {
                    "option_id": opt.option_id,
                    "title": opt.title,
                    "action_type": opt.action_type,
                    "workflow_name": opt.workflow_name,
                    "estimated_action_cost_usd": opt.estimated_action_cost_usd,
                    "avoided_loss_usd": opt.avoided_loss_usd,
                    "net_value_usd": opt.net_value_usd,
                    "plan_sha256": opt.plan_sha256,
                    "policy_allowed": decision.allowed,
                    "requires_vp_escalation": decision.requires_vp_escalation,
                    "policy_reason": decision.reason,
                    "is_recommended": opt.is_recommended,
                }
            )

        return {
            "incident_id": incident.incident_id,
            "severity": incident.severity.value,
            "sla_minutes_remaining": incident.sla_minutes_remaining,
            "financial_exposure_usd": incident.financial_exposure_usd,
            "manual_baseline_cost_usd": pack.manual_baseline_cost_usd,
            "manual_baseline_minutes": pack.manual_baseline_minutes,
            "margin_floor_ratio": pack.margin_floor_ratio,
            "benign_signals": {
                "approved_change_tickets": sorted(set(approved_change_tickets)),
                "has_signed_proof_of_delivery": has_signed_pod,
                "is_shadow_load_test_zero_prod_impact": is_shadow_load_test,
            },
            "remediation_option_audits": option_audits,
        }
