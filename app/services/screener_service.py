from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as redis
from redis.exceptions import WatchError

from app.core.config import settings
from app.mcp_server.tooling.analysis_tool_handlers import screen_stocks_impl
from app.mcp_server.tooling.order_execution import _place_order_impl
from app.services.openclaw_client import OpenClawClient

ScreenMarket = Literal["kr", "us", "crypto"]


@dataclass(slots=True)
class _ReportKeys:
    result_key: str
    inflight_key: str
    status_key: str
    job_key: str


class ScreenerService:
    SCREENING_CACHE_TTL_SECONDS = 300
    REPORT_CACHE_TTL_SECONDS = 3600
    REPORT_INFLIGHT_TTL_SECONDS = 120
    REPORT_STATUSES = frozenset({"queued", "running", "completed", "failed"})
    TERMINAL_REPORT_STATUSES = frozenset({"completed", "failed"})
    REPORT_STATUS_ORDER = {
        "queued": 0,
        "running": 1,
        "completed": 2,
        "failed": 2,
    }

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        openclaw_client: OpenClawClient | None = None,
    ) -> None:
        self._redis = redis_client
        self._openclaw = openclaw_client or OpenClawClient()

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

    @staticmethod
    def _normalize_market(market: str) -> ScreenMarket:
        normalized = (market or "").strip().lower()
        if normalized in {"kr", "kospi", "kosdaq"}:
            return "kr"
        if normalized in {"us", "nasdaq", "nyse"}:
            return "us"
        if normalized == "crypto":
            return "crypto"
        raise ValueError("market must be one of: kr, us, crypto")

    @staticmethod
    def _normalize_symbol(market: ScreenMarket, symbol: str) -> str:
        raw = (symbol or "").strip()
        if not raw:
            raise ValueError("symbol is required")
        if market == "kr":
            return raw.upper()
        if market == "us":
            return raw.upper()
        return raw.upper()

    @staticmethod
    def _instrument_type(market: ScreenMarket) -> str:
        mapping = {
            "kr": "equity_kr",
            "us": "equity_us",
            "crypto": "crypto",
        }
        return mapping[market]

    @staticmethod
    def _market_from_instrument_type(instrument_type: str | None) -> ScreenMarket:
        normalized = (instrument_type or "").strip().lower()
        mapping: dict[str, ScreenMarket] = {
            "equity_kr": "kr",
            "equity_us": "us",
            "crypto": "crypto",
        }
        if normalized in mapping:
            return mapping[normalized]
        raise ValueError("instrument_type must be one of: equity_kr, equity_us, crypto")

    @staticmethod
    def _compact_json(data: dict[str, Any]) -> str:
        return json.dumps(
            data, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    @staticmethod
    def _normalize_sort_by(market: ScreenMarket, sort_by: str | None) -> str | None:
        normalized = (sort_by or "").strip().lower() or None
        if market == "crypto" and normalized == "volume":
            return "trade_amount"
        return normalized

    @staticmethod
    def _normalize_min_volume(min_volume: float | None) -> float | None:
        if min_volume is None:
            return None
        if min_volume < 0:
            raise ValueError("min_volume must be >= 0")
        return min_volume

    @staticmethod
    def _calculate_overfetch_limit(request_limit: int) -> int:
        return min(50, max(request_limit * 3, request_limit))

    @staticmethod
    def _volume_metric_for_row(market: ScreenMarket, row: dict[str, Any]) -> float:
        raw_value = (
            row.get("trade_amount_24h") if market == "crypto" else row.get("volume")
        )
        try:
            return float(raw_value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _apply_min_volume_filter(
        cls,
        result: dict[str, Any],
        *,
        market: ScreenMarket,
        min_volume: float | None,
        request_limit: int,
    ) -> dict[str, Any]:
        if min_volume is None:
            return result

        raw_results = result.get("results")
        if not isinstance(raw_results, list):
            raw_results = []

        filtered_results = [
            row
            for row in raw_results
            if isinstance(row, dict)
            and cls._volume_metric_for_row(market, row) >= min_volume
        ]
        sliced_results = filtered_results[:request_limit]

        filters_applied = result.get("filters_applied")
        normalized_filters_applied = (
            dict(filters_applied) if isinstance(filters_applied, dict) else {}
        )
        normalized_filters_applied["min_volume"] = min_volume

        return {
            **result,
            "results": sliced_results,
            "total_count": len(filtered_results),
            "returned_count": len(sliced_results),
            "filters_applied": normalized_filters_applied,
        }

    def _screening_cache_key(self, filters: dict[str, Any]) -> str:
        serialized = self._compact_json(filters)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        return f"screener:list:{digest}"

    def _report_keys(
        self, market: ScreenMarket, symbol: str, job_id: str
    ) -> _ReportKeys:
        return _ReportKeys(
            result_key=f"screener:report:result:{market}:{symbol}",
            inflight_key=f"screener:report:inflight:{market}:{symbol}",
            status_key=f"screener:report:status:{job_id}",
            job_key=f"screener:report:job:{job_id}",
        )

    @classmethod
    def _normalize_report_status(cls, status: str | None) -> str | None:
        normalized = (status or "").strip().lower()
        if normalized in cls.REPORT_STATUSES:
            return normalized
        return None

    @classmethod
    def _can_transition_report_status(
        cls, current: str | None, next_status: str
    ) -> bool:
        normalized_next = cls._normalize_report_status(next_status)
        if normalized_next is None:
            raise ValueError(f"invalid report status: {next_status}")

        normalized_current = cls._normalize_report_status(current)
        if normalized_current is None:
            return True
        if normalized_current in cls.TERMINAL_REPORT_STATUSES:
            return False
        return (
            cls.REPORT_STATUS_ORDER[normalized_next]
            >= cls.REPORT_STATUS_ORDER[normalized_current]
        )

    async def _transition_report_status(
        self,
        status_key: str,
        next_status: str,
        *,
        redis_client: redis.Redis | None = None,
    ) -> str:
        normalized_next = self._normalize_report_status(next_status)
        if normalized_next is None:
            raise ValueError(f"invalid report status: {next_status}")

        if redis_client is None:
            redis_client = await self._get_redis()

        pipeline_factory = getattr(redis_client, "pipeline", None)
        if not callable(pipeline_factory):
            return await self._transition_report_status_non_atomic(
                redis_client,
                status_key,
                normalized_next,
            )

        for _ in range(3):
            pipeline = pipeline_factory(transaction=True)
            try:
                await pipeline.watch(status_key)
                current_status = self._normalize_report_status(
                    await pipeline.get(status_key)
                )
                if not self._can_transition_report_status(
                    current_status, normalized_next
                ):
                    return current_status or normalized_next

                pipeline.multi()
                pipeline.setex(
                    status_key,
                    self.REPORT_CACHE_TTL_SECONDS,
                    normalized_next,
                )
                await pipeline.execute()
                return normalized_next
            except WatchError:
                continue
            finally:
                await pipeline.reset()

        return await self._transition_report_status_non_atomic(
            redis_client,
            status_key,
            normalized_next,
        )

    async def _transition_report_status_non_atomic(
        self,
        redis_client: redis.Redis,
        status_key: str,
        normalized_next: str,
    ) -> str:
        current_status = self._normalize_report_status(
            await redis_client.get(status_key)
        )
        if not self._can_transition_report_status(current_status, normalized_next):
            return current_status or normalized_next

        await redis_client.setex(
            status_key,
            self.REPORT_CACHE_TTL_SECONDS,
            normalized_next,
        )
        return normalized_next

    async def _load_cached_json(self, key: str) -> dict[str, Any] | None:
        redis_client = await self._get_redis()
        raw = await redis_client.get(key)
        if not raw:
            return None
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return None

    async def _store_json(self, key: str, ttl: int, data: dict[str, Any]) -> None:
        redis_client = await self._get_redis()
        await redis_client.setex(key, ttl, self._compact_json(data))

    async def list_screening(
        self,
        market: str = "kr",
        asset_type: str | None = None,
        category: str | None = None,
        strategy: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        max_rsi: float | None = None,
        min_volume: float | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_market = self._normalize_market(market)
        normalized_sort_by = self._normalize_sort_by(normalized_market, sort_by)
        normalized_min_volume = self._normalize_min_volume(min_volume)
        request_limit = limit
        filters = {
            "market": normalized_market,
            "asset_type": asset_type,
            "category": category,
            "strategy": strategy,
            "sort_by": normalized_sort_by,
            "sort_order": sort_order,
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "min_dividend_yield": min_dividend_yield,
            "max_rsi": max_rsi,
            "min_volume": normalized_min_volume,
            "limit": request_limit,
        }
        cache_key = self._screening_cache_key(filters)
        cached = await self._load_cached_json(cache_key)
        if cached:
            return {**cached, "cache_hit": True}

        call_kwargs = {
            key: value
            for key, value in filters.items()
            if value is not None and key != "min_volume"
        }
        if normalized_min_volume is not None:
            call_kwargs["limit"] = self._calculate_overfetch_limit(request_limit)

        result = await screen_stocks_impl(**call_kwargs)
        filtered_result = self._apply_min_volume_filter(
            result,
            market=normalized_market,
            min_volume=normalized_min_volume,
            request_limit=request_limit,
        )
        await self._store_json(
            cache_key,
            self.SCREENING_CACHE_TTL_SECONDS,
            filtered_result,
        )
        return {**filtered_result, "cache_hit": False}

    async def refresh_screening(
        self,
        market: str = "kr",
        asset_type: str | None = None,
        category: str | None = None,
        strategy: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        max_rsi: float | None = None,
        min_volume: float | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_market = self._normalize_market(market)
        normalized_sort_by = self._normalize_sort_by(normalized_market, sort_by)
        normalized_min_volume = self._normalize_min_volume(min_volume)
        filters = {
            "market": normalized_market,
            "asset_type": asset_type,
            "category": category,
            "strategy": strategy,
            "sort_by": normalized_sort_by,
            "sort_order": sort_order,
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "min_dividend_yield": min_dividend_yield,
            "max_rsi": max_rsi,
            "min_volume": normalized_min_volume,
            "limit": limit,
        }
        cache_key = self._screening_cache_key(filters)
        redis_client = await self._get_redis()
        await redis_client.delete(cache_key)
        return await self.list_screening(**filters)

    async def request_report(
        self, market: str, symbol: str, name: str | None = None
    ) -> dict[str, Any]:
        normalized_market = self._normalize_market(market)
        normalized_symbol = self._normalize_symbol(normalized_market, symbol)
        report_key = f"screener:report:result:{normalized_market}:{normalized_symbol}"
        inflight_key = (
            f"screener:report:inflight:{normalized_market}:{normalized_symbol}"
        )
        redis_client = await self._get_redis()

        existing_report = await self._load_cached_json(report_key)
        if existing_report is not None:
            return {
                "job_id": existing_report.get("request_id"),
                "status": "completed",
                "is_reused": True,
                "report": existing_report,
            }

        inflight_job_id = await redis_client.get(inflight_key)
        if inflight_job_id:
            status_key = f"screener:report:status:{inflight_job_id}"
            status = self._normalize_report_status(await redis_client.get(status_key))
            return {
                "job_id": inflight_job_id,
                "status": status or "queued",
                "is_reused": True,
            }

        provisional_job_id = ""
        inflight_claimed = False
        for _ in range(3):
            provisional_job_id = str(uuid4())
            inflight_claimed = await redis_client.set(
                inflight_key,
                provisional_job_id,
                ex=self.REPORT_INFLIGHT_TTL_SECONDS,
                nx=True,
            )
            if inflight_claimed:
                break

            reused_job_id = await redis_client.get(inflight_key)
            if reused_job_id:
                status_key = f"screener:report:status:{reused_job_id}"
                status = self._normalize_report_status(
                    await redis_client.get(status_key)
                )
                return {
                    "job_id": reused_job_id,
                    "status": status or "queued",
                    "is_reused": True,
                }

        if not inflight_claimed:
            failed_job_id = provisional_job_id or str(uuid4())
            error_message = "inflight_job_unavailable"
            keys = self._report_keys(
                normalized_market, normalized_symbol, failed_job_id
            )
            await self._transition_report_status(
                keys.status_key,
                "failed",
                redis_client=redis_client,
            )
            await self._store_json(
                keys.job_key,
                self.REPORT_CACHE_TTL_SECONDS,
                {
                    "job_id": failed_job_id,
                    "market": normalized_market,
                    "symbol": normalized_symbol,
                    "result_key": keys.result_key,
                    "status_key": keys.status_key,
                    "inflight_key": keys.inflight_key,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "error": error_message,
                },
            )
            return {
                "job_id": failed_job_id,
                "status": "failed",
                "error": error_message,
                "is_reused": False,
            }

        display_name = (name or normalized_symbol).strip() or normalized_symbol
        prompt = (
            "Produce a trading report for the instrument below using MCP tools and return a JSON callback.\n"
            f"market={normalized_market}\n"
            f"symbol={normalized_symbol}\n"
            f"name={display_name}\n"
            "Include decision, confidence, reasons, and price_analysis ranges."
        )
        instrument_type = self._instrument_type(normalized_market)
        try:
            job_id = await self._openclaw.request_analysis(
                prompt=prompt,
                symbol=normalized_symbol,
                name=display_name,
                instrument_type=instrument_type,
                callback_url=settings.OPENCLAW_SCREENER_CALLBACK_URL,
                include_model_name=False,
                request_id=provisional_job_id,
            )
        except Exception as exc:
            await redis_client.delete(inflight_key)
            error_message = str(exc).strip() or exc.__class__.__name__
            keys = self._report_keys(
                normalized_market, normalized_symbol, provisional_job_id
            )
            await self._transition_report_status(
                keys.status_key,
                "failed",
                redis_client=redis_client,
            )
            await self._store_json(
                keys.job_key,
                self.REPORT_CACHE_TTL_SECONDS,
                {
                    "job_id": provisional_job_id,
                    "market": normalized_market,
                    "symbol": normalized_symbol,
                    "result_key": keys.result_key,
                    "status_key": keys.status_key,
                    "inflight_key": keys.inflight_key,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "error": error_message,
                },
            )
            return {
                "job_id": provisional_job_id,
                "status": "failed",
                "error": error_message,
                "is_reused": False,
            }

        keys = self._report_keys(normalized_market, normalized_symbol, job_id)
        metadata = {
            "job_id": job_id,
            "market": normalized_market,
            "symbol": normalized_symbol,
            "result_key": keys.result_key,
            "status_key": keys.status_key,
            "inflight_key": keys.inflight_key,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await self._store_json(keys.job_key, self.REPORT_CACHE_TTL_SECONDS, metadata)
        persisted_status = await self._transition_report_status(
            keys.status_key,
            "queued",
            redis_client=redis_client,
        )
        await redis_client.set(
            keys.inflight_key,
            job_id,
            ex=self.REPORT_INFLIGHT_TTL_SECONDS,
        )

        return {
            "job_id": job_id,
            "status": persisted_status,
            "is_reused": False,
        }

    async def get_report_status(self, job_id: str) -> dict[str, Any]:
        if not job_id:
            raise ValueError("job_id is required")

        redis_client = await self._get_redis()
        status_key = f"screener:report:status:{job_id}"
        status = self._normalize_report_status(await redis_client.get(status_key))
        metadata = await self._load_cached_json(f"screener:report:job:{job_id}")

        if status is None and metadata is None:
            return {
                "job_id": job_id,
                "status": "failed",
                "error": "job_not_found",
                "not_found": True,
            }

        inflight_key_value = metadata.get("inflight_key") if metadata else None
        if isinstance(inflight_key_value, str) and inflight_key_value:
            inflight_job_id = await redis_client.get(inflight_key_value)
            if inflight_job_id == job_id and status in {None, "queued"}:
                status = await self._transition_report_status(
                    status_key,
                    "running",
                    redis_client=redis_client,
                )

        if status is None:
            if metadata and isinstance(metadata.get("error"), str):
                status = "failed"
            else:
                status = "queued"

        response: dict[str, Any] = {"job_id": job_id, "status": status}
        if status == "completed":
            if metadata and isinstance(metadata.get("result_key"), str):
                report = await self._load_cached_json(metadata["result_key"])
                if report is not None:
                    response["report"] = report
        elif status == "failed":
            if (
                metadata
                and isinstance(metadata.get("error"), str)
                and metadata["error"]
            ):
                response["error"] = metadata["error"]
            else:
                response["error"] = "job_failed"
        return response

    async def process_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("request_id") or "").strip()
        if not job_id:
            raise ValueError("request_id is required")

        redis_client = await self._get_redis()
        job_key = f"screener:report:job:{job_id}"
        metadata = await self._load_cached_json(job_key)
        default_status_key = f"screener:report:status:{job_id}"
        status_key = (
            str(metadata.get("status_key"))
            if metadata and isinstance(metadata.get("status_key"), str)
            else default_status_key
        )
        inflight_key = (
            str(metadata.get("inflight_key"))
            if metadata and isinstance(metadata.get("inflight_key"), str)
            else ""
        )
        error_metadata_base = (
            {
                **metadata,
                "job_id": job_id,
                "status_key": status_key,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if metadata
            else {
                "job_id": job_id,
                "status_key": status_key,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

        try:
            callback_market = self._market_from_instrument_type(
                payload.get("instrument_type")
            )
            callback_symbol = self._normalize_symbol(
                callback_market, str(payload.get("symbol") or "")
            )
        except ValueError as exc:
            error_message = str(exc)
            persisted_status = await self._transition_report_status(
                status_key,
                "failed",
                redis_client=redis_client,
            )
            if persisted_status == "failed":
                await self._store_json(
                    job_key,
                    self.REPORT_CACHE_TTL_SECONDS,
                    {
                        **error_metadata_base,
                        "error": error_message,
                    },
                )
                if inflight_key:
                    await redis_client.delete(inflight_key)
            return {
                "status": "failed",
                "request_id": job_id,
                "job_id": job_id,
                "error": error_message,
            }

        if metadata is None:
            keys = self._report_keys(callback_market, callback_symbol, job_id)
            metadata = {
                "job_id": job_id,
                "market": callback_market,
                "symbol": callback_symbol,
                "result_key": keys.result_key,
                "status_key": keys.status_key,
                "inflight_key": keys.inflight_key,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        else:
            expected_market = None
            expected_symbol = None
            if isinstance(metadata.get("market"), str):
                try:
                    expected_market = self._normalize_market(metadata["market"])
                except ValueError:
                    expected_market = None
            if expected_market and isinstance(metadata.get("symbol"), str):
                try:
                    expected_symbol = self._normalize_symbol(
                        expected_market, metadata["symbol"]
                    )
                except ValueError:
                    expected_symbol = None

            if expected_market and expected_symbol:
                if (
                    callback_market != expected_market
                    or callback_symbol != expected_symbol
                ):
                    error_message = (
                        "callback_payload_mismatch:"
                        f" expected={expected_market}:{expected_symbol}"
                        f" actual={callback_market}:{callback_symbol}"
                    )
                    persisted_status = await self._transition_report_status(
                        status_key,
                        "failed",
                        redis_client=redis_client,
                    )
                    if persisted_status == "failed":
                        await self._store_json(
                            job_key,
                            self.REPORT_CACHE_TTL_SECONDS,
                            {
                                **error_metadata_base,
                                "market": expected_market,
                                "symbol": expected_symbol,
                                "error": error_message,
                            },
                        )
                        if inflight_key:
                            await redis_client.delete(inflight_key)
                    return {
                        "status": "failed",
                        "request_id": job_id,
                        "job_id": job_id,
                        "error": error_message,
                    }
            else:
                expected_market = callback_market
                expected_symbol = callback_symbol

            keys = self._report_keys(expected_market, expected_symbol, job_id)
            metadata = {
                **metadata,
                "job_id": job_id,
                "market": expected_market,
                "symbol": expected_symbol,
                "result_key": (
                    metadata["result_key"]
                    if isinstance(metadata.get("result_key"), str)
                    else keys.result_key
                ),
                "status_key": (
                    metadata["status_key"]
                    if isinstance(metadata.get("status_key"), str)
                    else keys.status_key
                ),
                "inflight_key": (
                    metadata["inflight_key"]
                    if isinstance(metadata.get("inflight_key"), str)
                    else keys.inflight_key
                ),
                "updated_at": datetime.now(UTC).isoformat(),
            }

        result_key = str(metadata["result_key"])
        status_key = str(metadata["status_key"])
        inflight_key = str(metadata["inflight_key"])
        payload_with_timestamp = {
            **payload,
            "received_at": datetime.now(UTC).isoformat(),
        }

        await self._store_json(
            result_key, self.REPORT_CACHE_TTL_SECONDS, payload_with_timestamp
        )
        await self._transition_report_status(
            status_key,
            "completed",
            redis_client=redis_client,
        )
        await self._store_json(
            f"screener:report:job:{job_id}",
            self.REPORT_CACHE_TTL_SECONDS,
            {
                **metadata,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        await redis_client.delete(inflight_key)

        return {
            "status": "ok",
            "request_id": job_id,
            "job_id": job_id,
            "is_reused": False,
        }

    async def place_order(
        self,
        market: str,
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit", "market"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        confirm: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        normalized_market = self._normalize_market(market)
        normalized_symbol = self._normalize_symbol(normalized_market, symbol)
        return await _place_order_impl(
            symbol=normalized_symbol,
            market=normalized_market,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            dry_run=not confirm,
            reason=reason,
        )
