"""
Telegram trade notification system with rich formatting.

Features:
- Singleton pattern for TradeNotifier
- Rich trade event formatting with markdown
- Support for buy, sell, cancel, and analysis notifications
- Multiple chat ID support
"""

import logging
from typing import Dict, List, Optional

import httpx

from app.core.timezone import format_datetime

logger = logging.getLogger(__name__)


class TradeNotifier:
    """
    Singleton trade notifier with Telegram integration.
    """

    _instance: Optional["TradeNotifier"] = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize TradeNotifier (only once due to singleton pattern)."""
        if not self._initialized:
            self._bot_token: Optional[str] = None
            self._chat_ids: List[str] = []
            self._enabled: bool = False
            self._http_client: Optional[httpx.AsyncClient] = None
            TradeNotifier._initialized = True

    def configure(
        self,
        bot_token: str,
        chat_ids: List[str],
        enabled: bool = True,
    ) -> None:
        """
        Configure the trade notifier.

        Args:
            bot_token: Telegram bot token
            chat_ids: List of Telegram chat IDs to send notifications to
            enabled: Whether trade notifications are enabled
        """
        self._bot_token = bot_token
        self._chat_ids = chat_ids
        self._enabled = enabled

        if enabled and not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=10.0)
            logger.info(
                f"TradeNotifier configured: {len(chat_ids)} chat(s)"
            )

    async def shutdown(self) -> None:
        """Shutdown HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
            logger.debug("TradeNotifier HTTP client closed")

        logger.info("TradeNotifier shutdown complete")

    def _format_buy_notification(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_amount: float,
        prices: List[float],
        volumes: List[float],
        market_type: str = "암호화폐",
    ) -> str:
        """
        Format buy order notification.

        Args:
            symbol: Trading symbol (e.g., "BTC", "005930")
            korean_name: Korean name of asset
            order_count: Number of orders placed
            total_amount: Total amount in KRW
            prices: List of order prices
            volumes: List of order volumes
            market_type: Type of market (암호화폐, 국내주식, 해외주식)

        Returns:
            Markdown-formatted notification message
        """
        timestamp = format_datetime()

        parts = [
            "💰 *매수 주문 접수*",
            f"🕒 {timestamp}",
            "",
            f"*종목:* {korean_name} ({symbol})",
            f"*시장:* {market_type}",
            f"*주문 수:* {order_count}건",
            f"*총 금액:* {total_amount:,.0f}원",
        ]

        # Add order details if available
        if prices and volumes and len(prices) == len(volumes):
            parts.append("")
            parts.append("*주문 상세:*")
            for i, (price, volume) in enumerate(zip(prices, volumes), 1):
                parts.append(f"  {i}. 가격: {price:,.2f}원 × 수량: {volume:.8g}")
        elif prices:
            parts.append("")
            parts.append("*매수 가격대:*")
            for i, price in enumerate(prices, 1):
                parts.append(f"  {i}. {price:,.2f}원")

        return "\n".join(parts)

    def _format_sell_notification(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_volume: float,
        prices: List[float],
        volumes: List[float],
        expected_amount: float,
        market_type: str = "암호화폐",
    ) -> str:
        """
        Format sell order notification.

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
            Markdown-formatted notification message
        """
        timestamp = format_datetime()

        parts = [
            "💸 *매도 주문 접수*",
            f"🕒 {timestamp}",
            "",
            f"*종목:* {korean_name} ({symbol})",
            f"*시장:* {market_type}",
            f"*주문 수:* {order_count}건",
            f"*총 수량:* {total_volume:.8g}",
            f"*예상 금액:* {expected_amount:,.0f}원",
        ]

        # Add order details if available
        if prices and volumes and len(prices) == len(volumes):
            parts.append("")
            parts.append("*주문 상세:*")
            for i, (price, volume) in enumerate(zip(prices, volumes), 1):
                parts.append(f"  {i}. 가격: {price:,.2f}원 × 수량: {volume:.8g}")
        elif prices:
            parts.append("")
            parts.append("*매도 가격대:*")
            for i, price in enumerate(prices, 1):
                parts.append(f"  {i}. {price:,.2f}원")

        return "\n".join(parts)

    def _format_cancel_notification(
        self,
        symbol: str,
        korean_name: str,
        cancel_count: int,
        order_type: str = "전체",
        market_type: str = "암호화폐",
    ) -> str:
        """
        Format order cancellation notification.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            cancel_count: Number of orders cancelled
            order_type: Type of orders cancelled (매수, 매도, 전체)
            market_type: Type of market

        Returns:
            Markdown-formatted notification message
        """
        timestamp = format_datetime()

        parts = [
            "🚫 *주문 취소*",
            f"🕒 {timestamp}",
            "",
            f"*종목:* {korean_name} ({symbol})",
            f"*시장:* {market_type}",
            f"*취소 유형:* {order_type}",
            f"*취소 건수:* {cancel_count}건",
        ]

        return "\n".join(parts)

    def _format_analysis_notification(
        self,
        symbol: str,
        korean_name: str,
        decision: str,
        confidence: float,
        reasons: List[str],
        market_type: str = "암호화폐",
    ) -> str:
        """
        Format AI analysis notification.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            decision: AI decision (buy, hold, sell)
            confidence: Confidence score (0-100)
            reasons: List of decision reasons
            market_type: Type of market

        Returns:
            Markdown-formatted notification message
        """
        timestamp = format_datetime()

        # Decision emoji mapping
        decision_emoji = {
            "buy": "🟢",
            "hold": "🟡",
            "sell": "🔴"
        }
        decision_text = {
            "buy": "매수",
            "hold": "보유",
            "sell": "매도"
        }

        emoji = decision_emoji.get(decision.lower(), "⚪")
        decision_kr = decision_text.get(decision.lower(), decision)

        parts = [
            f"{emoji} *AI 분석 완료*",
            f"🕒 {timestamp}",
            "",
            f"*종목:* {korean_name} ({symbol})",
            f"*시장:* {market_type}",
            f"*판단:* {decision_kr}",
            f"*신뢰도:* {confidence:.1f}%",
            "",
            "*주요 근거:*"
        ]

        # Add reasons
        for i, reason in enumerate(reasons[:3], 1):  # Max 3 reasons
            parts.append(f"  {i}. {reason}")

        return "\n".join(parts)

    def _format_automation_summary(
        self,
        total_coins: int,
        analyzed: int,
        bought: int,
        sold: int,
        errors: int,
        duration_seconds: float,
    ) -> str:
        """
        Format automation execution summary.

        Args:
            total_coins: Total number of coins processed
            analyzed: Number of coins analyzed
            bought: Number of buy orders placed
            sold: Number of sell orders placed
            errors: Number of errors occurred
            duration_seconds: Total execution time

        Returns:
            Markdown-formatted summary message
        """
        timestamp = format_datetime()

        parts = [
            "🤖 *자동 거래 실행 완료*",
            f"🕒 {timestamp}",
            "",
            f"*처리 종목:* {total_coins}개",
            f"*분석 완료:* {analyzed}개",
            f"*매수 주문:* {bought}건",
            f"*매도 주문:* {sold}건",
            f"*실행 시간:* {duration_seconds:.1f}초",
        ]

        if errors > 0:
            parts.append(f"⚠️ *오류 발생:* {errors}건")

        return "\n".join(parts)

    async def _send_to_telegram(self, message: str) -> bool:
        """
        Send message to all configured Telegram chats.

        Args:
            message: Message to send

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
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
                success_count += 1

            except Exception as e:
                logger.error(
                    f"Failed to send notification to chat {chat_id}: {e}"
                )

        if success_count > 0:
            logger.info(f"Notification sent to {success_count} chat(s)")
            return True
        return False

    async def notify_buy_order(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_amount: float,
        prices: List[float],
        volumes: List[float],
        market_type: str = "암호화폐",
    ) -> bool:
        """Send buy order notification."""
        if not self._enabled:
            return False

        try:
            message = self._format_buy_notification(
                symbol, korean_name, order_count, total_amount,
                prices, volumes, market_type
            )
            return await self._send_to_telegram(message)
        except Exception as e:
            logger.error(f"Failed to send buy notification: {e}")
            return False

    async def notify_sell_order(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_volume: float,
        prices: List[float],
        volumes: List[float],
        expected_amount: float,
        market_type: str = "암호화폐",
    ) -> bool:
        """Send sell order notification."""
        if not self._enabled:
            return False

        try:
            message = self._format_sell_notification(
                symbol, korean_name, order_count, total_volume,
                prices, volumes, expected_amount, market_type
            )
            return await self._send_to_telegram(message)
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
        reasons: List[str],
        market_type: str = "암호화폐",
    ) -> bool:
        """Send AI analysis completion notification."""
        if not self._enabled:
            return False

        try:
            message = self._format_analysis_notification(
                symbol, korean_name, decision, confidence,
                reasons, market_type
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
    ) -> str:
        """
        Format trade failure notification.

        Args:
            symbol: Trading symbol
            korean_name: Korean name of asset
            reason: Failure reason
            market_type: Type of market

        Returns:
            Markdown-formatted notification message
        """
        timestamp = format_datetime()

        parts = [
            "⚠️ *거래 실패 알림*",
            f"🕒 {timestamp}",
            "",
            f"*종목:* {korean_name} ({symbol})",
            f"*시장:* {market_type}",
            f"*사유:* {reason}",
        ]

        return "\n".join(parts)

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
            message = self._format_failure_notification(
                symbol, korean_name, reason, market_type
            )
            return await self._send_to_telegram(message)
        except Exception as e:
            logger.error(f"Failed to send failure notification: {e}")
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
