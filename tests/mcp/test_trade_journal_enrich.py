# tests/mcp/test_trade_journal_enrich.py
import pytest

from app.mcp_server.tooling import trade_journal_tools as tjt


def test_enrich_entry_long_target_reached(monkeypatch):
    # long position, current >= target -> target_reached True, stop False
    entry = {"entry_price": 100.0, "target_price": 110.0, "stop_loss": 90.0}
    tjt._apply_live_enrich(entry, current_price=112.0, side="buy")
    assert entry["current_price"] == 112.0
    assert entry["pnl_pct_live"] == pytest.approx(12.0)
    assert entry["target_reached"] is True
    assert entry["stop_reached"] is False


def test_enrich_entry_long_stop_reached(monkeypatch):
    entry = {"entry_price": 100.0, "target_price": 110.0, "stop_loss": 90.0}
    tjt._apply_live_enrich(entry, current_price=88.0, side="buy")
    assert entry["stop_reached"] is True
    assert entry["target_reached"] is False
    assert entry["pnl_pct_live"] == pytest.approx(-12.0)


def test_enrich_entry_short_inverts(monkeypatch):
    # short: target below entry, current <= target -> target_reached
    entry = {"entry_price": 100.0, "target_price": 90.0, "stop_loss": 110.0}
    tjt._apply_live_enrich(entry, current_price=88.0, side="sell")
    assert entry["target_reached"] is True
    assert entry["stop_reached"] is False
    # short pnl positive when price falls
    assert entry["pnl_pct_live"] == pytest.approx(12.0)


def test_enrich_entry_missing_entry_price_leaves_pnl_null():
    entry = {"entry_price": None, "target_price": 110.0, "stop_loss": 90.0}
    tjt._apply_live_enrich(entry, current_price=112.0, side="buy")
    assert entry["current_price"] == 112.0
    assert entry["pnl_pct_live"] is None
    assert entry["target_reached"] is True


@pytest.fixture
def journal_factory(db_session, monkeypatch):
    async def _factory(**kwargs):
        from decimal import Decimal

        from app.models.trade_journal import TradeJournal
        from app.models.trading import InstrumentType

        # Default some fields
        kwargs.setdefault("symbol", "BAC")
        kwargs.setdefault("thesis", "Test thesis")
        kwargs.setdefault("status", "active")
        kwargs.setdefault("account_type", "live")
        kwargs.setdefault("side", "buy")

        # Map type if it's string
        if isinstance(kwargs.get("instrument_type"), str):
            kwargs["instrument_type"] = InstrumentType(kwargs["instrument_type"])

        # Decimalize prices
        for price_field in ("entry_price", "target_price", "stop_loss"):
            if kwargs.get(price_field) is not None:
                kwargs[price_field] = Decimal(str(kwargs[price_field]))

        j = TradeJournal(**kwargs)
        db_session.add(j)
        await db_session.flush()

        # Monkeypatch the session factory in trade_journal_tools
        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_session_cm():
            yield db_session

        monkeypatch.setattr(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            lambda: _fake_session_cm,
        )
        return j

    return _factory


@pytest.mark.asyncio
async def test_get_trade_journal_enrich_live_summary(monkeypatch, journal_factory):
    # journal_factory creates one active US journal: entry 100, target 110, stop 90
    await journal_factory(
        symbol="BAC",
        entry_price=100.0,
        target_price=110.0,
        stop_loss=90.0,
        instrument_type="equity_us",
        side="buy",
    )

    async def _fake_quote(symbol, market):
        from app.services.market_data.contracts import Quote

        return Quote(symbol=symbol, market="equity_us", price=109.5, source="test")

    monkeypatch.setattr("app.services.market_data.service.get_quote", _fake_quote)
    res = await tjt.get_trade_journal(market="us", enrich_live=True)
    assert res["success"] is True
    e = res["entries"][0]
    assert e["current_price"] == 109.5
    assert e["target_reached"] is False
    # within 1.5% of target (109.5 vs 110) -> near_target counted
    assert res["summary"]["near_target"] == 1


@pytest.mark.asyncio
async def test_get_trade_journal_enrich_false_is_unchanged(journal_factory):
    await journal_factory(symbol="BAC", instrument_type="equity_us")
    res = await tjt.get_trade_journal(market="us")  # enrich_live defaults False
    e = res["entries"][0]
    assert e["current_price"] is None
    assert e["pnl_pct_live"] is None
    assert res["summary"]["near_target"] == 0
