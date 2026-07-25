"""ROB-843 — KisMockRiskGate adapter + order-history loader.

The gate must:
* build position (has-open / count) from a fresh **holdings** snapshot, never
  from order lifecycle rows;
* fail-close (raise) on any malformed live-market field — missing/stale
  timestamp, non-positive/NaN/Inf bid or ask, or a crossed book (ask < bid);
* read daily order count / realized loss / cooldown from the mock order ledger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.kis.mock_scalping.contract import (
    MarketConditions,
    ScalpingRiskLimits,
    evaluate_risk,
)
from app.services.brokers.kis.mock_scalping_exec.ledger_state import MockOrderHistory


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


async def _empty_holdings() -> dict[str, Any]:
    return {"holdings": [], "cash": {}}


def _holdings(*entries: tuple[str, str]) -> Any:
    async def _provider() -> dict[str, Any]:
        return {"holdings": [{"pdno": s, "hldg_qty": q} for s, q in entries]}

    return _provider


async def _no_history(*, symbol: str) -> MockOrderHistory:
    return MockOrderHistory(
        orders_today=0,
        realized_loss_today_krw=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )


def _gate(state, *, holdings=_empty_holdings, history=_no_history):
    from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockRiskGate

    return KisMockRiskGate(
        get_state=lambda _s: state,
        holdings_provider=holdings,
        clock=lambda: 100.0,
        order_history_loader=history,
    )


# (The durable reservation fail-close is enforced inside the real order-history
# loader and is exercised end-to-end in test_order_history_ledger.py.)


# --- Market snapshot fail-close (Blocker 3) -----------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_builds_market_conditions_from_live_state() -> None:
    inputs = await _gate(_FakeState()).load(symbol="005930", side="BUY")
    assert inputs.market.spread_bps == Decimal("14.0")
    assert inputs.market.data_age_seconds == 1.0
    assert inputs.ledger.open_position_count == 0
    assert inputs.ledger.has_open_position_for_symbol is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_raises_when_no_state() -> None:
    with pytest.raises(RuntimeError):
        await _gate(None).load(symbol="005930", side="BUY")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        _FakeState(bid=None),
        _FakeState(ask=None),
        _FakeState(_book_age=None),
        _FakeState(_spread=None),
        _FakeState(bid=0.0),  # non-positive
        _FakeState(ask=-5.0),  # non-positive
        _FakeState(bid=math.nan),  # NaN
        _FakeState(ask=math.inf),  # Inf
        _FakeState(_book_age=math.nan),  # NaN timestamp age
        _FakeState(_book_age=-1.0),  # negative age
        _FakeState(bid=70200.0, ask=70000.0),  # crossed book (ask < bid)
    ],
)
async def test_gate_fail_closes_on_malformed_market(state) -> None:
    with pytest.raises(RuntimeError):
        await _gate(state).load(symbol="005930", side="BUY")


@pytest.mark.unit
def test_crossed_book_would_pass_evaluate_risk_as_negative_spread() -> None:
    """Documents why the gate must reject a crossed book BEFORE evaluate_risk:
    a negative spread slips past the SPREAD_TOO_WIDE guard."""
    from app.services.brokers.kis.mock_scalping.contract import LedgerSnapshot

    decision = evaluate_risk(
        symbol="005930",
        side="BUY",
        target_notional_krw=Decimal("100000"),
        limits=ScalpingRiskLimits(),
        ledger=LedgerSnapshot(
            has_open_position_for_symbol=False,
            open_position_count=0,
            orders_today=0,
            realized_loss_today_krw=Decimal("0"),
            seconds_since_last_close_for_symbol=None,
        ),
        market=MarketConditions(spread_bps=Decimal("-50"), data_age_seconds=1.0),
    )
    assert decision.allowed  # negative spread is NOT caught -> gate must reject


# --- Position from holdings, not order lifecycle (Blocker 2) -------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_held_position_from_holdings_blocks_reentry() -> None:
    gate = _gate(_FakeState(), holdings=_holdings(("005930", "3"), ("000660", "1")))
    inputs = await gate.load(symbol="005930", side="BUY")
    assert inputs.ledger.has_open_position_for_symbol is True
    assert inputs.ledger.open_position_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flat_after_roundtrip_allows_reentry() -> None:
    """Holdings empty (scalping entry+exit closed flat) -> no open position."""
    gate = _gate(_FakeState(), holdings=_empty_holdings)
    inputs = await gate.load(symbol="005930", side="BUY")
    assert inputs.ledger.has_open_position_for_symbol is False
    assert inputs.ledger.open_position_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_qty_holding_is_not_a_position() -> None:
    gate = _gate(_FakeState(), holdings=_holdings(("005930", "0")))
    inputs = await gate.load(symbol="005930", side="BUY")
    assert inputs.ledger.has_open_position_for_symbol is False
    assert inputs.ledger.open_position_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holdings_read_failure_fail_closes() -> None:
    async def _boom() -> dict[str, Any]:
        raise RuntimeError("balance read failed")

    with pytest.raises(RuntimeError):
        await _gate(_FakeState(), holdings=_boom).load(symbol="005930", side="BUY")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cooldown_and_daily_counters_come_from_order_history() -> None:
    async def _hist(*, symbol: str) -> MockOrderHistory:
        return MockOrderHistory(
            orders_today=4,
            realized_loss_today_krw=Decimal("1000"),
            seconds_since_last_close_for_symbol=42.0,
        )

    gate = _gate(_FakeState(), history=_hist)
    inputs = await gate.load(symbol="005930", side="BUY")
    assert inputs.ledger.orders_today == 4
    assert inputs.ledger.realized_loss_today_krw == Decimal("1000")
    assert inputs.ledger.seconds_since_last_close_for_symbol == 42.0


# --- Order-history loader reads durable order ledger (Blocker 2) ---------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_order_history_loader_reads_ledger(db_session) -> None:
    """A reconciled loss row for a unique symbol drives cooldown basis + realized
    loss; open-order rows do NOT become positions (position isn't read here)."""
    from sqlalchemy import delete

    from app.mcp_server.tooling.kis_mock_ledger import (
        _order_session_factory,
        _save_kis_mock_order_ledger,
    )
    from app.models.review import KISMockOrderLedger, OrderSendIntent
    from app.services.brokers.kis.mock_scalping_exec.ledger_state import (
        load_kis_mock_order_history,
    )
    from app.services.order_send_intent_service import KIS_MOCK_SCALPING_SCOPE

    symbol = "900843"
    # Deterministic across re-runs of the persistent shared test DB (clear any
    # prior-run rows and any stray scalping reservation that would fail-close).
    async with _order_session_factory()() as _db:
        await _db.execute(
            delete(KISMockOrderLedger).where(KISMockOrderLedger.symbol == symbol)
        )
        await _db.execute(
            delete(OrderSendIntent).where(
                OrderSendIntent.account_scope == KIS_MOCK_SCALPING_SCOPE
            )
        )
        await _db.commit()
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

    hist = await load_kis_mock_order_history(symbol=symbol)
    assert hist.orders_today >= 2
    assert hist.realized_loss_today_krw >= Decimal("20")
    assert hist.seconds_since_last_close_for_symbol is not None
