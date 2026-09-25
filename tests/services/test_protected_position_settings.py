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
async def test_us_kis_fresh_observation_uses_overseas_sellable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The US settings reader must accept the real overseas sellable field."""

    class _KIS:
        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple",
                    "ovrs_cblc_qty": "10",
                    "ovrs_ord_psbl_qty": "7",
                }
            ]

    monkeypatch.setattr(settings_read, "KISClient", _KIS)
    observation = await settings_read.fresh_broker_observation(
        key=ProtectionKey("kis_live", "us", "AAPL")
    )
    assert observation.held == Decimal("10")
    assert observation.sellable == Decimal("7")


@pytest.mark.asyncio
async def test_us_kis_fresh_observation_rejects_when_all_sellable_fields_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _KIS:
        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple",
                    "ovrs_cblc_qty": "10",
                }
            ]

    monkeypatch.setattr(settings_read, "KISClient", _KIS)
    with pytest.raises(settings_read.BrokerObservationUnavailable):
        await settings_read.fresh_broker_observation(
            key=ProtectionKey("kis_live", "us", "AAPL")
        )


@pytest.mark.asyncio
async def test_successful_missing_kis_head_is_orphan_shortfall_not_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ProtectionKey("kis_live", "kr", "005930")
    monkeypatch.setattr(
        settings_read,
        "ProtectedQuantityService",
        lambda db: _Service(db, _head(key)),
    )

    async def inventory():
        return {}, {}

    async def projection_must_not_replace_successful_absence(**_kwargs):
        raise AssertionError("orphan shortfall must retain direct zero evidence")

    monkeypatch.setattr(settings_read, "read_live_position_inventory", inventory)
    monkeypatch.setattr(
        settings_read,
        "headroom_for_observation",
        projection_must_not_replace_successful_absence,
    )
    row = (await settings_read.read_protected_position_settings(object()))[0]
    assert row["protected_quantity"] == "4"
    assert row["broker_held"] == "0"
    assert row["broker_sellable"] == "0"
    assert row["headroom"] == "0"
    assert row["state"] == "shortfall"
    assert row["read_error"] is None


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
