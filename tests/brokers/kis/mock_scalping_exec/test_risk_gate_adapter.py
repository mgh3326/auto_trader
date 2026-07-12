"""ROB-843 — KisMockRiskGate adapter + durable ledger snapshot loader.

The adapter must fail-close (raise) on any missing/stale live-market field so
the executor records ``risk_snapshot_unavailable`` and mutates zero times. The
durable loader must read authoritative per-symbol state from the mock ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping.contract import LedgerSnapshot


@dataclass
class _FakeState:
    bid: float | None = 70000.0
    ask: float | None = 70100.0
    _book_age: float | None = 1.0
    _spread: float | None = 14.0

    def book_age_seconds(self, *, now: float) -> float | None:
        return self._book_age

    def spread_bps(self) -> float | None:
        return self._spread


async def _stub_loader(*, symbol: str) -> LedgerSnapshot:
    return LedgerSnapshot(
        has_open_position_for_symbol=False,
        open_position_count=0,
        orders_today=0,
        realized_loss_today_krw=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )


def _gate(state: _FakeState | None):
    from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockRiskGate

    return KisMockRiskGate(
        get_state=lambda _s: state,
        clock=lambda: 100.0,
        snapshot_loader=_stub_loader,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_builds_market_conditions_from_live_state() -> None:
    gate = _gate(_FakeState())
    inputs = await gate.load(symbol="005930", side="BUY")
    assert inputs.market.spread_bps == Decimal("14.0")
    assert inputs.market.data_age_seconds == 1.0
    assert inputs.ledger.open_position_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_raises_when_no_state() -> None:
    gate = _gate(None)
    with pytest.raises(RuntimeError):
        await gate.load(symbol="005930", side="BUY")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        _FakeState(bid=None),
        _FakeState(ask=None),
        _FakeState(_book_age=None),
        _FakeState(_spread=None),
    ],
)
async def test_gate_raises_on_incomplete_market_snapshot(state) -> None:
    gate = _gate(state)
    with pytest.raises(RuntimeError):
        await gate.load(symbol="005930", side="BUY")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_snapshot_reads_durable_state(db_session) -> None:
    """Insert open + reconciled rows for a unique symbol; the loader reports
    symbol-scoped open position + last-close, and lenient global counts."""
    from app.mcp_server.tooling.kis_mock_ledger import _save_kis_mock_order_ledger
    from app.services.brokers.kis.mock_scalping_exec.ledger_state import (
        load_kis_mock_ledger_snapshot,
    )

    symbol = "900843"  # unique symbol to keep per-symbol asserts isolated

    await _save_kis_mock_order_ledger(
        symbol=symbol,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=1000.0,
        amount=1000.0,
        currency="KRW",
        order_no=f"ROB843-open-{symbol}",
        order_time=None,
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code=None,
        response_message=None,
        raw_response=None,
        reason="t",
        thesis=None,
        strategy=None,
        notes=None,
        lifecycle_state="fill",
        correlation_id=f"c-open-{symbol}",
    )
    await _save_kis_mock_order_ledger(
        symbol=symbol,
        instrument_type="equity_kr",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=980.0,
        amount=980.0,
        currency="KRW",
        order_no=f"ROB843-close-{symbol}",
        order_time=None,
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code=None,
        response_message=None,
        raw_response=None,
        reason="t",
        thesis=None,
        strategy=None,
        notes=None,
        lifecycle_state="reconciled",
        net_pnl=Decimal("-20"),
        correlation_id=f"c-close-{symbol}",
    )

    snap = await load_kis_mock_ledger_snapshot(symbol=symbol)

    assert snap.has_open_position_for_symbol is True
    assert snap.seconds_since_last_close_for_symbol is not None
    assert snap.open_position_count >= 1
    assert snap.orders_today >= 2
    assert snap.realized_loss_today_krw >= Decimal("20")
