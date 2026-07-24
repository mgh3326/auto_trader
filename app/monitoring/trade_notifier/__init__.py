"""Trade notification system with Telegram and Discord integration."""

import httpx  # noqa: F401 — needed for backward-compatible test patching

from app.telegram_contract import TelegramMethodResult

from .notifier import TradeNotifier, get_trade_notifier
from .types import DiscordEmbed, DiscordField

__all__ = [
    "DiscordEmbed",
    "DiscordField",
    "TelegramMethodResult",
    "TradeNotifier",
    "get_trade_notifier",
]
