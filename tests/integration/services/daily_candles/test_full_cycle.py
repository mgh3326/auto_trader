"""Integration tests: daily candle store upsert / fetch round-trip and
source-precedence invariant.

The candle tables have no ORM models, so the shared test-schema bootstrap
creates their production-compatible relational shape in the run-owned
PostgreSQL database before these tests execute.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

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


@pytest.mark.integration
class TestFullCycle:
    @pytest.mark.asyncio
    async def test_upsert_then_fetch_round_trip(self, db_session):
        repo = DailyCandlesRepository(session=db_session)
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
            await db_session.commit()
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
            await db_session.rollback()
            raise
        finally:
            # Rollback any aborted transaction before cleanup.
            await db_session.rollback()
            await db_session.execute(
                text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
                {"symbol": _SYMBOL_KR},
            )
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_yahoo_fallback_does_not_clobber_kis(self, db_session):
        repo = DailyCandlesRepository(session=db_session)
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
            await db_session.commit()
            await repo.upsert_rows(market=MarketKey.US, rows=[yahoo_row])
            await db_session.commit()

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
            await db_session.rollback()
            raise
        finally:
            # Rollback any aborted transaction before cleanup.
            await db_session.rollback()
            await db_session.execute(
                text(
                    "DELETE FROM public.us_candles_1d "
                    "WHERE symbol = :symbol AND exchange = :exchange AND time = :t"
                ),
                {"symbol": _SYMBOL_US, "exchange": "NASD", "t": t},
            )
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_fetch_recent_bounded_window_matches_unbounded(self, db_session):
        """ROB-812: the bounded time predicate must not drop rows vs an
        unbounded LIMIT. Insert a 250-row daily history, then
        assert fetch_recent(count=200) returns exactly the newest 200 rows."""
        from datetime import UTC, datetime, timedelta

        repo = DailyCandlesRepository(session=db_session)
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
            for i in range(250)
        ]
        try:
            await repo.upsert_rows(market=MarketKey.KR, rows=rows)
            await db_session.commit()

            fetched = await repo.fetch_recent(
                market=MarketKey.KR,
                symbol=_SYMBOL_KR,
                partition="KRX",
                count=200,
            )

            # Unbounded reference (no time predicate) — the source of truth.
            ref = (
                (
                    await db_session.execute(
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
            await db_session.rollback()
            raise
        finally:
            await db_session.rollback()
            await db_session.execute(
                text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
                {"symbol": _SYMBOL_KR},
            )
            await db_session.commit()
