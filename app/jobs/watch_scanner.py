from __future__ import annotations

import logging
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd
from pandas import Timestamp

from app.mcp_server.tooling.market_data_indicators import _calculate_rsi
from app.services import market_data as market_data_service
from app.services.openclaw_client import OpenClawClient
from app.services.watch_alerts import WatchAlertService

logger = logging.getLogger(__name__)
_CRYPTO_RSI_LOOKBACK_DAYS = 200


class WatchScanner:
    def __init__(self) -> None:
        self._watch_service = WatchAlertService()
        self._openclaw = OpenClawClient()

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
