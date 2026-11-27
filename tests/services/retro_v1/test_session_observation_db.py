"""hk 6421: the ``session_observation`` SELECTs against a real schema (CI only).

The unit tests route a fake session; this runs the same statements on PostgreSQL
inside one rolled-back transaction: research tables from their migrations, the
daily candle tables from their CREATE TABLE statements, the ORM tables from the
test bootstrap. The reader itself is not called here — its ``SET TRANSACTION``
must be the first statement of a transaction the fixture has already opened.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retro_v1 import session_observation as so

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"
MINUTE_STEPS = (
    "20261124_hk6145_session_minutes.py",
    "20261125_hk6322_us_toss_venue.py",
    "20261124_hk6148_futures_minutes.py",
    "20261126_hk6757_kr_auction_revisions.py",
)
TABLES = (
    "research.kr_auction_minute_revisions",
    "research.kr_futures_candles_1m",
    "research.us_candles_1m",
    "research.kr_candles_1m",
    "public.kr_candles_1d",
    "public.us_candles_1d",
)
DAY = date(2031, 3, 4)
PREVIOUS = date(2031, 3, 3)
KR, KR2, US = "Z64211", "Z64212", "ZZHKA"
CONTRACT = "MF200.3103"
T0 = datetime(2031, 3, 4, 0, 0, tzinfo=UTC)  # 09:00 KST
MINUTE = timedelta(minutes=1)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"hk6421_{name[:-3]}", VERSIONS / name
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_table_sql(migration_file: str, table: str) -> str:
    """The real ``CREATE TABLE`` statement of a migration, without timescaledb."""
    migration = _load(migration_file)
    captured: list[str] = []
    migration.op = SimpleNamespace(
        execute=lambda sql: captured.append(str(sql)),
        create_table=lambda *a, **k: None,
        create_index=lambda *a, **k: None,
    )
    migration.upgrade()
    (ddl,) = [s for s in captured if f"CREATE TABLE {table} (" in s]
    return ddl


def _upgrade(sync_conn, name: str) -> None:
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        _load(name).upgrade()


@pytest_asyncio.fixture
async def db():
    from app.core.db import engine
    from tests._run_owned_database import validate_run_owned_database_url

    validate_run_owned_database_url(engine.url)
    async with engine.connect() as connection:
        try:
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS research"))
            for table in TABLES:
                await connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            for migration, table in (
                ("20260803_research_kr_candles.py", "research.kr_candles_1m"),
                ("bad6e17e4115_add_kr_candles_1d.py", "public.kr_candles_1d"),
                ("142b01f2eba0_add_us_candles_1d.py", "public.us_candles_1d"),
            ):
                await connection.execute(text(_create_table_sql(migration, table)))
            for step in MINUTE_STEPS:
                await connection.run_sync(_upgrade, step)
            session = AsyncSession(
                bind=connection, join_transaction_mode="create_savepoint"
            )
            try:
                yield session
            finally:
                await session.close()
        finally:
            await connection.rollback()


async def _kr_bar(
    db: AsyncSession,
    symbol: str,
    minute: int,
    volume: int,
    *,
    venue: str = "KRX",
    segment: str = "KRX_REGULAR",
    source: str = "KIS",
    batch: str = "session-minutes-kr-1",
    lag: int = 50,
) -> None:
    await db.execute(
        text(
            "INSERT INTO research.kr_candles_1m (time_utc, session_date_kst, symbol, "
            "venue, session_segment, source, open, high, low, close, volume, value, "
            "retrieved_at, batch_id) VALUES (:t, :d, :symbol, :venue, :segment, "
            ":source, 1, 1, 1, 1, :volume, NULL, :received, :batch)"
        ),
        {
            "t": T0 + minute * MINUTE,
            "d": DAY,
            "symbol": symbol,
            "venue": venue,
            "segment": segment,
            "source": source,
            "volume": volume,
            "received": T0 + (minute + 1) * MINUTE + timedelta(seconds=lag),
            "batch": batch,
        },
    )


async def test_kr_aggregate_skips_backfills_and_applies_auction_revisions(
    db: AsyncSession,
) -> None:
    await _kr_bar(db, KR, 0, 5)
    await _kr_bar(db, KR, 1, 7, source="TOSS", lag=90)
    await _kr_bar(db, KR, 2, 0, lag=70)
    await _kr_bar(db, KR, 3, 9, batch="session-minutes-kr-BackFill")
    await _kr_bar(db, KR, 4, 9, batch="Promote-1")
    await _kr_bar(db, KR2, 0, 0, venue="NTX", segment="NXT_OVERLAP")
    # A later KIS auction revision turns a stored volume-0 minute into a trade.
    await _kr_bar(db, KR2, 5, 0)
    await db.execute(
        text(
            "INSERT INTO research.kr_auction_minute_revisions (time_utc, symbol, "
            "venue, open, high, low, close, volume, value, revised_at) VALUES "
            "(:t, :symbol, 'KRX', 1, 1, 1, 1, 11, NULL, :t)"
        ),
        {"t": T0 + 5 * MINUTE, "symbol": KR2},
    )

    found = dict(await so.load_session_bars(db, "kr", DAY, [KR, KR2]))

    regular = found[(KR, "KRX", "KRX_REGULAR")]
    assert (regular.bar_count, regular.trade_bar_count) == (3, 2)
    assert regular.last_bar_at == T0 + 2 * MINUTE
    assert regular.last_trade_bar_at == T0 + MINUTE
    assert regular.receive_lag_seconds == 70
    assert regular.last_received_at == T0 + 3 * MINUTE + timedelta(seconds=70)
    assert (regular.last_bar_source, regular.last_trade_source) == ("KIS", "TOSS")
    assert regular.sources == ("KIS", "TOSS")

    zero = found[(KR2, "NTX", "NXT_OVERLAP")]
    assert (zero.bar_count, zero.trade_bar_count) == (1, 0)
    assert zero.last_trade_bar_at is None and zero.last_trade_source is None

    revised = found[(KR2, "KRX", "KRX_REGULAR")]
    assert (revised.trade_bar_count, revised.last_trade_source) == (1, "KIS")
    assert await so.load_session_bars(db, "kr", PREVIOUS, [KR, KR2]) == []


async def test_us_and_futures_aggregates_read_the_stored_session_date(
    db: AsyncSession,
) -> None:
    common = {"t": T0, "d": DAY, "received": T0 + 3 * MINUTE}
    await db.execute(
        text(
            "INSERT INTO research.us_candles_1m (time_utc, session_date, symbol, "
            "venue, session_segment, source, open, high, low, close, volume, value, "
            "retrieved_at, batch_id) VALUES (:t, :d, :symbol, 'TOSS_INTEGRATED', "
            "'US_DAY', 'TOSS', 1, 1, 1, 1, 3, NULL, :received, 'b1')"
        ),
        {**common, "symbol": US},
    )
    await db.execute(
        text(
            "INSERT INTO research.kr_futures_candles_1m (time_utc, symbol, product, "
            "contract_month, roll_state, session_segment, session_date, source, "
            "open, high, low, close, volume, retrieved_at, batch_id) VALUES (:t, "
            ":symbol, 'MF200', '3103', 'front', 'FUT_DAY', :d, 'KIS', 1, 1, 1, 1, 0, "
            ":received, 'b1')"
        ),
        {**common, "symbol": CONTRACT},
    )

    us = dict(await so.load_session_bars(db, "us", DAY, [US]))
    assert us[(US, "TOSS_INTEGRATED", "US_DAY")].receive_lag_seconds == 120
    futures = dict(await so.load_session_bars(db, "futures", DAY, [CONTRACT]))
    assert futures[(CONTRACT, "KRX", "FUT_DAY")].trade_bar_count == 0
    assert await so.load_contracts(db, DAY + timedelta(days=7)) == [CONTRACT]
    assert await so.load_contracts(db, DAY + timedelta(days=8)) == []


async def test_population_universe_and_reference_selects(db: AsyncSession) -> None:
    for rank, order_type, symbol, minute in (
        (2, "up", KR, 0),
        (1, "down", KR2, 5),
        (1, "priceTop", KR, 9),
        (1, "excluded", "Z64213", 0),
    ):
        await db.execute(
            text(
                "INSERT INTO public.invest_momentum_event_snapshots (snapshot_at, "
                "trading_date, source, surface, market, order_type, rank, symbol) "
                "VALUES (:at, :d, 'naver_stock', :surface, 'kr', :order_type, :rank, "
                ":symbol)"
            ),
            {
                "at": T0 + minute * MINUTE,
                "d": DAY,
                "surface": f"stock:KRX:ALL:{order_type}",
                "order_type": order_type,
                "rank": rank,
                "symbol": symbol,
            },
        )
    # best rank first, then earliest capture; excluded rows are not candidates
    assert await so.load_movers(db, "kr", DAY, 10) == [KR, KR2]
    assert await so.load_movers(db, "kr", DAY, 1) == [KR]
    assert await so.load_movers(db, "us", DAY, 10) == []

    await db.execute(
        text(
            "INSERT INTO public.kr_symbol_universe (symbol, name, exchange, "
            "nxt_eligible, is_active) VALUES (:symbol, 'synthetic', 'KOSPI', true, "
            "true)"
        ),
        {"symbol": KR},
    )
    await db.execute(
        text(
            "INSERT INTO public.us_symbol_universe (symbol, exchange, name_kr, "
            "name_en, is_active) VALUES (:symbol, 'NASD', '', '', true)"
        ),
        {"symbol": US},
    )
    assert await so.load_universe(db, "kr", [KR, KR2]) == {KR: True}
    assert await so.load_universe(db, "us", [US]) == {US: "NASD"}

    await db.execute(
        text(
            "INSERT INTO public.krx_official_daily (symbol, session_date, market, "
            "close_price, source, first_received_at) VALUES (:symbol, :d, 'kospi', "
            "171300, 'krx_openapi', :at)"
        ),
        {"symbol": KR, "d": PREVIOUS, "at": T0},
    )
    for venue, close in (("KRX", "170900"), ("NTX", "170000")):
        await db.execute(
            text(
                "INSERT INTO public.kr_candles_1d (time, symbol, venue, open, high, "
                "low, close, volume, value, source) VALUES (:t, :symbol, :venue, 1, "
                "1, 1, :close, 1, 1, 'kis')"
            ),
            {
                "t": datetime(2031, 3, 3, tzinfo=UTC),
                "symbol": KR,
                "venue": venue,
                "close": Decimal(close),
            },
        )
    await db.execute(
        text(
            "INSERT INTO public.us_candles_1d (time, symbol, exchange, open, high, "
            "low, close, volume, value, source) VALUES (:t, :symbol, 'NASD', 1, 1, "
            "1, 12.5, 1, 1, 'kis')"
        ),
        {"t": datetime(2031, 3, 3, tzinfo=UTC), "symbol": US},
    )
    official = await so.load_official_closes(db, PREVIOUS, [KR])
    daily = await so.load_daily_closes(db, "kr", PREVIOUS, [KR])
    assert so.pick_reference(official, daily).price == Decimal("171300")
    # only the KRX venue is a daily reference
    assert so.pick_reference([], daily).price == Decimal("170900")
    us_daily = await so.load_daily_closes(db, "us", PREVIOUS, [US])
    assert so.pick_reference([], us_daily).price == Decimal("12.5")
    assert await so.load_daily_closes(db, "us", DAY, [US]) == []
