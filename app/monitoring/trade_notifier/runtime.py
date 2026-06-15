"""Shared runtime setup for the TradeNotifier singleton."""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.monitoring.trade_notifier import get_trade_notifier

logger = logging.getLogger(__name__)


def _has_discord(settings_obj: Any) -> bool:
    return any(
        [
            getattr(settings_obj, "discord_webhook_us", None),
            getattr(settings_obj, "discord_webhook_kr", None),
            getattr(settings_obj, "discord_webhook_crypto", None),
            getattr(settings_obj, "discord_webhook_alerts", None),
        ]
    )


def _has_telegram(settings_obj: Any) -> bool:
    return bool(
        getattr(settings_obj, "telegram_token", None)
        and getattr(settings_obj, "telegram_chat_id", None)
    )


def configure_trade_notifier_from_settings(
    *, log_context: str = "Trade notifier", settings_obj: Any = settings
) -> bool:
    """Configure the process-local TradeNotifier from application settings."""
    has_discord = _has_discord(settings_obj)
    has_telegram = _has_telegram(settings_obj)

    if not has_discord and not has_telegram:
        logger.info("%s disabled (no Discord or Telegram configured)", log_context)
        return False

    try:
        trade_notifier = get_trade_notifier()
        bot_token = getattr(settings_obj, "telegram_token", None) or ""
        chat_ids = settings_obj.telegram_chat_ids if has_telegram else []

        trade_notifier.configure(
            bot_token=bot_token,
            chat_ids=chat_ids,
            enabled=True,
            discord_webhook_us=getattr(settings_obj, "discord_webhook_us", None),
            discord_webhook_kr=getattr(settings_obj, "discord_webhook_kr", None),
            discord_webhook_crypto=getattr(
                settings_obj, "discord_webhook_crypto", None
            ),
            discord_webhook_alerts=getattr(
                settings_obj, "discord_webhook_alerts", None
            ),
        )

        configured_systems: list[str] = []
        if has_discord:
            webhook_count = sum(
                [
                    bool(getattr(settings_obj, "discord_webhook_us", None)),
                    bool(getattr(settings_obj, "discord_webhook_kr", None)),
                    bool(getattr(settings_obj, "discord_webhook_crypto", None)),
                    bool(getattr(settings_obj, "discord_webhook_alerts", None)),
                ]
            )
            configured_systems.append(f"Discord ({webhook_count} webhook(s))")
        if has_telegram:
            configured_systems.append(
                f"Telegram (chat_id={getattr(settings_obj, 'telegram_chat_id', '')})"
            )

        logger.info("%s initialized: %s", log_context, ", ".join(configured_systems))
        return True
    except Exception as exc:
        logger.error("%s initialization failed: %s", log_context, exc, exc_info=True)
        return False


async def shutdown_trade_notifier(*, log_context: str = "Trade notifier") -> None:
    """Close the process-local TradeNotifier HTTP client."""
    try:
        await get_trade_notifier().shutdown()
        logger.info("%s shutdown complete", log_context)
    except Exception as exc:
        logger.error("%s shutdown failed: %s", log_context, exc, exc_info=True)
