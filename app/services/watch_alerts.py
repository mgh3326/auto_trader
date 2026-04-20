from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import redis.asyncio as redis

from app.core.config import settings
from app.core.timezone import now_kst

_SUPPORTED_MARKETS = {"crypto", "kr", "us"}
_SUPPORTED_TARGET_KINDS = {"asset", "index", "fx"}
_SUPPORTED_CONDITIONS = {
    "price_above",
    "price_below",
    "rsi_above",
    "rsi_below",
    "trade_value_above",
    "trade_value_below",
}
_SUPPORTED_INDEX_SYMBOLS = {"KOSPI", "KOSDAQ"}

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
    target_kind: str
    symbol: str
    condition_type: str
    threshold: float
    threshold_key: str

    @property
    def field(self) -> str:
        return (
            f"{self.target_kind}:{self.symbol}:"
            f"{self.condition_type}:{self.threshold_key}"
        )

    @property
    def legacy_field(self) -> str:
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
    def _normalize_target_kind(target_kind: str | None) -> str:
        normalized = str(target_kind or "asset").strip().lower()
        if normalized not in _SUPPORTED_TARGET_KINDS:
            raise ValueError("target_kind must be one of: asset, index, fx")
        return normalized

    @staticmethod
    def _normalize_condition_type(condition_type: str) -> str:
        normalized = str(condition_type or "").strip().lower()
        if normalized not in _SUPPORTED_CONDITIONS:
            raise ValueError(
                "condition_type must be one of: price_above, price_below, "
                "rsi_above, rsi_below, trade_value_above, trade_value_below"
            )
        return normalized

    @staticmethod
    def _split_condition(condition_type: str) -> tuple[str, str]:
        metric, operator = condition_type.rsplit("_", 1)
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
        target_kind: str | None = None,
    ) -> _WatchKey:
        normalized_market = self._normalize_market(market)
        normalized_target_kind = self._normalize_target_kind(target_kind)
        normalized_symbol = self._normalize_symbol(symbol)
        normalized_condition = self._normalize_condition_type(condition_type)
        threshold_float, threshold_key = _normalize_threshold(threshold)
        metric, _operator = self._split_condition(normalized_condition)

        if normalized_condition.startswith("rsi_") and not (
            0.0 <= threshold_float <= 100.0
        ):
            raise ValueError("RSI threshold must be between 0 and 100")

        if normalized_target_kind == "asset":
            if metric == "trade_value" and normalized_market != "kr":
                raise ValueError("trade_value watches are supported for KR assets only")
            if metric not in {"price", "rsi", "trade_value"}:
                raise ValueError("asset watches support price, rsi, or trade_value")
        elif normalized_target_kind == "index":
            if normalized_market != "kr":
                raise ValueError("index watches are supported for market=kr only")
            if normalized_symbol not in _SUPPORTED_INDEX_SYMBOLS:
                raise ValueError("index symbol must be one of: KOSPI, KOSDAQ")
            if metric != "price":
                raise ValueError("index watches support price only")
        elif normalized_target_kind == "fx":
            if normalized_symbol != "USDKRW":
                raise ValueError("fx symbol must be USDKRW")
            if metric != "price":
                raise ValueError("fx watches support price only")

        return _WatchKey(
            target_kind=normalized_target_kind,
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
        target_kind: str | None = None,
    ) -> dict[str, object]:
        normalized_market = self._normalize_market(market)
        watch_key = self.validate_watch_inputs(
            market=normalized_market,
            symbol=symbol,
            condition_type=condition_type,
            threshold=threshold,
            target_kind=target_kind,
        )
        redis_client = await self._get_redis()
        redis_key = self._key_for_market(normalized_market)

        already_exists = await redis_client.hexists(redis_key, watch_key.field)
        existing_field = watch_key.field
        if not already_exists and watch_key.target_kind == "asset":
            already_exists = await redis_client.hexists(
                redis_key,
                watch_key.legacy_field,
            )
            if already_exists:
                existing_field = watch_key.legacy_field
        if already_exists:
            return {
                "market": normalized_market,
                "target_kind": watch_key.target_kind,
                "symbol": watch_key.symbol,
                "condition_type": watch_key.condition_type,
                "threshold": watch_key.threshold,
                "field": existing_field,
                "created": False,
                "already_exists": True,
            }

        payload = {"created_at": now_kst().isoformat()}
        await redis_client.hset(redis_key, watch_key.field, json.dumps(payload))

        return {
            "market": normalized_market,
            "target_kind": watch_key.target_kind,
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
        target_kind: str | None = None,
    ) -> dict[str, object]:
        normalized_market = self._normalize_market(market)
        watch_key = self.validate_watch_inputs(
            market=normalized_market,
            symbol=symbol,
            condition_type=condition_type,
            threshold=threshold,
            target_kind=target_kind,
        )
        removed = await self.trigger_and_remove(normalized_market, watch_key.field)
        if not removed and watch_key.target_kind == "asset":
            removed = await self.trigger_and_remove(
                normalized_market,
                watch_key.legacy_field,
            )
        return {
            "market": normalized_market,
            "target_kind": watch_key.target_kind,
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
                parts = field.split(":")
                try:
                    if len(parts) == 3:
                        target_kind = "asset"
                        symbol, condition_type, threshold_key = parts
                    elif len(parts) == 4:
                        target_kind, symbol, condition_type, threshold_key = parts
                        target_kind = self._normalize_target_kind(target_kind)
                    else:
                        raise ValueError
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

                normalized_symbol = self._normalize_symbol(symbol)
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
                        "target_kind": target_kind,
                        "symbol": normalized_symbol,
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
