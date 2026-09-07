"""Shared post-upsert orchestration (fillwire P0).

The websocket monitor and the HTTP ingest route both run this function, so the
notification-suppression semantics the monitor grew (KIS duplicate suppression,
Upbit small-fill proposal-rung recovery) are asserted here once.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.execution_ledger.fill_ingest import (
    DownstreamHooks,
    run_post_upsert_downstream,
)
from app.services.fill_notification import FillOrder


def _order(*, amount: float, market_type: str = "crypto") -> FillOrder:
    return FillOrder(
        symbol="KRW-BTC" if market_type == "crypto" else "000660",
        side="bid",
        filled_price=92_800_000.0,
        filled_qty=amount / 92_800_000.0,
        filled_amount=amount,
        filled_at="2026-09-07T12:00:00+09:00",
        account="upbit" if market_type == "crypto" else "kis",
        order_id="order-1",
        market_type=market_type,
        currency="KRW",
    )


class _Hooks:
    def __init__(self, *, projected: bool = False) -> None:
        self.projected = projected
        self.projection_calls: list[dict[str, Any]] = []
        self.notify_calls: list[tuple[tuple, dict]] = []

    async def project(self, order_data: dict[str, Any]) -> bool:
        self.projection_calls.append(order_data)
        return self.projected

    async def notify(self, *args: Any, **kwargs: Any) -> None:
        self.notify_calls.append((args, kwargs))

    def as_hooks(self) -> DownstreamHooks:
        return DownstreamHooks(
            project_upbit_proposal_fill=self.project,
            send_fill_notification=self.notify,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_kis_fill_notifies_with_the_correlation_id() -> None:
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status="inserted",
        fill_order=_order(amount=1_959_000, market_type="kr"),
        raw_event={"symbol": "000660"},
        correlation_id="corr-1",
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is True
    assert hooks.projection_calls == [], "no Upbit projection for a KIS fill"
    assert hooks.notify_calls[0][1] == {"correlation_id": "corr-1"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["updated", "unchanged"])
async def test_duplicate_kis_row_suppresses_the_notification(status: str) -> None:
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status=status,
        fill_order=_order(amount=1_959_000, market_type="kr"),
        raw_event=None,
        hooks=hooks.as_hooks(),
    )

    assert outcome.duplicate is True
    assert outcome.notification_attempted is False
    assert hooks.notify_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_ledger_row_still_notifies() -> None:
    """``None`` means the commit gate is off — not duplicate evidence."""
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status=None,
        fill_order=_order(amount=1_959_000, market_type="kr"),
        raw_event=None,
        hooks=hooks.as_hooks(),
    )

    assert outcome.duplicate is False
    assert outcome.notification_attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_projects_then_notifies_with_the_rung_flag() -> None:
    hooks = _Hooks(projected=True)
    frame = {"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"}

    await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="inserted",
        fill_order=_order(amount=27_840),
        raw_event=frame,
        hooks=hooks.as_hooks(),
    )

    assert hooks.projection_calls == [frame]
    assert hooks.notify_calls[0][1] == {"proposal_rung_fill": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_projection_is_skipped_when_no_row_was_written() -> None:
    hooks = _Hooks(projected=True)

    await run_post_upsert_downstream(
        broker="upbit",
        upsert_status=None,
        fill_order=_order(amount=27_840),
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"},
        hooks=hooks.as_hooks(),
    )

    assert hooks.projection_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_upbit_row_recovers_a_suppressed_small_alert() -> None:
    """A rung that only projected on the second delivery still gets announced."""
    hooks = _Hooks(projected=True)

    outcome = await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="unchanged",
        fill_order=_order(amount=27_840),  # below the 50,000 KRW threshold
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"},
        hooks=hooks.as_hooks(),
    )

    assert outcome.duplicate is True
    assert outcome.notification_attempted is True
    assert hooks.notify_calls[0][1] == {"proposal_rung_fill": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_upbit_large_fill_stays_suppressed() -> None:
    """The large fill already alerted on its first delivery."""
    hooks = _Hooks(projected=True)

    outcome = await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="unchanged",
        fill_order=_order(amount=1_000_000),
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.01"},
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is False
    assert hooks.notify_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_upbit_without_projection_stays_suppressed() -> None:
    hooks = _Hooks(projected=False)

    outcome = await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="unchanged",
        fill_order=_order(amount=27_840),
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"},
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_order_context_reports_no_notification() -> None:
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status="inserted",
        fill_order=None,
        raw_event=None,
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is False
    assert hooks.notify_calls == []
