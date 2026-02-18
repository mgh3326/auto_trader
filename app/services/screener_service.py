from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as redis

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
        raise ValueError(
            "instrument_type must be one of: equity_kr, equity_us, crypto"
        )

    @staticmethod
    def _compact_json(data: dict[str, Any]) -> str:
        return json.dumps(
            data, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

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
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_market = self._normalize_market(market)
        filters = {
            "market": normalized_market,
            "asset_type": asset_type,
            "category": category,
            "strategy": strategy,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "min_dividend_yield": min_dividend_yield,
            "max_rsi": max_rsi,
            "limit": limit,
        }
        cache_key = self._screening_cache_key(filters)
        cached = await self._load_cached_json(cache_key)
        if cached:
            return {**cached, "cache_hit": True}

        call_kwargs = {k: v for k, v in filters.items() if v is not None}
        result = await screen_stocks_impl(**call_kwargs)
        await self._store_json(cache_key, self.SCREENING_CACHE_TTL_SECONDS, result)
        return {**result, "cache_hit": False}

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
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_market = self._normalize_market(market)
        filters = {
            "market": normalized_market,
            "asset_type": asset_type,
            "category": category,
            "strategy": strategy,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "min_dividend_yield": min_dividend_yield,
            "max_rsi": max_rsi,
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
            status = await redis_client.get(status_key)
            return {
                "job_id": inflight_job_id,
                "status": status or "queued",
                "is_reused": True,
            }

        provisional_job_id = str(uuid4())
        inflight_claimed = await redis_client.set(
            inflight_key,
            provisional_job_id,
            ex=self.REPORT_INFLIGHT_TTL_SECONDS,
            nx=True,
        )
        if not inflight_claimed:
            reused_job_id = await redis_client.get(inflight_key)
            if reused_job_id:
                status_key = f"screener:report:status:{reused_job_id}"
                status = await redis_client.get(status_key)
                return {
                    "job_id": reused_job_id,
                    "status": status or "queued",
                    "is_reused": True,
                }
            return {
                "job_id": provisional_job_id,
                "status": "queued",
                "is_reused": True,
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
            await redis_client.setex(
                keys.status_key, self.REPORT_CACHE_TTL_SECONDS, "failed"
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
        await redis_client.setex(
            keys.status_key, self.REPORT_CACHE_TTL_SECONDS, "queued"
        )
        await redis_client.set(
            keys.inflight_key,
            job_id,
            ex=self.REPORT_INFLIGHT_TTL_SECONDS,
        )

        return {"job_id": job_id, "status": "queued", "is_reused": False}

    async def get_report_status(self, job_id: str) -> dict[str, Any]:
        if not job_id:
            raise ValueError("job_id is required")
        redis_client = await self._get_redis()
        status_key = f"screener:report:status:{job_id}"
        status = await redis_client.get(status_key)
        response: dict[str, Any] = {"job_id": job_id, "status": status or "queued"}
        metadata = await self._load_cached_json(f"screener:report:job:{job_id}")
        if status == "completed":
            if metadata and isinstance(metadata.get("result_key"), str):
                report = await self._load_cached_json(metadata["result_key"])
                if report is not None:
                    response["report"] = report
        elif status == "failed":
            if metadata and isinstance(metadata.get("error"), str):
                response["error"] = metadata["error"]
        return response

    async def process_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("request_id") or "").strip()
        if not job_id:
            raise ValueError("request_id is required")

        metadata = await self._load_cached_json(f"screener:report:job:{job_id}")
        if metadata is None:
            redis_client = await self._get_redis()
            status_key = f"screener:report:status:{job_id}"
            try:
                market = self._market_from_instrument_type(payload.get("instrument_type"))
                symbol = self._normalize_symbol(market, str(payload.get("symbol") or ""))
            except ValueError as exc:
                error_message = str(exc)
                await redis_client.setex(
                    status_key, self.REPORT_CACHE_TTL_SECONDS, "failed"
                )
                await self._store_json(
                    f"screener:report:job:{job_id}",
                    self.REPORT_CACHE_TTL_SECONDS,
                    {
                        "job_id": job_id,
                        "status_key": status_key,
                        "updated_at": datetime.now(UTC).isoformat(),
                        "error": error_message,
                    },
                )
                return {
                    "status": "failed",
                    "request_id": job_id,
                    "job_id": job_id,
                    "error": error_message,
                }
            keys = self._report_keys(market, symbol, job_id)
            metadata = {
                "job_id": job_id,
                "market": market,
                "symbol": symbol,
                "result_key": keys.result_key,
                "status_key": keys.status_key,
                "inflight_key": keys.inflight_key,
            }

        result_key = str(metadata["result_key"])
        status_key = str(metadata["status_key"])
        inflight_key = str(metadata["inflight_key"])
        payload_with_timestamp = {
            **payload,
            "received_at": datetime.now(UTC).isoformat(),
        }

        redis_client = await self._get_redis()
        await self._store_json(
            result_key, self.REPORT_CACHE_TTL_SECONDS, payload_with_timestamp
        )
        await redis_client.setex(status_key, self.REPORT_CACHE_TTL_SECONDS, "completed")
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
