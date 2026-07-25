"""Integration tests: daily candle store upsert / fetch round-trip and
source-precedence invariant.

These tests connect directly to the dev Timescale container (port 5434,
db auto_trader) because the test-suite conftest force-overrides DATABASE_URL
to test_db at port 5432, which does not contain the hypertable schema.
We read the real DATABASE_URL from the .env file before the conftest
applies its override.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)

# Per-process unique suffix prevents collisions when integration tests run
# in parallel (CI workers, or a developer running tests while CI runs).
_TEST_SUFFIX = uuid.uuid4().hex[:8].upper()
_SYMBOL_KR = f"TSTKR{_TEST_SUFFIX}"
_SYMBOL_US = f"TSTUS{_TEST_SUFFIX}"


def _find_dotenv_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            candidate = parent / ".env"
            return candidate if candidate.is_file() else None
    return None


def _read_dev_database_url() -> str | None:
    """Read DATABASE_URL from the .env file (not the test-overridden env var).

    The conftest force-sets DATABASE_URL to test_db at port 5432.  Integration
    tests that exercise Timescale hypertables must use the real dev DB at port
    5434.  We read .env directly to recover the original URL.
    """
    env_path = _find_dotenv_path()
    if env_path is None:
        return None
    with env_path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "DATABASE_URL":
                return value.strip()
    return None


_DEV_DB_URL = _read_dev_database_url() or os.environ.get("DEV_DATABASE_URL")


@pytest_asyncio.fixture
async def dev_session():
    """Async session against an explicitly configured dev Timescale database."""
    if not _DEV_DB_URL:
        pytest.skip(
            "DEV_DATABASE_URL or local .env DATABASE_URL is required for live Timescale integration tests"
        )

    engine = create_async_engine(_DEV_DB_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.integration
class TestFullCycle:
    @pytest.mark.asyncio
    async def test_upsert_then_fetch_round_trip(self, dev_session):
        repo = DailyCandlesRepository(session=dev_session)
        rows = [
            DailyCandleRow(
                time_utc=datetime(2026, 5, d, tzinfo=UTC),
                symbol=_SYMBOL_KR,
                partition="KRX",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                adj_close=None,
                volume=1000.0,
                value=100500.0,
                source="kis",
            )
            for d in range(1, 6)
        ]
        try:
            inserted = await repo.upsert_rows(market=MarketKey.KR, rows=rows)
            await dev_session.commit()
            # rowcount for batch ON CONFLICT upserts is not reliable across
            # drivers (asyncpg returns 0 for bulk execute) — assert non-negative.
            assert inserted >= 0

            fetched = await repo.fetch_recent(
                market=MarketKey.KR,
                symbol=_SYMBOL_KR,
                partition="KRX",
                count=10,
            )
            assert len(fetched) == 5
            assert all(r.source == "kis" for r in fetched)
        except Exception:
            await dev_session.rollback()
            raise
        finally:
            # Rollback any aborted transaction before cleanup.
            await dev_session.rollback()
            await dev_session.execute(
                text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
                {"symbol": _SYMBOL_KR},
            )
            await dev_session.commit()

    @pytest.mark.asyncio
    async def test_yahoo_fallback_does_not_clobber_kis(self, dev_session):
        repo = DailyCandlesRepository(session=dev_session)
        t = datetime(2026, 5, 14, tzinfo=UTC)
        kis_row = DailyCandleRow(
            time_utc=t,
            symbol=_SYMBOL_US,
            partition="NASD",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            adj_close=None,
            volume=1000.0,
            value=100500.0,
            source="kis",
        )
        yahoo_row = DailyCandleRow(
            time_utc=t,
            symbol=_SYMBOL_US,
            partition="NASD",
            open=200.0,
            high=201.0,
            low=199.0,
            close=200.5,
            adj_close=199.0,
            volume=2000.0,
            value=401000.0,
            source="yahoo_fallback",
        )
        try:
            await repo.upsert_rows(market=MarketKey.US, rows=[kis_row])
            await dev_session.commit()
            await repo.upsert_rows(market=MarketKey.US, rows=[yahoo_row])
            await dev_session.commit()

            fetched = await repo.fetch_recent(
                market=MarketKey.US,
                symbol=_SYMBOL_US,
                partition="NASD",
                count=1,
            )
            assert len(fetched) == 1
            assert fetched[0].source == "kis"
            assert fetched[0].close == pytest.approx(100.5)  # KIS row not clobbered
        except Exception:
            await dev_session.rollback()
            raise
        finally:
            # Rollback any aborted transaction before cleanup.
            await dev_session.rollback()
            await dev_session.execute(
                text(
                    "DELETE FROM public.us_candles_1d "
                    "WHERE symbol = :symbol AND exchange = :exchange AND time = :t"
                ),
                {"symbol": _SYMBOL_US, "exchange": "NASD", "t": t},
            )
            await dev_session.commit()

    @pytest.mark.asyncio
    async def test_fetch_recent_bounded_window_matches_unbounded(self, dev_session):
        """ROB-812: the bounded time predicate must not drop rows vs an
        unbounded LIMIT.  Insert 250 daily rows (>2 chunks of 90 days), then
        assert fetch_recent(count=200) returns exactly the newest 200 rows."""
        from datetime import UTC, datetime, timedelta

        repo = DailyCandlesRepository(session=dev_session)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        rows = [
            DailyCandleRow(
                time_utc=base + timedelta(days=i),
                symbol=_SYMBOL_KR,
                partition="KRX",
                open=1.0,
                high=2.0,
                low=0.5,
                close=1.5,
                adj_close=None,
                volume=10.0,
                value=15.0,
                source="test",
            )
            for i in range(250)  # 250 days => spans >2 chunks (90d each)
        ]
        try:
            await repo.upsert_rows(market=MarketKey.KR, rows=rows)
            await dev_session.commit()

            fetched = await repo.fetch_recent(
                market=MarketKey.KR,
                symbol=_SYMBOL_KR,
                partition="KRX",
                count=200,
            )

            # Unbounded reference (no time predicate) — the source of truth.
            ref = (
                (
                    await dev_session.execute(
                        text(
                            "SELECT time FROM public.kr_candles_1d "
                            "WHERE symbol=:s AND venue='KRX' "
                            "ORDER BY time DESC LIMIT 200"
                        ),
                        {"s": _SYMBOL_KR},
                    )
                )
                .scalars()
                .all()
            )

            assert len(fetched) == 200
            # fetch_recent returns ascending (reversed); compare newest set.
            fetched_times = {r.time_utc for r in fetched}
            assert fetched_times == set(ref)
        except Exception:
            await dev_session.rollback()
            raise
        finally:
            await dev_session.rollback()
            await dev_session.execute(
                text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
                {"symbol": _SYMBOL_KR},
            )
            await dev_session.commit()
