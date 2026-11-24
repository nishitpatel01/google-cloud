"""Single-Agent Serial Baseline Engine (comparison arm without parallel fan-out or quarantine)."""

from __future__ import annotations

import json
import time
from typing import Any

from app.core.config import MODEL_CATALOG, Settings
from app.core.evidence_verifier import check_remediation_policy, verify_incident_provenance
from app.core.gcp_clients import GCPClientPool, run_blocking
from app.core.models import IncidentRecord, IncidentVerdict, PackConfig
from app.core.pricing import estimate_query_cost
from app.core.stage_emitter import StageEmitter
from app.engines.base import (
    AgentDAGNode,
    BaseWarRoomEngine,
    TriageRequest,
    TriageResult,
    parse_verdict_and_action,
    select_recommended_option,
)
from app.engines.safe_bq_runner import SafeBigQueryRunner


class SingleAgentSerialEngine(BaseWarRoomEngine):
    """Traditional sequential single-prompt triage baseline (no parallel fan-out, no log sanitizer)."""

    engine_id = "single_agent_serial"

    def __init__(
        self,
        settings: Settings,
        *,
        bq_runner: SafeBigQueryRunner,
        gcp_clients: GCPClientPool | None = None,
    ) -> None:
        self._settings = settings
        self._bq_runner = bq_runner
        self._gcp_clients = gcp_clients

    def _serialize_events_raw(self, events: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [
            {
                "event_id": ev.event_id,
                "timestamp_iso": ev.timestamp_iso,
                "source_table": ev.source_table,
                "actor": ev.actor,
                "resource": ev.resource,
                "action": ev.action,
                "status": ev.status,
                "severity": ev.severity,
                "raw_message": ev.raw_message,
                "structured_fields": ev.structured_fields,
                "contains_prompt_injection": ev.contains_prompt_injection,
                "data_origin": ev.data_origin.value,
            }
            for ev in events
        ]

    async def investigate(
        self,
        req: TriageRequest,
        *,
        pack: PackConfig,
        incident: IncidentRecord,
        emitter: StageEmitter,
    ) -> TriageResult:
        t_start = time.perf_counter()
        dag_nodes: list[AgentDAGNode] = []

        # Step 1 (Serial): Query raw telemetry without adversarial quarantine
        h_sql = await emitter.start(
            "serial_telemetry",
            "Fetch Raw Telemetry Logs (Serial · No Quarantine)",
            service="BigQuery Telemetry",
            agent_node="serial_generalist",
        )
        t1 = time.perf_counter()
        telemetry_res = await run_blocking(
            self._bq_runner.fetch_incident_telemetry,
            pack,
            incident.incident_id,
            use_live_bq=req.use_live_bq,
        )
        sql_ms = int((time.perf_counter() - t1) * 1000)
        await emitter.complete(
            h_sql,
            records_count=len(telemetry_res.rows),
            detail=f"Fetched {len(telemetry_res.rows)} raw events sequentially ({telemetry_res.source})",
            sql_or_tool=telemetry_res.parameterized_sql,
        )

        # Step 2 (Serial): Fetch top generic intel rows without MCP correlation
        h_intel = await emitter.start(
            "serial_intel",
            "Fetch General Intel Slice (Serial)",
            service="BigQuery Intel Catalog",
            agent_node="serial_generalist",
        )
        intel_res = await run_blocking(
            self._bq_runner.fetch_pack_intel,
            pack,
            use_live_bq=req.use_live_bq,
        )
        # Baseline takes only top 4 unranked catalog entries (misses deep correlation)
        limited_intel = intel_res.rows[:4]
        await emitter.complete(
            h_intel,
            records_count=len(limited_intel),
            detail=f"Loaded top {len(limited_intel)} unranked intel records",
            sql_or_tool=intel_res.parameterized_sql,
        )

        # Step 3 (Serial): Single generalist Gemini prompt over raw unscreened logs
        h_gen = await emitter.start(
            "serial_synthesize",
            f"Single-Agent Generalist Triage ({req.synthesis_model})",
            service=f"Vertex AI {req.synthesis_model}",
            agent_node="serial_generalist",
        )
        t_gen = time.perf_counter()
        raw_events = self._serialize_events_raw(telemetry_res.rows)
        intel_dicts = [
            {
                "intel_id": r.intel_id,
                "intel_type": r.intel_type,
                "title": r.title,
                "summary": r.summary[:220],
                "source_name": r.source_name,
                "license_name": r.license_name,
                "data_origin": r.data_origin.value,
            }
            for r in limited_intel
        ]

        prompt = (
            "You are a generalist operations triage assistant. Review the incident and raw logs below.\n"
            "Start your response with two lines:\n"
            "VERDICT: <CONFIRMED_INCIDENT | BENIGN_FALSE_POSITIVE | NEEDS_VP_ESCALATION>\n"
            "RECOMMENDED_ACTION: <action_type>\n\n"
            "Then write a concise incident brief citing relevant event IDs as [EVENT:<event_id>] and "
            "intel IDs as [INTEL:<intel_id>].\n\n"
            f"Incident: {incident.incident_id} — {incident.title}\n"
            f"Summary: {incident.summary}\n"
            f"Raw Logs:\n{json.dumps(raw_events, indent=2)}\n\n"
            f"General Intel Sample:\n{json.dumps(intel_dicts, indent=2)}\n"
        )

        if self._gcp_clients is None:
            err = RuntimeError("GCPClientPool required for live SingleAgentSerialEngine execution.")
            await emitter.fail(h_gen, error=err)
            raise err

        from google.genai import types

        spec = MODEL_CATALOG[req.synthesis_model]
        client = self._gcp_clients.genai(location=spec.location)
        gen_cfg = types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=1024,
            labels=self._settings.gcp_labels(component="serial_baseline"),
        )
        try:
            resp = await run_blocking(
                client.models.generate_content,
                model=spec.model_id,
                contents=prompt,
                config=gen_cfg,
            )
        except Exception as exc:
            await emitter.fail(h_gen, error=exc)
            raise

        gen_ms = int((time.perf_counter() - t_gen) * 1000)
        brief_text = (getattr(resp, "text", None) or "").strip()
        usage = getattr(resp, "usage_metadata", None)
        in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
        await emitter.complete(
            h_gen,
            records_count=1,
            detail=f"Single generalist call complete ({in_tok} in / {out_tok} out tokens)",
        )

        dag_nodes.append(
            AgentDAGNode(
                node_id="serial_generalist",
                label="Single-Agent Serial Triage",
                role="Sequential generalist prompt without log quarantine or specialist fan-out",
                phase="parallel_specialist",
                status="completed",
                duration_ms=sql_ms + gen_ms,
                summary=f"Processed {len(raw_events)} raw logs sequentially in {gen_ms}ms",
                tool_name="BigQuery + Vertex AI",
                records_returned=len(raw_events),
                sql_or_payload=telemetry_res.parameterized_sql,
            )
        )

        verdict, parsed_action = parse_verdict_and_action(
            brief_text,
            fallback_verdict=IncidentVerdict.CONFIRMED_INCIDENT,
            fallback_action=incident.expected_action_type,
        )
        opt_id, action_type = select_recommended_option(
            incident,
            verdict=verdict,
            preferred_action=parsed_action,
        )

        # Deterministic post-audit (baseline did NOT run ReviewAndCritiqueAgent repair loop)
        h_ver = await emitter.start(
            "serial_verify",
            "Audit Citations & Grounding",
            service="Deterministic Provenance Verifier",
            agent_node="verifier",
        )
        prov = verify_incident_provenance(
            brief_text=brief_text,
            retrieved_event_ids={e["event_id"] for e in raw_events},
            retrieved_intel_ids={i["intel_id"] for i in intel_dicts},
            incident=incident,
            predicted_verdict=verdict,
        )
        await emitter.complete(
            h_ver,
            records_count=prov.total_citations,
            detail=f"Verified={prov.provenance_verified}, Recall={int(prov.root_cause_recall * 100)}%",
        )

        option_audits = []
        for opt in incident.remediation_options:
            pol = check_remediation_policy(
                verdict=verdict,
                option=opt,
                margin_floor_ratio=pack.margin_floor_ratio,
            )
            option_audits.append(
                {
                    "option_id": opt.option_id,
                    "title": opt.title,
                    "action_type": opt.action_type,
                    "workflow_name": opt.workflow_name,
                    "command_or_diff": opt.command_or_diff,
                    "estimated_action_cost_usd": opt.estimated_action_cost_usd,
                    "avoided_loss_usd": opt.avoided_loss_usd,
                    "net_value_usd": opt.net_value_usd,
                    "sla_risk_hours": opt.sla_risk_hours,
                    "plan_sha256": opt.plan_sha256,
                    "policy_allowed": pol.allowed,
                    "requires_vp_escalation": pol.requires_vp_escalation,
                    "policy_reason": pol.reason,
                    "is_recommended": opt.option_id == opt_id,
                    "compensation_steps": list(opt.compensation_steps),
                }
            )

        total_bytes = telemetry_res.bytes_billed + intel_res.bytes_billed
        cost = estimate_query_cost(
            model_id=req.synthesis_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            bq_bytes_billed=total_bytes,
            workflow_steps=0,
        )
        total_ms = int((time.perf_counter() - t_start) * 1000)
        return TriageResult(
            incident_id=incident.incident_id,
            pack_id=pack.pack_id,
            engine_id=self.engine_id,
            synthesis_model=req.synthesis_model,
            mode=req.mode,
            verdict=verdict,
            executive_brief=brief_text,
            recommended_option_id=opt_id,
            recommended_action_type=action_type,
            remediation_options=tuple(option_audits),
            prompt_injection_detected=False,  # Baseline lacks adversarial log sanitizer
            injection_signals=(),
            dag_nodes=tuple(dag_nodes),
            retrieved_events=tuple(raw_events),
            retrieved_intel=tuple(intel_dicts),
            retrieved_runbooks=(),
            provenance=prov,
            cost=cost,
            latency_ms=total_ms,
            stage_events=tuple(ev.to_dict() for ev in emitter.events),
        )
