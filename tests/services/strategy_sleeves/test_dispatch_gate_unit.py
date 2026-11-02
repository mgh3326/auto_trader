"""ROB-1470 dispatch gate wrapper: lock order and fail-closed paths (no database)."""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.services.strategy_sleeves import dispatch_gate
from app.services.strategy_sleeves.gate import SleeveGateVerdict, SleeveVersionView
from app.services.strategy_sleeves.vocabulary import SleeveGateReason
from tests.services.strategy_sleeves.support import NOW, D

pytestmark = pytest.mark.unit


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def execute(self, statement, params=None):
        self.events.append(f"execute:{params['key']}")

    def begin_nested(self):
        events = self.events

        @contextlib.asynccontextmanager
        async def _savepoint():
            events.append("savepoint")
            try:
                yield
            except BaseException:
                events.append("rollback_savepoint")
                raise
            events.append("release_savepoint")

        return _savepoint()


def _group():
    return SimpleNamespace(
        id=1, proposal_id=uuid.uuid4(), sleeve_id=7, account_mode="kis_live"
    )


@pytest.mark.asyncio
async def test_the_lock_is_taken_before_the_measurement_savepoint(monkeypatch) -> None:
    events: list[str] = []

    async def measure(session, **_kw):
        events.append("measure")
        return SleeveGateVerdict(True, None), None

    monkeypatch.setattr(dispatch_gate, "_measure", measure)
    verdict = await dispatch_gate.evaluate_sleeve_for_dispatch(
        _Session(events), group=_group(), rungs=[], now=NOW
    )
    assert verdict.allowed is True
    assert events == [
        "execute:review.strategy_sleeves:7",
        "savepoint",
        "measure",
        "release_savepoint",
    ]


@pytest.mark.asyncio
async def test_a_measurement_error_is_unreadable_not_an_exception(monkeypatch) -> None:
    events: list[str] = []

    async def measure(session, **_kw):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(dispatch_gate, "_measure", measure)
    verdict = await dispatch_gate.evaluate_sleeve_for_dispatch(
        _Session(events), group=_group(), rungs=[], now=NOW
    )
    assert (verdict.allowed, verdict.reason) == (False, SleeveGateReason.UNREADABLE)
    assert "rollback_savepoint" in events


@pytest.mark.asyncio
async def test_a_failed_stop_write_keeps_the_loss_verdict(monkeypatch) -> None:
    view = SleeveVersionView(7, 2, "live", D("1000"), D("100"))
    loss = SleeveGateVerdict(
        False, SleeveGateReason.LOSS_LIMIT_REACHED, {}, stop_sleeve=True
    )

    async def measure(session, **_kw):
        return loss, view

    async def stop(session, **_kw):
        raise RuntimeError("version insert failed")

    monkeypatch.setattr(dispatch_gate, "_measure", measure)
    monkeypatch.setattr(dispatch_gate, "stop_at_loss_limit", stop)
    verdict = await dispatch_gate.evaluate_sleeve_for_dispatch(
        _Session([]), group=_group(), rungs=[], now=NOW
    )
    assert verdict is loss


@pytest.mark.asyncio
async def test_the_stop_is_written_only_for_a_loss_verdict(monkeypatch) -> None:
    view = SleeveVersionView(7, 2, "live", D("1000"), D("100"))
    stops: list[int] = []

    async def stop(session, *, expected, reason):
        stops.append(expected.version)

    monkeypatch.setattr(dispatch_gate, "stop_at_loss_limit", stop)
    for verdict, expected in (
        (SleeveGateVerdict(False, SleeveGateReason.BUDGET_EXCEEDED), []),
        (
            SleeveGateVerdict(
                False, SleeveGateReason.LOSS_LIMIT_REACHED, stop_sleeve=True
            ),
            [2],
        ),
    ):
        stops.clear()

        async def measure(session, _verdict=verdict, **_kw):
            return _verdict, view

        monkeypatch.setattr(dispatch_gate, "_measure", measure)
        await dispatch_gate.evaluate_sleeve_for_dispatch(
            _Session([]), group=_group(), rungs=[], now=NOW
        )
        assert stops == expected


def test_order_notional_counts_only_pending_buy_rungs() -> None:
    group = SimpleNamespace(id=1, symbol="AAA", action="place")
    rungs = [
        SimpleNamespace(
            id=1,
            state="pending_approval",
            side="buy",
            quantity=D("2"),
            limit_price=D("10"),
            notional=None,
        ),
        SimpleNamespace(
            id=2,
            state="voided",
            side="buy",
            quantity=D("9"),
            limit_price=D("10"),
            notional=None,
        ),
    ]
    assert dispatch_gate._order_buy_notional(group, rungs) == (True, D("20"))
    cancel = SimpleNamespace(id=1, symbol="AAA", action="cancel")
    assert dispatch_gate._order_buy_notional(cancel, rungs) == (False, D("0"))
    unpriced = [
        SimpleNamespace(
            id=3,
            state="pending_approval",
            side="buy",
            quantity=D("1"),
            limit_price=None,
            notional=None,
        )
    ]
    assert dispatch_gate._order_buy_notional(group, unpriced) == (True, None)
