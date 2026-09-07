"""Scheduleless artifact consumer for the closed Phase 0 boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.services.fill_watch_context.contracts import ContextArtifact
from app.services.fill_watch_context.repository import FillWatchContextOutcomeRepository
from app.services.fill_watch_context.service import (
    ConsumptionReceipt,
    FillWatchContextOutcomeService,
)

__all__ = [
    "ContextArtifactConsumer",
    "EVENT_LOOP_FLAG",
    "EventLoopDisabled",
    "build_default_consumer",
    "consume_once_if_armed",
]

EVENT_LOOP_FLAG = "FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED"


class EventLoopDisabled(RuntimeError):
    """Raised before a session is opened when the independent gate is false."""


AfterPersistHook = Callable[[ConsumptionReceipt], Awaitable[None]]


class ContextArtifactConsumer:
    """Consumes supplied artifacts one at a time; it owns no polling loop."""

    def __init__(self, session_factory: object) -> None:
        self._session_factory = session_factory

    async def consume_raw(
        self,
        raw_artifact: Mapping[str, Any],
        *,
        after_persist: AfterPersistHook | None = None,
    ) -> ConsumptionReceipt:
        artifact = ContextArtifact.model_validate(raw_artifact)
        async with self._session_factory() as session:  # type: ignore[operator]
            service = FillWatchContextOutcomeService(
                FillWatchContextOutcomeRepository(session)
            )
            receipt = await service.consume(artifact)
        # The hook is intentionally after commit. It is a testable crash seam:
        # a restart sees the durable UUID and cannot create a second outcome.
        if after_persist is not None:
            await after_persist(receipt)
        return receipt

    async def consume_batch(
        self, raw_artifacts: list[Mapping[str, Any]]
    ) -> list[ConsumptionReceipt]:
        """Preserve one result mapping for every batch input, including replays."""
        return [await self.consume_raw(raw_artifact) for raw_artifact in raw_artifacts]


def build_default_consumer() -> ContextArtifactConsumer:
    """Resolve the repository-local session factory only when explicitly run."""
    from app.core.db import AsyncSessionLocal

    return ContextArtifactConsumer(AsyncSessionLocal)


async def consume_once_if_armed(
    raw_artifact: Mapping[str, Any],
    *,
    settings_obj: object,
    consumer: ContextArtifactConsumer | None = None,
) -> ConsumptionReceipt:
    """Run exactly one artifact only when the new independent gate is true."""
    if not bool(getattr(settings_obj, EVENT_LOOP_FLAG, False)):
        raise EventLoopDisabled(f"{EVENT_LOOP_FLAG} is false")
    resolved_consumer = consumer if consumer is not None else build_default_consumer()
    return await resolved_consumer.consume_raw(raw_artifact)
