"""Structured stage-by-stage SSE telemetry emitter supporting parallel agent DAG visualization."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass(frozen=True)
class StageEvent:
    stage_id: str
    label: str
    status: Literal["started", "completed", "failed"]
    service: str
    agent_node: str = "coordinator"
    arm: str | None = None
    duration_ms: int = 0
    records_count: int | None = None
    detail: str = ""
    sql_or_tool: str = ""
    timestamp_iso: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageHandle:
    stage_id: str
    label: str
    service: str
    agent_node: str
    arm: str | None
    started_mono: float


class StageEmitter:
    """Async event emitter pushing StageEvents onto an optional SSE queue."""

    def __init__(
        self,
        queue: asyncio.Queue[dict[str, Any]] | None = None,
        *,
        arm: str | None = None,
    ) -> None:
        self._queue = queue
        self._arm = arm
        self.events: list[StageEvent] = []

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    async def _push(self, event: StageEvent) -> None:
        self.events.append(event)
        if self._queue is not None:
            await self._queue.put({"type": "stage", "data": event.to_dict()})

    async def start(
        self,
        stage_id: str,
        label: str,
        *,
        service: str,
        agent_node: str = "coordinator",
        detail: str = "",
        arm: str | None = None,
    ) -> StageHandle:
        effective_arm = arm if arm is not None else self._arm
        handle = StageHandle(
            stage_id=stage_id,
            label=label,
            service=service,
            agent_node=agent_node,
            arm=effective_arm,
            started_mono=time.perf_counter(),
        )
        await self._push(
            StageEvent(
                stage_id=stage_id,
                label=label,
                status="started",
                service=service,
                agent_node=agent_node,
                arm=effective_arm,
                duration_ms=0,
                detail=detail,
                timestamp_iso=self._now_iso(),
            )
        )
        return handle

    async def complete(
        self,
        handle: StageHandle,
        *,
        records_count: int | None = None,
        detail: str = "",
        sql_or_tool: str = "",
    ) -> None:
        elapsed_ms = max(0, int((time.perf_counter() - handle.started_mono) * 1000))
        await self._push(
            StageEvent(
                stage_id=handle.stage_id,
                label=handle.label,
                status="completed",
                service=handle.service,
                agent_node=handle.agent_node,
                arm=handle.arm,
                duration_ms=elapsed_ms,
                records_count=records_count,
                detail=detail,
                sql_or_tool=sql_or_tool,
                timestamp_iso=self._now_iso(),
            )
        )

    async def fail(self, handle: StageHandle, *, error: BaseException | str) -> None:
        elapsed_ms = max(0, int((time.perf_counter() - handle.started_mono) * 1000))
        msg = str(error)[:300]
        await self._push(
            StageEvent(
                stage_id=handle.stage_id,
                label=handle.label,
                status="failed",
                service=handle.service,
                agent_node=handle.agent_node,
                arm=handle.arm,
                duration_ms=elapsed_ms,
                detail=msg,
                timestamp_iso=self._now_iso(),
            )
        )


class RecordingStageEmitter(StageEmitter):
    """Records all emitted stages cleanly for offline Replay fixtures."""

    def serialized_events(self) -> list[dict[str, Any]]:
        return [ev.to_dict() for ev in self.events]
