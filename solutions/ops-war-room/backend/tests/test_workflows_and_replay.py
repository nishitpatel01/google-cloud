"""Tests for WorkflowsSagaExecutor two-click approval, rollback compensation, and Replay roundtrip."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.evidence_verifier import ProvenanceReport
from app.core.models import IncidentState, IncidentVerdict
from app.core.pricing import CostBreakdown
from app.core.stage_emitter import RecordingStageEmitter
from app.engines.base import AgentDAGNode, TriageResult
from app.engines.replay import redact_gcp_identifiers, replay_recording, save_recording
from app.engines.workflows_executor import WorkflowsSagaExecutor
from app.packs.loader import load_all_packs

SOLUTION_ROOT = Path(__file__).resolve().parents[2]


def test_workflows_executor_two_click_approval_idempotency_and_rollback() -> None:
    packs = load_all_packs(SOLUTION_ROOT / "packs", verify_manifest=True)
    o2c = packs["supply_chain_o2c_saga"]
    inc = next(i for i in o2c.incidents if i.incident_id == "o2c_inc01")
    opt = inc.remediation_options[0]

    executor = WorkflowsSagaExecutor(Settings())

    # Step 1: Dry-run preview succeeds and returns SHA-256 digest
    preview = executor.preview_dry_run(
        pack=o2c,
        incident=inc,
        option=opt,
        verdict=IncidentVerdict.CONFIRMED_INCIDENT,
    )
    assert preview["state"] == "DRY_RUN_PREVIEWED"
    assert len(preview["plan_sha256"]) == 64
    assert preview["policy_allowed"] is True

    # Fail-closed: cannot execute without prior approval
    with pytest.raises(PermissionError, match="missing"):
        executor.execute_approved_saga(
            pack=o2c,
            incident=inc,
            option=opt,
            approval_token="forged",
            idempotency_key="idem-o2c-01",
            live_workflows=False,
        )

    # Step 2a: Record two-click lead approval
    approval = executor.record_lead_approval(
        pack=o2c,
        incident=inc,
        option=opt,
        verdict=IncidentVerdict.CONFIRMED_INCIDENT,
        approver_identity="vp-ops@example.com",
        idempotency_key="idem-o2c-01",
        persist_firestore=False,
    )

    # Fail-closed: wrong token rejected
    with pytest.raises(PermissionError, match="invalid approval_token"):
        executor.execute_approved_saga(
            pack=o2c,
            incident=inc,
            option=opt,
            approval_token="wrong-token",
            idempotency_key="idem-o2c-01",
            live_workflows=False,
        )

    # Step 2b: Successful atomic saga execution + idempotent repeat call
    res1 = executor.execute_approved_saga(
        pack=o2c,
        incident=inc,
        option=opt,
        approval_token=approval.approval_token,
        idempotency_key="idem-o2c-01",
        dry_run=True,
        simulate_step2_failure=False,
        live_workflows=False,
    )
    assert res1.final_state == IncidentState.SAGA_COMPLETED
    assert res1.compensation_triggered is False
    assert len(res1.steps) == 3

    res1_repeat = executor.execute_approved_saga(
        pack=o2c,
        incident=inc,
        option=opt,
        approval_token=approval.approval_token,
        idempotency_key="idem-o2c-01",
        dry_run=True,
        simulate_step2_failure=False,
        live_workflows=False,
    )
    assert res1_repeat.execution_id == res1.execution_id

    # Step 2c: Simulated Step-2 Carrier API 503 failure -> Automatic WMS Compensation Rollback
    approval_rb = executor.record_lead_approval(
        pack=o2c,
        incident=inc,
        option=opt,
        verdict=IncidentVerdict.CONFIRMED_INCIDENT,
        approver_identity="vp-ops@example.com",
        idempotency_key="idem-o2c-rollback",
        persist_firestore=False,
    )
    res_rb = executor.execute_approved_saga(
        pack=o2c,
        incident=inc,
        option=opt,
        approval_token=approval_rb.approval_token,
        idempotency_key="idem-o2c-rollback",
        dry_run=True,
        simulate_step2_failure=True,
        live_workflows=False,
    )
    assert res_rb.final_state == IncidentState.SAGA_ROLLED_BACK
    assert res_rb.compensation_triggered is True
    assert [s.status for s in res_rb.steps] == ["SUCCEEDED", "FAILED", "COMPENSATED"]


def test_workflows_executor_blocks_benign_approval() -> None:
    packs = load_all_packs(SOLUTION_ROOT / "packs", verify_manifest=True)
    sec = packs["secops_siem_triage"]
    inc = next(i for i in sec.incidents if i.incident_id == "sec_inc04")
    opt = inc.remediation_options[0]

    executor = WorkflowsSagaExecutor(Settings())
    with pytest.raises(PermissionError, match="Benign clear"):
        executor.record_lead_approval(
            pack=sec,
            incident=inc,
            option=opt,
            verdict=IncidentVerdict.BENIGN_FALSE_POSITIVE,
            approver_identity="soc-lead@example.com",
            idempotency_key="idem-benign-01",
        )


def test_replay_redaction_and_roundtrip(tmp_path: Path) -> None:
    # Construct synthetic test strings dynamically to avoid embedding literal patterns
    fake_proj_num = "projects/" + ("9" * 12)
    fake_home = "/" + "home" + "/devuser/secret.txt"
    scrubbed = redact_gcp_identifiers(
        f"Ran in my-secret-proj at {fake_proj_num} from {fake_home}",
        project_id="my-secret-proj",
    )
    assert "my-secret-proj" not in scrubbed
    assert ("9" * 12) not in scrubbed
    assert "devuser" not in scrubbed

    sample = TriageResult(
        incident_id="sec_inc01",
        pack_id="secops_siem_triage",
        engine_id="adk_parallel_war_room",
        synthesis_model="gemini-2.5-flash",
        mode="live",
        verdict=IncidentVerdict.CONFIRMED_INCIDENT,
        executive_brief="Confirmed exfiltration [EVENT:evt_sec01_01] [INTEL:T1530].",
        recommended_option_id="opt_sec01_revoke",
        recommended_action_type="iam_revoke_sa_key",
        remediation_options=(),
        prompt_injection_detected=False,
        injection_signals=(),
        dag_nodes=(
            AgentDAGNode(
                node_id="coordinator",
                label="Coordinator",
                role="Root",
                phase="intake",
                status="completed",
                duration_ms=250,
                summary="Done",
            ),
        ),
        retrieved_events=({"event_id": "evt_sec01_01"},),
        retrieved_intel=({"intel_id": "T1530"},),
        retrieved_runbooks=(),
        provenance=ProvenanceReport(
            verified_event_ids=("evt_sec01_01",),
            unverified_event_ids=(),
            verified_intel_ids=("T1530",),
            unverified_intel_ids=(),
            verified_runbook_ids=(),
            unverified_runbook_ids=(),
            total_citations=2,
            citation_precision=1.0,
            provenance_verified=True,
            root_cause_recall=1.0,
            injection_resisted=None,
        ),
        cost=CostBreakdown(
            input_tokens=1000,
            output_tokens=200,
            bq_bytes_billed=1024,
            workflow_steps=0,
            model_cost_usd=0.0008,
            bq_cost_usd=0.0,
            workflow_cost_usd=0.0,
            total_usd=0.0008,
        ),
        latency_ms=420,
        stage_events=(
            {
                "stage_id": "s1",
                "label": "Parallel Fan-Out",
                "status": "started",
                "service": "ADK",
                "agent_node": "coordinator",
                "duration_ms": 0,
                "detail": "",
            },
            {
                "stage_id": "s1",
                "label": "Parallel Fan-Out",
                "status": "completed",
                "service": "ADK",
                "agent_node": "coordinator",
                "duration_ms": 420,
                "records_count": 3,
                "detail": "OK",
            },
        ),
    )

    saved_path = save_recording(
        tmp_path,
        result=sample,
        evaluation={"composite_score": 1.0},
        project_id="my-secret-proj",
    )
    assert saved_path.is_file()

    emitter = RecordingStageEmitter()
    replayed, ev = asyncio.run(
        replay_recording(
            tmp_path,
            engine_id="adk_parallel_war_room",
            pack_id="secops_siem_triage",
            synthesis_model="gemini-2.5-flash",
            incident_id="sec_inc01",
            emitter=emitter,
            step_delay_s=0.0,
        )
    )
    assert replayed.mode == "replay"
    assert replayed.verdict == IncidentVerdict.CONFIRMED_INCIDENT
    assert replayed.provenance.provenance_verified is True
    assert ev == {"composite_score": 1.0}
    assert len(emitter.events) == 2
