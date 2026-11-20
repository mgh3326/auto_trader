"""hk 5622 DB round trip: rows written by the screen_stocks* observer (CI-owned DB).

``review.screener_pick_log`` is append-only, so each test uses its own call ids and
leaves its rows behind (the run-owned test database is dropped after the run).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.models.screener_pick_log import ScreenerPickLog
from app.services import screener_tool_pick_log as mod

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _screen_result() -> dict[str, Any]:
    return {
        "results": [
            {"symbol": "BRK-B", "price": 512.34, "name": "Berkshire"},
            {"symbol": "AAPL", "price": 190.0},
        ],
        "filters_applied": {"market": "us", "sort_by": "volume", "limit": 50},
        "meta": {
            "source": "tvscreener",
            "halted_suspect_excluded": [{"symbol": "ZZZ", "frozen_sessions": 3}],
        },
        "timestamp": "2026-11-20T22:59:00+00:00",
    }


async def test_screen_stocks_rows_round_trip(db_session: Any) -> None:
    out = await mod.record_screen_stocks_picks(
        _screen_result(), request={"market": "us", "limit": 50}, enabled=True
    )
    call_id = uuid.UUID(out["pick_log_call_id"])

    rows = (
        (
            await db_session.execute(
                select(ScreenerPickLog)
                .where(ScreenerPickLog.call_id == call_id)
                .order_by(ScreenerPickLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.symbol, r.admission, r.admission_reason) for r in rows] == [
        ("BRK.B", "admitted", "returned_to_session"),
        ("AAPL", "admitted", "returned_to_session"),
        ("ZZZ", "not_admitted", "halted_suspect"),
    ]
    first = rows[0]
    assert first.collection_version == "screener-tools-v1"
    assert (first.market, first.source, first.rank) == ("us", "screen_stocks", 1)
    assert first.decision_price_text == "512.34"
    assert first.raw_row["symbol"] == "BRK-B"
    assert first.call_context["tool"] == "screen_stocks"

    # The candidate link a session cites resolves by call_id + canonical symbol,
    # the same query decision_evidence.record_proposal_decision runs.
    linked = (
        await db_session.execute(
            select(ScreenerPickLog).where(
                ScreenerPickLog.symbol == "BRK.B",
                ScreenerPickLog.market == "us",
                ScreenerPickLog.call_id == call_id,
            )
        )
    ).scalar_one()
    assert linked.id == first.id


async def test_snapshot_rows_round_trip_with_multi_preset_sources(
    db_session: Any,
) -> None:
    payload = {
        "results": [
            {
                "rank": 1,
                "symbol": "005930",
                "market": "kr",
                "priceLabel": "75,000원",
                "matchedPresets": ["double_buy", "support_proximity"],
            }
        ],
        "pagination": {"offset": 0, "limit": 40, "total_available": 1},
    }
    observation = {
        "dropped_rows": [
            (
                {
                    "rank": 2,
                    "symbol": "000660",
                    "market": "kr",
                    "priceLabel": "201,500원",
                    "matchedPresets": ["double_buy"],
                },
                "exclude_symbols",
            )
        ],
        "freshness_by_preset": {
            "double_buy": {
                "fetchedAt": "2026-11-20T22:00:00+00:00",
                "primary": {"snapshotDate": "2026-11-19"},
            }
        },
        "preset_ids": ["double_buy", "support_proximity"],
    }
    out = await mod.record_snapshot_picks(
        payload,
        request={"market": "kr", "presets": ["double_buy", "support_proximity"]},
        observation=observation,
        enabled=True,
    )
    call_id = uuid.UUID(out["pick_log_call_id"])
    rows = (
        (
            await db_session.execute(
                select(ScreenerPickLog).where(ScreenerPickLog.call_id == call_id)
            )
        )
        .scalars()
        .all()
    )
    got = {(r.source, r.symbol, r.admission, r.rank) for r in rows}
    assert got == {
        ("screen_stocks_snapshot:double_buy", "005930", "admitted", 1),
        ("screen_stocks_snapshot:support_proximity", "005930", "admitted", None),
        ("screen_stocks_snapshot:double_buy", "000660", "not_admitted", 2),
    }
    by = {(r.source, r.symbol): r for r in rows}
    row = by[("screen_stocks_snapshot:double_buy", "005930")]
    assert row.decision_price_text == "75000"
    assert row.data_asof == "2026-11-19"
    assert row.fetched_at is not None


async def test_second_call_gets_a_distinct_call_id(db_session: Any) -> None:
    a = await mod.record_screen_stocks_picks(
        _screen_result(), request={"market": "us"}, enabled=True
    )
    b = await mod.record_screen_stocks_picks(
        _screen_result(), request={"market": "us"}, enabled=True
    )
    assert a["pick_log_call_id"] != b["pick_log_call_id"]
