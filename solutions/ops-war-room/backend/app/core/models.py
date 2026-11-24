"""Canonical domain contracts and Illegal-States-Unrepresentable Remediation/Saga FSM."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

PACK_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,48}$")
INCIDENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")


class DataOrigin(str, Enum):
    REAL_PUBLIC = "REAL_PUBLIC"
    MODELED_BRIDGE = "MODELED_BRIDGE"


class IncidentSeverity(str, Enum):
    P1_CRITICAL = "P1_CRITICAL"
    P2_HIGH = "P2_HIGH"
    P3_MEDIUM = "P3_MEDIUM"
    BENIGN_VERIFY = "BENIGN_VERIFY"


class IncidentVerdict(str, Enum):
    CONFIRMED_INCIDENT = "CONFIRMED_INCIDENT"
    BENIGN_FALSE_POSITIVE = "BENIGN_FALSE_POSITIVE"
    NEEDS_VP_ESCALATION = "NEEDS_VP_ESCALATION"


class IncidentState(str, Enum):
    """Finite state machine enforcing zero autonomous mutations."""

    PENDING_TRIAGE = "PENDING_TRIAGE"
    TRIAGED = "TRIAGED"
    DRY_RUN_PREVIEWED = "DRY_RUN_PREVIEWED"
    APPROVED_BY_LEAD = "APPROVED_BY_LEAD"
    SAGA_EXECUTING = "SAGA_EXECUTING"
    SAGA_COMPLETED = "SAGA_COMPLETED"
    SAGA_ROLLED_BACK = "SAGA_ROLLED_BACK"
    DISMISSED_BENIGN = "DISMISSED_BENIGN"


VALID_STATE_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.PENDING_TRIAGE: frozenset({IncidentState.TRIAGED}),
    IncidentState.TRIAGED: frozenset(
        {IncidentState.DRY_RUN_PREVIEWED, IncidentState.DISMISSED_BENIGN}
    ),
    IncidentState.DRY_RUN_PREVIEWED: frozenset(
        {IncidentState.APPROVED_BY_LEAD, IncidentState.DISMISSED_BENIGN}
    ),
    IncidentState.APPROVED_BY_LEAD: frozenset({IncidentState.SAGA_EXECUTING}),
    IncidentState.SAGA_EXECUTING: frozenset(
        {IncidentState.SAGA_COMPLETED, IncidentState.SAGA_ROLLED_BACK}
    ),
    IncidentState.SAGA_COMPLETED: frozenset(),
    IncidentState.SAGA_ROLLED_BACK: frozenset({IncidentState.DRY_RUN_PREVIEWED}),
    IncidentState.DISMISSED_BENIGN: frozenset(),
}


class InvalidStateTransitionError(ValueError):
    """Raised when an operation attempts an illegal incident/remediation transition."""


def assert_valid_transition(current: IncidentState, target: IncidentState) -> None:
    """Enforce legal FSM transitions; makes autonomous execution unrepresentable."""
    allowed = VALID_STATE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransitionError(
            f"Illegal incident state transition: {current.value} -> {target.value} "
            f"(allowed: {sorted(s.value for s in allowed)})"
        )


@dataclass(frozen=True)
class RemediationOption:
    option_id: str
    title: str
    action_type: Literal[
        "iam_revoke_sa_key",
        "gcs_lockdown_bucket",
        "o2c_split_shipment",
        "o2c_air_freight_expedite",
        "sre_traffic_rollback",
        "sre_circuit_breaker",
        "no_action_benign_clear",
    ]
    workflow_name: str
    command_or_diff: str
    estimated_action_cost_usd: float
    avoided_loss_usd: float
    sla_risk_hours: float
    requires_vp_escalation: bool = False
    is_recommended: bool = False
    compensation_steps: tuple[str, ...] = ()

    @property
    def net_value_usd(self) -> float:
        return round(self.avoided_loss_usd - self.estimated_action_cost_usd, 2)

    @property
    def plan_sha256(self) -> str:
        payload = json.dumps(
            {
                "option_id": self.option_id,
                "action_type": self.action_type,
                "workflow_name": self.workflow_name,
                "command_or_diff": self.command_or_diff,
                "estimated_action_cost_usd": self.estimated_action_cost_usd,
            },
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable proof of mandatory two-click lead approval."""

    incident_id: str
    pack_id: str
    option_id: str
    plan_sha256: str
    idempotency_key: str
    approver_identity: str
    approved_at_iso: str
    verdict: IncidentVerdict
    approval_token: str
    state: IncidentState = IncidentState.APPROVED_BY_LEAD

    def __post_init__(self) -> None:
        if self.verdict == IncidentVerdict.BENIGN_FALSE_POSITIVE:
            raise ValueError("Cannot approve remediation for BENIGN_FALSE_POSITIVE verdict")
        if not self.plan_sha256 or len(self.plan_sha256) != 64:
            raise ValueError("plan_sha256 must be a 64-char SHA-256 hex digest")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if not self.approval_token.strip():
            raise ValueError("approval_token must be non-empty")


@dataclass(frozen=True)
class SagaStepResult:
    step_id: str
    title: str
    status: Literal["SUCCEEDED", "FAILED", "COMPENSATED", "SKIPPED", "DRY_RUN"]
    system: str
    duration_ms: int
    detail: str


@dataclass(frozen=True)
class SagaExecutionResult:
    execution_id: str
    incident_id: str
    workflow_name: str
    idempotency_key: str
    mode: Literal["dry_run", "live_workflow", "simulated_workflow"]
    final_state: IncidentState
    steps: tuple[SagaStepResult, ...]
    compensation_triggered: bool = False
    audit_persisted: bool = False


@dataclass(frozen=True)
class TelemetryEvent:
    event_id: str
    pack_id: str
    incident_id: str
    timestamp_iso: str
    source_table: str
    actor: str
    resource: str
    action: str
    status: str
    severity: str
    raw_message: str
    structured_fields: dict[str, Any] = field(default_factory=dict)
    contains_prompt_injection: bool = False
    data_origin: DataOrigin = DataOrigin.MODELED_BRIDGE


@dataclass(frozen=True)
class IntelRecord:
    intel_id: str
    pack_id: str
    intel_type: Literal["mitre_technique", "cisa_kev", "nws_alert", "un_locode", "gcp_status", "otel_metric"]
    title: str
    summary: str
    source_name: str
    source_url: str
    license_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    data_origin: DataOrigin = DataOrigin.REAL_PUBLIC


@dataclass(frozen=True)
class RunbookRecord:
    runbook_id: str
    pack_id: str
    title: str
    category: str
    content: str
    safe_commands: tuple[str, ...] = ()
    data_origin: DataOrigin = DataOrigin.REAL_PUBLIC


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: str
    pack_id: str
    title: str
    summary: str
    severity: IncidentSeverity
    sla_minutes_remaining: int
    expected_verdict: IncidentVerdict
    expected_root_cause: str
    expected_event_ids: tuple[str, ...]
    expected_intel_ids: tuple[str, ...]
    expected_action_type: str
    has_prompt_injection: bool = False
    financial_exposure_usd: float = 0.0
    remediation_options: tuple[RemediationOption, ...] = ()
    data_origin: DataOrigin = DataOrigin.MODELED_BRIDGE


@dataclass(frozen=True)
class SpecialistRole:
    agent_id: str
    display_name: str
    responsibility: str
    tool_source: str


@dataclass(frozen=True)
class PackConfig:
    pack_id: str
    title: str
    subtitle: str
    industry: str
    persona: str
    manual_baseline_cost_usd: float
    manual_baseline_minutes: int
    default_workflow_name: str
    margin_floor_ratio: float
    primary_sources: tuple[str, ...]
    specialists: tuple[SpecialistRole, ...]
    incidents: tuple[IncidentRecord, ...]
    telemetry_events: tuple[TelemetryEvent, ...]
    intel_records: tuple[IntelRecord, ...]
    runbooks: tuple[RunbookRecord, ...]
    manifest_sha256_verified: bool = False
    real_public_share_pct: float = 0.0
