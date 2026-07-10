# app/monitoring/trade_notifier/transports.py
"""HTTP transport functions for Telegram and Discord delivery."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def send_telegram(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_ids: list[str],
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    """Send a message to multiple Telegram chat IDs.

    Returns True if at least one chat received the message.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    any_success = False
    for chat_id in chat_ids:
        try:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            response = await http_client.post(url, json=payload)
            response.raise_for_status()
            any_success = True
            logger.info("Telegram message sent")
        except Exception:
            logger.error("Failed to send Telegram message")
    return any_success


async def send_telegram_message(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> int | None:
    """Send one Telegram message and return its message ID on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        response = await http_client.post(url, json=payload)
        response.raise_for_status()
        message_id = response.json().get("result", {}).get("message_id")
        if isinstance(message_id, int):
            logger.info("Telegram message sent")
            return message_id
    except Exception:
        logger.error("Failed to send Telegram message")
    return None


async def answer_callback_query(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    callback_query_id: str,
    text: str | None = None,
) -> bool:
    """Acknowledge a Telegram callback query."""
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text

    try:
        response = await http_client.post(url, json=payload)
        response.raise_for_status()
        return True
    except Exception:
        logger.error("Failed to answer Telegram callback query")
        return False


async def edit_message_text(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    message_id: int,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    """Edit one Telegram message."""
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        response = await http_client.post(url, json=payload)
        response.raise_for_status()
        return True
    except Exception:
        logger.error("Failed to edit Telegram message")
        return False


async def send_discord_embed_single(
    *,
    http_client: httpx.AsyncClient,
    webhook_url: str,
    embed: dict[str, Any],
) -> bool:
    """Send a single Discord embed to one webhook URL.

    Returns True on success, False on failure.
    """
    try:
        response = await http_client.post(
            webhook_url,
            json={"embeds": [embed]},
        )
        response.raise_for_status()
        logger.info(f"Discord embed sent to {webhook_url[:50]}...")
        return True
    except Exception:
        logger.error(f"Failed to send Discord embed to {webhook_url[:50]}...")
        return False


async def send_discord_content_single(
    *,
    http_client: httpx.AsyncClient,
    webhook_url: str,
    content: str,
) -> bool:
    """Send plain text content to one Discord webhook URL.

    Returns True on success, False on failure.
    """
    try:
        response = await http_client.post(
            webhook_url,
            json={"content": content},
        )
        response.raise_for_status()
        logger.info(f"Discord content sent to {webhook_url[:50]}...")
        return True
    except Exception:
        logger.error(f"Failed to send Discord content to {webhook_url[:50]}...")
        return False
