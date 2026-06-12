from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.brokers.toss.dto import TossPrice, TossStockInfo
from app.services.toss_symbol_master_service import (
    TossSymbolMasterSyncRequest,
    sync_toss_symbol_master,
)


class FakeTossClient:
    def __init__(self) -> None:
        self.stock_batches: list[tuple[str, ...]] = []
        self.price_batches: list[tuple[str, ...]] = []

    async def stocks(self, symbols: list[str] | tuple[str, ...]) -> list[TossStockInfo]:
        self.stock_batches.append(tuple(symbols))
        out = []
        for symbol in symbols:
            if symbol == "MISSING":
                continue
            is_kr = symbol.isdigit()
            out.append(
                TossStockInfo(
                    symbol=symbol,
                    name="삼성전자" if is_kr else "Apple",
                    english_name="Samsung Electronics" if is_kr else "Apple Inc.",
                    isin_code="KR7005930003" if is_kr else "US0378331005",
                    market="KR" if is_kr else "US",
                    security_type="STOCK",
                    is_common_share=symbol != "005935",
                    status="ACTIVE",
                    currency="KRW" if is_kr else "USD",
                    list_date="1975-06-11" if is_kr else "1980-12-12",
                    delist_date=None,
                    shares_outstanding=Decimal("5846278608")
                    if is_kr
                    else Decimal("14687356000"),
                    leverage_factor=None,
                    korean_market_detail={
                        "krxTradingSuspended": False,
                        "nxtTradingSuspended": False,
                    }
                    if is_kr
                    else None,
                )
            )
        return out

    async def prices(self, symbols: list[str] | tuple[str, ...]) -> list[TossPrice]:
        self.price_batches.append(tuple(symbols))
        return [
            TossPrice(
                symbol=symbol,
                timestamp="2026-06-12T00:00:00Z",
                last_price=Decimal("70000") if symbol.isdigit() else Decimal("190"),
                currency="KRW" if symbol.isdigit() else "USD",
            )
            for symbol in symbols
            if symbol != "MISSING"
        ]


@pytest.mark.asyncio
async def test_sync_toss_symbol_master_dry_run_does_not_mutate(db_session) -> None:
    import sqlalchemy as sa

    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == "005930")
    )
    db_session.add(
        KRSymbolUniverse(
            symbol="005930", name="삼성전자", exchange="KOSPI", is_active=True
        )
    )
    await db_session.commit()

    result = await sync_toss_symbol_master(
        db_session,
        client=FakeTossClient(),
        request=TossSymbolMasterSyncRequest(
            market="kr", symbols=("005930",), commit=False
        ),
    )

    assert result.commit is False
    assert result.symbols_requested == 1
    assert result.stocks_matched == 1
    row = await db_session.get(KRSymbolUniverse, "005930")
    assert row.shares_outstanding is None


@pytest.mark.asyncio
async def test_sync_toss_symbol_master_commit_updates_master_and_market_cap(
    db_session,
) -> None:
    import sqlalchemy as sa

    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == "005930")
    )
    db_session.add(
        KRSymbolUniverse(
            symbol="005930", name="삼성전자", exchange="KOSPI", is_active=True
        )
    )
    await db_session.commit()

    result = await sync_toss_symbol_master(
        db_session,
        client=FakeTossClient(),
        request=TossSymbolMasterSyncRequest(
            market="kr",
            symbols=("005930",),
            commit=True,
            snapshot_date=dt.date(2026, 6, 12),
        ),
    )

    row = await db_session.get(KRSymbolUniverse, "005930")
    assert row.security_type == "STOCK"
    assert row.is_common_share is True
    assert row.shares_outstanding == Decimal("5846278608")
    assert row.krx_trading_suspended is False
    assert result.market_cap_payloads == 1
    assert result.market_cap_nonnull == 1
