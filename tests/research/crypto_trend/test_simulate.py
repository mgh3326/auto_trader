"""Sleeve simulator, controls and metrics on synthetic bars (no network).

Bar j of the window opens at T0 + j*BAR_MS. A decision at the close of bar j
(time T0 + (j+1)*BAR_MS) reads close j and close j-6 (one-day lookback), and
its market order fills at the open of bar j+1.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research.crypto_trend.metrics import max_drawdown, net_return, sharpe
from research.crypto_trend.signal import (
    BAR_MS,
    DAY_MS,
    entry_notional,
    ewma_annual_vol,
    last_completed_day_open,
)
from research.crypto_trend.simulate import (
    CAP_USD,
    FEE_RATE,
    GROSS_CAPITAL_USD,
    SymbolData,
    run_btc_long_only,
    run_candidate,
    run_cash,
    run_sleeve,
)

T0 = int(datetime(2024, 1, 10, tzinfo=UTC).timestamp() * 1000)
PRE_BARS = 10  # warm-up bars before the window, all at 100
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


def _data(
    closes: list[float],
    opens: list[float] | None = None,
    missing: frozenset[int] = frozenset(),
) -> SymbolData:
    closes_4h: dict[int, float] = {}
    opens_4h: dict[int, float] = {}
    for k in range(PRE_BARS):
        t = T0 - (PRE_BARS - k) * BAR_MS
        closes_4h[t] = 100.0
        opens_4h[t] = 100.0
    window_opens = opens if opens is not None else closes
    for j, (c, o) in enumerate(zip(closes, window_opens, strict=True)):
        if j in missing:
            continue
        closes_4h[T0 + j * BAR_MS] = c
        opens_4h[T0 + j * BAR_MS] = o
    # Daily closes alternate so the EWMA variance is positive everywhere.
    daily = {T0 + i * DAY_MS: 100.0 + (i % 3) for i in range(-60, 120)}
    return SymbolData(closes_4h=closes_4h, opens_4h=opens_4h, daily_closes=daily)


def _end(closes: list[float]) -> int:
    return T0 + len(closes) * BAR_MS


def _expected_notional(entry_decision_bar: int) -> float:
    """Notional a decision at the close of ``entry_decision_bar`` would size to."""
    t = T0 + (entry_decision_bar + 1) * BAR_MS
    daily = {T0 + i * DAY_MS: 100.0 + (i % 3) for i in range(-60, 120)}
    sigma = ewma_annual_vol(daily, last_completed_day_open(t))
    assert sigma is not None
    return entry_notional(CAP_USD, sigma)


FEE = FEE_RATE


@pytest.mark.unit
def test_entry_fills_at_the_open_of_the_bar_after_the_decision() -> None:
    closes = [100.0] * 6 + [120.0] * 4
    opens = [100.0] * 7 + [121.0] + [120.0] * 2  # open of bar 7 differs from close
    run = run_sleeve(
        "BTCUSDT",
        _data(closes, opens),
        1,
        long_only=False,
        window_start_ms=T0,
        window_end_ms=_end(closes),
    )
    assert run.entry_requests == 1 and run.entries_filled == 1
    (episode,) = run.episodes
    assert episode.direction == 1
    assert episode.entry_ms == T0 + 7 * BAR_MS
    assert episode.entry_px == pytest.approx(121.0)
    assert episode.exit_reason == "window_end_open"


@pytest.mark.unit
def test_flip_closes_the_old_leg_before_opening_the_new_one_at_the_same_open() -> None:
    # Strictly falling after the reversal: a flat run gives a zero six-bar
    # return, which is a flat signal, not a short signal.
    closes = [100.0] * 6 + [120.0] * 6 + [80.0 - 0.1 * i for i in range(10)]
    opens = list(closes)
    opens[7] = 121.0
    opens[13] = 79.0
    run = run_sleeve(
        "BTCUSDT",
        _data(closes, opens),
        1,
        long_only=False,
        window_start_ms=T0,
        window_end_ms=_end(closes),
    )
    closed, opened = run.episodes
    assert closed.direction == 1 and closed.exit_reason == "signal_reversal"
    assert closed.exit_ms == T0 + 13 * BAR_MS and closed.exit_px == pytest.approx(79.0)
    assert opened.direction == -1 and opened.exit_reason == "window_end_open"
    assert opened.entry_ms == T0 + 13 * BAR_MS
    assert opened.entry_px == pytest.approx(79.0)


@pytest.mark.unit
def test_round_trip_pnl_charges_both_taker_fees_on_the_real_notional() -> None:
    # Strictly falling after the reversal: a flat run gives a zero six-bar
    # return, which is a flat signal, not a short signal.
    closes = [100.0] * 6 + [120.0] * 6 + [80.0 - 0.1 * i for i in range(10)]
    opens = list(closes)
    opens[7] = 121.0
    opens[13] = 79.0
    run = run_sleeve(
        "BTCUSDT",
        _data(closes, opens),
        1,
        long_only=False,
        window_start_ms=T0,
        window_end_ms=_end(closes),
    )
    closed = run.episodes[0]
    notional = _expected_notional(6)  # the entry decision was at bar 6's close
    qty = notional / 121.0
    assert closed.notional_usd == pytest.approx(notional)
    assert notional <= CAP_USD + 1e-12
    gross = qty * (79.0 - 121.0)
    expected = gross - FEE * qty * 121.0 - FEE * qty * 79.0
    assert closed.pnl_usd == pytest.approx(expected, rel=1e-12)


@pytest.mark.unit
def test_open_leg_at_window_end_is_marked_with_its_exit_fee() -> None:
    closes = [100.0] * 6 + [120.0] * 4
    opens = [100.0] * 7 + [121.0] + [120.0] * 2
    run = run_sleeve(
        "BTCUSDT",
        _data(closes, opens),
        1,
        long_only=False,
        window_start_ms=T0,
        window_end_ms=_end(closes),
    )
    (episode,) = run.episodes
    qty = _expected_notional(6) / 121.0
    expected = qty * (120.0 - 121.0) - FEE * qty * 121.0 - FEE * qty * 120.0
    assert episode.pnl_usd == pytest.approx(expected, rel=1e-12)
    assert run.equity[-1] == pytest.approx(expected, rel=1e-12)


@pytest.mark.unit
def test_maximum_hold_forces_an_exit_and_a_later_signal_opens_a_new_episode() -> None:
    closes = [100.0] * 6 + [100.0 + 0.1 * i for i in range(1, 221)]
    run = run_sleeve(
        "BTCUSDT",
        _data(closes),
        1,
        long_only=False,
        window_start_ms=T0,
        window_end_ms=_end(closes),
    )
    first, second = run.episodes
    assert first.exit_reason == "max_hold"
    # Entry filled at bar 7; the 30-day hold is reached at the decision of bar
    # 186, so the exit fills at bar 187.
    assert first.entry_ms == T0 + 7 * BAR_MS
    assert first.exit_ms == T0 + 187 * BAR_MS
    assert second.exit_reason == "window_end_open"
    assert second.entry_ms == T0 + 188 * BAR_MS


@pytest.mark.unit
def test_missing_bar_is_no_signal_and_keeps_the_open_position() -> None:
    closes = [100.0] * 6 + [120.0 + i for i in range(20)]  # strictly rising
    run = run_sleeve(
        "BTCUSDT",
        _data(closes, missing=frozenset({10})),
        1,
        long_only=False,
        window_start_ms=T0,
        window_end_ms=_end(closes),
    )
    assert run.missing_grid_bars == 1
    assert run.no_signal >= 1
    assert [e.exit_reason for e in run.episodes] == ["window_end_open"]
    assert run.episodes[0].direction == 1


@pytest.mark.unit
def test_pending_entry_waits_for_the_next_present_bar_open() -> None:
    closes = [100.0] * 6 + [120.0] * 6
    run = run_sleeve(
        "BTCUSDT",
        _data(closes, missing=frozenset({7})),
        1,
        long_only=False,
        window_start_ms=T0,
        window_end_ms=_end(closes),
    )
    (episode,) = run.episodes
    assert episode.entry_ms == T0 + 8 * BAR_MS  # bar 7 absent: fill at bar 8 open
    assert episode.entry_px == pytest.approx(120.0)
    assert run.entry_requests == 1 and run.entries_filled == 1


@pytest.mark.unit
def test_long_only_control_ignores_the_short_side() -> None:
    closes = [100.0] * 6 + [80.0] * 10
    kwargs = {
        "window_start_ms": T0,
        "window_end_ms": _end(closes),
    }
    long_only = run_sleeve("BTCUSDT", _data(closes), 1, long_only=True, **kwargs)
    assert long_only.episodes == [] and long_only.entry_requests == 0
    both = run_sleeve("BTCUSDT", _data(closes), 1, long_only=False, **kwargs)
    (short,) = both.episodes
    assert short.direction == -1


@pytest.mark.unit
def test_candidate_respects_the_gross_and_concurrency_caps() -> None:
    closes = [100.0] * 6 + [120.0] * 10
    market = {symbol: _data(closes) for symbol in SYMBOLS}
    cfg = run_candidate(market, SYMBOLS, 1, T0, _end(closes))
    assert cfg.max_concurrent == 4
    assert cfg.max_gross_usd <= GROSS_CAPITAL_USD + 1e-9
    assert all(e.notional_usd <= CAP_USD + 1e-12 for e in cfg.episodes)

    btc = run_btc_long_only(market, 1, T0, _end(closes))
    assert btc.base_usd == GROSS_CAPITAL_USD
    assert {e.symbol for e in btc.episodes} == {"BTCUSDT"}


@pytest.mark.unit
def test_cash_control_is_flat_zero_return() -> None:
    cash = run_cash(T0, T0 + 10 * BAR_MS)
    assert cash.base_usd == GROSS_CAPITAL_USD
    assert net_return(cash.base_usd, cash.equity) == 0.0
    assert max_drawdown(cash.base_usd, cash.equity) == 0.0
    assert sharpe(cash.base_usd, cash.equity) is None


@pytest.mark.unit
def test_drawdown_and_net_return_on_a_small_equity_path() -> None:
    equity = [120.0, 60.0, 150.0]
    assert max_drawdown(100.0, equity) == pytest.approx(0.5)  # 120 -> 60
    assert net_return(100.0, equity) == pytest.approx(0.5)
    assert sharpe(100.0, [100.0, 100.0]) is None  # no variance
