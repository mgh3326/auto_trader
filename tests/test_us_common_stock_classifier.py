from __future__ import annotations

import pytest

from app.services.us_common_stock_classifier import (
    parse_common_stock_flags,
    sync_us_common_stock_flags,
)


NASDAQ_SAMPLE = "\n".join(
    [
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares",
        "AAPL|Apple Inc. Common Stock|Q|N|N|100|N|N",
        "QQQM|Invesco NASDAQ 100 ETF|G|N|N|100|Y|N",
        "ZZTEST|Test Issue Inc. Common Stock|Q|Y|N|100|N|N",
        "File Creation Time: 0512202621:30|||||||",
    ]
)

OTHER_SAMPLE = "\n".join(
    [
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol",
        "BRK.B|Berkshire Hathaway Inc. Class B Common Stock|N|BRK.B|N|100|N|BRK-B",
        "ABC.WS|ABC Corp Warrants|A|ABC.WS|N|100|N|ABC+",
        "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY",
        "File Creation Time: 0512202621:30|||||||",
    ]
)


def test_parse_common_stock_flags_filters_etfs_warrants_tests_and_normalizes() -> None:
    flags = parse_common_stock_flags(NASDAQ_SAMPLE, OTHER_SAMPLE)

    assert flags["AAPL"] is True
    assert flags["QQQM"] is False
    assert flags["ZZTEST"] is False
    assert flags["BRK-B"] is True
    assert flags["ABC-WS"] is False
    assert flags["SPY"] is False


@pytest.mark.asyncio
async def test_sync_us_common_stock_flags_dry_run_does_not_commit(db_session) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.us_symbol_universe import USSymbolUniverse

    stmt = (
        pg_insert(USSymbolUniverse)
        .values(symbol="ROB204DRY", exchange="NASDAQ", name_en="ROB Dry", is_active=True)
        .on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                "exchange": "NASDAQ",
                "name_en": "ROB Dry",
                "is_active": True,
                "is_common_stock": None,
            },
        )
    )
    await db_session.execute(stmt)
    await db_session.commit()

    result = await sync_us_common_stock_flags(
        session=db_session,
        flags={"ROB204DRY": True},
        commit=False,
    )

    assert result.committed is False
    assert result.changed >= 1
    row = await db_session.get(USSymbolUniverse, "ROB204DRY")
    assert row is not None
    assert row.is_common_stock is None


@pytest.mark.asyncio
async def test_sync_us_common_stock_flags_commit_updates_known_active_rows(db_session) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.us_symbol_universe import USSymbolUniverse

    stmt = (
        pg_insert(USSymbolUniverse)
        .values(symbol="ROB204CMT", exchange="NASDAQ", name_en="ROB Commit", is_active=True)
        .on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                "exchange": "NASDAQ",
                "name_en": "ROB Commit",
                "is_active": True,
                "is_common_stock": None,
            },
        )
    )
    await db_session.execute(stmt)
    await db_session.commit()

    result = await sync_us_common_stock_flags(
        session=db_session,
        flags={"ROB204CMT": True},
        commit=True,
    )

    assert result.committed is True
    row = await db_session.get(USSymbolUniverse, "ROB204CMT")
    assert row is not None
    assert row.is_common_stock is True
