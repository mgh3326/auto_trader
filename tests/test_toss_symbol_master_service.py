from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.us_symbol_universe import USSymbolUniverse
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
                    is_common_share=symbol not in {"005935", "GOOGL"},
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
    # ROB-546: gap-fill skips toss market_cap when another source already covers
    # the key; clear prior valuation rows so this asserts the no-other-source path
    # deterministically across repeated local runs (db_session does not truncate).
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol == "005930",
            MarketValuationSnapshot.snapshot_date == dt.date(2026, 6, 12),
        )
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


@pytest.mark.asyncio
async def test_sync_toss_symbol_master_commit_updates_us_common_stock_flag(
    db_session,
) -> None:
    import sqlalchemy as sa

    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol == "AAPL")
    )
    db_session.add(
        USSymbolUniverse(
            symbol="AAPL",
            exchange="NASDAQ",
            name_en="Apple Inc.",
            is_active=True,
            is_common_stock=None,
        )
    )
    await db_session.commit()

    result = await sync_toss_symbol_master(
        db_session,
        client=FakeTossClient(),
        request=TossSymbolMasterSyncRequest(
            market="us",
            symbols=("AAPL",),
            commit=True,
            snapshot_date=dt.date(2026, 6, 12),
        ),
    )

    row = await db_session.get(USSymbolUniverse, "AAPL")
    assert row.security_type == "STOCK"
    assert row.is_common_share is True
    assert row.is_common_stock is True
    assert row.shares_outstanding == Decimal("14687356000")
    assert result.market_cap_payloads == 1


@pytest.mark.asyncio
async def test_sync_toss_symbol_master_batches_symbols_by_200(db_session) -> None:
    import sqlalchemy as sa

    symbols = tuple(f"{idx:06d}" for idx in range(201))
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(symbols))
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=symbol,
                name=f"테스트{symbol}",
                exchange="KOSPI",
                is_active=True,
            )
            for symbol in symbols
        ]
    )
    await db_session.commit()
    client = FakeTossClient()

    result = await sync_toss_symbol_master(
        db_session,
        client=client,
        request=TossSymbolMasterSyncRequest(
            market="kr",
            symbols=symbols,
            commit=False,
            snapshot_date=dt.date(2026, 6, 12),
        ),
    )

    assert result.batches == 2
    assert [len(batch) for batch in client.stock_batches] == [200, 1]
    assert [len(batch) for batch in client.price_batches] == [200, 1]


@pytest.mark.asyncio
async def test_sync_toss_symbol_master_skips_market_cap_for_unregistered_symbol(
    db_session,
) -> None:
    import sqlalchemy as sa

    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == "999999")
    )
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol == "999999"
        )
    )
    await db_session.commit()

    result = await sync_toss_symbol_master(
        db_session,
        client=FakeTossClient(),
        request=TossSymbolMasterSyncRequest(
            market="kr",
            symbols=("999999",),
            commit=True,
            snapshot_date=dt.date(2026, 6, 12),
        ),
    )

    rows = (
        (
            await db_session.execute(
                sa.select(MarketValuationSnapshot).where(
                    MarketValuationSnapshot.symbol == "999999"
                )
            )
        )
        .scalars()
        .all()
    )
    assert result.stocks_matched == 1
    assert result.market_cap_payloads == 0
    assert rows == []


@pytest.mark.asyncio
async def test_sync_toss_skips_market_cap_when_other_source_exists(
    db_session,
) -> None:
    """ROB-546 gap-fill: a toss market_cap row must NOT be written when a
    naver/yahoo row already exists for the same (market, symbol, date)."""
    import sqlalchemy as sa

    snapshot_date = dt.date(2026, 6, 12)
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == "005930")
    )
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol == "005930",
            MarketValuationSnapshot.snapshot_date == snapshot_date,
        )
    )
    db_session.add(
        KRSymbolUniverse(
            symbol="005930", name="삼성전자", exchange="KOSPI", is_active=True
        )
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="005930",
            snapshot_date=snapshot_date,
            source="naver_finance",
            per=Decimal("11"),
            pbr=Decimal("1.2"),
            roe=Decimal("0.15"),
            dividend_yield=Decimal("0.02"),
            market_cap=Decimal("1000000"),
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
            snapshot_date=snapshot_date,
        ),
    )

    # master fields still updated even though market_cap row is skipped
    row = await db_session.get(KRSymbolUniverse, "005930")
    assert row.shares_outstanding == Decimal("5846278608")

    assert result.market_cap_payloads == 0
    assert result.market_cap_skipped_existing == 1

    toss_rows = (
        (
            await db_session.execute(
                sa.select(MarketValuationSnapshot).where(
                    MarketValuationSnapshot.symbol == "005930",
                    MarketValuationSnapshot.snapshot_date == snapshot_date,
                    MarketValuationSnapshot.source == "toss_openapi",
                )
            )
        )
        .scalars()
        .all()
    )
    assert toss_rows == []


@pytest.mark.asyncio
async def test_sync_toss_preserves_existing_us_common_stock(db_session) -> None:
    """ROB-546 minor: Toss must not flip an already-classified NASDAQ-Trader
    is_common_stock value; it only fills when currently NULL."""
    import sqlalchemy as sa

    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol == "GOOGL")
    )
    db_session.add(
        USSymbolUniverse(
            symbol="GOOGL",
            exchange="NASDAQ",
            name_en="Alphabet Inc.",
            is_active=True,
            is_common_stock=True,  # NASDAQ-Trader classified
        )
    )
    await db_session.commit()

    result = await sync_toss_symbol_master(
        db_session,
        client=FakeTossClient(),
        request=TossSymbolMasterSyncRequest(
            market="us",
            symbols=("GOOGL",),
            commit=True,
            snapshot_date=dt.date(2026, 6, 12),
        ),
    )

    row = await db_session.get(USSymbolUniverse, "GOOGL")
    # Toss reports is_common_share=False for GOOGL, but the existing TRUE is preserved
    assert row.is_common_share is False
    assert row.is_common_stock is True
    # count guard surfaced as a warning
    assert any("is_common_stock" in w for w in result.warnings)
