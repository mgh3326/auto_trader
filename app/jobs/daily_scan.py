from __future__ import annotations

import logging
from typing import Literal

import redis.asyncio as redis

from app.core.config import settings
from app.core.timezone import now_kst
from app.mcp_server.tooling.analysis_tool_handlers import get_fear_greed_index_impl
from app.mcp_server.tooling.market_data_indicators import _calculate_rsi, _calculate_sma
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.brokers.upbit.client import (
    fetch_multiple_tickers,
    fetch_my_coins,
    fetch_ohlcv,
    fetch_top_traded_coins,
)
from app.services.openclaw_client import OpenClawClient
from app.services.upbit_symbol_universe_service import get_upbit_korean_name_by_coin

logger = logging.getLogger(__name__)

COOLDOWN_HOURS = {
    "overbought": 6,
    "oversold": 6,
    "crash": 2,
    "fng": 12,
    "sma_cross": 24,
}


class DailyScanner:
    def __init__(
        self,
        *,
        alert_mode: Literal["both", "telegram_only", "openclaw_only", "none"] = "both",
    ):
        self._redis: redis.Redis | None = None
        self._openclaw = OpenClawClient()
        self._alert_mode: Literal[
            "both", "telegram_only", "openclaw_only", "none"
        ] = alert_mode

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
    def _collect_krw_markets(
        top_coins: list[dict],
        my_coins: list[dict],
        top_coin_limit: int | None = None,
    ) -> tuple[list[str], list[str]]:
        tradable_markets = {
            str(item.get("market") or "")
            for item in top_coins
            if str(item.get("market") or "").startswith("KRW-")
        }

        candidate_top_coins = (
            top_coins[: max(0, top_coin_limit)]
            if top_coin_limit is not None
            else top_coins
        )
        market_codes = {
            str(item.get("market") or "")
            for item in candidate_top_coins
            if str(item.get("market") or "").startswith("KRW-")
        }

        skipped_markets: set[str] = set()
        for coin in my_coins:
            currency = str(coin.get("currency") or "").upper()
            if not currency or currency == "KRW":
                continue

            market = f"KRW-{currency}"
            if market in tradable_markets:
                market_codes.add(market)
            else:
                skipped_markets.add(market)

        return sorted(market_codes), sorted(skipped_markets)

    async def _coin_name(self, currency: str) -> str:
        return await get_upbit_korean_name_by_coin(currency, quote_currency="KRW")

    @staticmethod
    def _build_rank_by_market(top_coins: list[dict]) -> dict[str, int]:
        rank_by_market: dict[str, int] = {}
        for rank, item in enumerate(top_coins, start=1):
            market = str(item.get("market") or "")
            if not market.startswith("KRW-"):
                continue
            # Keep the first rank when duplicates are present.
            rank_by_market.setdefault(market, rank)
        return rank_by_market

    @staticmethod
    def _collect_holding_markets(
        my_coins: list[dict],
        tradable_markets: set[str],
    ) -> tuple[set[str], list[str]]:
        holding_markets: set[str] = set()
        skipped_markets: set[str] = set()
        for coin in my_coins:
            currency = str(coin.get("currency") or "").upper()
            if not currency or currency == "KRW":
                continue
            market = f"KRW-{currency}"
            if market in tradable_markets:
                holding_markets.add(market)
            else:
                skipped_markets.add(market)
        return holding_markets, sorted(skipped_markets)

    @staticmethod
    def _crash_threshold_for_rank(rank: int | None) -> float:
        if rank is None:
            return settings.DAILY_SCAN_CRASH_TOP100_THRESHOLD
        if rank <= 10:
            return settings.DAILY_SCAN_CRASH_TOP10_THRESHOLD
        if rank <= 30:
            return settings.DAILY_SCAN_CRASH_TOP30_THRESHOLD
        if rank <= 50:
            return settings.DAILY_SCAN_CRASH_TOP50_THRESHOLD
        return settings.DAILY_SCAN_CRASH_TOP100_THRESHOLD

    @staticmethod
    def _crash_threshold_for_candidate(rank: int | None, is_holding: bool) -> float:
        rank_threshold = DailyScanner._crash_threshold_for_rank(rank)
        if is_holding:
            return min(rank_threshold, settings.DAILY_SCAN_CRASH_HOLDING_THRESHOLD)
        return rank_threshold

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

    @staticmethod
    def _dedupe_pending_cooldowns(
        pending_cooldowns: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        deduped: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pending in pending_cooldowns:
            if pending in seen:
                continue
            seen.add(pending)
            deduped.append(pending)
        return deduped

    @staticmethod
    def _build_strategy_scan_batch_message(
        *,
        btc_ctx: str,
        buy_signals: list[str],
        sell_signals: list[str],
        sentiment_signals: list[str],
    ) -> str:
        timestamp = now_kst().strftime("%H:%M")
        lines: list[str] = [
            f"🔎 크립토 스캔 ({timestamp})",
            btc_ctx,
        ]

        def add_section(title: str, items: list[str]) -> None:
            if not items:
                return
            lines.append("")
            lines.append(title)
            for item in items:
                lines.append(f"- {item}")

        add_section("📈 매수 신호", buy_signals)
        add_section("📉 매도 신호", sell_signals)
        add_section("💭 시장 심리", sentiment_signals)
        return "\n".join(lines)

    @staticmethod
    def _build_crash_detection_batch_message(
        *,
        crash_signals: list[str],
    ) -> str:
        timestamp = now_kst().strftime("%H:%M")
        lines: list[str] = [f"크래시 감지 스캔 ({timestamp})"]
        if crash_signals:
            lines.append("")
            lines.append("변동성 경보")
            for signal in crash_signals:
                lines.append(f"- {signal}")
        return "\n".join(lines)

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

    async def check_overbought_holdings(
        self,
        btc_ctx: str,
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
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

            name = await self._coin_name(currency)
            base_message = f"⚠️ {name}({currency}) RSI {rsi:.1f} — 과매수 구간"
            if send_immediately:
                message = f"{base_message}\n{btc_ctx}"
                request_id = await self._send_alert(message)
                if request_id:
                    await self._record_alert(currency, "overbought")
                    alerts.append(message)
            else:
                if pending_cooldowns is not None:
                    pending_cooldowns.append((currency, "overbought"))
                alerts.append(base_message)

        return alerts

    async def check_oversold_top30(
        self,
        btc_ctx: str,
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
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

            name = await self._coin_name(currency)
            base_message = f"📉 {name}({currency}) RSI {rsi:.1f} — 과매도 구간"
            if send_immediately:
                message = f"{base_message}\n{btc_ctx}"
                request_id = await self._send_alert(message)
                if request_id:
                    await self._record_alert(currency, "oversold")
                    alerts.append(message)
            else:
                if pending_cooldowns is not None:
                    pending_cooldowns.append((currency, "oversold"))
                alerts.append(base_message)

        return alerts

    async def check_price_crash(
        self,
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        alerts: list[str] = []
        top_coins = await fetch_top_traded_coins("KRW")
        my_coins = await fetch_my_coins()

        rank_by_market = self._build_rank_by_market(top_coins)
        tradable_markets = set(rank_by_market.keys())
        holding_markets, skipped_markets = self._collect_holding_markets(
            my_coins=my_coins,
            tradable_markets=tradable_markets,
        )
        if skipped_markets:
            logger.info(
                "Skipping non-tradable KRW holding markets in crash scan: %s",
                ", ".join(skipped_markets),
            )

        rank_limit = max(0, settings.DAILY_SCAN_CRASH_TOP_RANK_LIMIT)
        ranked_markets = {
            market for market, rank in rank_by_market.items() if rank <= rank_limit
        }
        market_codes = sorted(ranked_markets | holding_markets)

        if not market_codes:
            return alerts

        logger.info(
            "Crash scan universe: rank_limit=%d ranked=%d holdings=%d merged=%d",
            rank_limit,
            len(ranked_markets),
            len(holding_markets),
            len(market_codes),
        )

        tickers = await fetch_multiple_tickers(market_codes)
        near_miss_ratio = min(max(settings.DAILY_SCAN_CRASH_NEAR_MISS_RATIO, 0.0), 1.0)
        near_miss_count = 0
        below_threshold_count = 0
        cooldown_skip_count = 0
        out_of_universe_count = 0
        missing_change_rate_count = 0

        for ticker in tickers:
            market = str(ticker.get("market") or "")
            if not market:
                continue
            change_rate = self._to_float(ticker.get("signed_change_rate"))
            if change_rate is None:
                missing_change_rate_count += 1
                continue

            rank = rank_by_market.get(market)
            is_holding = market in holding_markets
            if rank is None and not is_holding:
                out_of_universe_count += 1
                continue

            threshold = self._crash_threshold_for_candidate(rank, is_holding)
            abs_change_rate = abs(change_rate)
            if abs_change_rate < threshold:
                below_threshold_count += 1
                if abs_change_rate >= threshold * near_miss_ratio:
                    near_miss_count += 1
                    logger.info(
                        "Crash scan near-miss market=%s rank=%s holding=%s change=%+.2f%% threshold=%.2f%%",
                        market,
                        rank if rank is not None else "-",
                        is_holding,
                        change_rate * 100,
                        threshold * 100,
                    )
                continue

            currency = self._currency_from_market(market)
            if not await self._should_alert(currency, "crash"):
                cooldown_skip_count += 1
                logger.info(
                    "Crash scan cooldown-skip market=%s rank=%s holding=%s change=%+.2f%% threshold=%.2f%%",
                    market,
                    rank if rank is not None else "-",
                    is_holding,
                    change_rate * 100,
                    threshold * 100,
                )
                continue

            name = await self._coin_name(currency)
            direction = "급등" if change_rate > 0 else "급락"
            message = f"{name}({currency}) 24h {change_rate:+.2%} — {direction} 감지"
            logger.info(
                "Crash alert accepted market=%s rank=%s holding=%s change=%+.2f%% threshold=%.2f%%",
                market,
                rank if rank is not None else "-",
                is_holding,
                change_rate * 100,
                threshold * 100,
            )
            if send_immediately:
                request_id = await self._send_alert(message)
                if request_id:
                    await self._record_alert(currency, "crash")
                    alerts.append(message)
            else:
                if pending_cooldowns is not None:
                    pending_cooldowns.append((currency, "crash"))
                alerts.append(message)

        logger.info(
            (
                "Crash scan summary: requested=%d ticker_rows=%d alerts=%d "
                "near_miss=%d below_threshold=%d cooldown_skip=%d "
                "missing_change_rate=%d out_of_universe=%d"
            ),
            len(market_codes),
            len(tickers),
            len(alerts),
            near_miss_count,
            below_threshold_count,
            cooldown_skip_count,
            missing_change_rate_count,
            out_of_universe_count,
        )

        return alerts

    async def check_fear_greed(
        self,
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
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
        if send_immediately:
            request_id = await self._send_alert(message)
            if request_id:
                await self._record_alert(symbol, "fng")
                alerts.append(message)
        else:
            if pending_cooldowns is not None:
                pending_cooldowns.append((symbol, "fng"))
            alerts.append(message)

        return alerts

    async def check_sma20_crossings(
        self,
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        alerts: list[str] = []
        top_coins = await fetch_top_traded_coins("KRW")
        my_coins = await fetch_my_coins()

        market_codes, skipped_markets = self._collect_krw_markets(
            top_coins=top_coins,
            my_coins=my_coins,
            top_coin_limit=max(0, settings.DAILY_SCAN_TOP_COINS_COUNT),
        )
        if skipped_markets:
            logger.info(
                "Skipping non-tradable KRW holding markets in sma20 scan: %s",
                ", ".join(skipped_markets),
            )

        for market in market_codes:
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

            name = await self._coin_name(currency)
            message = (
                f"{emoji} {name}({currency}) SMA20 {crossing_label} — "
                f"종가 {curr_close:,.0f} / SMA20 {curr_sma20:,.0f}"
            )
            if send_immediately:
                request_id = await self._send_alert(message)
                if request_id:
                    await self._record_alert(symbol, "sma_cross")
                    alerts.append(message)
            else:
                if pending_cooldowns is not None:
                    pending_cooldowns.append((symbol, "sma_cross"))
                alerts.append(message)

        return alerts

    async def _send_alert(self, message: str) -> str | None:
        if self._alert_mode == "none":
            return "none"

        if self._alert_mode == "telegram_only":
            telegram_sent = await self._send_telegram_alert(message)
            return "telegram" if telegram_sent else None

        if self._alert_mode == "openclaw_only":
            return await self._send_openclaw_alert(message)

        openclaw_request_id = await self._send_openclaw_alert(message)
        telegram_sent = await self._send_telegram_alert(message)
        if openclaw_request_id and telegram_sent:
            return openclaw_request_id

        logger.error(
            "Failed to send scan alert in both mode: openclaw_success=%s telegram_success=%s",
            bool(openclaw_request_id),
            telegram_sent,
        )
        return None

    async def _send_openclaw_alert(self, message: str) -> str | None:
        try:
            request_id = await self._openclaw.send_scan_alert(
                message,
                mirror_to_telegram=False,
            )
            if not request_id:
                logger.error("OpenClaw scan alert failed")
            return request_id
        except Exception as exc:
            logger.error("OpenClaw scan alert error: %s", exc)
            return None

    async def _send_telegram_alert(self, message: str) -> bool:
        try:
            sent = await get_trade_notifier().notify_openclaw_message(message)
            if not sent:
                logger.error("Telegram scan alert failed")
            return sent
        except Exception as exc:
            logger.error("Telegram scan alert error: %s", exc)
            return False

    async def run_strategy_scan(self) -> dict:
        if not settings.DAILY_SCAN_ENABLED:
            return {"skipped": True, "reason": "disabled"}

        btc_ctx = await self._get_btc_context()
        pending_cooldowns: list[tuple[str, str]] = []

        overbought_alerts = await self.check_overbought_holdings(
            btc_ctx,
            send_immediately=False,
            pending_cooldowns=pending_cooldowns,
        )
        oversold_alerts = await self.check_oversold_top30(
            btc_ctx,
            send_immediately=False,
            pending_cooldowns=pending_cooldowns,
        )
        fng_alerts = await self.check_fear_greed(
            send_immediately=False,
            pending_cooldowns=pending_cooldowns,
        )
        sma_alerts = await self.check_sma20_crossings(
            send_immediately=False,
            pending_cooldowns=pending_cooldowns,
        )

        buy_signals = [*oversold_alerts]
        sell_signals = [*overbought_alerts]
        for sma_alert in sma_alerts:
            if "골든크로스" in sma_alert:
                buy_signals.append(sma_alert)
            elif "데드크로스" in sma_alert:
                sell_signals.append(sma_alert)

        details = {
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "sentiment_signals": fng_alerts,
            "btc_context": btc_ctx,
        }

        if not buy_signals and not sell_signals and not fng_alerts:
            return {"alerts_sent": 0, "message": "", "details": details}

        batched_message = self._build_strategy_scan_batch_message(
            btc_ctx=btc_ctx,
            buy_signals=buy_signals,
            sell_signals=sell_signals,
            sentiment_signals=fng_alerts,
        )
        request_id = await self._send_alert(batched_message)
        if not request_id:
            return {"alerts_sent": 0, "message": "", "details": details}

        for symbol, alert_type in self._dedupe_pending_cooldowns(pending_cooldowns):
            await self._record_alert(symbol, alert_type)

        return {"alerts_sent": 1, "message": batched_message, "details": details}

    async def run_crash_detection(self) -> dict:
        if not settings.DAILY_SCAN_ENABLED:
            return {"skipped": True, "reason": "disabled"}

        pending_cooldowns: list[tuple[str, str]] = []
        alerts = await self.check_price_crash(
            send_immediately=False,
            pending_cooldowns=pending_cooldowns,
        )
        if not alerts:
            return {"alerts_sent": 0, "details": []}

        batched_message = self._build_crash_detection_batch_message(
            crash_signals=alerts
        )
        request_id = await self._send_alert(batched_message)
        if not request_id:
            return {"alerts_sent": 0, "details": []}

        for symbol, alert_type in self._dedupe_pending_cooldowns(pending_cooldowns):
            await self._record_alert(symbol, alert_type)

        return {"alerts_sent": 1, "details": [batched_message]}

    async def close(self):
        if self._redis is not None:
            await self._redis.close()
            self._redis = None
