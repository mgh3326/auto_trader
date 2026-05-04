from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import lru_cache
from uuid import uuid4

import exchange_calendars as xcals
import pandas as pd
from pandas import Timestamp

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.market_data_indicators import _calculate_rsi
from app.services import exchange_rate_service, market_index_service
from app.services import market_data as market_data_service
from app.services.openclaw_client import OpenClawClient, WatchAlertDeliveryResult
from app.services.watch_alerts import WatchAlertService
from app.services.watch_intent_policy import (
    IntentPolicy,
    NotifyOnlyPolicy,
    WatchPolicyError,
    parse_policy,
)
from app.services.watch_order_intent_service import WatchOrderIntentService

logger = logging.getLogger(__name__)
_CRYPTO_RSI_LOOKBACK_DAYS = 200


class WatchScanner:
    def __init__(
        self,
        *,
        intent_service_factory: Any | None = None,
    ) -> None:
        self._watch_service = WatchAlertService()
        self._openclaw = OpenClawClient()
        self._intent_service_factory = intent_service_factory

    @staticmethod
    @lru_cache(maxsize=2)
    def _get_calendar(market: str):
        if market == "kr":
            return xcals.get_calendar("XKRX")
        if market == "us":
            return xcals.get_calendar("XNYS")
        return None

    def _is_market_open(self, market: str) -> bool:
        if market == "crypto":
            return True

        calendar = self._get_calendar(market)
        if calendar is None:
            return False

        now_utc = Timestamp.now("UTC").floor("min")
        if now_utc.tz is None:
            now_utc = now_utc.tz_localize("UTC")
        now_in_market_tz = now_utc.tz_convert(calendar.tz)
        return bool(calendar.is_trading_minute(now_in_market_tz))

    @staticmethod
    def _to_float(value: object) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_close(frame: pd.DataFrame) -> float | None:
        if frame.empty or "close" not in frame.columns:
            return None
        value = frame["close"].iloc[-1]
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_triggered(current: float | None, operator: str, threshold: float) -> bool:
        if current is None:
            return False
        if operator == "above":
            return current > threshold
        if operator == "below":
            return current < threshold
        return False

    @staticmethod
    def _normalize_crypto_symbol(symbol: str) -> str:
        upper_symbol = symbol.strip().upper()
        if "-" in upper_symbol:
            return upper_symbol
        return f"KRW-{upper_symbol}"

    async def _get_price(self, symbol: str, market: str) -> float | None:
        if market == "crypto":
            quote = await market_data_service.get_quote(
                symbol=self._normalize_crypto_symbol(symbol),
                market="crypto",
            )
            return self._to_float(getattr(quote, "price", None))
        if market == "kr":
            quote = await market_data_service.get_quote(
                symbol=symbol,
                market="equity_kr",
            )
            return self._to_float(getattr(quote, "price", None))
        if market == "us":
            normalized_symbol = str(symbol or "").strip().upper()
            quote = await market_data_service.get_quote(
                symbol=normalized_symbol,
                market="equity_us",
            )
            price = self._to_float(getattr(quote, "price", None))
            if price is None:
                raise ValueError(
                    f"US watch price fetch failed for {normalized_symbol}: invalid close"
                )
            return price
        return None

    async def _get_trade_value(self, symbol: str, market: str) -> float | None:
        if market != "kr":
            return None
        quote = await market_data_service.get_quote(
            symbol=symbol,
            market="equity_kr",
        )
        return self._to_float(getattr(quote, "value", None))

    async def _get_index_price(self, symbol: str, market: str) -> float | None:
        if market != "kr":
            return None
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_symbol not in {"KOSPI", "KOSDAQ"}:
            return None
        data = await market_index_service.get_kr_index_quote(normalized_symbol)
        return self._to_float(data.get("current"))

    async def _get_fx_price(self, symbol: str) -> float | None:
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_symbol != "USDKRW":
            return None
        return self._to_float(await exchange_rate_service.get_usd_krw_quote())

    async def _get_rsi(self, symbol: str, market: str) -> float | None:
        if market == "crypto":
            symbol_for_query = self._normalize_crypto_symbol(symbol)
            market_for_query = "crypto"
            count = _CRYPTO_RSI_LOOKBACK_DAYS
        elif market == "kr":
            symbol_for_query = symbol
            market_for_query = "equity_kr"
            count = 250
        elif market == "us":
            symbol_for_query = symbol
            market_for_query = "equity_us"
            count = 250
        else:
            return None

        candles = await market_data_service.get_ohlcv(
            symbol=symbol_for_query,
            market=market_for_query,
            period="day",
            count=count,
        )

        if not candles:
            return None

        close_values: list[float] = []
        for candle in candles:
            if isinstance(candle, dict):
                close_raw = candle.get("close")
            else:
                close_raw = getattr(candle, "close", None)
            close_value = self._to_float(close_raw)
            if close_value is None:
                continue
            close_values.append(close_value)

        if not close_values:
            return None

        close = pd.Series(close_values, dtype="float64").dropna()
        if close.empty:
            return None

        return self._to_float(_calculate_rsi(close).get("14"))

    @staticmethod
    def _build_batched_message(
        market: str,
        triggered: list[dict[str, object]],
        intents: list[dict[str, object]] | None = None,
    ) -> str:
        lines = [f"Watch alerts ({market})"]
        for row in triggered:
            symbol = str(row["symbol"])
            condition_type = str(row["condition_type"])
            threshold = float(row["threshold"])
            current = float(row["current"])
            lines.append(
                f"- {symbol} {condition_type}: current={current:.4f}, threshold={threshold:.4f}"
            )

        if intents:
            lines.append("")
            lines.append(f"Order intents ({market}, kis_mock)")
            for intent in intents:
                status = intent["status"]
                symbol = intent["symbol"]
                side = intent["side"]
                if status == "previewed":
                    lines.append(
                        f"- previewed: {symbol} {side} "
                        f"qty={intent['quantity']} limit={intent['limit_price']} "
                        f"ledger={intent['ledger_id']}"
                    )
                elif status == "dedupe_hit":
                    lines.append(
                        f"- dedupe_hit: {symbol} {side} "
                        f"(already previewed today, ledger={intent['ledger_id']})"
                    )
                else:  # failed
                    lines.append(
                        f"- failed: {symbol} {side} "
                        f"qty={intent['quantity']} limit={intent['limit_price']} "
                        f"(blocked_by={intent['blocked_by']}, watch kept)"
                    )
        return "\n".join(lines)

    async def _get_current_value(
        self,
        *,
        target_kind: str,
        metric: str,
        symbol: str,
        market: str,
    ) -> float | None:
        if target_kind == "asset":
            if metric == "price":
                return await self._get_price(symbol, market)
            if metric == "rsi":
                return await self._get_rsi(symbol, market)
            if metric == "trade_value":
                return await self._get_trade_value(symbol, market)
            return None
        if target_kind == "index":
            if metric == "price":
                return await self._get_index_price(symbol, market)
            return None
        if target_kind == "fx":
            if metric == "price":
                return await self._get_fx_price(symbol)
            return None
        return None

    async def _send_alert(
        self,
        *,
        market: str,
        triggered: list[dict[str, object]],
        message: str,
        intents: list[dict[str, object]] | None = None,
    ) -> WatchAlertDeliveryResult:
        correlation_id = str(uuid4())
        as_of = Timestamp.now("UTC").isoformat()
        try:
            return await self._openclaw.send_watch_alert_to_n8n(
                message=message,
                market=market,
                triggered=triggered,
                as_of=as_of,
                correlation_id=correlation_id,
                intents=intents or [],
            )
        except Exception as exc:
            logger.error("Failed to send watch scan alert: %s", exc)
            return WatchAlertDeliveryResult(status="failed", reason="request_failed")

    def _intent_session(self):
        if self._intent_service_factory is not None:
            return self._intent_service_factory()
        return _default_intent_session()

    async def scan_market(self, market: str) -> dict[str, object]:
        normalized_market = str(market).strip().lower()
        market_open = self._is_market_open(normalized_market)

        watches = await self._watch_service.get_watches_for_market(normalized_market)
        if not watches:
            if not market_open:
                return {
                    "market": normalized_market,
                    "status": "skipped",
                    "skipped": True,
                    "reason": "market_closed",
                }
            return {
                "market": normalized_market,
                "status": "skipped",
                "reason": "no_watch_records",
                "alerts_sent": 0,
                "details": [],
            }

        triggered: list[dict[str, object]] = []
        intents: list[dict[str, object]] = []
        triggered_fields: list[str] = []
        kst_date = now_kst().date().isoformat()

        for watch in watches:
            target_kind = str(watch.get("target_kind") or "asset").strip().lower()
            if not market_open and target_kind != "fx":
                continue
            symbol = str(watch.get("symbol") or "").strip().upper()
            condition_type = str(watch.get("condition_type") or "").strip().lower()
            field = str(watch.get("field") or "")
            threshold = self._to_float(watch.get("threshold"))

            if not symbol or not condition_type or not field or threshold is None:
                continue

            try:
                metric, operator = condition_type.rsplit("_", 1)
            except ValueError:
                continue

            current = await self._get_current_value(
                target_kind=target_kind,
                metric=metric,
                symbol=symbol,
                market=normalized_market,
            )

            if not self._is_triggered(current, operator, threshold):
                continue

            try:
                policy = parse_policy(
                    market=normalized_market,
                    target_kind=target_kind,
                    condition_type=condition_type,
                    raw_payload=watch.get("raw_payload"),
                )
            except WatchPolicyError as exc:
                logger.warning(
                    "Skipping watch with invalid policy: market=%s field=%s code=%s",
                    normalized_market,
                    field,
                    exc.code,
                )
                continue

            if isinstance(policy, NotifyOnlyPolicy):
                triggered.append(
                    {
                        "target_kind": target_kind,
                        "symbol": symbol,
                        "condition_type": condition_type,
                        "threshold": threshold,
                        "current": current,
                    }
                )
                triggered_fields.append(field)
                continue

            assert isinstance(policy, IntentPolicy)
            async with self._intent_session() as (db, factory):
                service = factory(db)
                emission = await service.emit_intent(
                    watch={
                        "market": normalized_market,
                        "target_kind": target_kind,
                        "symbol": symbol,
                        "condition_type": condition_type,
                        "threshold": Decimal(str(threshold)),
                        "threshold_key": str(threshold),
                    },
                    policy=policy,
                    triggered_value=Decimal(str(current)),
                    kst_date=kst_date,
                    correlation_id=uuid4().hex,
                )
            intents.append(emission.to_alert_dict())
            if emission.status in {"previewed", "dedupe_hit"}:
                triggered_fields.append(field)

        if not triggered and not intents:
            if not market_open:
                return {
                    "market": normalized_market,
                    "status": "skipped",
                    "skipped": True,
                    "reason": "market_closed",
                }
            return {
                "market": normalized_market,
                "status": "skipped",
                "reason": "no_triggered_alerts",
                "alerts_sent": 0,
                "details": [],
            }

        message = self._build_batched_message(
            normalized_market, triggered, intents=intents
        )
        result = await self._send_alert(
            market=normalized_market,
            triggered=triggered,
            intents=intents,
            message=message,
        )
        if result.status != "success":
            logger.warning(
                "Watch alert delivery was not successful: market=%s status=%s reason=%s",
                normalized_market,
                result.status,
                result.reason,
            )
            return {
                "market": normalized_market,
                "status": result.status,
                "reason": result.reason,
                "alerts_sent": 0,
                "details": [message],
            }

        for field in triggered_fields:
            await self._watch_service.trigger_and_remove(normalized_market, field)

        return {
            "market": normalized_market,
            "status": "success",
            "request_id": result.request_id,
            "alerts_sent": len(triggered) + len(intents),
            "details": [message],
        }

    async def run(self) -> dict[str, dict[str, object]]:
        results: dict[str, dict[str, object]] = {}
        for market in ("crypto", "kr", "us"):
            market_result = await self.scan_market(market)
            results[market] = dict(market_result)
        return results

    async def close(self) -> None:
        await self._watch_service.close()


@asynccontextmanager
async def _default_intent_session():
    async with AsyncSessionLocal() as db:
        yield db, lambda session: WatchOrderIntentService(session)
