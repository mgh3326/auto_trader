from __future__ import annotations

import datetime as dt

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.kr_symbol_universe_service import (
    get_kr_nxt_tradability,
    search_kr_symbols,
)

_KST = dt.timezone(dt.timedelta(hours=9))


@pytest.mark.asyncio
async def test_accessor_returns_tradability(db_session):
    db_session.add(
        KRSymbolUniverse(
            symbol="777001",
            name="엔엑스티가능",
            exchange="KOSPI",
            is_active=True,
            nxt_eligible=True,
            nxt_trading_suspended=False,
            toss_master_updated_at=dt.datetime(2026, 7, 3, 6, 0, tzinfo=_KST),
        )
    )
    db_session.add(
        KRSymbolUniverse(
            symbol="777002",
            name="엔엑스티불가",
            exchange="KOSDAQ",
            is_active=True,
            nxt_eligible=False,
            nxt_trading_suspended=None,
        )
    )
    await db_session.flush()

    result = await get_kr_nxt_tradability(["777001", "777002", "777999"], db=db_session)
    assert result["777001"].nxt_tradable is True
    assert result["777001"].source == "kr_symbol_universe"
    assert result["777001"].asof is not None
    assert result["777002"].nxt_tradable is False
    assert "777999" not in result  # missing symbol omitted
    await db_session.rollback()


@pytest.mark.asyncio
async def test_search_rows_carry_nxt_fields(db_session):
    db_session.add(
        KRSymbolUniverse(
            symbol="777003",
            name="검색엔엑스티",
            exchange="KOSPI",
            is_active=True,
            nxt_eligible=True,
            nxt_trading_suspended=False,
            toss_master_updated_at=dt.datetime.now(_KST),
        )
    )
    await db_session.flush()
    rows = await search_kr_symbols("검색엔엑스티", 10, db=db_session)
    assert rows
    row = next(r for r in rows if r["symbol"] == "777003")
    assert row["nxt_tradable"] is True
    assert row["nxt_tradable_source"] == "kr_symbol_universe"
    assert "nxt_tradable_asof" in row
    assert "nxt_tradable_stale" in row
    await db_session.rollback()
