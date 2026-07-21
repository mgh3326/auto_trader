"""ROB-993 — orchestrator control flow (no_signal / kill-switch / dry-run /
symbol-rejected / round-trip success), isolated from real HTTP and the
execution client's internals (already covered by the ROB-298 smoke-CLI
test suite this module's execution.py mirrors)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_strategy_loop import orchestrator
from app.services.brokers.binance.demo_strategy_loop.execution import (
    RoundTripBlocked,
    RoundTripResult,
)
from app.services.brokers.binance.demo_strategy_loop.kill_switch import (
    KillSwitchLimitsNotLocked,
    KillSwitchReasonCode,
    KillSwitchSnapshot,
    StrategyLoopKillSwitchLimits,
)
from app.services.brokers.binance.demo_strategy_loop.sizing import (
    LegNotionalCapNotLocked,
)
from app.services.brokers.binance.demo_strategy_loop.strategy import (
    NullStrategy,
    Signal,
)
from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.sizing import FuturesSizingBlocked


class _FakeLedger:
    def __init__(self) -> None:
        self.resolve_calls = 0

    async def resolve_or_create_instrument(self, **kwargs):
        self.resolve_calls += 1
        return 42


_NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
_SIGNAL = Signal(
    symbol="XRPUSDT",
    side="BUY",
    decision_ts=1_700_000_000_000,
    strategy_id="test-strategy",
    reason="unit test",
)


def _common_kwargs(**overrides):
    kwargs = {
        "strategy": NullStrategy(),
        "execution": object(),
        "ledger": _FakeLedger(),
        "session": object(),
        "market_client": object(),
        "venue_host": "demo-fapi.binance.com",
        "symbols": ("XRPUSDT", "DOGEUSDT", "SOLUSDT"),
        "cap_usdt": Decimal("10"),
        "leverage": 1,
        "kill_switch_limits": StrategyLoopKillSwitchLimits(),
        "now": _NOW,
        "confirm": False,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_run_tick_rejects_cap_usdt_above_locked_range_before_any_call(
    monkeypatch,
) -> None:
    """ROB-993 verify-993-2256.md Finding 1: cap_usdt must be a hard-locked
    invariant ([6,10]), not a value the caller can widen — and the rejection
    must happen before ANY network/DB call."""

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "no network/DB call is allowed before the cap invariant check"
        )

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _fail_if_called)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _fail_if_called)
    monkeypatch.setattr(orchestrator, "fetch_reference_price", _fail_if_called)
    monkeypatch.setattr(orchestrator, "collect_4h_bars", _fail_if_called)

    with pytest.raises(LegNotionalCapNotLocked):
        await orchestrator.run_tick(
            **_common_kwargs(
                signal_override=_SIGNAL, confirm=True, cap_usdt=Decimal("100")
            )
        )


@pytest.mark.asyncio
async def test_run_tick_rejects_cap_usdt_below_locked_range_before_any_call(
    monkeypatch,
) -> None:
    async def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "no network/DB call is allowed before the cap invariant check"
        )

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _fail_if_called)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _fail_if_called)

    with pytest.raises(LegNotionalCapNotLocked):
        await orchestrator.run_tick(
            **_common_kwargs(
                signal_override=_SIGNAL, confirm=True, cap_usdt=Decimal("5")
            )
        )


@pytest.mark.asyncio
async def test_run_tick_rejects_widened_kill_switch_limits_before_any_call(
    monkeypatch,
) -> None:
    """ROB-993 verify-993-2256.md Finding 1: max_concurrent_positions /
    max_consecutive_stop_losses_per_utc_day must be hard-locked, not caller
    tunable — rejection must happen before ANY network/DB call."""

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "no network/DB call is allowed before the kill-switch invariant check"
        )

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _fail_if_called)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _fail_if_called)

    with pytest.raises(KillSwitchLimitsNotLocked):
        await orchestrator.run_tick(
            **_common_kwargs(
                signal_override=_SIGNAL,
                confirm=True,
                kill_switch_limits=StrategyLoopKillSwitchLimits(
                    max_concurrent_positions=9,
                    max_consecutive_stop_losses_per_utc_day=999,
                ),
            )
        )


def test_assert_demo_only_accepts_futures_demo_host() -> None:
    orchestrator.assert_demo_only("demo-fapi.binance.com", "demo-fapi.binance.com")


def test_assert_demo_only_rejects_live_host() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        orchestrator.assert_demo_only("fapi.binance.com")


@pytest.mark.asyncio
async def test_run_tick_null_strategy_declines_to_signal(monkeypatch) -> None:
    from research.nautilus_scalping.rob974_features import Bar4h

    fake_bar = Bar4h(0, 14_400_000, 1.0, 2.0, 0.5, 1.5, 10.0, True)

    async def _fake_collect(market_client, symbols, *, minute_limit=500):
        return dict.fromkeys(symbols, (fake_bar,))

    monkeypatch.setattr(orchestrator, "collect_4h_bars", _fake_collect)

    outcome = await orchestrator.run_tick(
        **_common_kwargs(signal_override=None, confirm=True),
    )
    assert outcome.signal is None
    assert outcome.round_trip is None
    assert outcome.blocked_reason == "no_signal"
    assert outcome.decision_ts == fake_bar.close_ts


@pytest.mark.asyncio
async def test_run_tick_skips_strategy_when_symbols_have_mismatched_latest_bar(
    monkeypatch,
) -> None:
    """ROB-993 verify-993-2256.md Finding 5: the strategy must only be
    invoked when EVERY symbol in the universe has a complete 4h bar ending
    at the exact same decision_ts — a lagging symbol's stale bar must not
    be silently dropped from consideration by picking the freshest
    symbol's close_ts as decision_ts."""
    from research.nautilus_scalping.rob974_features import Bar4h

    fresh_bar = Bar4h(
        28_800_000, 28_800_000 + 14_400_000, 1.0, 2.0, 0.5, 1.5, 10.0, True
    )
    stale_bar = Bar4h(
        14_400_000, 14_400_000 + 14_400_000, 1.0, 2.0, 0.5, 1.5, 10.0, True
    )

    async def _fake_collect(market_client, symbols, *, minute_limit=500):
        return {
            "XRPUSDT": (fresh_bar,),
            "DOGEUSDT": (stale_bar,),  # lagging behind the other two symbols
            "SOLUSDT": (fresh_bar,),
        }

    monkeypatch.setattr(orchestrator, "collect_4h_bars", _fake_collect)

    called_with: list[int] = []

    class _RecordingStrategy:
        strategy_id = "recording"

        def evaluate(self, bars_4h_multi_symbol, *, decision_ts):
            called_with.append(decision_ts)
            return None

    outcome = await orchestrator.run_tick(
        **_common_kwargs(
            signal_override=None, confirm=True, strategy=_RecordingStrategy()
        )
    )
    assert called_with == [], "strategy must not be called on a mismatched bucket"
    assert outcome.signal is None
    assert outcome.round_trip is None
    assert outcome.blocked_reason == "missing_complete_4h_bar"


@pytest.mark.asyncio
async def test_run_tick_calls_strategy_when_all_symbols_share_latest_close_ts(
    monkeypatch,
) -> None:
    """Positive case: when every symbol's latest complete bar shares the
    same close_ts, the strategy IS invoked with that synchronized
    decision_ts."""
    from research.nautilus_scalping.rob974_features import Bar4h

    bar = Bar4h(28_800_000, 28_800_000 + 14_400_000, 1.0, 2.0, 0.5, 1.5, 10.0, True)

    async def _fake_collect(market_client, symbols, *, minute_limit=500):
        return dict.fromkeys(symbols, (bar,))

    monkeypatch.setattr(orchestrator, "collect_4h_bars", _fake_collect)

    called_with: list[int] = []

    class _RecordingStrategy:
        strategy_id = "recording"

        def evaluate(self, bars_4h_multi_symbol, *, decision_ts):
            called_with.append(decision_ts)
            return None

    outcome = await orchestrator.run_tick(
        **_common_kwargs(
            signal_override=None, confirm=True, strategy=_RecordingStrategy()
        )
    )
    assert called_with == [bar.close_ts]
    assert outcome.decision_ts == bar.close_ts
    assert outcome.blocked_reason == "no_signal"


@pytest.mark.asyncio
async def test_run_tick_symbol_rejected(monkeypatch) -> None:
    signal = Signal(
        symbol="BTCUSDT",  # excluded — MIN_NOTIONAL(50) > cap(10)
        side="BUY",
        decision_ts=1_700_000_000_000,
        strategy_id="test-strategy",
        reason="unit test",
    )
    outcome = await orchestrator.run_tick(
        **_common_kwargs(signal_override=signal, confirm=True)
    )
    assert outcome.signal is signal
    assert outcome.round_trip is None
    assert outcome.blocked_reason is not None
    assert outcome.blocked_reason.startswith("symbol_rejected:")


@pytest.mark.asyncio
async def test_run_tick_kill_switch_blocks_before_sizing(monkeypatch) -> None:
    async def _tripped_snapshot(ledger, *, strategy_loop_tag, now):
        return KillSwitchSnapshot(
            open_position_count=1, consecutive_stop_losses_today=0
        )

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _tripped_snapshot)

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("sizing must not run once the kill switch trips")

    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _fail_if_called)

    outcome = await orchestrator.run_tick(
        **_common_kwargs(
            signal_override=_SIGNAL,
            confirm=True,
            kill_switch_limits=StrategyLoopKillSwitchLimits(max_concurrent_positions=1),
        )
    )
    assert outcome.round_trip is None
    assert outcome.blocked_reason == (
        f"kill_switch:{KillSwitchReasonCode.MAX_CONCURRENT_POSITIONS_REACHED}"
    )


@pytest.mark.asyncio
async def test_run_tick_dry_run_stops_before_execution(monkeypatch) -> None:
    async def _clean_snapshot(ledger, *, strategy_loop_tag, now):
        return KillSwitchSnapshot(
            open_position_count=0, consecutive_stop_losses_today=0
        )

    async def _filters(client, symbol):
        return {
            "step_size": Decimal("0.1"),
            "min_notional": Decimal("5"),
            "quantity_precision": 1,
        }

    async def _price(client, symbol):
        return Decimal("0.5")

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("execute_signal_round_trip must not run in dry-run mode")

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _clean_snapshot)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _filters)
    monkeypatch.setattr(orchestrator, "fetch_reference_price", _price)
    monkeypatch.setattr(orchestrator, "execute_signal_round_trip", _fail_if_called)

    ledger = _FakeLedger()
    outcome = await orchestrator.run_tick(
        **_common_kwargs(signal_override=_SIGNAL, confirm=False, ledger=ledger)
    )
    assert outcome.blocked_reason == "dry_run"
    assert outcome.round_trip is None
    assert ledger.resolve_calls == 0  # instrument resolution is confirm-gated too


@pytest.mark.asyncio
async def test_run_tick_sizing_blocked(monkeypatch) -> None:
    async def _clean_snapshot(ledger, *, strategy_loop_tag, now):
        return KillSwitchSnapshot(
            open_position_count=0, consecutive_stop_losses_today=0
        )

    async def _filters(client, symbol):
        return {
            "step_size": Decimal("0.1"),
            "min_notional": Decimal("5"),
            "quantity_precision": 1,
        }

    async def _price(client, symbol):
        return Decimal("0.5")

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _clean_snapshot)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _filters)
    monkeypatch.setattr(orchestrator, "fetch_reference_price", _price)
    monkeypatch.setattr(
        orchestrator,
        "compute_futures_demo_order_qty",
        lambda **kwargs: FuturesSizingBlocked(reason="notional too small"),
    )

    outcome = await orchestrator.run_tick(
        **_common_kwargs(signal_override=_SIGNAL, confirm=True)
    )
    assert outcome.blocked_reason == "sizing_blocked:notional too small"


@pytest.mark.asyncio
async def test_run_tick_rejects_realized_notional_below_locked_floor(
    monkeypatch,
) -> None:
    """ROB-993 R2 adversarial review (verify-993-r2-2329.md, Finding 1):
    ``assert_leg_notional_cap_locked`` only validates the INPUT cap_usdt —
    it never re-checks the REALIZED notional after LOT_SIZE floor. With
    price=0.51, step=0.1, min_notional=5, cap=6 (a value inside the locked
    [6,10] range), the real (unmocked) sizing function floors to
    qty=11.7 / notional=5.967 — below the $6 floor. run_tick must refuse
    to proceed rather than submit an order smaller than the locked leg
    size."""

    async def _clean_snapshot(ledger, *, strategy_loop_tag, now):
        return KillSwitchSnapshot(
            open_position_count=0, consecutive_stop_losses_today=0
        )

    async def _filters(client, symbol):
        return {
            "step_size": Decimal("0.1"),
            "min_notional": Decimal("5"),
            "quantity_precision": 1,
        }

    async def _price(client, symbol):
        return Decimal("0.51")

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "execute_signal_round_trip must not run on an undersized order"
        )

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _clean_snapshot)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _filters)
    monkeypatch.setattr(orchestrator, "fetch_reference_price", _price)
    monkeypatch.setattr(orchestrator, "execute_signal_round_trip", _fail_if_called)

    outcome = await orchestrator.run_tick(
        **_common_kwargs(signal_override=_SIGNAL, confirm=True, cap_usdt=Decimal("6"))
    )
    assert outcome.round_trip is None
    assert outcome.blocked_reason is not None
    assert outcome.blocked_reason.startswith("sizing_blocked:")


@pytest.mark.asyncio
async def test_run_tick_round_trip_blocked_reports_exposure_slot_taken(
    monkeypatch,
) -> None:
    async def _clean_snapshot(ledger, *, strategy_loop_tag, now):
        return KillSwitchSnapshot(
            open_position_count=0, consecutive_stop_losses_today=0
        )

    async def _filters(client, symbol):
        return {
            "step_size": Decimal("0.1"),
            "min_notional": Decimal("5"),
            "quantity_precision": 1,
        }

    async def _price(client, symbol):
        return Decimal("0.5")

    async def _blocked(*args, **kwargs):
        raise RoundTripBlocked("exposure_slot_taken")

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _clean_snapshot)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _filters)
    monkeypatch.setattr(orchestrator, "fetch_reference_price", _price)
    monkeypatch.setattr(orchestrator, "execute_signal_round_trip", _blocked)

    outcome = await orchestrator.run_tick(
        **_common_kwargs(signal_override=_SIGNAL, confirm=True)
    )
    assert outcome.round_trip is None
    assert outcome.blocked_reason is not None
    assert outcome.blocked_reason.startswith("kill_switch:exposure_slot_taken")


@pytest.mark.asyncio
async def test_run_tick_success_path_saves_forecast(monkeypatch) -> None:
    async def _clean_snapshot(ledger, *, strategy_loop_tag, now):
        return KillSwitchSnapshot(
            open_position_count=0, consecutive_stop_losses_today=0
        )

    async def _filters(client, symbol):
        return {
            "step_size": Decimal("0.1"),
            "min_notional": Decimal("5"),
            "quantity_precision": 1,
        }

    async def _price(client, symbol):
        return Decimal("0.5")

    expected_round_trip = RoundTripResult(
        open_client_order_id="open-cid",
        close_client_order_id="close-cid",
        symbol="XRPUSDT",
        side="BUY",
        qty=Decimal("20"),
        reconciled=True,
    )

    async def _execute(**kwargs):
        return expected_round_trip

    forecast_calls: list[dict] = []

    async def _save_forecast(**kwargs):
        forecast_calls.append(kwargs)
        return True, None

    monkeypatch.setattr(orchestrator, "build_kill_switch_snapshot", _clean_snapshot)
    monkeypatch.setattr(orchestrator, "fetch_symbol_filters", _filters)
    monkeypatch.setattr(orchestrator, "fetch_reference_price", _price)
    monkeypatch.setattr(orchestrator, "execute_signal_round_trip", _execute)
    monkeypatch.setattr(orchestrator, "_save_forecast", _save_forecast)

    outcome = await orchestrator.run_tick(
        **_common_kwargs(signal_override=_SIGNAL, confirm=True)
    )
    assert outcome.round_trip is expected_round_trip
    assert outcome.forecast_saved is True
    assert len(forecast_calls) == 1
    assert forecast_calls[0]["signal"] is _SIGNAL


@pytest.mark.asyncio
async def test_save_forecast_writes_a_schema_valid_forecast_target() -> None:
    """Regression test for a bug caught during the ROB-993 live e2e smoke:
    ``forecast_target`` without a ``kind`` key raises
    ``ForecastValidationError("forecast_target.kind is required")``."""
    round_trip = RoundTripResult(
        open_client_order_id=f"rob-993-fc-test-open-{uuid.uuid4().hex[:8]}",
        close_client_order_id=f"rob-993-fc-test-close-{uuid.uuid4().hex[:8]}",
        symbol="XRPUSDT",
        side="BUY",
        qty=Decimal("9.1"),
        reconciled=True,
    )
    saved, error = await orchestrator._save_forecast(
        signal=_SIGNAL,
        correlation_id=f"rob-993-fc-test-correlation-{uuid.uuid4().hex[:8]}",
        round_trip=round_trip,
        now=_NOW,
    )
    assert error is None
    assert saved is True


def test_to_forecast_symbol_renders_upbit_style_quote_base() -> None:
    """Regression test for a bug caught during the ROB-993 live e2e smoke:
    passing a bare ``XRPUSDT`` symbol into ``save_forecast`` (crypto
    instrument type) got silently re-tagged as ``KRW-XRPUSDT`` by
    ``forecast_service._normalize_symbol`` (it treats any dash-less symbol
    as an Upbit base asset). The dash-separated ``USDT-XRP`` form is left
    untouched by that normalizer."""
    assert orchestrator._to_forecast_symbol("XRPUSDT") == "USDT-XRP"
    assert orchestrator._to_forecast_symbol("dogeusdt") == "USDT-DOGE"
    assert orchestrator._to_forecast_symbol("SOLUSDT") == "USDT-SOL"
