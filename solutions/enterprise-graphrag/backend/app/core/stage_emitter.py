"""Measured-not-simulated stage telemetry emitter for SSE streaming and recording."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.models import (
    StageCompletedEvent,
    StageEvent,
    StageFailedEvent,
    StageSkippedEvent,
    StageStartedEvent,
)


@dataclass(frozen=True)
class StageHandle:
    """Opaque timing handle returned by StageEmitter.started."""

    stage: str
    service: str
    started_perf: float


class StageEmitter(Protocol):
    """Contract enforced across all engines; engines emit real timings, routes only forward."""

    async def started(
        self,
        stage: str,
        *,
        label: str,
        service: str,
        request: dict[str, Any] | None = None,
    ) -> StageHandle: ...

    async def completed(
        self,
        handle: StageHandle,
        *,
        service_used: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None: ...

    async def failed(
        self,
        handle: StageHandle,
        *,
        error: BaseException,
    ) -> None: ...

    async def skipped(
        self,
        stage: str,
        *,
        reason: str,
    ) -> None: ...


class RecordingStageEmitter:
    """In-memory stage event collector with optional real-time SSE asyncio.Queue fan-out."""

    def __init__(self, sse_queue: asyncio.Queue[dict[str, Any]] | None = None) -> None:
        self._events: list[StageEvent] = []
        self._queue = sse_queue

    @property
    def events(self) -> tuple[StageEvent, ...]:
        return tuple(self._events)

    async def _push(self, event: StageEvent) -> None:
        self._events.append(event)
        if self._queue is not None:
            await self._queue.put(event.model_dump(mode="json"))

    async def started(
        self,
        stage: str,
        *,
        label: str,
        service: str,
        request: dict[str, Any] | None = None,
    ) -> StageHandle:
        now = time.perf_counter()
        await self._push(
            StageStartedEvent(
                stage=stage,
                label=label,
                service=service,
                request=dict(request or {}),
            )
        )
        return StageHandle(stage=stage, service=service, started_perf=now)

    async def completed(
        self,
        handle: StageHandle,
        *,
        service_used: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        duration_ms = round((time.perf_counter() - handle.started_perf) * 1000.0, 2)
        await self._push(
            StageCompletedEvent(
                stage=handle.stage,
                service_used=service_used or handle.service,
                duration_ms=duration_ms,
                payload=dict(payload or {}),
            )
        )

    async def failed(
        self,
        handle: StageHandle,
        *,
        error: BaseException,
    ) -> None:
        duration_ms = round((time.perf_counter() - handle.started_perf) * 1000.0, 2)
        await self._push(
            StageFailedEvent(
                stage=handle.stage,
                error_type=type(error).__name__,
                error_message=str(error)[:400],
                duration_ms=duration_ms,
            )
        )

    async def skipped(
        self,
        stage: str,
        *,
        reason: str,
    ) -> None:
        await self._push(StageSkippedEvent(stage=stage, reason=reason))
