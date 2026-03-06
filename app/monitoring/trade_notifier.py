"""
Trade notification system with Telegram and Discord integration.

Features:
- Singleton pattern for TradeNotifier
- Rich trade event formatting with markdown
- Support for buy, sell, cancel, and analysis notifications
- Multiple Telegram chat ID support
- Multiple Discord webhook URL support
"""

from __future__ import annotations

import logging

import httpx

from app.core.timezone import format_datetime

logger = logging.getLogger(__name__)


class TradeNotifier:
    """
    Singleton trade notifier with Telegram and Discord integration.
    """

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
        """
        Configure the trade notifier.

        Args:
            bot_token: Telegram bot token
            chat_ids: List of Telegram chat IDs to send notifications to
            enabled: Whether trade notifications are enabled
            discord_webhook_urls: List of Discord webhook URLs (deprecated, use specific webhooks)
            discord_webhook_us: Discord webhook URL for US stocks
            discord_webhook_kr: Discord webhook URL for Korean stocks
            discord_webhook_crypto: Discord webhook URL for crypto
            discord_webhook_alerts: Discord webhook URL for alerts/analysis
        """
        self._bot_token = bot_token
        self._chat_ids = chat_ids
        self._discord_webhook_us = discord_webhook_us
        self._discord_webhook_kr = discord_webhook_kr
        self._discord_webhook_crypto = discord_webhook_crypto
        self._discord_webhook_alerts = discord_webhook_alerts
        self._enabled = enabled

        if enabled and not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=10.0)
            logger.info(f"TradeNotifier configured: {len(chat_ids)} chat(s)")

            # Log configured Discord webhooks
            webhook_count = sum([
                bool(self._discord_webhook_us),
                bool(self._discord_webhook_kr),
                bool(self._discord_webhook_crypto),
                bool(self._discord_webhook_alerts),
            ])
            if webhook_count > 0:
                logger.info(f"TradeNotifier Discord webhooks: {webhook_count} webhook(s) configured")

    async def shutdown(self) -> None:
        """Shutdown HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
            logger.debug("TradeNotifier HTTP client closed")

        logger.info("TradeNotifier shutdown complete")

    def _get_webhook_for_market_type(self, market_type: str) -> str | None:
        """
        Get the appropriate Discord webhook URL for a given market type.

        Args:
            market_type: Type of market (US, 해외주식, 국내주식, 암호화폐)

        Returns:
            Discord webhook URL for the market type, or None if not configured
        """
        # Normalize market type - handle both English and Korean
        market_type_normalized = market_type.strip()

        # Map market types to webhooks
        if market_type_normalized in ("US", "해외주식"):
            return self._discord_webhook_us
        elif market_type_normalized == "국내주식":
            return self._discord_webhook_kr
        elif market_type_normalized == "암호화폐":
            return self._discord_webhook_crypto
        else:
            logger.warning(f"Unknown market type: {market_type}")
            return None

    def _format_buy_notification(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_amount: float,
        prices: list[float],
        volumes: list[float],
        market_type: str = "암호화폐",
    ) -> dict:
        """
        Format buy order notification as Discord embed.

        Args:
            symbol: Trading symbol (e.g., "BTC", "005930")
            korean_name: Korean name of asset
            order_count: Number of orders placed
            total_amount: Total amount in KRW
            prices: List of order prices
            volumes: List of order volumes
            market_type: Type of market (암호화폐, 국내주식, 해외주식)

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "시장", "value": market_type, "inline": True},
            {"name": "주문 수", "value": f"{order_count}건", "inline": True},
            {"name": "총 금액", "value": f"{total_amount:,.0f}원", "inline": False},
        ]

        # Add order details if available
        if prices and volumes and len(prices) == len(volumes):
            order_details = []
            for i, (price, volume) in enumerate(zip(prices, volumes, strict=True), 1):
                order_details.append(f"{i}. {price:,.2f}원 × {volume:.8g}")
            fields.append({
                "name": "주문 상세",
                "value": "\n".join(order_details),
                "inline": False,
            })
        elif prices:
            price_list = []
            for i, price in enumerate(prices, 1):
                price_list.append(f"{i}. {price:,.2f}원")
            fields.append({
                "name": "매수 가격대",
                "value": "\n".join(price_list),
                "inline": False,
            })

        return {
            "title": "💰 매수 주문 접수",
            "description": f"🕒 {timestamp}",
            "color": 0x00FF00,  # Green for buy
            "fields": fields,
        }

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
    ) -> dict:
        """
        Format sell order notification as Discord embed.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            order_count: Number of orders placed
            total_volume: Total volume being sold
            prices: List of order prices
            volumes: List of order volumes
            expected_amount: Expected total amount in KRW
            market_type: Type of market

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "시장", "value": market_type, "inline": True},
            {"name": "주문 수", "value": f"{order_count}건", "inline": True},
            {"name": "총 수량", "value": f"{total_volume:.8g}", "inline": False},
            {"name": "예상 금액", "value": f"{expected_amount:,.0f}원", "inline": False},
        ]

        # Add order details if available
        if prices and volumes and len(prices) == len(volumes):
            order_details = []
            for i, (price, volume) in enumerate(zip(prices, volumes, strict=True), 1):
                order_details.append(f"{i}. {price:,.2f}원 × {volume:.8g}")
            fields.append({
                "name": "주문 상세",
                "value": "\n".join(order_details),
                "inline": False,
            })
        elif prices:
            price_list = []
            for i, price in enumerate(prices, 1):
                price_list.append(f"{i}. {price:,.2f}원")
            fields.append({
                "name": "매도 가격대",
                "value": "\n".join(price_list),
                "inline": False,
            })

        return {
            "title": "💸 매도 주문 접수",
            "description": f"🕒 {timestamp}",
            "color": 0xFF0000,  # Red for sell
            "fields": fields,
        }

    def _format_cancel_notification(
        self,
        symbol: str,
        korean_name: str,
        cancel_count: int,
        order_type: str = "전체",
        market_type: str = "암호화폐",
    ) -> dict:
        """
        Format order cancellation notification as Discord embed.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            cancel_count: Number of orders cancelled
            order_type: Type of orders cancelled (매수, 매도, 전체)
            market_type: Type of market

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "시장", "value": market_type, "inline": True},
            {"name": "취소 유형", "value": order_type, "inline": True},
            {"name": "취소 건수", "value": f"{cancel_count}건", "inline": False},
        ]

        return {
            "title": "🚫 주문 취소",
            "description": f"🕒 {timestamp}",
            "color": 0xFFFF00,  # Yellow for cancel
            "fields": fields,
        }

    def _format_analysis_notification(
        self,
        symbol: str,
        korean_name: str,
        decision: str,
        confidence: float,
        reasons: list[str],
        market_type: str = "암호화폐",
    ) -> dict:
        """
        Format AI analysis notification as Discord embed.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            decision: AI decision (buy, hold, sell)
            confidence: Confidence score (0-100)
            reasons: List of decision reasons
            market_type: Type of market

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()

        # Decision emoji mapping
        decision_emoji = {"buy": "🟢", "hold": "🟡", "sell": "🔴"}
        decision_text = {"buy": "매수", "hold": "보유", "sell": "매도"}

        emoji = decision_emoji.get(decision.lower(), "⚪")
        decision_kr = decision_text.get(decision.lower(), decision)

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "시장", "value": market_type, "inline": True},
            {"name": "판단", "value": f"{emoji} {decision_kr}", "inline": True},
            {"name": "신뢰도", "value": f"{confidence:.1f}%", "inline": False},
        ]

        # Add reasons if available
        if reasons:
            reason_text = "\n".join(
                f"{i}. {reason}" for i, reason in enumerate(reasons[:3], 1)  # Max 3 reasons
            )
            fields.append({
                "name": "주요 근거",
                "value": reason_text,
                "inline": False,
            })

        return {
            "title": "📊 AI 분석 완료",
            "description": f"🕒 {timestamp}",
            "color": 0x0000FF,  # Blue for analysis
            "fields": fields,
        }

    def _format_automation_summary(
        self,
        total_coins: int,
        analyzed: int,
        bought: int,
        sold: int,
        errors: int,
        duration_seconds: float,
    ) -> dict:
        """
        Format automation execution summary as Discord embed.

        Args:
            total_coins: Total number of coins processed
            analyzed: Number of coins analyzed
            bought: Number of buy orders placed
            sold: Number of sell orders placed
            errors: Number of errors occurred
            duration_seconds: Total execution time

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()

        # Build fields list
        fields = [
            {"name": "처리 종목", "value": f"{total_coins}개", "inline": True},
            {"name": "분석 완료", "value": f"{analyzed}개", "inline": True},
            {"name": "매수 주문", "value": f"{bought}건", "inline": True},
            {"name": "매도 주문", "value": f"{sold}건", "inline": True},
            {"name": "실행 시간", "value": f"{duration_seconds:.1f}초", "inline": True},
        ]

        # Add error count if any errors occurred
        if errors > 0:
            fields.append({
                "name": "오류 발생",
                "value": f"{errors}건",
                "inline": False,
            })

        return {
            "title": "🤖 자동 거래 실행 완료",
            "description": f"🕒 {timestamp}",
            "color": 0x00BFFF,  # Deep Sky Blue for automation
            "fields": fields,
        }

    async def _send_to_telegram(
        self, message: str, parse_mode: str = "Markdown"
    ) -> bool:
        """
        Send message to all configured Telegram chats.

        Args:
            message: Message to send
            parse_mode: Telegram parse mode ("Markdown" or "HTML")

        Returns:
            True if at least one message was sent successfully
        """
        if not self._enabled or not self._http_client or not self._bot_token:
            return False

        success_count = 0
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"

        for chat_id in self._chat_ids:
            try:
                response = await self._http_client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
                success_count += 1

            except Exception as e:
                logger.error(f"Failed to send notification to chat {chat_id}: {e}")

        if success_count > 0:
            logger.info(f"Notification sent to {success_count} chat(s)")
            return True
        return False

    async def _send_to_discord(self, message: str) -> bool:
        """
        Send message to all configured Discord webhooks.

        Args:
            message: Message to send

        Returns:
            True if at least one message was sent successfully
        """
        if not self._enabled or not self._http_client or not self._discord_webhook_urls:
            return False

        success_count = 0

        for webhook_url in self._discord_webhook_urls:
            try:
                response = await self._http_client.post(
                    webhook_url,
                    json={"content": message},
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                success_count += 1

            except Exception as e:
                logger.error(f"Failed to send notification to Discord webhook: {e}")

        if success_count > 0:
            logger.info(f"Notification sent to {success_count} Discord webhook(s)")
            return True
        return False

    async def _send_to_discord_embed(self, embed: dict) -> bool:
        """
        Send Discord embed to all configured Discord webhooks.

        Args:
            embed: Discord embed dict

        Returns:
            True if at least one message was sent successfully
        """
        if not self._enabled or not self._http_client or not self._discord_webhook_urls:
            return False

        success_count = 0

        for webhook_url in self._discord_webhook_urls:
            try:
                response = await self._http_client.post(
                    webhook_url,
                    json={"embeds": [embed]},
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                success_count += 1

            except Exception as e:
                logger.error(f"Failed to send embed to Discord webhook: {e}")

        if success_count > 0:
            logger.info(f"Embed sent to {success_count} Discord webhook(s)")
            return True
        return False

    async def _send_to_discord_embed_single(self, embed: dict, webhook_url: str) -> bool:
        """
        Send Discord embed to a specific webhook URL.

        Args:
            embed: Discord embed dict
            webhook_url: Specific Discord webhook URL to send to

        Returns:
            True if message was sent successfully
        """
        if not self._enabled or not self._http_client:
            return False

        if not webhook_url:
            logger.warning("No Discord webhook URL provided")
            return False

        try:
            response = await self._http_client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            logger.info(f"Embed sent to Discord webhook")
            return True

        except Exception as e:
            logger.error(f"Failed to send embed to Discord webhook: {e}")
            return False

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
        """
        Send buy order notification to Discord webhook based on market_type.

        Routes to the appropriate Discord webhook:
        - US/해외주식 → discord_webhook_us
        - 국내주식 → discord_webhook_kr
        - 암호화폐 → discord_webhook_crypto
        """
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

            # Get the appropriate Discord webhook for this market type
            webhook_url = self._get_webhook_for_market_type(market_type)

            # Send to Discord if webhook is configured
            if webhook_url:
                return await self._send_to_discord_embed_single(embed, webhook_url)
            else:
                logger.warning(
                    f"No Discord webhook configured for market type: {market_type}"
                )
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
        """
        Send sell order notification to Discord webhook based on market_type.

        Routes to the appropriate Discord webhook:
        - US/해외주식 → discord_webhook_us
        - 국내주식 → discord_webhook_kr
        - 암호화폐 → discord_webhook_crypto
        """
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

            # Get the appropriate Discord webhook for this market type
            webhook_url = self._get_webhook_for_market_type(market_type)

            # Send to Discord if webhook is configured
            if webhook_url:
                return await self._send_to_discord_embed_single(embed, webhook_url)
            else:
                logger.warning(
                    f"No Discord webhook configured for market type: {market_type}"
                )
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
        """Send order cancellation notification."""
        if not self._enabled:
            return False

        try:
            message = self._format_cancel_notification(
                symbol, korean_name, cancel_count, order_type, market_type
            )
            return await self._send_to_telegram(message)
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
        """Send AI analysis completion notification."""
        if not self._enabled:
            return False

        try:
            message = self._format_analysis_notification(
                symbol, korean_name, decision, confidence, reasons, market_type
            )
            return await self._send_to_telegram(message)
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
            message = self._format_automation_summary(
                total_coins, analyzed, bought, sold, errors, duration_seconds
            )
            return await self._send_to_telegram(message)
        except Exception as e:
            logger.error(f"Failed to send summary notification: {e}")
            return False

    def _format_failure_notification(
        self,
        symbol: str,
        korean_name: str,
        reason: str,
        market_type: str = "암호화폐",
    ) -> dict:
        """
        Format trade failure notification as Discord embed.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            reason: Failure reason
            market_type: Type of market

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "시장", "value": market_type, "inline": True},
            {"name": "사유", "value": reason, "inline": False},
        ]

        return {
            "title": "⚠️ 거래 실패",
            "description": f"🕒 {timestamp}",
            "color": 0xFF6600,  # Orange for failure
            "fields": fields,
        }

    async def notify_trade_failure(
        self,
        symbol: str,
        korean_name: str,
        reason: str,
        market_type: str = "암호화폐",
    ) -> bool:
        """Send trade failure notification."""
        if not self._enabled:
            return False

        try:
            embed = self._format_failure_notification(
                symbol, korean_name, reason, market_type
            )
            return await self._send_to_discord_embed(embed)
        except Exception as e:
            logger.error(f"Failed to send failure notification: {e}")
            return False

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
    ) -> dict:
        """
        Format Toss manual buy recommendation notification as Discord embed.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            current_price: Current market price
            toss_quantity: Quantity held in Toss
            toss_avg_price: Average price in Toss
            kis_quantity: Quantity held in KIS (optional)
            kis_avg_price: Average price in KIS (optional)
            recommended_price: AI recommended buy price
            recommended_quantity: AI recommended buy quantity
            currency: Currency symbol (원, $)
            market_type: Type of market

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()
        is_usd = currency == "$"

        def price_fmt(p: float) -> str:
            return f"${p:,.2f}" if is_usd else f"{p:,.0f}{currency}"

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "시장", "value": market_type, "inline": True},
            {"name": "현재가", "value": price_fmt(current_price), "inline": False},
            {
                "name": "토스 보유",
                "value": f"{toss_quantity}주 (평단가 {price_fmt(toss_avg_price)})",
                "inline": False,
            },
        ]

        # Add KIS holdings if available
        if kis_quantity and kis_quantity > 0 and kis_avg_price:
            fields.append({
                "name": "한투 보유",
                "value": f"{kis_quantity}주 (평단가 {price_fmt(kis_avg_price)})",
                "inline": False,
            })

        # Add recommendation
        fields.extend([
            {
                "name": "💡 추천 매수가",
                "value": price_fmt(recommended_price),
                "inline": False,
            },
            {
                "name": "추천 수량",
                "value": f"{recommended_quantity}주",
                "inline": False,
            },
        ])

        return {
            "title": "📈 [토스 수동매수]",
            "description": f"🕒 {timestamp}",
            "color": 0x00FF00,  # Green for buy
            "fields": fields,
        }

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
    ) -> dict:
        """
        Format Toss manual sell recommendation notification as Discord embed.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            current_price: Current market price
            toss_quantity: Quantity held in Toss
            toss_avg_price: Average price in Toss
            kis_quantity: Quantity held in KIS (optional)
            kis_avg_price: Average price in KIS (optional)
            recommended_price: AI recommended sell price
            recommended_quantity: AI recommended sell quantity
            expected_profit: Expected profit amount
            profit_percent: Expected profit percentage
            currency: Currency symbol (원, $)
            market_type: Type of market

        Returns:
            Discord embed dict
        """
        timestamp = format_datetime()
        is_usd = currency == "$"

        def price_fmt(p: float) -> str:
            return f"${p:,.2f}" if is_usd else f"{p:,.0f}{currency}"

        profit_sign = "+" if profit_percent >= 0 else ""

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "시장", "value": market_type, "inline": True},
            {"name": "현재가", "value": price_fmt(current_price), "inline": False},
            {
                "name": "토스 보유",
                "value": f"{toss_quantity}주 (평단가 {price_fmt(toss_avg_price)})",
                "inline": False,
            },
        ]

        # Add KIS holdings if available
        if kis_quantity and kis_quantity > 0 and kis_avg_price:
            fields.append({
                "name": "한투 보유",
                "value": f"{kis_quantity}주 (평단가 {price_fmt(kis_avg_price)})",
                "inline": False,
            })

        # Add recommendation with profit
        fields.extend([
            {
                "name": "💡 추천 매도가",
                "value": f"{price_fmt(recommended_price)} ({profit_sign}{profit_percent:.1f}%)",
                "inline": False,
            },
            {
                "name": "추천 수량",
                "value": f"{recommended_quantity}주",
                "inline": False,
            },
            {
                "name": "예상 수익",
                "value": price_fmt(expected_profit),
                "inline": False,
            },
        ])

        return {
            "title": "📉 [토스 수동매도]",
            "description": f"🕒 {timestamp}",
            "color": 0xFF0000,  # Red for sell
            "fields": fields,
        }

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
    ) -> bool:
        """
        Send Toss manual buy recommendation notification.

        Only sends if toss_quantity > 0.

        Returns:
            True if notification sent successfully
        """
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
            )
            return await self._send_to_discord_embed(embed)
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
    ) -> bool:
        """
        Send Toss manual sell recommendation notification.

        Only sends if toss_quantity > 0.

        Returns:
            True if notification sent successfully
        """
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
            )
            return await self._send_to_discord_embed(embed)
        except Exception as e:
            logger.error(f"Failed to send Toss sell recommendation: {e}")
            return False

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
    ) -> dict:
        """
        Format Toss price recommendation notification with AI analysis as Discord embed.
        """
        timestamp = format_datetime()
        is_usd = currency == "$"

        def price_fmt(p: float) -> str:
            return f"${p:,.2f}" if is_usd else f"{p:,.0f}{currency}"

        # 수익률 계산
        profit_percent = (
            ((current_price / toss_avg_price) - 1) * 100 if toss_avg_price > 0 else 0
        )
        profit_sign = "+" if profit_percent >= 0 else ""

        # Decision emoji mapping
        decision_emoji = {"buy": "🟢", "hold": "🟡", "sell": "🔴"}
        decision_text = {"buy": "매수", "hold": "보유", "sell": "매도"}
        emoji = decision_emoji.get(decision.lower(), "⚪")
        decision_kr = decision_text.get(decision.lower(), decision)

        # Color based on decision
        decision_color = {"buy": 0x00FF00, "hold": 0xFFFF00, "sell": 0xFF0000}
        color = decision_color.get(decision.lower(), 0x0000FF)

        # Build fields list
        fields = [
            {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
            {"name": "현재가", "value": price_fmt(current_price), "inline": True},
            {
                "name": "보유",
                "value": f"{toss_quantity}주 (평단가 {price_fmt(toss_avg_price)}, {profit_sign}{profit_percent:.1f}%)",
                "inline": False,
            },
            {
                "name": "AI 판단",
                "value": f"{emoji} {decision_kr} (신뢰도 {confidence:.0f}%)",
                "inline": False,
            },
        ]

        # 근거 추가
        if reasons:
            reason_text = "\n".join(
                f"{i}. {reason[:80]}..." if len(reason) > 80 else f"{i}. {reason}"
                for i, reason in enumerate(reasons[:3], 1)
            )
            fields.append({
                "name": "근거",
                "value": reason_text,
                "inline": False,
            })

        # 가격 제안 추가
        price_suggestions = []

        if appropriate_buy_min or appropriate_buy_max:
            buy_range = []
            if appropriate_buy_min:
                buy_range.append(price_fmt(appropriate_buy_min))
            if appropriate_buy_max:
                buy_range.append(price_fmt(appropriate_buy_max))
            price_suggestions.append(f"적정 매수: {' ~ '.join(buy_range)}")

        if appropriate_sell_min or appropriate_sell_max:
            sell_range = []
            if appropriate_sell_min:
                sell_range.append(price_fmt(appropriate_sell_min))
            if appropriate_sell_max:
                sell_range.append(price_fmt(appropriate_sell_max))
            price_suggestions.append(f"적정 매도: {' ~ '.join(sell_range)}")

        if buy_hope_min or buy_hope_max:
            hope_range = []
            if buy_hope_min:
                hope_range.append(price_fmt(buy_hope_min))
            if buy_hope_max:
                hope_range.append(price_fmt(buy_hope_max))
            price_suggestions.append(f"매수 희망: {' ~ '.join(hope_range)}")

        if sell_target_min or sell_target_max:
            target_range = []
            if sell_target_min:
                target_range.append(price_fmt(sell_target_min))
            if sell_target_max:
                target_range.append(price_fmt(sell_target_max))
            price_suggestions.append(f"매도 목표: {' ~ '.join(target_range)}")

        if price_suggestions:
            fields.append({
                "name": "가격 제안",
                "value": "\n".join(price_suggestions),
                "inline": False,
            })

        return {
            "title": "📊 [토스] AI 분석",
            "description": f"🕒 {timestamp}",
            "color": color,
            "fields": fields,
        }

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
    ) -> bool:
        """
        Send Toss price recommendation notification with AI analysis.

        Always sends regardless of AI decision (buy/hold/sell).
        Uses HTML parse mode for robust handling of special characters.
        """
        if not self._enabled:
            return False

        if toss_quantity <= 0:
            logger.debug(f"Skipping Toss notification for {symbol}: no Toss holdings")
            return False

        try:
            embed = self._format_toss_price_recommendation_html(
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
            )
            return await self._send_to_discord_embed(embed)
        except Exception as e:
            logger.error(f"Failed to send Toss price recommendation: {e}")
            return False

    async def notify_openclaw_message(
        self,
        message: str,
        parse_mode: str = "Markdown",
    ) -> bool:
        """
        Forward an OpenClaw outbound message to Telegram.

        Args:
            message: Original message payload sent to OpenClaw
            parse_mode: Telegram parse mode ("Markdown" or "HTML")

        Returns:
            True if notification sent successfully
        """
        if not self._enabled:
            return False

        try:
            return await self._send_to_telegram(message, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Failed to forward OpenClaw message: {e}")
            return False

    async def test_connection(self) -> bool:
        """
        Test Telegram connection by sending a test message.

        Returns:
            True if successful, False otherwise
        """
        if not self._enabled or not self._http_client or not self._bot_token:
            logger.warning("TradeNotifier is not configured")
            return False

        try:
            test_message = (
                "✅ *거래 알림 테스트*\n\n"
                f"연결 성공: {format_datetime()}\n"
                "거래 알림 시스템이 정상 작동 중입니다."
            )

            return await self._send_to_telegram(test_message)

        except Exception as e:
            logger.error(f"Telegram connection test failed: {e}", exc_info=True)
            return False


# Singleton instance getter
def get_trade_notifier() -> TradeNotifier:
    """
    Get the singleton TradeNotifier instance.

    Returns:
        TradeNotifier instance
    """
    return TradeNotifier()
