from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.schemas.execution_ledger import ExecutionLedgerRead
from app.services.execution_ledger import query_service
from app.services.execution_ledger.query_service import ExecutionLedgerQueryService

pytestmark = pytest.mark.asyncio


def _item(
    symbol: str, instrument_type: str, raw_symbol: str | None = None
) -> ExecutionLedgerRead:
    return ExecutionLedgerRead(
        id=None,
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type=instrument_type,
        symbol=symbol,
        raw_symbol=raw_symbol or symbol,
        side="sell",
        broker_order_id="o1",
        fill_seq=1,
        filled_qty=Decimal("1"),
        filled_price=Decimal("1"),
        filled_notional=Decimal("1"),
        filled_at=datetime(2026, 6, 1, tzinfo=UTC),
        currency="KRW",
        source="reconciler",
    )


async def test_attach_symbol_names_resolves_per_market(monkeypatch) -> None:
    async def fake_kr(symbols, db):
        return {"035420": "NAVER"}

    async def fake_us(symbols, db):
        return {"TSLA": "테슬라"}

    async def fake_crypto(markets, db):
        return {"KRW-BTC": {"korean_name": "비트코인", "english_name": "Bitcoin"}}

    monkeypatch.setattr(query_service, "get_kr_names_by_symbols", fake_kr)
    monkeypatch.setattr(query_service, "get_us_names_by_symbols", fake_us)
    monkeypatch.setattr(query_service, "get_upbit_market_display_names", fake_crypto)

    svc = ExecutionLedgerQueryService(db=object())  # db unused (resolvers faked)
    items = [
        _item("035420", "equity_kr"),
        _item("TSLA", "equity_us"),
        _item("BTC", "crypto", raw_symbol="KRW-BTC"),
        _item("999999", "equity_kr"),  # unresolved -> stays None
    ]
    out = await svc._attach_symbol_names(items)

    by_symbol = {i.symbol: i.symbol_name for i in out}
    assert by_symbol["035420"] == "NAVER"
    assert by_symbol["TSLA"] == "테슬라"
    assert by_symbol["BTC"] == "비트코인"
    assert by_symbol["999999"] is None


async def test_attach_symbol_names_fails_open_on_resolver_error(monkeypatch) -> None:
    async def boom(symbols, db):
        raise RuntimeError("universe empty")

    monkeypatch.setattr(query_service, "get_kr_names_by_symbols", boom)

    svc = ExecutionLedgerQueryService(db=object())
    out = await svc._attach_symbol_names([_item("035420", "equity_kr")])

    assert out[0].symbol_name is None  # never breaks the endpoint
