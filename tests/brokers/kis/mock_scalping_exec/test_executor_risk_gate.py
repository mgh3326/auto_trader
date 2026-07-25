"""ROB-843 — executor-owned final risk re-check (WS scalping).

The monitored executor must reload a fresh ledger/market snapshot and run
``evaluate_risk`` immediately before any broker submit. Every risk denial and
every snapshot load/parse/freshness failure fail-closes to ZERO broker calls.
Caller-computed risk is advisory only — the executor never trusts it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.kis.mock_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
    ReasonCode,
    ScalpingRiskLimits,
)
from app.services.brokers.kis.mock_scalping.order_intent import OrderIntent
from app.services.brokers.kis.mock_scalping_exec.executor import (
    ExecutorConfig,
    Fill,
    MockScalpingExecutor,
    Quote,
    RiskInputs,
)

SYMBOL = "005930"


def _intent(entry: Decimal | None = Decimal("70000")) -> OrderIntent:
    return OrderIntent(
        symbol=SYMBOL,
        side="BUY",
        order_type="limit",
        target_notional_krw=Decimal("100000"),
        entry_reference_price=entry,
        tp_price=Decimal("70210"),
        sl_price=Decimal("69860"),
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )


class FakeBroker:
    def __init__(self, *, quotes=None, entry_fill=None, exit_fill=None):
        self._quotes = list(quotes or [])
        self._i = 0
        self.entry_fill = entry_fill
        self.exit_fill = exit_fill
        self.submitted: list[tuple[str, dict]] = []

    async def submit_buy(self, **kw: Any) -> dict:
        self.submitted.append(("buy", kw))
        return {"kind": "buy", **kw}

    async def submit_exit_sell(self, **kw: Any) -> dict:
        self.submitted.append(("sell", kw))
        return {"kind": "sell", **kw}

    async def confirm_fill(self, submit_result: dict) -> Fill | None:
        return self.entry_fill if submit_result["kind"] == "buy" else self.exit_fill

    def quote(self, symbol: str) -> Quote | None:
        if not self._quotes:
            return None
        q = self._quotes[min(self._i, len(self._quotes) - 1)]
        self._i += 1
        return q


class FakeLedger:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def record_entry(self, **kw: Any) -> None:
        self.calls.append(("entry", kw))

    async def record_exit_reconciled(self, **kw: Any) -> None:
        self.calls.append(("exit_reconciled", kw))

    async def record_anomaly(self, **kw: Any) -> None:
        self.calls.append(("anomaly", kw))


class FakeRiskGate:
    """Executor-owned snapshot provider. Records every load call."""

    def __init__(
        self, *, inputs: RiskInputs | None = None, raises: Exception | None = None
    ):
        self._inputs = inputs
        self._raises = raises
        self.calls = 0

    async def load(self, *, symbol: str, side: str) -> RiskInputs:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._inputs is not None
        return self._inputs


def _pass_inputs(
    *,
    has_open=False,
    open_count=0,
    orders_today=0,
    realized_loss=Decimal("0"),
    since_close: float | None = None,
    spread_bps=Decimal("10"),
    data_age=1.0,
) -> RiskInputs:
    return RiskInputs(
        ledger=LedgerSnapshot(
            has_open_position_for_symbol=has_open,
            open_position_count=open_count,
            orders_today=orders_today,
            realized_loss_today_krw=realized_loss,
            seconds_since_last_close_for_symbol=since_close,
        ),
        market=MarketConditions(spread_bps=spread_bps, data_age_seconds=data_age),
    )


def _executor(broker, ledger, *, risk=None, limits=None, **cfg):
    async def _no_sleep(_s: float) -> None:
        return None

    return MockScalpingExecutor(
        broker=broker,
        ledger=ledger,
        config=ExecutorConfig(**cfg) if cfg else ExecutorConfig(),
        sleep=_no_sleep,
        clock=lambda: 0.0,
        risk=risk,
        limits=limits or ScalpingRiskLimits(),
    )


# --- Five risk denials each place ZERO broker orders (AC1) --------------------

_DENIALS = [
    pytest.param(
        {"realized_loss": Decimal("50000")},
        ReasonCode.DAILY_LOSS_BUDGET_EXHAUSTED,
        id="daily_loss",
    ),
    pytest.param(
        {"open_count": 1},
        ReasonCode.MAX_OPEN_POSITIONS_REACHED,
        id="position_cap",
    ),
    pytest.param(
        {"since_close": 10.0},
        ReasonCode.COOLDOWN_ACTIVE,
        id="cooldown",
    ),
    pytest.param(
        {"data_age": 120.0},
        ReasonCode.STALE_DATA,
        id="stale_quote",
    ),
    pytest.param(
        {"spread_bps": Decimal("50")},
        ReasonCode.SPREAD_TOO_WIDE,
        id="wide_spread",
    ),
]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,reason", _DENIALS)
async def test_risk_denial_places_zero_broker_orders(overrides, reason) -> None:
    broker = FakeBroker()
    ledger = FakeLedger()
    gate = FakeRiskGate(inputs=_pass_inputs(**overrides))
    result = await _executor(broker, ledger, risk=gate).execute_monitored(
        _intent(), confirm=True
    )
    assert result.status == "blocked"
    assert reason in result.reason_codes
    assert broker.submitted == []  # zero broker calls
    assert ledger.calls == []
    assert gate.calls == 1  # executor loaded its own fresh snapshot


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_unavailable_fail_closes() -> None:
    broker = FakeBroker()
    ledger = FakeLedger()
    gate = FakeRiskGate(raises=RuntimeError("db down"))
    result = await _executor(broker, ledger, risk=gate).execute_monitored(
        _intent(), confirm=True
    )
    assert result.status == "blocked"
    assert result.reason_codes == ("risk_snapshot_unavailable",)
    assert broker.submitted == []
    assert result.detail  # exception detail preserved


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_without_risk_gate_fail_closes() -> None:
    """A confirm-mode executor with no wired risk gate cannot mutate."""
    broker = FakeBroker()
    ledger = FakeLedger()
    result = await _executor(broker, ledger, risk=None).execute_monitored(
        _intent(), confirm=True
    )
    assert result.status == "blocked"
    assert result.reason_codes == ("risk_gate_unconfigured",)
    assert broker.submitted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_pass_proceeds_to_reconciled_round_trip() -> None:
    broker = FakeBroker(
        quotes=[
            Quote(bid=Decimal("70000"), ask=None, last=None),
            Quote(bid=Decimal("70300"), ask=None, last=None),
        ],
        entry_fill=Fill(Decimal("70000"), Decimal("1")),
        exit_fill=Fill(Decimal("70300"), Decimal("1")),
    )
    ledger = FakeLedger()
    gate = FakeRiskGate(inputs=_pass_inputs())
    result = await _executor(broker, ledger, risk=gate).execute_monitored(
        _intent(), confirm=True
    )
    assert result.status == "reconciled"
    assert [s[0] for s in broker.submitted] == ["buy", "sell"]
    assert gate.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_with_gate_denial_previews_nothing() -> None:
    broker = FakeBroker()
    ledger = FakeLedger()
    gate = FakeRiskGate(inputs=_pass_inputs(spread_bps=Decimal("50")))
    result = await _executor(broker, ledger, risk=gate).execute_monitored(
        _intent(), confirm=False
    )
    assert result.status == "blocked"
    assert ReasonCode.SPREAD_TOO_WIDE in result.reason_codes
    assert broker.submitted == []  # not even a dry-run preview


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_without_gate_still_previews() -> None:
    """Legacy dry-run with no gate keeps previewing (no mutation happens)."""
    broker = FakeBroker(quotes=[Quote(bid=Decimal("70300"), ask=None, last=None)])
    ledger = FakeLedger()
    result = await _executor(broker, ledger, risk=None).execute_monitored(
        _intent(), confirm=False
    )
    assert result.status == "dry_run"
    assert [s[0] for s in broker.submitted] == ["buy"]
