"""ROB-866 — Toss manual-activity detection sweep tests.

Toss has no execution websocket, so operator app-side manual trades are invisible
until reported. This sweep diffs GET /orders against the ledger + proposal rungs to
surface manual (unbooked) orders, then in execution mode alerts Telegram +
session_context. Stage 1 = detect + alert only (no auto-bookkeeping).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from app.services.brokers.toss.dto import TossOrder, TossOrdersPage
from app.services.toss_manual_activity import (
    ManualOrder,
    TossManualActivityAlertStore,
    detect_manual_activity,
    run_manual_activity_sweep,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _order(
    order_id: str,
    symbol: str,
    side: str,
    status: str,
    *,
    qty: str = "0",
    filled: str | None = None,
    avg: str | None = None,
    currency: str = "KRW",
) -> TossOrder:
    execution: dict[str, object] = {}
    if filled is not None:
        execution["filledQuantity"] = Decimal(filled)
    if avg is not None:
        execution["averageFilledPrice"] = Decimal(avg)
    return TossOrder(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type="limit",
        time_in_force="DAY",
        status=status,
        price=None,
        quantity=Decimal(qty),
        order_amount=None,
        currency=currency,
        ordered_at="2026-07-13T10:00:00",
        canceled_at=None,
        execution=execution,
    )


class FakeTossReadClient:
    """Read-only fake — only exposes list_orders (the sweep must never mutate)."""

    def __init__(
        self,
        *,
        open_orders: list[TossOrder],
        closed_pages: list[tuple[list[TossOrder], str | None, bool]],
    ) -> None:
        self._open = open_orders
        self._closed_pages = closed_pages
        self.calls: list[dict[str, object]] = []

    async def list_orders(
        self,
        *,
        status: str,
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> TossOrdersPage:
        self.calls.append(
            {
                "status": status,
                "cursor": cursor,
                "from_date": from_date,
                "to_date": to_date,
                "limit": limit,
            }
        )
        if status == "OPEN":
            return TossOrdersPage(orders=self._open, next_cursor=None, has_next=False)
        # CLOSED — resolve page by cursor position.
        if cursor is None:
            idx = 0
        else:
            idx = int(cursor)
        orders, next_cursor, has_next = self._closed_pages[idx]
        return TossOrdersPage(orders=orders, next_cursor=next_cursor, has_next=has_next)


class FakeAlertStore:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    async def existing_alerted_ids(self, order_ids: set[str]) -> set[str]:
        return set(self.recorded) & set(order_ids)

    async def record_alerts(self, orders: list[ManualOrder]) -> int:
        self.recorded.extend(o.order_id for o in orders)
        return len(orders)


async def _all_known(ids: set[str]) -> set[str]:
    return set(ids)


async def _none_known(ids: set[str]) -> set[str]:
    return set()


# --------------------------------------------------------------------------- #
# Pure detection
# --------------------------------------------------------------------------- #


async def test_manual_filled_order_absent_from_ledger_is_reported() -> None:
    manual = _order(
        "m1", "005930", "sell", "FILLED", qty="10", filled="10", avg="70000"
    )
    known = _order("k1", "000660", "buy", "FILLED", qty="5", filled="5", avg="100000")
    client = FakeTossReadClient(
        open_orders=[], closed_pages=[([manual, known], None, False)]
    )

    async def known_lookup(ids: set[str]) -> set[str]:
        return {"k1"} & ids

    sweep = await detect_manual_activity(
        client=client, known_order_ids=known_lookup, now=NOW, window_hours=24
    )

    assert [o.order_id for o in sweep.filled] == ["m1"]
    assert sweep.filled[0].symbol == "005930"
    assert sweep.filled[0].side == "sell"
    assert sweep.filled[0].filled_quantity == Decimal("10")
    assert sweep.filled[0].avg_fill_price == Decimal("70000")
    assert sweep.filled[0].market == "kr"
    assert sweep.open_orders == []


async def test_no_false_positive_when_every_order_is_known() -> None:
    filled = _order("k1", "005930", "buy", "FILLED", qty="5", filled="5", avg="70000")
    open_order = _order("k2", "000660", "buy", "PENDING", qty="3")
    client = FakeTossReadClient(
        open_orders=[open_order], closed_pages=[([filled], None, False)]
    )

    sweep = await detect_manual_activity(
        client=client, known_order_ids=_all_known, now=NOW, window_hours=24
    )

    assert sweep.filled == []
    assert sweep.open_orders == []


async def test_open_manual_order_listed_separately_from_fills() -> None:
    open_order = _order("o1", "000660", "buy", "PENDING", qty="3", currency="KRW")
    client = FakeTossReadClient(
        open_orders=[open_order], closed_pages=[([], None, False)]
    )

    sweep = await detect_manual_activity(
        client=client, known_order_ids=_none_known, now=NOW, window_hours=24
    )

    assert [o.order_id for o in sweep.open_orders] == ["o1"]
    assert sweep.open_orders[0].is_open is True
    assert sweep.filled == []


async def test_cancelled_closed_manual_order_is_not_reported_as_fill() -> None:
    cancelled = _order("c1", "005930", "buy", "CANCELLED", qty="10", filled="0")
    client = FakeTossReadClient(
        open_orders=[], closed_pages=[([cancelled], None, False)]
    )

    sweep = await detect_manual_activity(
        client=client, known_order_ids=_none_known, now=NOW, window_hours=24
    )

    assert sweep.filled == []
    assert sweep.open_orders == []


async def test_partial_filled_closed_manual_order_is_reported() -> None:
    partial = _order(
        "p1", "005930", "sell", "PARTIAL_FILLED", qty="10", filled="4", avg="70000"
    )
    client = FakeTossReadClient(open_orders=[], closed_pages=[([partial], None, False)])

    sweep = await detect_manual_activity(
        client=client, known_order_ids=_none_known, now=NOW, window_hours=24
    )

    assert [o.order_id for o in sweep.filled] == ["p1"]


async def test_closed_orders_are_paginated() -> None:
    m1 = _order("m1", "005930", "buy", "FILLED", qty="1", filled="1", avg="70000")
    m2 = _order("m2", "000660", "buy", "FILLED", qty="1", filled="1", avg="90000")
    client = FakeTossReadClient(
        open_orders=[],
        closed_pages=[([m1], "1", True), ([m2], None, False)],
    )

    sweep = await detect_manual_activity(
        client=client, known_order_ids=_none_known, now=NOW, window_hours=24
    )

    assert {o.order_id for o in sweep.filled} == {"m1", "m2"}
    closed_calls = [c for c in client.calls if c["status"] == "CLOSED"]
    assert len(closed_calls) == 2
    # Window from/to are derived and passed on every CLOSED page.
    assert all(c["from_date"] and c["to_date"] for c in closed_calls)
    assert all(c["limit"] == 100 for c in closed_calls)


async def test_detection_only_issues_read_calls() -> None:
    m1 = _order("m1", "005930", "buy", "FILLED", qty="1", filled="1", avg="70000")
    client = FakeTossReadClient(open_orders=[], closed_pages=[([m1], None, False)])

    await detect_manual_activity(
        client=client, known_order_ids=_none_known, now=NOW, window_hours=24
    )

    assert client.calls, "sweep must call the read API"
    assert all(c["status"] in {"OPEN", "CLOSED"} for c in client.calls)


# --------------------------------------------------------------------------- #
# Orchestration (dry-run vs execution, idempotency)
# --------------------------------------------------------------------------- #


def _one_manual_client() -> FakeTossReadClient:
    manual = _order(
        "m1", "005930", "sell", "FILLED", qty="10", filled="10", avg="70000"
    )
    return FakeTossReadClient(open_orders=[], closed_pages=[([manual], None, False)])


async def test_dry_run_lists_manual_without_any_write() -> None:
    store = FakeAlertStore()
    notify = AsyncMock()
    append = AsyncMock()

    res = await run_manual_activity_sweep(
        dry_run=True,
        client=_one_manual_client(),
        known_order_ids=_none_known,
        alert_store=store,
        notify=notify,
        append_session_context=append,
        now=NOW,
    )

    assert res["success"] is True
    assert res["dry_run"] is True
    assert res["mutation_sent"] is False
    assert res["new_count"] == 1
    assert res["alerted"] is False
    assert len(res["manual_filled"]) == 1
    notify.assert_not_awaited()
    append.assert_not_awaited()
    assert store.recorded == []


async def test_execution_mode_alerts_and_records_marker() -> None:
    store = FakeAlertStore()
    notify = AsyncMock()
    append = AsyncMock()

    res = await run_manual_activity_sweep(
        dry_run=False,
        client=_one_manual_client(),
        known_order_ids=_none_known,
        alert_store=store,
        notify=notify,
        append_session_context=append,
        now=NOW,
    )

    assert res["alerted"] is True
    assert res["alerted_count"] == 1
    notify.assert_awaited()
    append.assert_awaited()
    # session_context entry carries the kr market handoff.
    entries = append.await_args.args[0]
    assert entries[0]["market"] == "kr"
    assert entries[0]["entry_type"] == "handoff_note"
    assert store.recorded == ["m1"]


async def test_second_sweep_does_not_realert_same_order() -> None:
    store = FakeAlertStore()

    first_notify = AsyncMock()
    await run_manual_activity_sweep(
        dry_run=False,
        client=_one_manual_client(),
        known_order_ids=_none_known,
        alert_store=store,
        notify=first_notify,
        append_session_context=AsyncMock(),
        now=NOW,
    )
    assert first_notify.await_count == 1

    second_notify = AsyncMock()
    res = await run_manual_activity_sweep(
        dry_run=False,
        client=_one_manual_client(),
        known_order_ids=_none_known,
        alert_store=store,
        notify=second_notify,
        append_session_context=AsyncMock(),
        now=NOW,
    )

    second_notify.assert_not_awaited()
    assert res["new_count"] == 0
    assert res["alerted"] is False
    assert store.recorded == ["m1"]


# --------------------------------------------------------------------------- #
# Alert store — DB-backed idempotency marker
# --------------------------------------------------------------------------- #


def _manual(order_id: str) -> ManualOrder:
    return ManualOrder(
        order_id=order_id,
        symbol="005930",
        side="sell",
        status="FILLED",
        market="kr",
        quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        avg_fill_price=Decimal("70000"),
        ordered_at="2026-07-13T10:00:00",
        is_open=False,
    )


async def test_alert_store_records_and_is_idempotent(db_session) -> None:
    from app.models.review import TossManualActivityAlert

    ids = {"rob866-a", "rob866-b"}
    store = TossManualActivityAlertStore(db_session)
    try:
        assert await store.existing_alerted_ids(ids) == set()

        await store.record_alerts([_manual("rob866-a")])
        assert await store.existing_alerted_ids(ids) == {"rob866-a"}

        # Re-recording the same order must not raise and must not duplicate.
        await store.record_alerts([_manual("rob866-a")])
        assert await store.existing_alerted_ids(ids) == {"rob866-a"}
    finally:
        await db_session.execute(
            delete(TossManualActivityAlert).where(
                TossManualActivityAlert.broker_order_id.in_(ids)
            )
        )
        await db_session.commit()


# --------------------------------------------------------------------------- #
# MCP tool + TaskIQ task surface
# --------------------------------------------------------------------------- #


async def test_mcp_tool_reports_config_missing_when_toss_disabled(monkeypatch) -> None:
    from app.mcp_server.tooling import toss_manual_activity_tools as mod

    monkeypatch.setattr(
        mod, "validate_toss_api_config", lambda *a, **k: ["TOSS_API_ENABLED"]
    )

    res = await mod.toss_detect_manual_activity()

    assert res["success"] is False
    assert "TOSS_API_ENABLED" in res["missing_env"]


async def test_mcp_tool_passes_params_and_clamps_window(monkeypatch) -> None:
    from app.mcp_server.tooling import toss_manual_activity_tools as mod

    monkeypatch.setattr(mod, "validate_toss_api_config", lambda *a, **k: [])
    captured: dict[str, object] = {}

    async def fake_run(*, window_hours: int, dry_run: bool) -> dict[str, object]:
        captured["window_hours"] = window_hours
        captured["dry_run"] = dry_run
        return {"success": True}

    monkeypatch.setattr(mod, "run_manual_activity_sweep", fake_run)

    await mod.toss_detect_manual_activity(window_hours=9999, dry_run=False)

    assert captured["window_hours"] == 168  # clamped to the 7-day cap
    assert captured["dry_run"] is False


async def test_task_is_disabled_by_default(monkeypatch) -> None:
    from app.tasks import toss_manual_activity_tasks as mod

    monkeypatch.setattr(
        mod.settings, "toss_manual_activity_sweep_enabled", False, raising=False
    )

    called = False

    async def fake_run(**kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(mod, "run_manual_activity_sweep", fake_run)

    res = await mod.toss_manual_activity_sweep_task()

    assert res["status"] == "disabled"
    assert called is False


async def test_task_runs_execution_sweep_when_enabled(monkeypatch) -> None:
    from app.tasks import toss_manual_activity_tasks as mod

    monkeypatch.setattr(
        mod.settings, "toss_manual_activity_sweep_enabled", True, raising=False
    )
    captured: dict[str, object] = {}

    async def fake_run(*, window_hours: int, dry_run: bool) -> dict[str, object]:
        captured["window_hours"] = window_hours
        captured["dry_run"] = dry_run
        return {"manual_filled": [{}], "manual_open": [], "alerted_count": 1}

    monkeypatch.setattr(mod, "run_manual_activity_sweep", fake_run)

    res = await mod.toss_manual_activity_sweep_task(window_hours=12)

    assert captured["dry_run"] is False
    assert captured["window_hours"] == 12
    assert res["status"] == "ok"
    assert res["found"] == 1
    assert res["alerted"] == 1
