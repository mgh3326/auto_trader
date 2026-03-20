from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import redis.asyncio as redis

from app.core.config import settings
from app.core.timezone import now_kst

_SUPPORTED_MARKETS = {"crypto", "kr", "us"}
_SUPPORTED_CONDITIONS = {
    "price_above",
    "price_below",
    "rsi_above",
    "rsi_below",
}

logger = logging.getLogger(__name__)


def _normalize_threshold(threshold: float) -> tuple[float, str]:
    threshold_float = float(threshold)
    canonical = format(threshold_float, ".15g")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical == "":
        canonical = "0"
    return threshold_float, canonical


@dataclass(slots=True)
class _WatchKey:
    symbol: str
    condition_type: str
    threshold: float
    threshold_key: str

    @property
    def field(self) -> str:
        return f"{self.symbol}:{self.condition_type}:{self.threshold_key}"


class WatchAlertService:
    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    @staticmethod
    def _normalize_market(market: str) -> str:
        normalized = str(market or "").strip().lower()
        if normalized not in _SUPPORTED_MARKETS:
            raise ValueError("market must be one of: crypto, kr, us")
        return normalized

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        return normalized

    @staticmethod
    def _normalize_condition_type(condition_type: str) -> str:
        normalized = str(condition_type or "").strip().lower()
        if normalized not in _SUPPORTED_CONDITIONS:
            raise ValueError(
                "condition_type must be one of: price_above, price_below, "
                "rsi_above, rsi_below"
            )
        return normalized

    @staticmethod
    def _split_condition(condition_type: str) -> tuple[str, str]:
        metric, operator = condition_type.split("_", 1)
        return metric, operator

    @staticmethod
    def _key_for_market(market: str) -> str:
        return f"watch:alerts:{market}"

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

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    def validate_watch_inputs(
        self,
        market: str,
        symbol: str,
        condition_type: str,
        threshold: float,
    ) -> _WatchKey:
        normalized_market = self._normalize_market(market)
        _ = normalized_market

        normalized_symbol = self._normalize_symbol(symbol)
        normalized_condition = self._normalize_condition_type(condition_type)
        threshold_float, threshold_key = _normalize_threshold(threshold)

        if normalized_condition.startswith("rsi_") and not (
            0.0 <= threshold_float <= 100.0
        ):
            raise ValueError("RSI threshold must be between 0 and 100")

        return _WatchKey(
            symbol=normalized_symbol,
            condition_type=normalized_condition,
            threshold=threshold_float,
            threshold_key=threshold_key,
        )

    async def add_watch(
        self,
        market: str,
        symbol: str,
        condition_type: str,
        threshold: float,
    ) -> dict[str, object]:
        normalized_market = self._normalize_market(market)
        watch_key = self.validate_watch_inputs(
            market=normalized_market,
            symbol=symbol,
            condition_type=condition_type,
            threshold=threshold,
        )
        redis_client = await self._get_redis()
        redis_key = self._key_for_market(normalized_market)

        already_exists = await redis_client.hexists(redis_key, watch_key.field)
        if already_exists:
            return {
                "market": normalized_market,
                "symbol": watch_key.symbol,
                "condition_type": watch_key.condition_type,
                "threshold": watch_key.threshold,
                "field": watch_key.field,
                "created": False,
                "already_exists": True,
            }

        payload = {"created_at": now_kst().isoformat()}
        await redis_client.hset(redis_key, watch_key.field, json.dumps(payload))

        return {
            "market": normalized_market,
            "symbol": watch_key.symbol,
            "condition_type": watch_key.condition_type,
            "threshold": watch_key.threshold,
            "field": watch_key.field,
            "created": True,
            "already_exists": False,
        }

    async def remove_watch(
        self,
        market: str,
        symbol: str,
        condition_type: str,
        threshold: float,
    ) -> dict[str, object]:
        normalized_market = self._normalize_market(market)
        watch_key = self.validate_watch_inputs(
            market=normalized_market,
            symbol=symbol,
            condition_type=condition_type,
            threshold=threshold,
        )
        removed = await self.trigger_and_remove(normalized_market, watch_key.field)
        return {
            "market": normalized_market,
            "symbol": watch_key.symbol,
            "condition_type": watch_key.condition_type,
            "threshold": watch_key.threshold,
            "field": watch_key.field,
            "removed": removed,
        }

    async def trigger_and_remove(self, market: str, field: str) -> bool:
        normalized_market = self._normalize_market(market)
        redis_client = await self._get_redis()
        removed = await redis_client.hdel(
            self._key_for_market(normalized_market), field
        )
        return bool(removed)

    async def get_watches_for_market(self, market: str) -> list[dict[str, object]]:
        normalized_market = self._normalize_market(market)
        listed = await self.list_watches(normalized_market)
        return listed[normalized_market]

    async def list_watches(
        self,
        market: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        markets: list[str]
        if market is None:
            markets = ["crypto", "kr", "us"]
        else:
            markets = [self._normalize_market(market)]

        redis_client = await self._get_redis()
        results: dict[str, list[dict[str, object]]] = {}

        for market_name in markets:
            redis_key = self._key_for_market(market_name)
            payloads = await redis_client.hgetall(redis_key)
            rows: list[dict[str, object]] = []
            for field, raw_payload in payloads.items():
                try:
                    symbol, condition_type, threshold_key = field.split(":", 2)
                except ValueError:
                    logger.warning(
                        "Skipping malformed watch field: market=%s field=%s",
                        market_name,
                        field,
                    )
                    continue

                try:
                    normalized_condition = self._normalize_condition_type(
                        condition_type
                    )
                except ValueError:
                    logger.warning(
                        "Skipping unsupported condition type: market=%s field=%s",
                        market_name,
                        field,
                    )
                    continue

                try:
                    threshold_value = float(threshold_key)
                except (TypeError, ValueError):
                    logger.warning(
                        "Skipping malformed threshold: market=%s field=%s",
                        market_name,
                        field,
                    )
                    continue

                metric, operator = self._split_condition(normalized_condition)

                created_at: str | None = None
                if raw_payload:
                    try:
                        parsed = json.loads(raw_payload)
                    except json.JSONDecodeError:
                        parsed = {}
                    if isinstance(parsed, dict):
                        value = parsed.get("created_at")
                        if isinstance(value, str):
                            created_at = value

                rows.append(
                    {
                        "market": market_name,
                        "symbol": symbol,
                        "condition_type": normalized_condition,
                        "metric": metric,
                        "operator": operator,
                        "threshold": threshold_value,
                        "field": field,
                        "created_at": created_at,
                    }
                )

            rows.sort(key=lambda row: str(row["field"]))
            results[market_name] = rows

        return results
