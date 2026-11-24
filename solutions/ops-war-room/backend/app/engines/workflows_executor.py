"""Two-Click Lead Approval Ledger (Named Firestore DB) and Atomic Cloud Workflows Saga Executor."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from app.core.config import Settings
from app.core.evidence_verifier import check_remediation_policy, verify_execution_approval
from app.core.gcp_clients import GCPClientPool
from app.core.models import (
    ApprovalRecord,
    IncidentRecord,
    IncidentState,
    IncidentVerdict,
    PackConfig,
    RemediationOption,
    SagaExecutionResult,
    SagaStepResult,
    assert_valid_transition,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WorkflowsSagaExecutor:
    """Enforces two-click approval FSM, idempotency, Firestore audit, and Cloud Workflows sagas."""

    def __init__(
        self,
        settings: Settings,
        gcp_clients: GCPClientPool | None = None,
    ) -> None:
        self._settings = settings
        self._gcp_clients = gcp_clients
        self._lock = threading.RLock()
        self._approvals: dict[str, ApprovalRecord] = {}
        self._executions: dict[str, SagaExecutionResult] = {}

    def preview_dry_run(
        self,
        *,
        pack: PackConfig,
        incident: IncidentRecord,
        option: RemediationOption,
        verdict: IncidentVerdict,
    ) -> dict[str, Any]:
        """Step 1 of two-click workflow: compute policy audit, SHA-256 digest, and dry-run diff."""
        assert_valid_transition(IncidentState.TRIAGED, IncidentState.DRY_RUN_PREVIEWED)
        policy = check_remediation_policy(
            verdict=verdict,
            option=option,
            margin_floor_ratio=pack.margin_floor_ratio,
        )
        return {
            "incident_id": incident.incident_id,
            "pack_id": pack.pack_id,
            "option_id": option.option_id,
            "title": option.title,
            "action_type": option.action_type,
            "workflow_name": option.workflow_name,
            "state": IncidentState.DRY_RUN_PREVIEWED.value,
            "plan_sha256": option.plan_sha256,
            "command_or_diff": option.command_or_diff,
            "estimated_action_cost_usd": option.estimated_action_cost_usd,
            "avoided_loss_usd": option.avoided_loss_usd,
            "net_value_usd": option.net_value_usd,
            "compensation_steps": list(option.compensation_steps),
            "policy_allowed": policy.allowed,
            "requires_vp_escalation": policy.requires_vp_escalation,
            "policy_reason": policy.reason,
        }

    def record_lead_approval(
        self,
        *,
        pack: PackConfig,
        incident: IncidentRecord,
        option: RemediationOption,
        verdict: IncidentVerdict,
        approver_identity: str,
        idempotency_key: str,
        persist_firestore: bool = False,
    ) -> ApprovalRecord:
        """Step 2a: Persist immutable SHA-256 lead approval token (fail-closed on benign alerts)."""
        assert_valid_transition(IncidentState.DRY_RUN_PREVIEWED, IncidentState.APPROVED_BY_LEAD)
        policy = check_remediation_policy(
            verdict=verdict,
            option=option,
            margin_floor_ratio=pack.margin_floor_ratio,
        )
        if not policy.allowed:
            raise PermissionError(policy.reason)
        if option.action_type == "no_action_benign_clear":
            raise PermissionError("Benign clear requires dismissal, not remediation workflow approval.")

        clean_key = idempotency_key.strip()
        if not clean_key:
            raise ValueError("idempotency_key must be non-empty")

        with self._lock:
            existing = self._approvals.get(clean_key)
            if existing is not None:
                if (
                    existing.incident_id == incident.incident_id
                    and existing.option_id == option.option_id
                    and existing.plan_sha256 == option.plan_sha256
                ):
                    return existing
                raise ValueError(
                    f"Idempotency key {clean_key!r} already bound to a different remediation plan."
                )

            record = ApprovalRecord(
                incident_id=incident.incident_id,
                pack_id=pack.pack_id,
                option_id=option.option_id,
                plan_sha256=option.plan_sha256,
                idempotency_key=clean_key,
                approver_identity=(approver_identity or "war-room-lead").strip()[:120],
                approved_at_iso=_utc_now(),
                verdict=verdict,
                approval_token=secrets.token_urlsafe(24),
                state=IncidentState.APPROVED_BY_LEAD,
            )
            self._approvals[clean_key] = record

        if persist_firestore:
            self._write_firestore_doc("approvals", clean_key, asdict(record))
        return record

    def _get_approval(self, idempotency_key: str, *, use_firestore: bool = False) -> ApprovalRecord | None:
        with self._lock:
            cached = self._approvals.get(idempotency_key)
            if cached is not None:
                return cached
        if use_firestore and self._gcp_clients is not None:
            fs = self._gcp_clients.firestore()
            snap = fs.collection("approvals").document(idempotency_key).get()
            if snap.exists:
                d = snap.to_dict() or {}
                rec = ApprovalRecord(
                    incident_id=str(d["incident_id"]),
                    pack_id=str(d["pack_id"]),
                    option_id=str(d["option_id"]),
                    plan_sha256=str(d["plan_sha256"]),
                    idempotency_key=str(d["idempotency_key"]),
                    approver_identity=str(d["approver_identity"]),
                    approved_at_iso=str(d["approved_at_iso"]),
                    verdict=IncidentVerdict(d["verdict"]),
                    approval_token=str(d["approval_token"]),
                    state=IncidentState(d["state"]),
                )
                with self._lock:
                    self._approvals[idempotency_key] = rec
                return rec
        return None

    def _write_firestore_doc(self, collection: str, doc_id: str, payload: dict[str, Any]) -> None:
        if self._gcp_clients is None:
            raise RuntimeError("GCPClientPool required to persist state to Firestore.")
        fs = self._gcp_clients.firestore()
        serializable = json.loads(json.dumps(payload, default=str))
        fs.collection(collection).document(doc_id).set(serializable)

    def execute_approved_saga(
        self,
        *,
        pack: PackConfig,
        incident: IncidentRecord,
        option: RemediationOption,
        approval_token: str,
        idempotency_key: str,
        dry_run: bool = True,
        simulate_step2_failure: bool = False,
        live_workflows: bool = False,
    ) -> SagaExecutionResult:
        """Step 2b: Fail-closed execution of Cloud Workflows atomic saga with automatic rollback."""
        clean_key = idempotency_key.strip()
        with self._lock:
            prior = self._executions.get(clean_key)
            if prior is not None and not simulate_step2_failure:
                return prior

        approval = self._get_approval(clean_key, use_firestore=live_workflows)
        verify_execution_approval(
            incident_id=incident.incident_id,
            option=option,
            approval=approval,
            idempotency_key=clean_key,
        )
        assert approval is not None
        if not secrets.compare_digest(approval.approval_token, (approval_token or "").strip()):
            raise PermissionError("Execution denied: invalid approval_token.")

        assert_valid_transition(approval.state, IncidentState.SAGA_EXECUTING)

        if live_workflows:
            result = self._invoke_cloud_workflow_live(
                incident=incident,
                option=option,
                approval=approval,
                dry_run=dry_run,
                simulate_step2_failure=simulate_step2_failure,
            )
            self._write_firestore_doc("saga_executions", result.execution_id, asdict(result))
        else:
            result = self._execute_sandbox_saga(
                incident=incident,
                option=option,
                approval=approval,
                dry_run=dry_run,
                simulate_step2_failure=simulate_step2_failure,
            )

        with self._lock:
            self._executions[clean_key] = result
        return result

    def _invoke_cloud_workflow_live(
        self,
        *,
        incident: IncidentRecord,
        option: RemediationOption,
        approval: ApprovalRecord,
        dry_run: bool,
        simulate_step2_failure: bool,
    ) -> SagaExecutionResult:
        if self._gcp_clients is None:
            raise RuntimeError("GCPClientPool required for live Cloud Workflows execution.")
        from google.cloud.workflows.executions_v1.types import Execution

        client = self._gcp_clients.workflows_executions()
        parent = (
            f"projects/{self._gcp_clients.project_id}"
            f"/locations/{self._settings.workflow_location}"
            f"/workflows/{option.workflow_name}"
        )
        wf_args = {
            "incident_id": incident.incident_id,
            "order_id": incident.incident_id.upper(),
            "plan_sha256": approval.plan_sha256,
            "idempotency_key": approval.idempotency_key,
            "dry_run": dry_run,
            "simulate_step2_failure": simulate_step2_failure,
        }
        created = client.create_execution(
            parent=parent,
            execution=Execution(argument=json.dumps(wf_args)),
        )
        exec_name = created.name
        exec_id = exec_name.rsplit("/", 1)[-1]

        deadline = time.time() + 30.0
        polled = created
        while polled.state == Execution.State.ACTIVE and time.time() < deadline:
            time.sleep(0.5)
            polled = client.get_execution(name=exec_name)

        if polled.state != Execution.State.SUCCEEDED:
            raise RuntimeError(
                f"Cloud Workflow {option.workflow_name} failed with state={polled.state.name}: "
                f"{getattr(polled.error, 'context', '')}"
            )

        body = json.loads(polled.result or "{}")
        final_state = IncidentState(body.get("final_state", "SAGA_COMPLETED"))
        assert_valid_transition(IncidentState.SAGA_EXECUTING, final_state)

        steps = tuple(
            SagaStepResult(
                step_id=str(s.get("step_id", f"step_{idx}")),
                title=str(s.get("step_id", f"Step {idx + 1}")),
                status=s.get("status", "SUCCEEDED"),
                system=str(s.get("system", "Cloud Workflows")),
                duration_ms=120,
                detail=str(s.get("detail", "")),
            )
            for idx, s in enumerate(body.get("steps", []))
        )
        return SagaExecutionResult(
            execution_id=exec_id,
            incident_id=incident.incident_id,
            workflow_name=option.workflow_name,
            idempotency_key=approval.idempotency_key,
            mode="live_workflow",
            final_state=final_state,
            steps=steps,
            compensation_triggered=bool(body.get("compensation_triggered", False)),
            audit_persisted=True,
        )

    def _execute_sandbox_saga(
        self,
        *,
        incident: IncidentRecord,
        option: RemediationOption,
        approval: ApprovalRecord,
        dry_run: bool,
        simulate_step2_failure: bool,
    ) -> SagaExecutionResult:
        """Deterministic offline mirror of infra/workflows/*.yaml for Replay & unit tests."""
        exec_id = f"sim-{secrets.token_hex(6)}"
        mode_tag = "dry_run" if dry_run else "simulated_workflow"

        if option.workflow_name == self._settings.workflow_o2c:
            step1 = SagaStepResult(
                step_id="step1_wms",
                title="Reserve Regional WMS Stock",
                status="SUCCEEDED",
                system="WMS Sandbox",
                duration_ms=85,
                detail=f"Reserved alternate hub stock for {incident.incident_id} (digest {approval.plan_sha256[:12]})",
            )
            if simulate_step2_failure:
                step2 = SagaStepResult(
                    step_id="step2_carrier",
                    title="Book Expedited Carrier Reroute",
                    status="FAILED",
                    system="Carrier API",
                    duration_ms=140,
                    detail="HTTP 503 Carrier API timeout during expedite booking (simulated fault)",
                )
                comp = SagaStepResult(
                    step_id="comp1_wms_release",
                    title="Compensate: Release WMS Reservation",
                    status="COMPENSATED",
                    system="WMS Sandbox",
                    duration_ms=65,
                    detail="Automatically released WMS stock reservation — zero orphaned inventory",
                )
                assert_valid_transition(IncidentState.SAGA_EXECUTING, IncidentState.SAGA_ROLLED_BACK)
                return SagaExecutionResult(
                    execution_id=exec_id,
                    incident_id=incident.incident_id,
                    workflow_name=option.workflow_name,
                    idempotency_key=approval.idempotency_key,
                    mode=mode_tag,
                    final_state=IncidentState.SAGA_ROLLED_BACK,
                    steps=(step1, step2, comp),
                    compensation_triggered=True,
                    audit_persisted=False,
                )
            step2 = SagaStepResult(
                step_id="step2_carrier",
                title="Book Expedited Carrier Reroute",
                status="SUCCEEDED",
                system="Carrier API",
                duration_ms=110,
                detail=f"Booked expedite transit (${option.estimated_action_cost_usd:,.0f}, dry_run={dry_run})",
            )
            step3 = SagaStepResult(
                step_id="step3_erp",
                title="Commit ERP Split-Shipment Allocation",
                status="SUCCEEDED",
                system="ERP Ledger",
                duration_ms=95,
                detail=f"Committed order allocation protecting ${option.avoided_loss_usd:,.0f} SLA",
            )
            assert_valid_transition(IncidentState.SAGA_EXECUTING, IncidentState.SAGA_COMPLETED)
            return SagaExecutionResult(
                execution_id=exec_id,
                incident_id=incident.incident_id,
                workflow_name=option.workflow_name,
                idempotency_key=approval.idempotency_key,
                mode=mode_tag,
                final_state=IncidentState.SAGA_COMPLETED,
                steps=(step1, step2, step3),
                compensation_triggered=False,
                audit_persisted=False,
            )

        # SecOps containment & SRE rollback sagas
        sys_primary = "Cloud IAM / GCS" if "secops" in option.workflow_name else "Cloud Run Traffic"
        step1 = SagaStepResult(
            step_id="step1_snapshot",
            title="Snapshot Baseline State",
            status="SUCCEEDED",
            system=sys_primary,
            duration_ms=70,
            detail=f"Captured immutable pre-change snapshot for {incident.incident_id}",
        )
        if simulate_step2_failure:
            step2 = SagaStepResult(
                step_id="step2_apply",
                title="Apply Approved Diff",
                status="FAILED",
                system=sys_primary,
                duration_ms=130,
                detail="Simulated policy ETag / post-shift health probe failure",
            )
            comp = SagaStepResult(
                step_id="comp1_restore",
                title="Compensate: Restore Baseline Snapshot",
                status="COMPENSATED",
                system=sys_primary,
                duration_ms=60,
                detail="Automatically restored baseline policy/traffic snapshot safely",
            )
            assert_valid_transition(IncidentState.SAGA_EXECUTING, IncidentState.SAGA_ROLLED_BACK)
            return SagaExecutionResult(
                execution_id=exec_id,
                incident_id=incident.incident_id,
                workflow_name=option.workflow_name,
                idempotency_key=approval.idempotency_key,
                mode=mode_tag,
                final_state=IncidentState.SAGA_ROLLED_BACK,
                steps=(step1, step2, comp),
                compensation_triggered=True,
                audit_persisted=False,
            )

        step2 = SagaStepResult(
            step_id="step2_apply",
            title="Apply Approved Diff",
            status="SUCCEEDED",
            system=sys_primary,
            duration_ms=115,
            detail=f"Applied approved action {option.action_type} (dry_run={dry_run})",
        )
        step3 = SagaStepResult(
            step_id="step3_audit",
            title="Verify Health & Record Audit Receipt",
            status="SUCCEEDED",
            system="Audit Ledger",
            duration_ms=55,
            detail=f"Verified post-action health and logged SHA-256 digest {approval.plan_sha256[:12]}",
        )
        assert_valid_transition(IncidentState.SAGA_EXECUTING, IncidentState.SAGA_COMPLETED)
        return SagaExecutionResult(
            execution_id=exec_id,
            incident_id=incident.incident_id,
            workflow_name=option.workflow_name,
            idempotency_key=approval.idempotency_key,
            mode=mode_tag,
            final_state=IncidentState.SAGA_COMPLETED,
            steps=(step1, step2, step3),
            compensation_triggered=False,
            audit_persisted=False,
        )
