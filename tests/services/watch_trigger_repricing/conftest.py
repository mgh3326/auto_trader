"""Shared fixtures for the ROB-1286 watch-trigger repricing tests."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.selection import CandidateEvent

KST = dt.timezone(dt.timedelta(hours=9))

# 2026-08-18 is the Tuesday of the real incident; 09:05 KST is when the
# Samsung ladder fired. Every test that needs "an open KR session" uses it.
INCIDENT_FIRE = dt.datetime(2026, 8, 18, 9, 5, tzinfo=KST)
INCIDENT_TICK = dt.datetime(2026, 8, 18, 9, 6, tzinfo=KST)


def make_event(
    *,
    event_uuid: str,
    symbol: str = "005930",
    market: str = "kr",
    outcome: str = "review_required",
    delivery_status: str = "delivered",
    delivered_at: dt.datetime | None = INCIDENT_FIRE,
) -> CandidateEvent:
    return CandidateEvent(
        event_uuid=event_uuid,
        symbol=symbol,
        market=market,
        outcome=outcome,
        delivery_status=delivery_status,
        delivered_at=delivered_at,
    )


@pytest.fixture
def store() -> InMemoryClaimStore:
    return InMemoryClaimStore()


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "WATCH_TRIGGER_REPRICING_ENABLED", True, raising=False
    )
