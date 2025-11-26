"""
Telegram error reporting with duplicate prevention using Redis.

Features:
- Singleton pattern for ErrorReporter
- Redis-based duplicate error filtering
- Rich error formatting with markdown
- Rate limiting per error type
"""

import hashlib
import logging
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

import httpx

# KST (한국 표준시, UTC+9)
KST = timezone(timedelta(hours=9))
from fastapi import Request
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class ErrorReporter:
    """
    Singleton error reporter with Telegram integration and Redis-based deduplication.
    """

    _instance: Optional["ErrorReporter"] = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize ErrorReporter (only once due to singleton pattern)."""
        if not self._initialized:
            self._bot_token: Optional[str] = None
            self._chat_id: Optional[str] = None
            self._enabled: bool = False
            self._duplicate_window: int = 300  # 5 minutes
            self._redis: Optional[Redis] = None
            self._http_client: Optional[httpx.AsyncClient] = None
            ErrorReporter._initialized = True

    def configure(
        self,
        bot_token: str,
        chat_id: str,
        redis_client: Redis,
        enabled: bool = True,
        duplicate_window: int = 300,
    ) -> None:
        """
        Configure the error reporter.

        Args:
            bot_token: Telegram bot token
            chat_id: Telegram chat ID to send errors to
            redis_client: Redis client for duplicate detection
            enabled: Whether error reporting is enabled
            duplicate_window: Time window in seconds for duplicate detection (default: 300)
        """
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._redis = redis_client
        self._enabled = enabled
        self._duplicate_window = duplicate_window

        if enabled and not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=10.0)
            logger.info(
                f"ErrorReporter configured: chat_id={chat_id}, "
                f"duplicate_window={duplicate_window}s"
            )

    async def shutdown(self) -> None:
        """Shutdown HTTP client and Redis connection."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
            logger.debug("HTTP client closed")

        if self._redis:
            await self._redis.aclose()
            self._redis = None
            logger.debug("Redis connection closed")

        logger.info("ErrorReporter shutdown complete")

    def _generate_rate_limit_key(
        self, error_type: str, error_message: str, stack_trace: str
    ) -> str:
        """
        Generate Redis key for rate limiting duplicate errors.

        Uses error type, first 200 chars of message, and first stack frame
        to create a unique identifier for the error.

        Args:
            error_type: Type of the error (e.g., "ValueError")
            error_message: Error message
            stack_trace: Full stack trace

        Returns:
            Redis key for rate limiting
        """
        # Extract first stack frame for better deduplication
        first_frame = ""
        for line in stack_trace.split("\n"):
            if line.strip().startswith("File "):
                first_frame = line.strip()
                break

        # Create unique signature
        signature = f"{error_type}:{error_message[:200]}:{first_frame}"
        error_hash = hashlib.sha256(signature.encode()).hexdigest()

        return f"error_rate_limit:{error_hash}"

    async def _should_send_error(self, rate_limit_key: str) -> bool:
        """
        Check if error should be sent based on rate limiting.

        Args:
            rate_limit_key: Redis key for rate limiting

        Returns:
            True if error should be sent, False if it's a duplicate
        """
        if not self._redis:
            # If Redis is not available, always send
            return True

        try:
            # Check if key exists
            exists = await self._redis.exists(rate_limit_key)

            if exists:
                logger.debug(f"Duplicate error detected: {rate_limit_key}")
                return False

            # Set key with expiration
            await self._redis.setex(
                rate_limit_key, self._duplicate_window, "1"
            )
            return True

        except Exception as e:
            logger.warning(f"Failed to check rate limit in Redis: {e}")
            # On Redis error, send the error to be safe
            return True

    def _format_error_message(
        self,
        error_type: str,
        error_message: str,
        stack_trace: str,
        request_info: Optional[Dict] = None,
        additional_context: Optional[Dict] = None,
    ) -> str:
        """
        Format error message in markdown for Telegram.

        Args:
            error_type: Type of error
            error_message: Error message
            stack_trace: Full stack trace
            request_info: Optional request context
            additional_context: Optional additional context (e.g., request_id, duration_ms)

        Returns:
            Markdown-formatted error message
        """
        timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")

        # Build message parts
        parts = [
            "🚨 *Error Alert*",
            f"🕒 {timestamp}",
            "",
            f"*Type:* `{error_type}`",
            f"*Message:* {error_message}",
        ]

        # Add request info if available
        if request_info:
            parts.append("")
            parts.append("*Request Info:*")
            if "method" in request_info:
                parts.append(f"  • Method: `{request_info['method']}`")
            if "url" in request_info:
                parts.append(f"  • URL: `{request_info['url']}`")
            if "client" in request_info:
                parts.append(f"  • Client: `{request_info['client']}`")
            if "user_agent" in request_info:
                user_agent = request_info["user_agent"][:100]  # Truncate
                parts.append(f"  • User-Agent: `{user_agent}`")

        # Add additional context if available
        if additional_context:
            parts.append("")
            parts.append("*Additional Context:*")
            for key, value in additional_context.items():
                # Format specific keys nicely
                if key == "request_id":
                    parts.append(f"  • Request ID: `{value}`")
                elif key == "duration_ms":
                    parts.append(f"  • Duration: `{value:.2f}ms`")
                else:
                    # Generic key-value pair
                    parts.append(f"  • {key}: `{value}`")

        # Add stack trace (truncated if too long)
        parts.append("")
        parts.append("*Stack Trace:*")
        parts.append("```")

        # Telegram message limit is 4096 characters
        # Reserve ~1000 chars for metadata, use rest for stack trace
        max_trace_length = 3000
        if len(stack_trace) > max_trace_length:
            stack_trace = stack_trace[:max_trace_length] + "\n... (truncated)"

        parts.append(stack_trace)
        parts.append("```")

        message = "\n".join(parts)

        # Final check for Telegram limit
        if len(message) > 4000:
            message = message[:3900] + "\n\n... (truncated)"

        return message

    async def send_error_to_telegram(
        self,
        error: Exception,
        request: Optional[Request] = None,
        additional_context: Optional[Dict] = None,
    ) -> bool:
        """
        Send error to Telegram with duplicate prevention.

        Args:
            error: Exception to report
            request: Optional FastAPI request for context
            additional_context: Optional additional context dict

        Returns:
            True if error was sent, False if it was filtered or failed
        """
        if not self._enabled or not self._http_client or not self._bot_token:
            return False

        try:
            # Extract error information
            error_type = type(error).__name__
            error_message = str(error)
            stack_trace = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )

            # Check rate limiting
            rate_limit_key = self._generate_rate_limit_key(
                error_type, error_message, stack_trace
            )

            if not await self._should_send_error(rate_limit_key):
                logger.debug(f"Skipping duplicate error: {error_type}")
                return False

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

            # Format message with both request_info and additional_context
            message = self._format_error_message(
                error_type, error_message, stack_trace, request_info, additional_context
            )

            # Send to Telegram
            url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
            response = await self._http_client.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()

            logger.info(f"Error reported to Telegram: {error_type}")
            return True

        except Exception as e:
            # Don't let error reporting break the application
            logger.error(f"Failed to send error to Telegram: {e}", exc_info=True)
            return False

    async def test_connection(self) -> bool:
        """
        Test Telegram connection by sending a test message.

        Returns:
            True if successful, False otherwise
        """
        if not self._enabled or not self._http_client or not self._bot_token:
            logger.warning("ErrorReporter is not configured")
            return False

        try:
            test_message = (
                "✅ *Telegram Error Reporter Test*\n\n"
                f"Connection successful at {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}\n"
                "Error reporting is working correctly."
            )

            url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
            response = await self._http_client.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": test_message,
                    "parse_mode": "Markdown",
                },
            )
            response.raise_for_status()

            logger.info("Telegram connection test successful")
            return True

        except Exception as e:
            logger.error(f"Telegram connection test failed: {e}", exc_info=True)
            return False


# Singleton instance getter
def get_error_reporter() -> ErrorReporter:
    """
    Get the singleton ErrorReporter instance.

    Returns:
        ErrorReporter instance
    """
    return ErrorReporter()
