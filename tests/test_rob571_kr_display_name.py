"""ROB-571: DB-backed KR display-name resolution + title dedup."""

from unittest.mock import AsyncMock, patch

import pytest

from app.monitoring.trade_notifier import TradeNotifier
from app.monitoring.trade_notifier.formatters_discord import (
    format_fill_notification,
)
from app.monitoring.trade_notifier.formatters_telegram import (
    format_fill_notification_telegram,
)
from app.services import fill_notification as fn
from app.services.fill_notification import FillOrder, resolve_display_name_db


def _kr_sell(symbol="011200"):
    return FillOrder(
        symbol=symbol,
        side="ask",
        filled_price=21600.0,
        filled_qty=12.0,
        filled_amount=259200.0,
        filled_at="2026-06-15T10:33:38",
        account="kis",
        order_price=21000.0,
        order_id="0016471700",
        market_type="kr",
        currency="KRW",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_display_name_db_kr_uses_universe(monkeypatch):
    async def fake(symbols, db=None):
        return {"011200": "HMM"}

    monkeypatch.setattr(fn, "get_kr_names_by_symbols", fake)
    assert await resolve_display_name_db("kr", "011200") == "HMM"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_display_name_db_kr_failopen_to_symbol(monkeypatch):
    async def boom(symbols, db=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(fn, "get_kr_names_by_symbols", boom)
    # 011200 not in hardcoded KR_SYMBOLS → fail-open returns the code
    assert await resolve_display_name_db("kr", "011200") == "011200"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_display_name_db_kr_missing_name_falls_back(monkeypatch):
    async def empty(symbols, db=None):
        return {}

    monkeypatch.setattr(fn, "get_kr_names_by_symbols", empty)
    assert await resolve_display_name_db("kr", "011200") == "011200"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_display_name_db_us_and_crypto_passthrough():
    assert await resolve_display_name_db("us", "AAPL") == "AAPL"
    assert await resolve_display_name_db("crypto", "KRW-BTC") == "BTC"


@pytest.mark.unit
class TestTitleDedup:
    def test_discord_collapses_when_name_equals_symbol(self):
        emb = format_fill_notification(
            _kr_sell(), display_name="011200", detail_url=None
        )
        assert emb["title"] == "🔴 체결 · 011200"

    def test_discord_keeps_name_and_symbol_when_resolved(self):
        emb = format_fill_notification(_kr_sell(), display_name="HMM", detail_url=None)
        assert emb["title"] == "🔴 체결 · HMM (011200)"

    def test_telegram_collapses_when_equal(self):
        msg = format_fill_notification_telegram(
            _kr_sell(), display_name="011200", detail_url=None
        )
        assert "체결 · 011200*" in msg
        assert "011200 \\(011200\\)" not in msg

    def test_telegram_keeps_when_resolved(self):
        msg = format_fill_notification_telegram(
            _kr_sell(), display_name="HMM", detail_url=None
        )
        assert "HMM \\(011200\\)" in msg


@pytest.fixture
def _notifier():
    TradeNotifier._instance = None
    TradeNotifier._initialized = False
    n = TradeNotifier()
    yield n
    TradeNotifier._instance = None
    TradeNotifier._initialized = False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_fill_uses_db_resolved_name(_notifier, monkeypatch):
    monkeypatch.setattr(
        "app.services.fill_notification.resolve_display_name_db",
        AsyncMock(return_value="HMM"),
    )
    _notifier.configure(
        bot_token="t",
        chat_ids=["1"],
        enabled=True,
        discord_webhook_kr="https://discord.com/api/webhooks/kr",
    )
    with patch.object(
        _notifier, "_send_to_discord_embed_single", new=AsyncMock(return_value=True)
    ) as md:
        ok = await _notifier.notify_fill(_kr_sell())
    assert ok is True
    assert md.await_args.args[0]["title"] == "🔴 체결 · HMM (011200)"
