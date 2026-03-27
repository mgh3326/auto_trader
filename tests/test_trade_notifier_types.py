# tests/test_trade_notifier_types.py
"""Tests for trade notifier shared types and constants."""

import pytest

from app.monitoring.trade_notifier.types import (
    COLORS,
    DECISION_EMOJI,
    DECISION_TEXT,
    DiscordEmbed,
    DiscordField,
)


@pytest.mark.unit
class TestColors:
    def test_buy_color_is_green(self):
        assert COLORS["buy"] == 0x00FF00

    def test_sell_color_is_red(self):
        assert COLORS["sell"] == 0xFF0000

    def test_cancel_color_is_yellow(self):
        assert COLORS["cancel"] == 0xFFFF00

    def test_analysis_color_is_blue(self):
        assert COLORS["analysis"] == 0x0000FF

    def test_summary_color_is_cyan(self):
        assert COLORS["summary"] == 0x00FFFF

    def test_failure_color_is_orange(self):
        assert COLORS["failure"] == 0xFF6600


@pytest.mark.unit
class TestDecisionEmoji:
    def test_buy_emoji(self):
        assert DECISION_EMOJI["buy"] == "\U0001f7e2"  # green circle

    def test_hold_emoji(self):
        assert DECISION_EMOJI["hold"] == "\U0001f7e1"  # yellow circle

    def test_sell_emoji(self):
        assert DECISION_EMOJI["sell"] == "\U0001f534"  # red circle

    def test_unknown_returns_default(self):
        assert DECISION_EMOJI.get("unknown", "\u26aa") == "\u26aa"


@pytest.mark.unit
class TestDecisionText:
    def test_buy_text(self):
        assert DECISION_TEXT["buy"] == "\ub9e4\uc218"

    def test_hold_text(self):
        assert DECISION_TEXT["hold"] == "\ubcf4\uc720"

    def test_sell_text(self):
        assert DECISION_TEXT["sell"] == "\ub9e4\ub3c4"


@pytest.mark.unit
class TestTypedDicts:
    def test_discord_field_creation(self):
        field: DiscordField = {"name": "\uc885\ubaa9", "value": "\uc0bc\uc131\uc804\uc790", "inline": True}
        assert field["name"] == "\uc885\ubaa9"

    def test_discord_embed_creation(self):
        embed: DiscordEmbed = {
            "title": "test",
            "description": "desc",
            "color": 0x00FF00,
            "fields": [],
        }
        assert embed["color"] == 0x00FF00
