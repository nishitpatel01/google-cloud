"""Base War Room engine contracts, DAG node telemetry, and serialization helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.core.evidence_verifier import ProvenanceReport
from app.core.models import (
    IncidentRecord,
    IncidentVerdict,
    PackConfig,
)
from app.core.pricing import CostBreakdown
from app.core.stage_emitter import StageEmitter

EngineId = Literal["adk_parallel_war_room", "single_agent_serial"]

_VERDICT_LINE_RE = re.compile(
    r"VERDICT\s*:\s*(CONFIRMED_INCIDENT|BENIGN_FALSE_POSITIVE|NEEDS_VP_ESCALATION)",
    re.IGNORECASE,
)
_ACTION_LINE_RE = re.compile(
    r"RECOMMENDED_ACTION\s*:\s*([a-z0-9_]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TriageRequest:
    pack_id: str
    incident_id: str
    engine_id: EngineId = "adk_parallel_war_room"
    synthesis_model: str = "gemini-2.5-flash"
    judge_model: str | None = None
    mode: Literal["replay", "live"] = "live"
    use_live_bq: bool = True


@dataclass(frozen=True)
class AgentDAGNode:
    node_id: str
    label: str
    role: str
    phase: Literal["intake", "parallel_specialist", "critique", "planner"]
    status: Literal["completed", "failed", "skipped"]
    duration_ms: int
    summary: str
    tool_name: str = ""
    records_returned: int = 0
    sql_or_payload: str = ""


@dataclass(frozen=True)
class TriageResult:
    incident_id: str
    pack_id: str
    engine_id: EngineId
    synthesis_model: str
    mode: Literal["replay", "live"]
    verdict: IncidentVerdict
    executive_brief: str
    recommended_option_id: str
    recommended_action_type: str
    remediation_options: tuple[dict[str, Any], ...]
    prompt_injection_detected: bool
    injection_signals: tuple[str, ...]
    dag_nodes: tuple[AgentDAGNode, ...]
    retrieved_events: tuple[dict[str, Any], ...]
    retrieved_intel: tuple[dict[str, Any], ...]
    retrieved_runbooks: tuple[dict[str, Any], ...]
    provenance: ProvenanceReport
    cost: CostBreakdown
    latency_ms: int
    stage_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "pack_id": self.pack_id,
            "engine_id": self.engine_id,
            "synthesis_model": self.synthesis_model,
            "mode": self.mode,
            "verdict": self.verdict.value,
            "executive_brief": self.executive_brief,
            "recommended_option_id": self.recommended_option_id,
            "recommended_action_type": self.recommended_action_type,
            "remediation_options": list(self.remediation_options),
            "prompt_injection_detected": self.prompt_injection_detected,
            "injection_signals": list(self.injection_signals),
            "dag_nodes": [asdict(n) for n in self.dag_nodes],
            "retrieved_events": list(self.retrieved_events),
            "retrieved_intel": list(self.retrieved_intel),
            "retrieved_runbooks": list(self.retrieved_runbooks),
            "provenance": asdict(self.provenance),
            "cost": asdict(self.cost),
            "latency_ms": self.latency_ms,
            "stage_events": list(self.stage_events),
        }


def parse_verdict_and_action(
    text: str,
    *,
    fallback_verdict: IncidentVerdict = IncidentVerdict.CONFIRMED_INCIDENT,
    fallback_action: str = "no_action_benign_clear",
) -> tuple[IncidentVerdict, str]:
    """Extract structured VERDICT and RECOMMENDED_ACTION directives from LLM output."""
    m_v = _VERDICT_LINE_RE.search(text or "")
    verdict = IncidentVerdict(m_v.group(1).upper()) if m_v else fallback_verdict
    m_a = _ACTION_LINE_RE.search(text or "")
    action = m_a.group(1).lower() if m_a else fallback_action
    return verdict, action


def select_recommended_option(
    incident: IncidentRecord,
    *,
    verdict: IncidentVerdict,
    preferred_action: str,
) -> tuple[str, str]:
    """Match an incident's declarative remediation option deterministically."""
    if verdict == IncidentVerdict.BENIGN_FALSE_POSITIVE:
        for opt in incident.remediation_options:
            if opt.action_type == "no_action_benign_clear":
                return opt.option_id, opt.action_type
        return "none_benign_clear", "no_action_benign_clear"

    for opt in incident.remediation_options:
        if opt.action_type == preferred_action:
            return opt.option_id, opt.action_type
    for opt in incident.remediation_options:
        if opt.is_recommended:
            return opt.option_id, opt.action_type
    if incident.remediation_options:
        first = incident.remediation_options[0]
        return first.option_id, first.action_type
    return "none", preferred_action


class BaseWarRoomEngine(ABC):
    engine_id: EngineId

    @abstractmethod
    async def investigate(
        self,
        req: TriageRequest,
        *,
        pack: PackConfig,
        incident: IncidentRecord,
        emitter: StageEmitter,
    ) -> TriageResult:
        """Execute an incident investigation and return a verified TriageResult."""
