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
            response = await http_client.post(url, json=payload)
            response.raise_for_status()
            any_success = True
            logger.info(f"Telegram message sent to chat {chat_id}")
        except Exception:
            logger.error(f"Failed to send Telegram message to chat {chat_id}")
    return any_success


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
