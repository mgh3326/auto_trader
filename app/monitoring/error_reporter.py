"""
Telegram error reporting for critical errors.

Features:
- Global exception handler integration
- ERROR/CRITICAL level filtering
- Duplicate error prevention (5-minute window)
- Rich error context (timestamp, type, message, stack trace, request info)
"""

import hashlib
import logging
import traceback
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)


class TelegramErrorReporter:
    """Reports critical errors to Telegram with deduplication."""

    def __init__(
        self,
        bot_token: str,
        chat_ids: list[str],
        enabled: bool = True,
        dedup_window_minutes: int = 5,
        min_level: int = logging.ERROR,
    ):
        """
        Initialize Telegram error reporter.

        Args:
            bot_token: Telegram bot token
            chat_ids: List of chat IDs to send errors to
            enabled: Whether error reporting is enabled
            dedup_window_minutes: Time window for deduplication (default: 5 minutes)
            min_level: Minimum logging level to report (default: ERROR)
        """
        self.bot_token = bot_token
        self.chat_ids = chat_ids
        self.enabled = enabled
        self.dedup_window_minutes = dedup_window_minutes
        self.min_level = min_level

        # Deduplication tracking: {error_hash: last_sent_timestamp}
        self._sent_errors: Dict[str, datetime] = {}
        self._http_client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> None:
        """Initialize HTTP client."""
        if not self.enabled:
            logger.info("Telegram error reporting is disabled")
            return

        if not self.bot_token or not self.chat_ids:
            logger.warning(
                "Telegram bot token or chat IDs not configured, "
                "error reporting disabled"
            )
            self.enabled = False
            return

        self._http_client = httpx.AsyncClient(timeout=10.0)
        logger.info(
            f"Telegram error reporter initialized "
            f"(chat_ids: {len(self.chat_ids)}, dedup: {self.dedup_window_minutes}m)"
        )

    async def shutdown(self) -> None:
        """Shutdown HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def _compute_error_hash(
        self, error_type: str, error_message: str, stack_trace: str
    ) -> str:
        """
        Compute hash for error deduplication.

        Hash is based on error type, message (first 200 chars), and
        the first stack frame to avoid minor variations.
        """
        # Get first stack frame (most specific location)
        first_frame = ""
        for line in stack_trace.split("\n"):
            if line.strip().startswith("File "):
                first_frame = line.strip()
                break

        # Create hash from error signature
        signature = f"{error_type}:{error_message[:200]}:{first_frame}"
        return hashlib.md5(signature.encode()).hexdigest()

    def _should_send_error(self, error_hash: str) -> bool:
        """
        Check if error should be sent based on deduplication window.

        Returns:
            True if error should be sent, False if it's a duplicate
        """
        now = datetime.utcnow()

        # Clean up old entries
        expired_hashes = [
            h
            for h, ts in self._sent_errors.items()
            if now - ts > timedelta(minutes=self.dedup_window_minutes)
        ]
        for h in expired_hashes:
            del self._sent_errors[h]

        # Check if we've sent this error recently
        if error_hash in self._sent_errors:
            return False

        # Mark error as sent
        self._sent_errors[error_hash] = now
        return True

    def _format_error_message(
        self,
        error_type: str,
        error_message: str,
        stack_trace: str,
        request_info: Optional[Dict] = None,
    ) -> str:
        """
        Format error message for Telegram.

        Args:
            error_type: Type of error
            error_message: Error message
            stack_trace: Full stack trace
            request_info: Optional request context

        Returns:
            Formatted message (max 4096 chars for Telegram)
        """
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        # Build message
        parts = [
            "🚨 <b>Error Alert</b>",
            f"⏰ {timestamp}",
            "",
            f"<b>Type:</b> <code>{error_type}</code>",
            f"<b>Message:</b> {error_message}",
        ]

        # Add request info if available
        if request_info:
            parts.append("")
            parts.append("<b>Request Info:</b>")
            if "method" in request_info:
                parts.append(f"  Method: {request_info['method']}")
            if "url" in request_info:
                parts.append(f"  URL: {request_info['url']}")
            if "client" in request_info:
                parts.append(f"  Client: {request_info['client']}")
            if "user_agent" in request_info:
                parts.append(f"  User-Agent: {request_info['user_agent']}")

        # Add stack trace (truncated if necessary)
        parts.append("")
        parts.append("<b>Stack Trace:</b>")
        parts.append(f"<pre>{stack_trace}</pre>")

        message = "\n".join(parts)

        # Telegram message limit is 4096 characters
        if len(message) > 4000:
            message = message[:3900] + "\n\n... (truncated)"

        return message

    async def report_error(
        self,
        error: Exception,
        level: int = logging.ERROR,
        request: Optional[Request] = None,
        additional_context: Optional[Dict] = None,
    ) -> None:
        """
        Report an error to Telegram.

        Args:
            error: Exception to report
            level: Logging level (default: ERROR)
            request: Optional FastAPI request for context
            additional_context: Optional additional context dict
        """
        if not self.enabled or not self._http_client:
            return

        # Check level threshold
        if level < self.min_level:
            return

        try:
            # Extract error information
            error_type = type(error).__name__
            error_message = str(error)
            stack_trace = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )

            # Check deduplication
            error_hash = self._compute_error_hash(
                error_type, error_message, stack_trace
            )
            if not self._should_send_error(error_hash):
                logger.debug(
                    f"Skipping duplicate error: {error_type} "
                    f"(hash: {error_hash[:8]}...)"
                )
                return

            # Extract request info
            request_info = None
            if request:
                request_info = {
                    "method": request.method,
                    "url": str(request.url),
                    "client": f"{request.client.host}:{request.client.port}"
                    if request.client
                    else "unknown",
                    "user_agent": request.headers.get("user-agent", "unknown"),
                }
                if additional_context:
                    request_info.update(additional_context)

            # Format message
            message = self._format_error_message(
                error_type, error_message, stack_trace, request_info
            )

            # Send to all chat IDs
            await self._send_to_telegram(message)

            logger.info(f"Error reported to Telegram: {error_type}")

        except Exception as e:
            # Don't let error reporting break the application
            logger.error(f"Failed to report error to Telegram: {e}", exc_info=True)

    async def _send_to_telegram(self, message: str) -> None:
        """
        Send message to all configured Telegram chat IDs.

        Args:
            message: Message to send (HTML formatted)
        """
        if not self._http_client:
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        for chat_id in self.chat_ids:
            try:
                response = await self._http_client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
                logger.debug(f"Error message sent to Telegram chat {chat_id}")

            except httpx.HTTPError as e:
                logger.warning(
                    f"Failed to send error to Telegram chat {chat_id}: {e}",
                    exc_info=True,
                )

    async def test_connection(self) -> bool:
        """
        Test Telegram connection by sending a test message.

        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self._http_client:
            logger.warning("Telegram error reporter is not enabled")
            return False

        try:
            test_message = (
                "✅ <b>Telegram Error Reporter Test</b>\n\n"
                f"Connection successful at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                "Error reporting is working correctly."
            )

            await self._send_to_telegram(test_message)
            logger.info("Telegram connection test successful")
            return True

        except Exception as e:
            logger.error(f"Telegram connection test failed: {e}", exc_info=True)
            return False


# Global error reporter instance
_error_reporter: Optional[TelegramErrorReporter] = None


def get_error_reporter() -> Optional[TelegramErrorReporter]:
    """Get the global error reporter instance."""
    return _error_reporter


def set_error_reporter(reporter: TelegramErrorReporter) -> None:
    """Set the global error reporter instance."""
    global _error_reporter
    _error_reporter = reporter
