# app/core/discord_transport.py
"""Discord webhook transport — a leaf module with no package side effects.

``app.monitoring.trade_notifier.transports`` re-exports these functions for
existing callers, but importing that submodule runs the ``trade_notifier``
package ``__init__``, which pulls the notifier and its DB-backed services.
Lightweight emitters (e.g. ``app.services.autonomous_order_alerts``) import
from here instead so their import closure stays free of the DB layer.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


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
