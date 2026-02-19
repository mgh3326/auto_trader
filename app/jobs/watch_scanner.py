from __future__ import annotations

import logging
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd
from pandas import Timestamp

from app.mcp_server.tooling.market_data_indicators import _calculate_rsi
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient
from app.services.openclaw_client import OpenClawClient
from app.services.watch_alerts import WatchAlertService

logger = logging.getLogger(__name__)
_CRYPTO_RSI_LOOKBACK_DAYS = 200


class WatchScanner:
    def __init__(self) -> None:
        self._watch_service = WatchAlertService()
        self._openclaw = OpenClawClient()
        self._kis = KISClient()

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
            frame = await upbit_service.fetch_price(
                self._normalize_crypto_symbol(symbol)
            )
            return self._extract_close(frame)
        if market == "kr":
            frame = await self._kis.inquire_price(symbol, market="UN")
            return self._extract_close(frame)
        if market == "us":
            frame = await yahoo_service.fetch_price(symbol)
            return self._extract_close(frame)
        return None

    async def _get_rsi(self, symbol: str, market: str) -> float | None:
        frame: pd.DataFrame
        if market == "crypto":
            frame = await upbit_service.fetch_ohlcv(
                market=self._normalize_crypto_symbol(symbol),
                days=_CRYPTO_RSI_LOOKBACK_DAYS,
                period="day",
            )
        elif market == "kr":
            frame = await self._kis.inquire_daily_itemchartprice(
                code=symbol,
                market="UN",
                n=250,
                period="D",
            )
        elif market == "us":
            frame = await yahoo_service.fetch_ohlcv(
                ticker=symbol,
                days=250,
                period="day",
            )
        else:
            return None

        if frame.empty or "close" not in frame.columns:
            return None

        close = pd.to_numeric(frame["close"], errors="coerce").dropna()
        if close.empty:
            return None

        return self._to_float(_calculate_rsi(close).get("14"))

    @staticmethod
    def _build_batched_message(
        market: str,
        triggered: list[dict[str, object]],
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
        return "\n".join(lines)

    async def _send_alert(self, message: str) -> str | None:
        try:
            return await self._openclaw.send_watch_alert(message)
        except Exception as exc:
            logger.error("Failed to send watch scan alert: %s", exc)
            return None

    async def scan_market(self, market: str) -> dict[str, object]:
        normalized_market = str(market).strip().lower()
        if not self._is_market_open(normalized_market):
            return {
                "market": normalized_market,
                "skipped": True,
                "reason": "market_closed",
            }

        watches = await self._watch_service.get_watches_for_market(normalized_market)
        if not watches:
            return {"market": normalized_market, "alerts_sent": 0, "details": []}

        triggered: list[dict[str, object]] = []
        triggered_fields: list[str] = []

        for watch in watches:
            symbol = str(watch.get("symbol") or "").strip().upper()
            condition_type = str(watch.get("condition_type") or "").strip().lower()
            field = str(watch.get("field") or "")
            threshold = self._to_float(watch.get("threshold"))

            if not symbol or not condition_type or not field or threshold is None:
                continue

            try:
                metric, operator = condition_type.split("_", 1)
            except ValueError:
                continue

            current: float | None
            if metric == "price":
                current = await self._get_price(symbol, normalized_market)
            elif metric == "rsi":
                current = await self._get_rsi(symbol, normalized_market)
            else:
                continue

            if not self._is_triggered(current, operator, threshold):
                continue

            triggered.append(
                {
                    "symbol": symbol,
                    "condition_type": condition_type,
                    "threshold": threshold,
                    "current": current,
                }
            )
            triggered_fields.append(field)

        if not triggered:
            return {"market": normalized_market, "alerts_sent": 0, "details": []}

        message = self._build_batched_message(normalized_market, triggered)
        request_id = await self._send_alert(message)
        if not request_id:
            return {"market": normalized_market, "alerts_sent": 0, "details": []}

        for field in triggered_fields:
            await self._watch_service.trigger_and_remove(normalized_market, field)

        return {
            "market": normalized_market,
            "alerts_sent": len(triggered_fields),
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
