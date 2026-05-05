from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import uuid4

import redis.asyncio as redis
from pandas import Timestamp

from app.core.config import settings
from app.jobs.watch_scanner import WatchScanner
from app.services.openclaw_client import OpenClawClient
from app.services.watch_alerts import WatchAlertService
from app.services.watch_proximity import (
    WatchProximityResult,
    compute_price_proximity,
    format_proximity_message,
)

logger = logging.getLogger(__name__)

DEFAULT_PROXIMITY_COOLDOWN_SECONDS = 60 * 60


class WatchServiceProtocol(Protocol):
    async def get_watches_for_market(self, market: str) -> list[dict[str, object]]: ...

    async def close(self) -> None: ...


class DedupeStoreProtocol(Protocol):
    async def mark_if_new(self, key: str, ttl_seconds: int) -> bool: ...

    async def close(self) -> None: ...


CurrentValueProvider = Callable[
    ...,
    Awaitable[float | None],
]
MarketOpenProvider = Callable[[str], bool]
Notifier = Callable[..., Awaitable[Any]]


class RedisProximityDedupeStore:
    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(
                settings.get_redis_url(),
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        return self._redis

    async def mark_if_new(self, key: str, ttl_seconds: int) -> bool:
        redis_client = await self._get_redis()
        stored = await redis_client.set(
            f"watch:proximity:sent:{key}",
            "1",
            ex=ttl_seconds,
            nx=True,
        )
        return bool(stored)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None


class OpenClawProximityNotifier:
    def __init__(self) -> None:
        self._client = OpenClawClient()

    async def __call__(
        self,
        *,
        message: str,
        market: str,
        results: list[WatchProximityResult],
    ) -> object:
        triggered = [
            {
                "target_kind": result.target_kind,
                "symbol": result.symbol,
                "condition_type": result.condition_type,
                "threshold": result.threshold,
                "current": result.current,
                "distance_abs": result.distance_abs,
                "distance_pct": result.distance_pct,
                "band": result.band,
                "triggered": result.triggered,
            }
            for result in results
        ]
        return await self._client.send_watch_alert_to_router(
            message=message,
            market=market,
            triggered=triggered,
            as_of=Timestamp.now("UTC").isoformat(),
            correlation_id=str(uuid4()),
        )


class WatchProximityMonitor:
    def __init__(
        self,
        *,
        watch_service: WatchServiceProtocol | None = None,
        current_value_provider: CurrentValueProvider | None = None,
        market_open_provider: MarketOpenProvider | None = None,
        dedupe_store: DedupeStoreProtocol | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._scanner = WatchScanner()
        self._watch_service = watch_service or WatchAlertService()
        self._current_value_provider = (
            current_value_provider or self._scanner._get_current_value
        )
        self._market_open_provider = (
            market_open_provider or self._scanner._is_market_open
        )
        self._dedupe_store = dedupe_store or RedisProximityDedupeStore()
        self._notifier = notifier or OpenClawProximityNotifier()

    @staticmethod
    def _to_float(value: object) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _detail(result: WatchProximityResult) -> dict[str, object]:
        return {
            "market": result.market,
            "target_kind": result.target_kind,
            "symbol": result.symbol,
            "condition_type": result.condition_type,
            "threshold": result.threshold,
            "current": result.current,
            "distance_abs": result.distance_abs,
            "distance_pct": result.distance_pct,
            "band": result.band,
            "triggered": result.triggered,
            "dedupe_key": result.dedupe_key,
        }

    async def scan_market(
        self,
        market: str,
        *,
        cooldown_seconds: int = DEFAULT_PROXIMITY_COOLDOWN_SECONDS,
    ) -> dict[str, object]:
        normalized_market = str(market or "").strip().lower()
        watches = await self._watch_service.get_watches_for_market(normalized_market)
        market_open = self._market_open_provider(normalized_market)

        if not market_open:
            return {
                "market": normalized_market,
                "status": "skipped",
                "reason": "market_closed",
                "market_open": False,
                "evaluated": 0,
                "skipped": len(watches),
                "unsupported": 0,
                "notified": 0,
                "deduped": 0,
                "details": [],
            }

        evaluated = 0
        skipped = 0
        unsupported = 0
        deduped = 0
        proximity_results: list[WatchProximityResult] = []
        notify_results: list[WatchProximityResult] = []

        for watch in watches:
            target_kind = str(watch.get("target_kind") or "asset").strip().lower()
            symbol = str(watch.get("symbol") or "").strip().upper()
            condition_type = str(watch.get("condition_type") or "").strip().lower()
            threshold = self._to_float(watch.get("threshold"))

            if (
                not symbol
                or not condition_type
                or threshold is None
                or not condition_type.startswith("price_")
            ):
                skipped += 1
                if condition_type and not condition_type.startswith("price_"):
                    unsupported += 1
                continue

            current = await self._current_value_provider(
                target_kind=target_kind,
                metric="price",
                symbol=symbol,
                market=normalized_market,
            )
            current_value = self._to_float(current)
            if current_value is None:
                skipped += 1
                continue

            evaluated += 1
            try:
                result = compute_price_proximity(
                    market=normalized_market,
                    target_kind=target_kind,
                    symbol=symbol,
                    condition_type=condition_type,
                    threshold=threshold,
                    current=current_value,
                )
            except ValueError:
                skipped += 1
                unsupported += 1
                continue

            if result.band == "outside":
                continue

            proximity_results.append(result)
            if await self._dedupe_store.mark_if_new(
                result.dedupe_key,
                cooldown_seconds,
            ):
                notify_results.append(result)
            else:
                deduped += 1

        if notify_results:
            message = format_proximity_message(notify_results)
            try:
                await self._notifier(
                    message=message,
                    market=normalized_market,
                    results=notify_results,
                )
            except Exception as exc:
                logger.error("Failed to send watch proximity alert: %s", exc)
                return {
                    "market": normalized_market,
                    "status": "failed",
                    "reason": "notification_failed",
                    "market_open": market_open,
                    "evaluated": evaluated,
                    "skipped": skipped,
                    "unsupported": unsupported,
                    "notified": 0,
                    "deduped": deduped,
                    "details": [self._detail(result) for result in proximity_results],
                }

        if notify_results:
            status = "success"
            reason = None
        elif proximity_results and deduped == len(proximity_results):
            status = "skipped"
            reason = "all_notifications_deduped"
        else:
            status = "skipped"
            reason = "no_proximity_alerts"

        response: dict[str, object] = {
            "market": normalized_market,
            "status": status,
            "market_open": market_open,
            "evaluated": evaluated,
            "skipped": skipped,
            "unsupported": unsupported,
            "notified": len(notify_results),
            "deduped": deduped,
            "details": [self._detail(result) for result in proximity_results],
        }
        if reason is not None:
            response["reason"] = reason
        return response

    async def run(
        self,
        *,
        cooldown_seconds: int = DEFAULT_PROXIMITY_COOLDOWN_SECONDS,
    ) -> dict[str, dict[str, object]]:
        results: dict[str, dict[str, object]] = {}
        for market in ("crypto", "kr", "us"):
            market_result = await self.scan_market(
                market,
                cooldown_seconds=cooldown_seconds,
            )
            results[market] = dict(market_result)
        return results

    async def close(self) -> None:
        await self._watch_service.close()
        await self._dedupe_store.close()
        await self._scanner.close()


__all__ = [
    "DEFAULT_PROXIMITY_COOLDOWN_SECONDS",
    "RedisProximityDedupeStore",
    "WatchProximityMonitor",
]
