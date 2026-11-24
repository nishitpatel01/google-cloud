"""Tests for Remediation/Saga FSM invariants and fail-closed approval gates."""

from __future__ import annotations

import pytest

from app.core.evidence_verifier import verify_execution_approval
from app.core.models import (
    ApprovalRecord,
    IncidentState,
    IncidentVerdict,
    InvalidStateTransitionError,
    RemediationOption,
    assert_valid_transition,
)


def _sample_option(cost: float = 1850.0) -> RemediationOption:
    return RemediationOption(
        option_id="opt_o2c01_split",
        title="Split-Shipment from Dallas Hub",
        action_type="o2c_split_shipment",
        workflow_name="acc04-o2c-saga",
        command_or_diff="workflow: acc04-o2c-saga\nstep1_wms: RESERVE",
        estimated_action_cost_usd=cost,
        avoided_loss_usd=12000.0,
        sla_risk_hours=18.0,
        is_recommended=True,
    )


def test_valid_remediation_fsm_lifecycle() -> None:
    assert_valid_transition(IncidentState.PENDING_TRIAGE, IncidentState.TRIAGED)
    assert_valid_transition(IncidentState.TRIAGED, IncidentState.DRY_RUN_PREVIEWED)
    assert_valid_transition(IncidentState.DRY_RUN_PREVIEWED, IncidentState.APPROVED_BY_LEAD)
    assert_valid_transition(IncidentState.APPROVED_BY_LEAD, IncidentState.SAGA_EXECUTING)
    assert_valid_transition(IncidentState.SAGA_EXECUTING, IncidentState.SAGA_COMPLETED)
    assert_valid_transition(IncidentState.SAGA_EXECUTING, IncidentState.SAGA_ROLLED_BACK)
    # Rolled-back saga can be re-previewed for a revised plan
    assert_valid_transition(IncidentState.SAGA_ROLLED_BACK, IncidentState.DRY_RUN_PREVIEWED)


def test_illegal_autonomous_transitions_rejected() -> None:
    illegal_pairs = [
        (IncidentState.PENDING_TRIAGE, IncidentState.SAGA_EXECUTING),
        (IncidentState.TRIAGED, IncidentState.APPROVED_BY_LEAD),
        (IncidentState.TRIAGED, IncidentState.SAGA_EXECUTING),
        (IncidentState.DRY_RUN_PREVIEWED, IncidentState.SAGA_EXECUTING),
        (IncidentState.DISMISSED_BENIGN, IncidentState.APPROVED_BY_LEAD),
        (IncidentState.SAGA_COMPLETED, IncidentState.SAGA_EXECUTING),
    ]
    for cur, nxt in illegal_pairs:
        with pytest.raises(InvalidStateTransitionError):
            assert_valid_transition(cur, nxt)


def test_approval_record_rejects_benign_verdict_and_short_hash() -> None:
    opt = _sample_option()
    with pytest.raises(ValueError, match="BENIGN_FALSE_POSITIVE"):
        ApprovalRecord(
            incident_id="sec_inc04",
            pack_id="secops_siem_triage",
            option_id=opt.option_id,
            plan_sha256=opt.plan_sha256,
            idempotency_key="idem-01",
            approver_identity="lead@example.com",
            approved_at_iso="2026-11-24T12:00:00Z",
            verdict=IncidentVerdict.BENIGN_FALSE_POSITIVE,
            approval_token="tok-123",
        )

    with pytest.raises(ValueError, match="64-char SHA-256"):
        ApprovalRecord(
            incident_id="o2c_inc01",
            pack_id="supply_chain_o2c_saga",
            option_id=opt.option_id,
            plan_sha256="short-hash",
            idempotency_key="idem-01",
            approver_identity="lead@example.com",
            approved_at_iso="2026-11-24T12:00:00Z",
            verdict=IncidentVerdict.CONFIRMED_INCIDENT,
            approval_token="tok-123",
        )


def test_verify_execution_approval_fail_closed() -> None:
    opt = _sample_option()
    approval = ApprovalRecord(
        incident_id="o2c_inc01",
        pack_id="supply_chain_o2c_saga",
        option_id=opt.option_id,
        plan_sha256=opt.plan_sha256,
        idempotency_key="idem-8841",
        approver_identity="vp-ops@example.com",
        approved_at_iso="2026-11-24T12:00:00Z",
        verdict=IncidentVerdict.CONFIRMED_INCIDENT,
        approval_token="signed-token-001",
    )

    # Valid approval succeeds
    verify_execution_approval(
        incident_id="o2c_inc01",
        option=opt,
        approval=approval,
        idempotency_key="idem-8841",
    )

    # Missing approval denied
    with pytest.raises(PermissionError, match="missing"):
        verify_execution_approval(
            incident_id="o2c_inc01",
            option=opt,
            approval=None,
            idempotency_key="idem-8841",
        )

    # Mutated plan after approval denied via SHA-256 digest mismatch
    tampered = _sample_option(cost=9999.0)
    with pytest.raises(PermissionError, match="SHA-256 digest mismatch"):
        verify_execution_approval(
            incident_id="o2c_inc01",
            option=tampered,
            approval=approval,
            idempotency_key="idem-8841",
        )

    # Reused idempotency key mismatch denied
    with pytest.raises(PermissionError, match="idempotency_key mismatch"):
        verify_execution_approval(
            incident_id="o2c_inc01",
            option=opt,
            approval=approval,
            idempotency_key="idem-other",
        )
