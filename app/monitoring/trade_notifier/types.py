# app/monitoring/trade_notifier/types.py
"""Shared types and constants for trade notifications."""

from __future__ import annotations

from typing import TypedDict


class DiscordField(TypedDict):
    name: str
    value: str
    inline: bool


class DiscordEmbed(TypedDict):
    title: str
    description: str
    color: int
    fields: list[DiscordField]


# Color constants used by formatters
COLORS: dict[str, int] = {
    "buy": 0x00FF00,
    "sell": 0xFF0000,
    "cancel": 0xFFFF00,
    "analysis": 0x0000FF,
    "summary": 0x00FFFF,
    "failure": 0xFF6600,
    "hold": 0xFFFF00,
    "default": 0x0000FF,
}

# Decision -> emoji mapping
DECISION_EMOJI: dict[str, str] = {
    "buy": "\U0001f7e2",   # green circle
    "hold": "\U0001f7e1",  # yellow circle
    "sell": "\U0001f534",  # red circle
}

# Decision -> Korean text
DECISION_TEXT: dict[str, str] = {
    "buy": "\ub9e4\uc218",
    "hold": "\ubcf4\uc720",
    "sell": "\ub9e4\ub3c4",
}
