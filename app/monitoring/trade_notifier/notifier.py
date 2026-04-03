"""TradeNotifier singleton class — orchestrates formatters and transports."""

from __future__ import annotations

import logging

import httpx

from app.core.timezone import format_datetime

from . import formatters_discord as fmt_discord
from . import formatters_telegram as fmt_telegram
from .transports import (
    send_discord_content_single,
    send_discord_embed_single,
    send_telegram,
)
from .types import DiscordEmbed

logger = logging.getLogger(__name__)


class TradeNotifier:
    """Singleton trade notifier with Telegram and Discord integration."""

    _instance: TradeNotifier | None = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize TradeNotifier (only once due to singleton pattern)."""
        if not self._initialized:
            self._bot_token: str | None = None
            self._chat_ids: list[str] = []
            # Discord webhooks for different market types
            self._discord_webhook_us: str | None = None
            self._discord_webhook_kr: str | None = None
            self._discord_webhook_crypto: str | None = None
            self._discord_webhook_alerts: str | None = None
            # Legacy list of all Discord webhooks (for backward compatibility)
            self._discord_webhook_urls: list[str] = []
            self._enabled: bool = False
            self._http_client: httpx.AsyncClient | None = None
            TradeNotifier._initialized = True

    def configure(
        self,
        bot_token: str,
        chat_ids: list[str],
        enabled: bool = True,
        discord_webhook_urls: list[str] | None = None,
        discord_webhook_us: str | None = None,
        discord_webhook_kr: str | None = None,
        discord_webhook_crypto: str | None = None,
        discord_webhook_alerts: str | None = None,
    ) -> None:
        """Configure the trade notifier."""
        self._bot_token = bot_token
        self._chat_ids = chat_ids
        self._discord_webhook_us = discord_webhook_us
        self._discord_webhook_kr = discord_webhook_kr
        self._discord_webhook_crypto = discord_webhook_crypto
        self._discord_webhook_alerts = discord_webhook_alerts
        # Store legacy webhook URLs list for backward compatibility
        self._discord_webhook_urls = discord_webhook_urls or []
        # Also build list from individual webhooks if legacy parameter not provided
        if not self._discord_webhook_urls:
            for webhook in [
                discord_webhook_us,
                discord_webhook_kr,
                discord_webhook_crypto,
                discord_webhook_alerts,
            ]:
                if webhook:
                    self._discord_webhook_urls.append(webhook)
        self._enabled = enabled

        if enabled and not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=10.0, trust_env=False)
            logger.info(f"TradeNotifier configured: {len(chat_ids)} chat(s)")

            # Log configured Discord webhooks
            webhook_count = sum(
                [
                    bool(self._discord_webhook_us),
                    bool(self._discord_webhook_kr),
                    bool(self._discord_webhook_crypto),
                    bool(self._discord_webhook_alerts),
                ]
            )
            if webhook_count > 0:
                logger.info(
                    f"TradeNotifier Discord webhooks: {webhook_count} webhook(s) configured"
                )

    async def shutdown(self) -> None:
        """Shutdown HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
            logger.debug("TradeNotifier HTTP client closed")

        logger.info("TradeNotifier shutdown complete")

    # ── routing ────────────────────────────────────────────────────────

    def _get_webhook_for_market_type(self, market_type: str) -> str | None:
        """Get the appropriate Discord webhook URL for a given market type."""
        market_type_normalized = market_type.strip().lower()

        if market_type_normalized in {
            "us",
            "usa",
            "overseas",
            "equity_us",
            "해외주식",
            "nas",
            "nasd",
            "nasdaq",
            "nys",
            "nyse",
            "ams",
            "amex",
        }:
            return self._discord_webhook_us
        elif market_type_normalized in {
            "kr",
            "krx",
            "domestic",
            "equity_kr",
            "국내주식",
        }:
            return self._discord_webhook_kr
        elif market_type_normalized in {"crypto", "cryptocurrency", "coin", "암호화폐"}:
            return self._discord_webhook_crypto
        else:
            logger.warning(f"Unknown market type: {market_type}")
            return None

    def _has_telegram_delivery_config(self) -> bool:
        return bool(self._http_client and self._bot_token and self._chat_ids)

    # ── format wrappers (delegate to formatter modules) ────────────────

    def _format_buy_notification(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_amount: float,
        prices: list[float],
        volumes: list[float],
        market_type: str = "암호화폐",
    ) -> DiscordEmbed:
        return fmt_discord.format_buy_notification(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_amount=total_amount,
            prices=prices,
            volumes=volumes,
            market_type=market_type,
        )

    def _format_sell_notification(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_volume: float,
        prices: list[float],
        volumes: list[float],
        expected_amount: float,
        market_type: str = "암호화폐",
    ) -> DiscordEmbed:
        return fmt_discord.format_sell_notification(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_volume=total_volume,
            prices=prices,
            volumes=volumes,
            expected_amount=expected_amount,
            market_type=market_type,
        )

    def _format_cancel_notification(
        self,
        symbol: str,
        korean_name: str,
        cancel_count: int,
        order_type: str,
        market_type: str = "암호화폐",
    ) -> DiscordEmbed:
        return fmt_discord.format_cancel_notification(
            symbol=symbol,
            korean_name=korean_name,
            cancel_count=cancel_count,
            order_type=order_type,
            market_type=market_type,
        )

    def _format_analysis_notification(
        self,
        symbol: str,
        korean_name: str,
        decision: str,
        confidence: float,
        reasons: list[str],
        market_type: str = "암호화폐",
    ) -> DiscordEmbed:
        return fmt_discord.format_analysis_notification(
            symbol=symbol,
            korean_name=korean_name,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            market_type=market_type,
        )

    def _format_automation_summary(
        self,
        total_coins: int,
        analyzed: int,
        bought: int,
        sold: int,
        errors: int,
        duration_seconds: float,
    ) -> DiscordEmbed:
        return fmt_discord.format_automation_summary(
            total_coins=total_coins,
            analyzed=analyzed,
            bought=bought,
            sold=sold,
            errors=errors,
            duration_seconds=duration_seconds,
        )

    def _format_failure_notification(
        self,
        symbol: str,
        korean_name: str,
        reason: str,
        market_type: str = "암호화폐",
    ) -> DiscordEmbed:
        return fmt_discord.format_failure_notification(
            symbol=symbol,
            korean_name=korean_name,
            reason=reason,
            market_type=market_type,
        )

    def _format_toss_buy_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        kis_quantity: int | None,
        kis_avg_price: float | None,
        recommended_price: float,
        recommended_quantity: int,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> DiscordEmbed:
        return fmt_discord.format_toss_buy_recommendation(
            symbol=symbol,
            korean_name=korean_name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            kis_quantity=kis_quantity,
            kis_avg_price=kis_avg_price,
            recommended_price=recommended_price,
            recommended_quantity=recommended_quantity,
            currency=currency,
            market_type=market_type,
            detail_url=detail_url,
        )

    def _format_toss_sell_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        kis_quantity: int | None,
        kis_avg_price: float | None,
        recommended_price: float,
        recommended_quantity: int,
        expected_profit: float,
        profit_percent: float,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> DiscordEmbed:
        return fmt_discord.format_toss_sell_recommendation(
            symbol=symbol,
            korean_name=korean_name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            kis_quantity=kis_quantity,
            kis_avg_price=kis_avg_price,
            recommended_price=recommended_price,
            recommended_quantity=recommended_quantity,
            expected_profit=expected_profit,
            profit_percent=profit_percent,
            currency=currency,
            market_type=market_type,
            detail_url=detail_url,
        )

    def _format_toss_price_recommendation_discord_embed(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        decision: str,
        confidence: float,
        reasons: list[str],
        appropriate_buy_min: float | None,
        appropriate_buy_max: float | None,
        appropriate_sell_min: float | None,
        appropriate_sell_max: float | None,
        buy_hope_min: float | None = None,
        buy_hope_max: float | None = None,
        sell_target_min: float | None = None,
        sell_target_max: float | None = None,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> DiscordEmbed:
        return fmt_discord.format_toss_price_recommendation(
            symbol=symbol,
            korean_name=korean_name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            appropriate_buy_min=appropriate_buy_min,
            appropriate_buy_max=appropriate_buy_max,
            appropriate_sell_min=appropriate_sell_min,
            appropriate_sell_max=appropriate_sell_max,
            buy_hope_min=buy_hope_min,
            buy_hope_max=buy_hope_max,
            sell_target_min=sell_target_min,
            sell_target_max=sell_target_max,
            currency=currency,
            market_type=market_type,
            detail_url=detail_url,
        )

    def _format_buy_notification_telegram(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_amount: float,
        prices: list[float],
        volumes: list[float],
        market_type: str = "암호화폐",
    ) -> str:
        return fmt_telegram.format_buy_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_amount=total_amount,
            prices=prices,
            volumes=volumes,
            market_type=market_type,
        )

    def _format_sell_notification_telegram(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_volume: float,
        prices: list[float],
        volumes: list[float],
        expected_amount: float,
        market_type: str = "암호화폐",
    ) -> str:
        return fmt_telegram.format_sell_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_volume=total_volume,
            prices=prices,
            volumes=volumes,
            expected_amount=expected_amount,
            market_type=market_type,
        )

    def _format_cancel_notification_telegram(
        self,
        symbol: str,
        korean_name: str,
        cancel_count: int,
        order_type: str,
        market_type: str = "암호화폐",
    ) -> str:
        return fmt_telegram.format_cancel_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            cancel_count=cancel_count,
            order_type=order_type,
            market_type=market_type,
        )

    def _format_analysis_notification_telegram(
        self,
        symbol: str,
        korean_name: str,
        decision: str,
        confidence: float,
        reasons: list[str],
        market_type: str = "암호화폐",
    ) -> str:
        return fmt_telegram.format_analysis_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            market_type=market_type,
        )

    def _format_automation_summary_telegram(
        self,
        total_coins: int,
        analyzed: int,
        bought: int,
        sold: int,
        errors: int,
        duration_seconds: float,
    ) -> str:
        return fmt_telegram.format_automation_summary_telegram(
            total_coins=total_coins,
            analyzed=analyzed,
            bought=bought,
            sold=sold,
            errors=errors,
            duration_seconds=duration_seconds,
        )

    def _format_failure_notification_telegram(
        self,
        symbol: str,
        korean_name: str,
        reason: str,
        market_type: str = "암호화폐",
    ) -> str:
        return fmt_telegram.format_failure_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            reason=reason,
            market_type=market_type,
        )

    def _format_toss_price_recommendation_html(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        decision: str,
        confidence: float,
        reasons: list[str],
        appropriate_buy_min: float | None,
        appropriate_buy_max: float | None,
        appropriate_sell_min: float | None,
        appropriate_sell_max: float | None,
        buy_hope_min: float | None = None,
        buy_hope_max: float | None = None,
        sell_target_min: float | None = None,
        sell_target_max: float | None = None,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> str:
        return fmt_telegram.format_toss_price_recommendation_html(
            symbol=symbol,
            korean_name=korean_name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            appropriate_buy_min=appropriate_buy_min,
            appropriate_buy_max=appropriate_buy_max,
            appropriate_sell_min=appropriate_sell_min,
            appropriate_sell_max=appropriate_sell_max,
            buy_hope_min=buy_hope_min,
            buy_hope_max=buy_hope_max,
            sell_target_min=sell_target_min,
            sell_target_max=sell_target_max,
            currency=currency,
            market_type=market_type,
            detail_url=detail_url,
        )

    # ── transport wrappers (delegate to transports module) ──

    async def _send_to_telegram(
        self, message: str, parse_mode: str = "Markdown"
    ) -> bool:
        """Send message to all configured Telegram chats."""
        if not self._http_client or not self._bot_token or not self._chat_ids:
            return False
        return await send_telegram(
            http_client=self._http_client,
            bot_token=self._bot_token,
            chat_ids=self._chat_ids,
            text=message,
            parse_mode=parse_mode,
        )

    async def _send_to_discord_embed_single(
        self, embed: DiscordEmbed, webhook_url: str
    ) -> bool:
        """Send Discord embed to a specific webhook URL."""
        if not self._http_client or not webhook_url:
            return False
        return await send_discord_embed_single(
            http_client=self._http_client,
            webhook_url=webhook_url,
            embed=embed,
        )

    async def _send_to_discord_content_single(
        self, content: str, webhook_url: str
    ) -> bool:
        """Send plain text content to a specific Discord webhook URL."""
        if not self._http_client or not webhook_url:
            return False
        return await send_discord_content_single(
            http_client=self._http_client,
            webhook_url=webhook_url,
            content=content,
        )

    # ── public notify methods ──────────────────────────────────────────

    async def notify_buy_order(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_amount: float,
        prices: list[float],
        volumes: list[float],
        market_type: str = "암호화폐",
    ) -> bool:
        """Send buy order notification. Discord first, Telegram fallback."""
        if not self._enabled:
            return False

        try:
            embed = self._format_buy_notification(
                symbol,
                korean_name,
                order_count,
                total_amount,
                prices,
                volumes,
                market_type,
            )

            webhook_url = self._get_webhook_for_market_type(market_type)

            if webhook_url:
                discord_success = await self._send_to_discord_embed_single(
                    embed, webhook_url
                )
                if discord_success:
                    return True
                logger.info("Discord send failed, falling back to Telegram")

            telegram_message = self._format_buy_notification_telegram(
                symbol,
                korean_name,
                order_count,
                total_amount,
                prices,
                volumes,
                market_type,
            )
            telegram_success = await self._send_to_telegram(telegram_message)
            if telegram_success:
                return True

            logger.warning("Failed to send buy notification via Discord or Telegram")
            return False

        except Exception as e:
            logger.error(f"Failed to send buy notification: {e}")
            return False

    async def notify_sell_order(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_volume: float,
        prices: list[float],
        volumes: list[float],
        expected_amount: float,
        market_type: str = "암호화폐",
    ) -> bool:
        """Send sell order notification. Discord first, Telegram fallback."""
        if not self._enabled:
            return False

        try:
            embed = self._format_sell_notification(
                symbol,
                korean_name,
                order_count,
                total_volume,
                prices,
                volumes,
                expected_amount,
                market_type,
            )

            webhook_url = self._get_webhook_for_market_type(market_type)

            if webhook_url:
                discord_success = await self._send_to_discord_embed_single(
                    embed, webhook_url
                )
                if discord_success:
                    return True
                logger.info("Discord send failed, falling back to Telegram")

            telegram_message = self._format_sell_notification_telegram(
                symbol,
                korean_name,
                order_count,
                total_volume,
                prices,
                volumes,
                expected_amount,
                market_type,
            )
            telegram_success = await self._send_to_telegram(telegram_message)
            if telegram_success:
                return True

            logger.warning("Failed to send sell notification via Discord or Telegram")
            return False

        except Exception as e:
            logger.error(f"Failed to send sell notification: {e}")
            return False

    async def notify_cancel_orders(
        self,
        symbol: str,
        korean_name: str,
        cancel_count: int,
        order_type: str = "전체",
        market_type: str = "암호화폐",
    ) -> bool:
        """Send order cancellation notification. Discord first, Telegram fallback."""
        if not self._enabled:
            return False

        try:
            embed = self._format_cancel_notification(
                symbol, korean_name, cancel_count, order_type, market_type
            )

            webhook_url = self._get_webhook_for_market_type(market_type)

            if webhook_url:
                discord_success = await self._send_to_discord_embed_single(
                    embed, webhook_url
                )
                if discord_success:
                    return True
                logger.info("Discord send failed, falling back to Telegram")

            telegram_message = self._format_cancel_notification_telegram(
                symbol, korean_name, cancel_count, order_type, market_type
            )
            telegram_success = await self._send_to_telegram(telegram_message)
            if telegram_success:
                return True

            logger.warning("Failed to send cancel notification via Discord or Telegram")
            return False

        except Exception as e:
            logger.error(f"Failed to send cancel notification: {e}")
            return False

    async def notify_analysis_complete(
        self,
        symbol: str,
        korean_name: str,
        decision: str,
        confidence: float,
        reasons: list[str],
        market_type: str = "암호화폐",
    ) -> bool:
        """Send AI analysis completion notification. Discord first, Telegram fallback."""
        if not self._enabled:
            return False

        try:
            embed = self._format_analysis_notification(
                symbol, korean_name, decision, confidence, reasons, market_type
            )

            webhook_url = self._get_webhook_for_market_type(market_type)

            if webhook_url:
                discord_success = await self._send_to_discord_embed_single(
                    embed, webhook_url
                )
                if discord_success:
                    return True
                logger.info("Discord send failed, falling back to Telegram")

            telegram_message = self._format_analysis_notification_telegram(
                symbol, korean_name, decision, confidence, reasons, market_type
            )
            telegram_success = await self._send_to_telegram(telegram_message)
            if telegram_success:
                return True

            logger.warning(
                "Failed to send analysis notification via Discord or Telegram"
            )
            return False

        except Exception as e:
            logger.error(f"Failed to send analysis notification: {e}")
            return False

    async def notify_automation_summary(
        self,
        total_coins: int,
        analyzed: int,
        bought: int,
        sold: int,
        errors: int,
        duration_seconds: float,
    ) -> bool:
        """Send automation execution summary notification."""
        if not self._enabled:
            return False

        try:
            # Try Discord first
            if self._discord_webhook_alerts:
                embed = self._format_automation_summary(
                    total_coins, analyzed, bought, sold, errors, duration_seconds
                )
                discord_success = await self._send_to_discord_embed_single(
                    embed, self._discord_webhook_alerts
                )
                if discord_success:
                    return True
                logger.info("Discord send failed, falling back to Telegram")

            # Fall back to Telegram
            telegram_message = self._format_automation_summary_telegram(
                total_coins, analyzed, bought, sold, errors, duration_seconds
            )
            return await self._send_to_telegram(telegram_message)
        except Exception as e:
            logger.error(f"Failed to send summary notification: {e}")
            return False

    async def notify_trade_failure(
        self,
        symbol: str,
        korean_name: str,
        reason: str,
        market_type: str = "암호화폐",
    ) -> bool:
        """Send trade failure notification. Discord first, Telegram fallback."""
        if not self._enabled:
            return False

        try:
            embed = self._format_failure_notification(
                symbol, korean_name, reason, market_type
            )

            webhook_url = self._get_webhook_for_market_type(market_type)

            if webhook_url:
                discord_success = await self._send_to_discord_embed_single(
                    embed, webhook_url
                )
                if discord_success:
                    return True
                logger.info("Discord send failed, falling back to Telegram")

            telegram_message = self._format_failure_notification_telegram(
                symbol, korean_name, reason, market_type
            )
            telegram_success = await self._send_to_telegram(telegram_message)
            if telegram_success:
                return True

            logger.warning(
                "Failed to send failure notification via Discord or Telegram"
            )
            return False

        except Exception as e:
            logger.error(f"Failed to send failure notification: {e}")
            return False

    async def notify_toss_buy_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        kis_quantity: int | None,
        kis_avg_price: float | None,
        recommended_price: float,
        recommended_quantity: int,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> bool:
        """Send Toss manual buy recommendation notification."""
        if not self._enabled:
            return False

        if toss_quantity <= 0:
            logger.debug(
                f"Skipping Toss buy notification for {symbol}: no Toss holdings"
            )
            return False

        try:
            embed = self._format_toss_buy_recommendation(
                symbol=symbol,
                korean_name=korean_name,
                current_price=current_price,
                toss_quantity=toss_quantity,
                toss_avg_price=toss_avg_price,
                kis_quantity=kis_quantity,
                kis_avg_price=kis_avg_price,
                recommended_price=recommended_price,
                recommended_quantity=recommended_quantity,
                currency=currency,
                market_type=market_type,
                detail_url=detail_url,
            )
            webhook_url = self._get_webhook_for_market_type(market_type)
            if not webhook_url:
                return False
            return await self._send_to_discord_embed_single(embed, webhook_url)
        except Exception as e:
            logger.error(f"Failed to send Toss buy recommendation: {e}")
            return False

    async def notify_toss_sell_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        kis_quantity: int | None,
        kis_avg_price: float | None,
        recommended_price: float,
        recommended_quantity: int,
        expected_profit: float,
        profit_percent: float,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> bool:
        """Send Toss manual sell recommendation notification."""
        if not self._enabled:
            return False

        if toss_quantity <= 0:
            logger.debug(
                f"Skipping Toss sell notification for {symbol}: no Toss holdings"
            )
            return False

        try:
            embed = self._format_toss_sell_recommendation(
                symbol=symbol,
                korean_name=korean_name,
                current_price=current_price,
                toss_quantity=toss_quantity,
                toss_avg_price=toss_avg_price,
                kis_quantity=kis_quantity,
                kis_avg_price=kis_avg_price,
                recommended_price=recommended_price,
                recommended_quantity=recommended_quantity,
                expected_profit=expected_profit,
                profit_percent=profit_percent,
                currency=currency,
                market_type=market_type,
                detail_url=detail_url,
            )
            webhook_url = self._get_webhook_for_market_type(market_type)
            if not webhook_url:
                return False
            return await self._send_to_discord_embed_single(embed, webhook_url)
        except Exception as e:
            logger.error(f"Failed to send Toss sell recommendation: {e}")
            return False

    async def notify_toss_price_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        decision: str,
        confidence: float,
        reasons: list[str],
        appropriate_buy_min: float | None,
        appropriate_buy_max: float | None,
        appropriate_sell_min: float | None,
        appropriate_sell_max: float | None,
        buy_hope_min: float | None = None,
        buy_hope_max: float | None = None,
        sell_target_min: float | None = None,
        sell_target_max: float | None = None,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> bool:
        """Send Toss price recommendation notification with AI analysis."""
        if not self._enabled:
            return False

        if toss_quantity <= 0:
            logger.debug(f"Skipping Toss notification for {symbol}: no Toss holdings")
            return False

        try:
            embed = self._format_toss_price_recommendation_discord_embed(
                symbol=symbol,
                korean_name=korean_name,
                current_price=current_price,
                toss_quantity=toss_quantity,
                toss_avg_price=toss_avg_price,
                decision=decision,
                confidence=confidence,
                reasons=reasons,
                appropriate_buy_min=appropriate_buy_min,
                appropriate_buy_max=appropriate_buy_max,
                appropriate_sell_min=appropriate_sell_min,
                appropriate_sell_max=appropriate_sell_max,
                buy_hope_min=buy_hope_min,
                buy_hope_max=buy_hope_max,
                sell_target_min=sell_target_min,
                sell_target_max=sell_target_max,
                currency=currency,
                market_type=market_type,
                detail_url=detail_url,
            )
            webhook_url = self._get_webhook_for_market_type(market_type)
            if not webhook_url:
                return False
            return await self._send_to_discord_embed_single(embed, webhook_url)
        except Exception as e:
            logger.error(f"Failed to send Toss price recommendation: {e}")
            return False

    async def notify_openclaw_message(
        self,
        message: str,
        parse_mode: str = "Markdown",
        *,
        correlation_id: str | None = None,
        market_type: str | None = None,
        skip_discord: bool = False,
    ) -> bool:
        """Forward an OpenClaw outbound message to Discord or Telegram."""
        discord_result = "skipped(no_discord_webhook)"
        telegram_result = "skipped(not_attempted)"

        try:
            if not self._enabled:
                discord_result = "skipped(notifier_disabled)"
                telegram_result = "skipped(notifier_disabled)"
                logger.info(
                    "OpenClaw mirror result: correlation_id=%s discord=%s telegram=%s",
                    correlation_id,
                    discord_result,
                    telegram_result,
                )
                return False

            webhook_url: str | None = None
            if skip_discord:
                discord_result = "skipped(skip_discord)"
            elif market_type is not None:
                webhook_url = self._get_webhook_for_market_type(market_type)
                if webhook_url is None:
                    discord_result = "skipped(no_market_webhook)"
            else:
                webhook_url = self._discord_webhook_alerts

            if webhook_url:
                discord_success = await self._send_to_discord_content_single(
                    message, webhook_url
                )
                if discord_success:
                    discord_result = "success"
                    telegram_result = "skipped(fallback_not_needed)"
                    logger.info(
                        "OpenClaw mirror result: correlation_id=%s discord=%s telegram=%s",
                        correlation_id,
                        discord_result,
                        telegram_result,
                    )
                    return True
                discord_result = "failed"

            # Fall back to Telegram
            if not self._has_telegram_delivery_config():
                telegram_result = "skipped(no_telegram_config)"
                logger.info(
                    "OpenClaw mirror result: correlation_id=%s discord=%s telegram=%s",
                    correlation_id,
                    discord_result,
                    telegram_result,
                )
                return False

            telegram_success = await self._send_to_telegram(
                message, parse_mode=parse_mode
            )
            telegram_result = "success" if telegram_success else "failed"
            logger.info(
                "OpenClaw mirror result: correlation_id=%s discord=%s telegram=%s",
                correlation_id,
                discord_result,
                telegram_result,
            )
            return telegram_success
        except Exception as e:
            logger.error(f"Failed to forward OpenClaw message: {e}")
            logger.info(
                "OpenClaw mirror result: correlation_id=%s discord=%s telegram=%s",
                correlation_id,
                discord_result,
                telegram_result,
            )
            return False

    async def test_connection(self) -> bool:
        """Test notification connection by sending a test message."""
        if not self._enabled or not self._http_client:
            logger.warning("TradeNotifier is not configured")
            return False

        try:
            discord_webhooks = [
                self._discord_webhook_alerts,
                self._discord_webhook_us,
                self._discord_webhook_kr,
                self._discord_webhook_crypto,
            ]

            for webhook_url in discord_webhooks:
                if webhook_url:
                    test_embed: DiscordEmbed = {
                        "title": "✅ 거래 알림 테스트",
                        "description": f"연결 성공: {format_datetime()}\n거래 알림 시스템이 정상 작동 중입니다.",
                        "color": 0x00FF00,
                        "fields": [],
                    }
                    return await self._send_to_discord_embed_single(
                        test_embed, webhook_url
                    )

            if self._bot_token:
                test_message = (
                    "✅ *거래 알림 테스트*\n\n"
                    f"연결 성공: {format_datetime()}\n"
                    "거래 알림 시스템이 정상 작동 중입니다."
                )
                return await self._send_to_telegram(test_message)

            logger.warning("No notification system configured")
            return False

        except Exception as e:
            logger.error(f"Connection test failed: {e}", exc_info=True)
            return False


# Singleton instance getter
def get_trade_notifier() -> TradeNotifier:
    """Get the singleton TradeNotifier instance."""
    return TradeNotifier()
