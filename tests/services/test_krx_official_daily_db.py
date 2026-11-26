"""CI-only PostgreSQL contracts for the KRX official daily table (hk 6586).

The table is insert-only and the triggers forbid DELETE, so every test uses a
session date and symbols derived from a fresh UUID; rows from earlier runs can
never collide with this test's keys.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.services.krx_official_daily.parse import parse_daily_payload
from app.services.krx_official_daily.reader import read_zero_volume_fact
from app.services.krx_official_daily.store import insert_official_daily

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "krx_official_daily"
    / "stk_bydd_trd_spec_derived.json"
)


def _unique_session() -> date:
    return date(2090, 1, 1) + timedelta(days=int(uuid4().hex[:6], 16) % 3000)


def _rows(session_date: date, symbol_a: str, symbol_b: str) -> list[dict]:
    payload = json.loads(FIXTURE.read_text())
    stamped = json.loads(
        json.dumps(payload).replace("20261126", session_date.strftime("%Y%m%d"))
    )
    stamped["OutBlock_1"][0]["ISU_CD"] = symbol_a
    stamped["OutBlock_1"][1]["ISU_CD"] = symbol_b
    return parse_daily_payload(stamped, market="kospi", session_date=session_date).rows


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rerun_inserts_zero_rows_and_keeps_the_first_observation(
    db_session,
) -> None:
    session_date = _unique_session()
    symbol_a, symbol_b = "901001", "901002"
    first_at = datetime(2091, 1, 2, 8, 30, tzinfo=UTC)
    rows = _rows(session_date, symbol_a, symbol_b)

    first = await insert_official_daily(
        db_session, rows=rows, first_received_at=first_at
    )
    await db_session.commit()
    assert first == 2

    changed = [dict(row, close_price=row["close_price"] + 1) for row in rows]
    later_at = first_at + timedelta(hours=5)
    second = await insert_official_daily(
        db_session, rows=changed, first_received_at=later_at
    )
    await db_session.commit()
    assert second == 0

    stored = (
        await db_session.execute(
            text(
                "SELECT close_price, first_received_at, full_day_zero_volume, field_reasons "
                "FROM public.krx_official_daily WHERE symbol = :symbol "
                "AND session_date = :session_date"
            ),
            {"symbol": symbol_a, "session_date": session_date},
        )
    ).one()
    assert float(stored.close_price) == float(rows[0]["close_price"])
    assert stored.first_received_at == first_at
    assert stored.full_day_zero_volume is False
    assert stored.field_reasons == {}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_delete_and_truncate_are_rejected_for_every_role(
    db_session,
) -> None:
    session_date = _unique_session()
    symbol = "902001"
    rows = _rows(session_date, symbol, "902002")
    await insert_official_daily(
        db_session, rows=rows, first_received_at=datetime(2091, 1, 2, tzinfo=UTC)
    )
    await db_session.commit()

    statements = (
        "UPDATE public.krx_official_daily SET name = 'x' WHERE symbol = :symbol",
        "DELETE FROM public.krx_official_daily WHERE symbol = :symbol",
        "TRUNCATE public.krx_official_daily",
    )
    for statement in statements:
        with pytest.raises(DBAPIError) as excinfo:
            async with db_session.begin_nested():
                await db_session.execute(text(statement), {"symbol": symbol})
        assert "restrict_violation" in str(excinfo.value) or "insert-only" in str(
            excinfo.value
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_constraints_refuse_bad_symbol_market_and_zero_volume_mismatch(
    db_session,
) -> None:
    session_date = _unique_session()
    base = _rows(session_date, "903001", "903002")[0]
    bad_cases = (
        {**base, "symbol": "KR7903001"},
        {**base, "symbol": "903003", "market": "KOSPI"},
        {**base, "symbol": "903004", "full_day_zero_volume": True, "volume": 5},
    )
    for bad in bad_cases:
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await insert_official_daily(
                    db_session,
                    rows=[bad],
                    first_received_at=datetime(2091, 1, 2, tzinfo=UTC),
                )
        await db_session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reader_returns_the_zero_volume_fact_with_its_date(db_session) -> None:
    session_date = _unique_session()
    rows = _rows(session_date, "904001", "904002")
    await insert_official_daily(
        db_session, rows=rows, first_received_at=datetime(2091, 1, 2, tzinfo=UTC)
    )
    await db_session.commit()

    fact = await read_zero_volume_fact(
        db_session, symbol="904001", session_date=session_date
    )
    assert fact["state"] == "traded"
    assert fact["session_date"] == session_date.isoformat()
    assert fact["is_halt_confirmation"] is False

    absent = await read_zero_volume_fact(
        db_session, symbol="904999", session_date=session_date
    )
    assert absent["state"] == "unknown"
