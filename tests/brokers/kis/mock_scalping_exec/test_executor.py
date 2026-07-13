"""Monitored round-trip executor tests with fake broker/ledger (ROB-321 PR4a)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.kis.mock_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
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


class _PassRiskGate:
    """Permissive executor-owned risk gate (all guards clear)."""

    async def load(self, *, symbol: str, side: str) -> RiskInputs:
        return RiskInputs(
            ledger=LedgerSnapshot(
                has_open_position_for_symbol=False,
                open_position_count=0,
                orders_today=0,
                realized_loss_today_krw=Decimal("0"),
                seconds_since_last_close_for_symbol=None,
            ),
            market=MarketConditions(spread_bps=Decimal("10"), data_age_seconds=1.0),
        )


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
    def __init__(self, *, quotes, entry_fill, exit_fill):
        self._quotes = list(quotes)
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

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _executor(broker, ledger, *, risk=_PassRiskGate(), **cfg):
    async def _no_sleep(_s: float) -> None:
        return None

    return MockScalpingExecutor(
        broker=broker,
        ledger=ledger,
        config=ExecutorConfig(**cfg) if cfg else ExecutorConfig(),
        sleep=_no_sleep,
        clock=lambda: 0.0,  # frozen clock -> no time-stop unless max_hold=0
        risk=risk,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_submits_no_fill_and_no_ledger() -> None:
    broker = FakeBroker(
        quotes=[Quote(bid=Decimal("70300"), ask=None, last=None)],
        entry_fill=Fill(Decimal("70000"), Decimal("1")),
        exit_fill=Fill(Decimal("70300"), Decimal("1")),
    )
    ledger = FakeLedger()
    result = await _executor(broker, ledger).execute_monitored(_intent(), confirm=False)
    assert result.status == "dry_run"
    assert result.quantity == Decimal("1")
    assert ledger.calls == []  # dry-run writes nothing
    assert [s[0] for s in broker.submitted] == ["buy"]  # buy preview only


@pytest.mark.unit
@pytest.mark.asyncio
async def test_take_profit_round_trip_reconciles_with_pnl() -> None:
    broker = FakeBroker(
        quotes=[
            Quote(bid=Decimal("70000"), ask=None, last=None),  # poll 1: hold
            Quote(bid=Decimal("70300"), ask=None, last=None),  # poll 2: TP
        ],
        entry_fill=Fill(Decimal("70000"), Decimal("1")),
        exit_fill=Fill(Decimal("70300"), Decimal("1")),
    )
    ledger = FakeLedger()
    result = await _executor(broker, ledger).execute_monitored(_intent(), confirm=True)

    assert result.status == "reconciled"
    assert result.exit_reason == "take_profit"
    assert result.gross_pnl == Decimal("300")  # (70300-70000)*1
    assert result.net_pnl == result.gross_pnl - result.fees
    assert ledger.names() == ["entry", "exit_reconciled"]
    # exit submitted as scalping exit with the reason
    sell = next(kw for kind, kw in broker.submitted if kind == "sell")
    assert sell["exit_reason"] == "take_profit"
    assert sell["confirm"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_loss_round_trip_uses_scalping_exit() -> None:
    broker = FakeBroker(
        quotes=[Quote(bid=Decimal("69800"), ask=None, last=None)],  # <= sl
        entry_fill=Fill(Decimal("70000"), Decimal("1")),
        exit_fill=Fill(Decimal("69800"), Decimal("1")),
    )
    ledger = FakeLedger()
    result = await _executor(broker, ledger).execute_monitored(_intent(), confirm=True)
    assert result.status == "reconciled"
    assert result.exit_reason == "stop_loss"
    assert result.gross_pnl == Decimal("-200")  # loss
    assert ledger.names() == ["entry", "exit_reconciled"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_entry_unfilled_records_anomaly_no_exit() -> None:
    broker = FakeBroker(
        quotes=[Quote(bid=Decimal("70000"), ask=None, last=None)],
        entry_fill=None,  # never fills
        exit_fill=None,
    )
    ledger = FakeLedger()
    result = await _executor(broker, ledger, max_fill_polls=2).execute_monitored(
        _intent(), confirm=True
    )
    assert result.status == "entry_unfilled"
    assert ledger.names() == ["anomaly"]
    assert [s[0] for s in broker.submitted] == ["buy"]  # no sell attempted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exit_unconfirmed_records_anomaly_not_clean_success() -> None:
    broker = FakeBroker(
        quotes=[Quote(bid=Decimal("70300"), ask=None, last=None)],  # TP
        entry_fill=Fill(Decimal("70000"), Decimal("1")),
        exit_fill=None,  # exit never confirms
    )
    ledger = FakeLedger()
    result = await _executor(broker, ledger, max_fill_polls=2).execute_monitored(
        _intent(), confirm=True
    )
    assert result.status == "anomaly"
    assert "exit_unconfirmed" in result.reason_codes
    assert ledger.names() == ["entry", "anomaly"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_size_blocked() -> None:
    broker = FakeBroker(quotes=[], entry_fill=None, exit_fill=None)
    ledger = FakeLedger()
    result = await _executor(broker, ledger).execute_monitored(
        _intent(entry=None), confirm=True
    )
    assert result.status == "blocked"
    assert broker.submitted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_time_stop_when_quote_never_triggers() -> None:
    broker = FakeBroker(
        quotes=[Quote(bid=Decimal("70000"), ask=None, last=None)],  # always hold range
        entry_fill=Fill(Decimal("70000"), Decimal("1")),
        exit_fill=Fill(Decimal("70000"), Decimal("1")),
    )
    ledger = FakeLedger()
    # max_hold_seconds=0 with frozen clock -> immediate time-stop
    result = await _executor(broker, ledger, max_hold_seconds=0.0).execute_monitored(
        _intent(), confirm=True
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "time_stop"
