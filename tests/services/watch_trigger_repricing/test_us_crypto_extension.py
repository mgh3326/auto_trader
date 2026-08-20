"""ROB-1304 — fixed-fixture US/crypto watch-fire paths.

These are deliberately fixed fixtures, not a production arm: the feature
remains scheduleless and ``WATCH_TRIGGER_REPRICING_ENABLED`` defaults off.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.gate import evaluate_gate
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick
from app.services.watch_trigger_repricing.selection import CandidateEvent
from app.services.watch_trigger_repricing.spawn import DrySessionSpawner

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "symbol", "now", "event_uuid", "evidence"),
    [
        (
            "us",
            "BRK.B",
            dt.datetime(2026, 8, 18, 14, 0, tzinfo=dt.UTC),  # 10:00 EDT, XNYS
            "fixed-us-fire",
            {
                "market": "us",
                "symbol": "BRK.B",
                "metric": "price",
                "operator": "above",
                "threshold": "500.25",
                "currentValue": "501.00",
                "firedAt": "2026-08-18T14:00:00+00:00",
            },
        ),
        (
            "crypto",
            "KRW-BTC",
            dt.datetime(2026, 8, 22, 2, 0, tzinfo=dt.UTC),  # Saturday: still 24/7
            "fixed-crypto-fire",
            {
                "market": "crypto",
                "symbol": "KRW-BTC",
                "metric": "price",
                "operator": "below",
                "threshold": "150000000",
                "currentValue": "149900000",
                "firedAt": "2026-08-22T02:00:00+00:00",
            },
        ),
    ],
)
async def test_fixed_fixture_fire_reaches_its_market_lane_with_evidence(
    market: str,
    symbol: str,
    now: dt.datetime,
    event_uuid: str,
    evidence: dict[str, object],
) -> None:
    spawner = DrySessionSpawner()
    result = await run_repricing_tick(
        [
            CandidateEvent(
                event_uuid=event_uuid,
                symbol=symbol,
                market=market,
                outcome="review_required",
                delivery_status="delivered",
                delivered_at=now,
                trigger_evidence=evidence,
            )
        ],
        store=InMemoryClaimStore(),
        spawner=spawner,
        now=now,
        market=market,
    )

    assert result.gate.should_run is True
    assert [outcome.request.event_uuid for outcome in result.spawned] == [event_uuid]
    assert spawner.requests[0].trigger_evidence == evidence
    assert spawner.requests[0].market_date == (
        "2026-08-18" if market == "us" else "2026-08-22"
    )


def test_us_uses_xnys_regular_hours_not_kr_hours() -> None:
    # 10:00 EDT is open; this is 23:00 KST, which proves the KR window was
    # not copied into the US lane.
    decision = evaluate_gate(
        now=dt.datetime(2026, 8, 18, 14, 0, tzinfo=dt.UTC), market="us"
    )
    assert decision.should_run is True
    assert decision.market_date == "2026-08-18"


def test_crypto_does_not_inherit_equity_weekend_closure() -> None:
    decision = evaluate_gate(
        now=dt.datetime(2026, 8, 22, 2, 0, tzinfo=dt.UTC), market="crypto"
    )
    assert decision.should_run is True
    assert decision.reason == "ok_24x7"
    assert decision.session_status == "open_24x7"
