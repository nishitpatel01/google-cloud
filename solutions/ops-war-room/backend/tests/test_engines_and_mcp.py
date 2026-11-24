"""Tests for SafeBigQueryRunner, MCPRegistry SSRF defense, and ADKParallelWarRoomEngine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.models import IncidentVerdict
from app.core.stage_emitter import RecordingStageEmitter
from app.engines.adk_war_room import ADKParallelWarRoomEngine
from app.engines.base import TriageRequest
from app.engines.mcp_registry import MCPRegistry, SSRFValidationError, validate_external_mcp_url
from app.engines.safe_bq_runner import SafeBigQueryRunner, assert_read_only_sql
from app.packs.loader import load_all_packs

SOLUTION_ROOT = Path(__file__).resolve().parents[2]


def test_safe_bq_runner_blocks_dml_and_stacked_sql() -> None:
    assert_read_only_sql("SELECT event_id FROM `p.d.telemetry_events` WHERE pack_id = @pack_id")

    for bad in (
        "DELETE FROM telemetry_events WHERE 1=1",
        "DROP TABLE incidents",
        "SELECT 1; DROP TABLE incidents",
        "INSERT INTO incidents VALUES ('a')",
        "UPDATE incidents SET severity = 'LOW'",
    ):
        with pytest.raises(ValueError):
            assert_read_only_sql(bad)


def test_mcp_ssrf_url_validator_fail_closed() -> None:
    allowed = frozenset({"chronicle.googleapis.com", "apigee.googleapis.com"})
    ok = validate_external_mcp_url(
        "https://chronicle.googleapis.com/v1alpha/projects/demo/locations/us",
        allowed_domains=allowed,
    )
    assert ok.startswith("https://chronicle.googleapis.com")

    blocked_urls = [
        "http://chronicle.googleapis.com/insecure",
        "https://169.254.169.254/computeMetadata/v1/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://localhost:8080/admin",
        "https://127.0.0.1/secret",
        "https://10.0.0.5/internal",
        "https://evil.example.com/steal",
    ]
    for bad in blocked_urls:
        with pytest.raises(SSRFValidationError):
            validate_external_mcp_url(bad, allowed_domains=allowed)


def test_mcp_registry_correlates_all_packs_and_detects_benign_signals() -> None:
    packs = load_all_packs(SOLUTION_ROOT / "packs", verify_manifest=True)
    registry = MCPRegistry(SOLUTION_ROOT / "packs" / "mcp_registry.yaml")
    runner = SafeBigQueryRunner(Settings())

    # SecOps benign red-team drill
    sec_pack = packs["secops_siem_triage"]
    benign_inc = next(i for i in sec_pack.incidents if i.incident_id == "sec_inc04")
    tel = runner.fetch_incident_telemetry(sec_pack, benign_inc.incident_id, use_live_bq=False).rows
    impact = registry.evaluate_blast_radius_and_sla(pack=sec_pack, incident=benign_inc, telemetry=tel)
    assert "CHG-99210" in impact["benign_signals"]["approved_change_tickets"]

    # Supply Chain O2C split-shipment vs air-freight margin floor
    o2c_pack = packs["supply_chain_o2c_saga"]
    o2c_inc = next(i for i in o2c_pack.incidents if i.incident_id == "o2c_inc01")
    o2c_tel = runner.fetch_incident_telemetry(o2c_pack, o2c_inc.incident_id, use_live_bq=False).rows
    matched = registry.match_intel_for_incident(
        incident=o2c_inc, telemetry=o2c_tel, intel_catalog=o2c_pack.intel_records
    )
    matched_ids = {m["intel_id"] for m in matched}
    assert "LOCODE-USLAX" in matched_ids and "LOCODE-USDFW" in matched_ids

    o2c_impact = registry.evaluate_blast_radius_and_sla(pack=o2c_pack, incident=o2c_inc, telemetry=o2c_tel)
    audits = {a["option_id"]: a for a in o2c_impact["remediation_option_audits"]}
    assert audits["opt_o2c01_split"]["requires_vp_escalation"] is False
    assert audits["opt_o2c01_air"]["requires_vp_escalation"] is True


def test_adk_parallel_war_room_executes_tools_and_enforces_injection_invariant() -> None:
    """Offline test exercising full ADKParallelWarRoomEngine tool execution & security invariant."""
    packs = load_all_packs(SOLUTION_ROOT / "packs", verify_manifest=True)
    sec_pack = packs["secops_siem_triage"]
    inj_inc = next(i for i in sec_pack.incidents if i.incident_id == "sec_inc02")

    settings = Settings(gcp_project_id="demo-test-project")
    runner = SafeBigQueryRunner(settings)
    registry = MCPRegistry(SOLUTION_ROOT / "packs" / "mcp_registry.yaml")

    class _FakeSessionService:
        async def create_session(self, *, app_name: str, user_id: str) -> Any:
            return SimpleNamespace(id="sess-test-01")

    class _FakeRunner:
        def __init__(self, *, agent: Any, app_name: str) -> None:
            self.agent = agent
            self.session_service = _FakeSessionService()

        async def run_async(self, **kwargs: Any):
            # Invoke all 3 parallel specialist tools concurrently just like ADK ParallelAgent
            parallel_node = self.agent.sub_agents[0]
            critique_node = self.agent.sub_agents[1]
            await asyncio.gather(*(sa.tools[0]() for sa in parallel_node.sub_agents))

            # Simulate a fooled LLM trying to output BENIGN_FALSE_POSITIVE on an injected alert
            draft = (
                "VERDICT: BENIGN_FALSE_POSITIVE\n"
                "RECOMMENDED_ACTION: iam_revoke_sa_key\n\n"
                "Observed SetIamPolicy [EVENT:evt_sec02_01] and key creation [EVENT:evt_sec02_02] "
                "matching [INTEL:T1098.001] and [INTEL:T1078.004]. Follow [RUNBOOK:rb_sec_iam_revoke]."
            )
            await critique_node.tools[0](draft_brief=draft, proposed_verdict="CONFIRMED_INCIDENT")
            yield SimpleNamespace(
                author="review_and_critique_agent",
                usage_metadata=SimpleNamespace(prompt_token_count=4200, candidates_token_count=380),
                content=SimpleNamespace(parts=[SimpleNamespace(text=draft)]),
            )

    engine = ADKParallelWarRoomEngine(
        settings,
        bq_runner=runner,
        mcp_registry=registry,
        runner_factory=_FakeRunner,
    )
    emitter = RecordingStageEmitter()
    result = asyncio.run(
        engine.investigate(
            TriageRequest(
                pack_id="secops_siem_triage",
                incident_id="sec_inc02",
                use_live_bq=False,
            ),
            pack=sec_pack,
            incident=inj_inc,
            emitter=emitter,
        )
    )

    # Hard security invariant overrides fooled BENIGN verdict back to CONFIRMED_INCIDENT
    assert result.prompt_injection_detected is True
    assert result.verdict == IncidentVerdict.CONFIRMED_INCIDENT
    assert result.provenance.provenance_verified is True
    assert result.provenance.root_cause_recall == 1.0
    assert result.provenance.injection_resisted is True
    assert len(result.dag_nodes) == 5
    assert result.cost.total_usd > 0
