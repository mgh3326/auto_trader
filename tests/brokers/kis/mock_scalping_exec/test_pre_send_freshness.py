"""ROB-843 P1-1 — final pre-send freshness re-check (immediately before POST).

The risk gate samples quote age BEFORE awaiting holdings/history, and the broker
awaits baseline/preflight before the POST. A book that was fresh at gate time
(age 59s) can be stale by send time (age 61s). The pre-send hook re-reads the
CURRENT MarketState right before the broker POST and blocks with ZERO POSTs.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.kis.mock_scalping.contract import (
    ReasonCode,
    ScalpingRiskLimits,
)
from app.services.brokers.kis.mock_scalping.order_intent import OrderIntent
from app.services.brokers.kis.mock_scalping_exec.adapters import (
    KisMockBroker,
    PreSendFreshnessError,
    assert_market_fresh_for_send,
)
from app.services.brokers.kis.mock_scalping_exec.executor import (
    MockScalpingExecutor,
    RiskInputs,
)
from app.services.brokers.kis.mock_scalping_ws.state import MarketState

LIMITS = ScalpingRiskLimits()  # max_data_age=60s, max_spread_bps=30


def _state(*, bid=70000.0, ask=70100.0, book_at: float | None = 100.0) -> MarketState:
    return MarketState(symbol="005930", bid=bid, ask=ask, _book_updated_at=book_at)


# --- validator ----------------------------------------------------------------


@pytest.mark.unit
def test_fresh_book_passes() -> None:
    assert_market_fresh_for_send(
        _state(),
        now=101.0,  # age 1s
        max_data_age_seconds=LIMITS.max_data_age_seconds,
        max_spread_bps=LIMITS.max_spread_bps,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "state,now,reason",
    [
        (_state(), 100.0 + 61, ReasonCode.STALE_DATA),  # age 61 > 60
        (_state(book_at=None), 101.0, "invalid_book_timestamp"),
        (_state(bid=70200.0, ask=70000.0), 101.0, "crossed_book"),
        (_state(bid=0.0), 101.0, "invalid_quote"),
        (_state(bid=math.nan), 101.0, "invalid_quote"),
        (_state(bid=70000.0, ask=71000.0), 101.0, ReasonCode.SPREAD_TOO_WIDE),  # ~142bp
        (None, 101.0, "no_market_state"),
    ],
)
def test_stale_or_invalid_book_fails(state, now, reason) -> None:
    with pytest.raises(PreSendFreshnessError) as ei:
        assert_market_fresh_for_send(
            state,
            now=now,
            max_data_age_seconds=LIMITS.max_data_age_seconds,
            max_spread_bps=LIMITS.max_spread_bps,
        )
    assert reason in ei.value.reason_codes


# --- broker wires a pre-send hook + shared correlation_id ---------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_buy_wires_pre_send_hook_and_correlation_id(monkeypatch) -> None:
    from app.services.brokers.kis.mock_scalping_exec import adapters

    captured: dict[str, Any] = {}

    async def _fake_place(**kwargs):
        captured.update(kwargs)
        return {"rt_cd": "0", "odno": "1"}

    monkeypatch.setattr(adapters, "_place_order_impl", _fake_place)

    # clock returns 161 at send -> age 61 for a book stamped at 100 -> stale.
    broker = KisMockBroker(
        get_state=lambda _s: _state(book_at=100.0), limits=LIMITS, clock=lambda: 161.0
    )
    await broker.submit_buy(
        symbol="005930",
        price=Decimal("70000"),
        quantity=Decimal("1"),
        correlation_id="cid-1",
        confirm=False,
    )
    assert captured["correlation_id"] == "cid-1"  # linked for P1-2 de-dup
    hook = captured["pre_send_hook"]
    with pytest.raises(PreSendFreshnessError):  # age 61 at send -> blocked
        await hook()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_buy_hook_passes_when_fresh(monkeypatch) -> None:
    from app.services.brokers.kis.mock_scalping_exec import adapters

    captured: dict[str, Any] = {}

    async def _fake_place(**kwargs):
        captured.update(kwargs)
        return {"rt_cd": "0", "odno": "1"}

    monkeypatch.setattr(adapters, "_place_order_impl", _fake_place)
    broker = KisMockBroker(
        get_state=lambda _s: _state(book_at=100.0), limits=LIMITS, clock=lambda: 101.0
    )
    await broker.submit_buy(
        symbol="005930",
        price=Decimal("70000"),
        quantity=Decimal("1"),
        correlation_id="cid-1",
        confirm=False,
    )
    await captured["pre_send_hook"]()  # age 1s -> no raise


# --- _execute_and_record aborts BEFORE the POST -------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_and_record_blocks_before_post(monkeypatch) -> None:
    """A raising pre-send hook returns pre_send_blocked and NEVER calls the POST."""
    from unittest.mock import AsyncMock

    from app.mcp_server.tooling import order_execution

    post_spy = AsyncMock(return_value={"rt_cd": "0", "odno": "1"})
    monkeypatch.setattr(order_execution, "_execute_order", post_spy)

    async def _raise() -> None:
        raise PreSendFreshnessError((ReasonCode.STALE_DATA,))

    result = await order_execution._execute_and_record(
        normalized_symbol="005930",
        side="buy",
        order_type="limit",
        order_quantity=1.0,
        price=70000.0,
        market_type="equity_kr",
        current_price=70000.0,
        avg_price=0.0,
        dry_run_result={"price": 70000, "quantity": 1, "estimated_value": 70000},
        order_amount=70000.0,
        reason="t",
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        defensive_trim_ctx=None,
        order_error_fn=lambda m: {"success": False, "error": m},
        is_mock=False,
        pre_send_hook=_raise,
    )
    assert result["pre_send_blocked"] is True
    assert ReasonCode.STALE_DATA in result["reason_codes"]
    assert post_spy.await_count == 0  # ZERO broker POSTs


# --- executor surfaces the block as a clean blocked round trip ----------------


class _BlockingBroker:
    def __init__(self):
        self.submitted: list[str] = []

    async def submit_buy(self, **kw):
        self.submitted.append("buy")
        return {
            "success": False,
            "pre_send_blocked": True,
            "reason_codes": [ReasonCode.STALE_DATA],
            "detail": "stale",
        }

    async def submit_exit_sell(self, **kw):
        self.submitted.append("sell")
        return {"kind": "sell"}

    async def confirm_fill(self, r):
        return None

    def quote(self, s):
        return None


class _NullLedger:
    async def record_entry(self, **kw):
        return None

    async def record_exit_reconciled(self, **kw):
        return None

    async def record_anomaly(self, **kw):
        return None


class _PassGate:
    async def load(self, *, symbol, side) -> RiskInputs:
        from app.services.brokers.kis.mock_scalping.contract import (
            LedgerSnapshot,
            MarketConditions,
        )

        return RiskInputs(
            ledger=LedgerSnapshot(False, 0, 0, Decimal("0"), None),
            market=MarketConditions(spread_bps=Decimal("10"), data_age_seconds=1.0),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_returns_blocked_on_pre_send_marker() -> None:
    async def _no_sleep(_s):
        return None

    broker = _BlockingBroker()
    ledger = _NullLedger()
    ex = MockScalpingExecutor(
        broker=broker,
        ledger=ledger,
        sleep=_no_sleep,
        clock=lambda: 0.0,
        risk=_PassGate(),
        limits=LIMITS,
    )
    intent = OrderIntent(
        symbol="005930",
        side="BUY",
        order_type="limit",
        target_notional_krw=Decimal("100000"),
        entry_reference_price=Decimal("70000"),
        tp_price=Decimal("70210"),
        sl_price=Decimal("69860"),
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )
    result = await ex.execute_monitored(intent, confirm=True)
    assert result.status == "blocked"
    assert ReasonCode.STALE_DATA in result.reason_codes
    assert broker.submitted == ["buy"]  # only the (aborted) submit; no sell/POST
