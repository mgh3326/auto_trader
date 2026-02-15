from __future__ import annotations

import logging

import redis.asyncio as redis

from app.core.config import settings
from app.mcp_server.tooling.analysis_tool_handlers import get_fear_greed_index_impl
from app.mcp_server.tooling.market_data_indicators import _calculate_rsi, _calculate_sma
from app.services.openclaw_client import OpenClawClient
from app.services.upbit import (
    fetch_multiple_tickers,
    fetch_my_coins,
    fetch_ohlcv,
    fetch_top_traded_coins,
)
from data.coins_info import upbit_pairs

logger = logging.getLogger(__name__)

COOLDOWN_HOURS = {
    "overbought": 6,
    "oversold": 6,
    "crash": 2,
    "fng": 12,
    "sma_cross": 24,
}


class DailyScanner:
    def __init__(self):
        self._redis: redis.Redis | None = None
        self._openclaw = OpenClawClient()

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
    def _to_float(value: object) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _indicator_value(
        data: dict[str, float | None],
        key: str,
    ) -> float | None:
        return DailyScanner._to_float(data.get(key))

    @staticmethod
    def _currency_from_market(market: str) -> str:
        if "-" in market:
            return market.split("-")[-1].upper()
        return market.upper()

    @staticmethod
    def _cooldown_key(symbol: str, alert_type: str) -> str:
        return f"daily_scan:cooldown:{symbol}:{alert_type}"

    @staticmethod
    def _coin_name(currency: str) -> str:
        try:
            name = upbit_pairs.COIN_TO_NAME_KR.get(currency, currency)
            if isinstance(name, str) and name:
                return name
            return currency
        except Exception:
            return currency

    async def _should_alert(self, symbol: str, alert_type: str) -> bool:
        redis_client = await self._get_redis()
        key = self._cooldown_key(symbol, alert_type)
        current = await redis_client.get(key)
        return current is None

    async def _record_alert(self, symbol: str, alert_type: str):
        redis_client = await self._get_redis()
        ttl_hours = COOLDOWN_HOURS.get(alert_type, 6)
        ttl_seconds = int(ttl_hours * 3600)
        key = self._cooldown_key(symbol, alert_type)
        await redis_client.set(key, "1", ex=ttl_seconds)

    async def _get_btc_context(self) -> str:
        try:
            btc_df = await fetch_ohlcv("KRW-BTC", days=200)
            if btc_df.empty or "close" not in btc_df.columns:
                return "📌 BTC 컨텍스트: 데이터 없음"

            close = btc_df["close"]
            rsi = self._indicator_value(_calculate_rsi(close), "14")
            sma = _calculate_sma(close, periods=[20, 60, 200])
            sma20 = self._indicator_value(sma, "20")
            sma60 = self._indicator_value(sma, "60")
            sma200 = self._indicator_value(sma, "200")

            change_rate = 0.0
            ticker_rows = await fetch_multiple_tickers(["KRW-BTC"])
            if ticker_rows:
                parsed = self._to_float(ticker_rows[0].get("signed_change_rate"))
                if parsed is not None:
                    change_rate = parsed

            rsi_text = f"{rsi:.1f}" if rsi is not None else "N/A"
            sma20_text = f"{sma20:.1f}" if sma20 is not None else "N/A"
            sma60_text = f"{sma60:.1f}" if sma60 is not None else "N/A"
            sma200_text = f"{sma200:.1f}" if sma200 is not None else "N/A"
            return (
                "📌 BTC 컨텍스트: "
                f"RSI14 {rsi_text} | "
                f"SMA20 {sma20_text} / SMA60 {sma60_text} / SMA200 {sma200_text} | "
                f"24h {change_rate:+.2%}"
            )
        except Exception as exc:
            logger.warning("Failed to build BTC context: %s", exc)
            return "📌 BTC 컨텍스트: 조회 실패"

    async def check_overbought_holdings(self, btc_ctx: str) -> list[str]:
        alerts: list[str] = []
        my_coins = await fetch_my_coins()
        for coin in my_coins:
            currency = str(coin.get("currency") or "").upper()
            if not currency or currency == "KRW":
                continue

            market = f"KRW-{currency}"
            try:
                df = await fetch_ohlcv(market, days=50)
            except Exception as exc:
                logger.warning("Failed to fetch OHLCV for %s: %s", market, exc)
                continue

            if df.empty or "close" not in df.columns:
                continue

            rsi = self._indicator_value(_calculate_rsi(df["close"]), "14")
            if rsi is None or rsi < settings.DAILY_SCAN_RSI_OVERBOUGHT:
                continue

            if not await self._should_alert(currency, "overbought"):
                continue

            name = self._coin_name(currency)
            message = f"⚠️ {name}({currency}) RSI {rsi:.1f} — 과매수 구간\n{btc_ctx}"
            request_id = await self._send_alert(message)
            if request_id:
                await self._record_alert(currency, "overbought")
                alerts.append(message)

        return alerts

    async def check_oversold_top30(self, btc_ctx: str) -> list[str]:
        alerts: list[str] = []
        top_coins = await fetch_top_traded_coins("KRW")
        top_count = max(0, settings.DAILY_SCAN_TOP_COINS_COUNT)

        for coin in top_coins[:top_count]:
            market = str(coin.get("market") or "")
            if not market.startswith("KRW-"):
                continue

            currency = self._currency_from_market(market)
            try:
                df = await fetch_ohlcv(market, days=50)
            except Exception as exc:
                logger.warning("Failed to fetch OHLCV for %s: %s", market, exc)
                continue

            if df.empty or "close" not in df.columns:
                continue

            rsi = self._indicator_value(_calculate_rsi(df["close"]), "14")
            if rsi is None or rsi > settings.DAILY_SCAN_RSI_OVERSOLD:
                continue

            if not await self._should_alert(currency, "oversold"):
                continue

            name = self._coin_name(currency)
            message = f"📉 {name}({currency}) RSI {rsi:.1f} — 과매도 구간\n{btc_ctx}"
            request_id = await self._send_alert(message)
            if request_id:
                await self._record_alert(currency, "oversold")
                alerts.append(message)

        return alerts

    async def check_price_crash(self) -> list[str]:
        alerts: list[str] = []
        top_coins = await fetch_top_traded_coins("KRW")
        my_coins = await fetch_my_coins()

        market_codes = {
            str(item.get("market"))
            for item in top_coins
            if str(item.get("market") or "").startswith("KRW-")
        }
        market_codes.update(
            f"KRW-{str(coin.get('currency')).upper()}"
            for coin in my_coins
            if str(coin.get("currency") or "").upper() != "KRW"
        )

        if not market_codes:
            return alerts

        tickers = await fetch_multiple_tickers(sorted(market_codes))
        for ticker in tickers:
            market = str(ticker.get("market") or "")
            if not market:
                continue
            change_rate = self._to_float(ticker.get("signed_change_rate"))
            if change_rate is None:
                continue
            if abs(change_rate) < settings.DAILY_SCAN_CRASH_THRESHOLD:
                continue

            currency = self._currency_from_market(market)
            if not await self._should_alert(currency, "crash"):
                continue

            name = self._coin_name(currency)
            direction = "급등" if change_rate > 0 else "급락"
            message = f"🚨 {name}({currency}) 24h {change_rate:+.2%} — {direction} 감지"
            request_id = await self._send_alert(message)
            if request_id:
                await self._record_alert(currency, "crash")
                alerts.append(message)

        return alerts

    async def check_fear_greed(self) -> list[str]:
        alerts: list[str] = []
        result = await get_fear_greed_index_impl(days=1)
        if not result.get("success"):
            return alerts

        current = result.get("current") or {}
        value = self._to_float(current.get("value"))
        if value is None:
            return alerts

        if not (
            value <= settings.DAILY_SCAN_FNG_LOW
            or value >= settings.DAILY_SCAN_FNG_HIGH
        ):
            return alerts

        symbol = "GLOBAL"
        if not await self._should_alert(symbol, "fng"):
            return alerts

        classification = str(current.get("classification") or "Unknown")
        message = f"😱 Fear & Greed {int(value)} ({classification}) — 극단 구간"
        request_id = await self._send_alert(message)
        if request_id:
            await self._record_alert(symbol, "fng")
            alerts.append(message)

        return alerts

    async def check_sma20_crossings(self) -> list[str]:
        alerts: list[str] = []
        top_coins = await fetch_top_traded_coins("KRW")
        my_coins = await fetch_my_coins()

        market_codes = {
            str(item.get("market"))
            for item in top_coins[: max(0, settings.DAILY_SCAN_TOP_COINS_COUNT)]
            if str(item.get("market") or "").startswith("KRW-")
        }
        market_codes.update(
            f"KRW-{str(coin.get('currency')).upper()}"
            for coin in my_coins
            if str(coin.get("currency") or "").upper() != "KRW"
        )

        for market in sorted(market_codes):
            try:
                df = await fetch_ohlcv(market, days=50)
            except Exception as exc:
                logger.warning("Failed to fetch OHLCV for %s: %s", market, exc)
                continue

            if df.empty or "close" not in df.columns or len(df) < 21:
                continue

            close = df["close"]
            prev_close = self._to_float(close.iloc[-2])
            curr_close = self._to_float(close.iloc[-1])
            prev_sma20 = self._indicator_value(
                _calculate_sma(close.iloc[:-1], periods=[20]), "20"
            )
            curr_sma20 = self._indicator_value(
                _calculate_sma(close, periods=[20]), "20"
            )

            if (
                prev_close is None
                or curr_close is None
                or prev_sma20 is None
                or curr_sma20 is None
            ):
                continue

            crossing: str | None = None
            crossing_label = ""
            if prev_close < prev_sma20 and curr_close > curr_sma20:
                crossing = "golden"
                crossing_label = "골든크로스"
                emoji = "🟢"
            elif prev_close > prev_sma20 and curr_close < curr_sma20:
                crossing = "dead"
                crossing_label = "데드크로스"
                emoji = "🔴"
            else:
                continue

            currency = self._currency_from_market(market)
            symbol = f"{currency}:{crossing}"
            if not await self._should_alert(symbol, "sma_cross"):
                continue

            name = self._coin_name(currency)
            message = (
                f"{emoji} {name}({currency}) SMA20 {crossing_label} — "
                f"종가 {curr_close:,.0f} / SMA20 {curr_sma20:,.0f}"
            )
            request_id = await self._send_alert(message)
            if request_id:
                await self._record_alert(symbol, "sma_cross")
                alerts.append(message)

        return alerts

    async def _send_alert(self, message: str) -> str | None:
        try:
            return await self._openclaw.send_scan_alert(message)
        except Exception as exc:
            logger.error("Failed to send scan alert: %s", exc)
            return None

    async def run_strategy_scan(self) -> dict:
        if not settings.DAILY_SCAN_ENABLED:
            return {"skipped": True, "reason": "disabled"}

        await upbit_pairs.prime_upbit_constants()
        btc_ctx = await self._get_btc_context()
        alerts: list[str] = []
        alerts += await self.check_overbought_holdings(btc_ctx)
        alerts += await self.check_oversold_top30(btc_ctx)
        alerts += await self.check_fear_greed()
        alerts += await self.check_sma20_crossings()
        return {"alerts_sent": len(alerts), "details": alerts}

    async def run_crash_detection(self) -> dict:
        if not settings.DAILY_SCAN_ENABLED:
            return {"skipped": True, "reason": "disabled"}

        await upbit_pairs.prime_upbit_constants()
        alerts = await self.check_price_crash()
        return {"alerts_sent": len(alerts), "details": alerts}

    async def close(self):
        if self._redis is not None:
            await self._redis.close()
            self._redis = None
