"""hk 6503: write guard and unknown-as-absence against the run-owned test DB.

Runs in CI (database). Each test rolls back, so nothing persists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.store import (
    load_flag_records,
    upsert_flag_observations,
)

pytestmark = pytest.mark.integration

SYMBOL = "T6503A"  # fake code; passes the symbol CHECK, no real listing


def _obs(
    state: str,
    *,
    category: str = v.CATEGORY_TRADING_HALT,
    source: str = v.SOURCE_NAVER_INTRADAY,
    raw: str = "Y",
) -> v.FlagObservation:
    return v.FlagObservation(
        symbol=SYMBOL,
        category=category,
        state=state,
        source=source,
        raw_code=raw,
    )


T0 = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_newer_observation_replaces_older_and_older_never_does(
    db_session: AsyncSession,
) -> None:
    await upsert_flag_observations(db_session, [_obs("none", raw="N")], observed_at=T0)
    newer = await upsert_flag_observations(db_session, [_obs("halted")], observed_at=T1)
    assert newer.written == 1

    stale = await upsert_flag_observations(
        db_session, [_obs("none", raw="N")], observed_at=T0
    )
    assert stale.written == 0
    assert stale.guarded == 1

    record = (await load_flag_records(db_session, [SYMBOL]))[SYMBOL][
        v.CATEGORY_TRADING_HALT
    ]
    assert record.state == "halted"
    assert record.observed_at == T1
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unmapped_never_overwrites_a_mapped_state(
    db_session: AsyncSession,
) -> None:
    await upsert_flag_observations(
        db_session,
        [_obs("designated", category=v.CATEGORY_MANAGEMENT, raw="1")],
        observed_at=T0,
    )
    later_unmapped = await upsert_flag_observations(
        db_session,
        [_obs(v.STATE_UNMAPPED, category=v.CATEGORY_MANAGEMENT, raw="3")],
        observed_at=T1,
    )
    assert later_unmapped.written == 0

    record = (await load_flag_records(db_session, [SYMBOL]))[SYMBOL][
        v.CATEGORY_MANAGEMENT
    ]
    assert record.state == "designated"
    await db_session.rollback()


@pytest.mark.asyncio
async def test_absent_symbol_is_unknown_not_none(db_session: AsyncSession) -> None:
    records = await load_flag_records(db_session, [SYMBOL])
    assert SYMBOL not in records


@pytest.mark.asyncio
async def test_unmapped_is_stored_when_nothing_mapped_is_known(
    db_session: AsyncSession,
) -> None:
    stats = await upsert_flag_observations(
        db_session,
        [_obs(v.STATE_UNMAPPED, category=v.CATEGORY_MARKET_ALERT, raw="09")],
        observed_at=T0,
    )
    assert stats.written == 1
    record = (await load_flag_records(db_session, [SYMBOL]))[SYMBOL][
        v.CATEGORY_MARKET_ALERT
    ]
    assert record.state == v.STATE_UNMAPPED
    assert record.raw_code == "09"
    await db_session.rollback()
