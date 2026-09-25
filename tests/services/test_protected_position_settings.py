"""#728 operator read-model behavior for failed broker or policy observations."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import protected_position_settings as settings_read
from app.services.protected_quantity_service import (
    ProtectedPositionSnapshot,
    ProtectionKey,
    ProtectionStateUnavailable,
)


def _head(key: ProtectionKey) -> ProtectedPositionSnapshot:
    return ProtectedPositionSnapshot(
        id=1,
        key=key,
        protected_quantity=Decimal("4"),
        revision=3,
        last_confirmed_broker_held=Decimal("8"),
        last_confirmed_at=datetime.now(UTC),
        updated_by_user_id=728,
        updated_at=datetime.now(UTC),
    )


class _Service:
    def __init__(self, _db, head: ProtectedPositionSnapshot):
        self.head = head

    async def list(self):
        return [self.head]

    async def list_revisions(self, *, key):
        assert key == self.head.key
        return []


@pytest.mark.asyncio
async def test_broker_read_failure_remains_unverified_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ProtectionKey("kis_live", "kr", "005930")
    monkeypatch.setattr(
        settings_read,
        "ProtectedQuantityService",
        lambda db: _Service(db, _head(key)),
    )

    async def inventory():
        return {}, {("kis_live", "kr"): "broker_read_failed"}

    monkeypatch.setattr(settings_read, "read_live_position_inventory", inventory)
    rows = await settings_read.read_protected_position_settings(object())
    assert rows == [
        {
            "account_scope": "kis_live",
            "market": "kr",
            "symbol": "005930",
            "name": "005930",
            "protected_quantity": "4",
            "broker_held": None,
            "broker_sellable": None,
            "headroom": None,
            "state": "unverified",
            "mode": "off",
            "broker_observed_at": None,
            "read_error": "broker_read_failed",
            "revision": 3,
            "latest_revision": None,
            "history_url": "/invest/api/settings/protected-positions/kis_live/kr/005930/history",
        }
    ]


@pytest.mark.asyncio
async def test_policy_read_failure_keeps_raw_broker_facts_but_marks_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ProtectionKey("kis_live", "kr", "005930")
    monkeypatch.setattr(
        settings_read,
        "ProtectedQuantityService",
        lambda db: _Service(db, _head(key)),
    )

    async def inventory():
        return {
            key: settings_read.LivePositionObservation(
                key=key,
                name="삼성전자",
                held=Decimal("10"),
                sellable=Decimal("8"),
                observed_at=datetime.now(UTC),
            )
        }, {}

    async def unavailable(**_kwargs):
        raise ProtectionStateUnavailable("test policy read failure")

    monkeypatch.setattr(settings_read, "read_live_position_inventory", inventory)
    monkeypatch.setattr(settings_read, "headroom_for_observation", unavailable)
    row = (await settings_read.read_protected_position_settings(object()))[0]
    assert row["state"] == "unverified"
    assert row["broker_held"] == "10"
    assert row["broker_sellable"] == "8"
    assert row["headroom"] is None
    assert row["protected_quantity"] == "4"
