"""ROB-404 — Redis execution-event consumer for kis_mock fills.

Subscribes to ``execution:*``, hard-filters to kis_mock fills, dedupes by
correlation_id (Redis SETNX), and runs ``run_kis_mock_reconciliation`` for the
affected symbol (ROB-400 delta-budget kernel — no new fill matching). Default
off: reconcile runs dry-run unless ``KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.services.execution_event import _get_redis_client

logger = logging.getLogger(__name__)

_DEDUP_KEY = "kis_mock:exec_processed:{correlation_id}"
_DEDUP_TTL_SECONDS = 3600
_CHANNEL_PATTERN = "execution:*"


class KISMockExecutionConsumer:
    def __init__(
        self,
        *,
        redis_client: Any = None,
        reconcile_fn: Callable[..., Any] = run_kis_mock_reconciliation,
        session_factory: Callable[[], Any] = AsyncSessionLocal,
        force_dry_run: bool = False,
    ) -> None:
        self._redis = redis_client
        self._reconcile_fn = reconcile_fn
        self._session_factory = session_factory
        self._force_dry_run = force_dry_run
        self._stop = asyncio.Event()

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await _get_redis_client()
        return self._redis

    @staticmethod
    def _is_kis_mock_fill(event: dict) -> bool:
        if event.get("account_mode") != "kis_mock":
            return False
        if event.get("broker") != "kis":
            return False
        return event.get("fill_yn") == "Y" or str(event.get("execution_type")) == "1"

    async def handle_message(self, raw_message: str) -> str:
        try:
            event = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            return "ignored_unparseable"
        if not isinstance(event, dict) or not self._is_kis_mock_fill(event):
            return "ignored_non_mock_fill"

        symbol = event.get("symbol")
        correlation_id = event.get("correlation_id")
        if not symbol:
            logger.warning("kis_mock fill without symbol; skipping: %s", event)
            return "ignored_no_symbol"
        if not correlation_id:
            logger.warning(
                "kis_mock fill without correlation_id; cannot dedupe, skipping"
            )
            return "ignored_no_correlation_id"

        redis_client = await self._client()
        first = await redis_client.set(
            _DEDUP_KEY.format(correlation_id=correlation_id),
            "1",
            nx=True,
            ex=_DEDUP_TTL_SECONDS,
        )
        if not first:
            return "skipped_dedup"

        dry_run = self._force_dry_run or not (
            settings.KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED
        )
        async with self._session_factory() as db:
            await self._reconcile_fn(db, symbol=symbol, dry_run=dry_run)
        return "reconciled_dry_run" if dry_run else "reconciled"

    async def run(self) -> None:
        """Subscribe to execution:* and dispatch each fill to handle_message."""
        redis_client = await self._client()
        pubsub = redis_client.pubsub()
        await pubsub.psubscribe(_CHANNEL_PATTERN)
        logger.info("kis_mock execution consumer subscribed to %s", _CHANNEL_PATTERN)
        try:
            async for message in pubsub.listen():
                if self._stop.is_set():
                    break
                if message.get("type") != "pmessage":
                    continue
                try:
                    outcome = await self.handle_message(message["data"])
                    logger.debug("kis_mock execution event outcome=%s", outcome)
                except Exception:  # noqa: BLE001 - one bad event must not kill loop
                    logger.exception("kis_mock execution consumer handler failed")
        finally:
            await pubsub.punsubscribe(_CHANNEL_PATTERN)
            await pubsub.aclose()

    def request_stop(self) -> None:
        self._stop.set()
