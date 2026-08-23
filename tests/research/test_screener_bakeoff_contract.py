"""Contract tests for research/screener_bakeoff.

Two things must hold for the bakeoff report to mean anything:

1. **No look-ahead.**  Every candidate list and every indicator must be a pure
   function of rows dated at or before the decision date.
2. **Indicator fidelity.**  The reconstructed RSI / bollinger / fibonacci /
   clustering must match the production functions the live screener and the
   live buy gate actually use. Live ``tv_rsi45`` comparison was withdrawn;
   these helpers remain assets for prospective scoring.

These tests are pure: no database, no network.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from research.screener_bakeoff import indicators as ind
from research.screener_bakeoff import scoring
from research.screener_bakeoff.panel import PricePanel
from research.screener_bakeoff.sources import evaluate_gate


def _panel(n: int = 60) -> PricePanel:
    rng = np.random.default_rng(7)
    days = np.array([dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(n)])
    close = 100 + np.cumsum(rng.normal(0, 1.5, n))
    high = close + rng.uniform(0.2, 2.0, n)
    low = close - rng.uniform(0.2, 2.0, n)
    vol = rng.uniform(1e5, 1e6, n)
    return PricePanel(
        "kr", {"X": days}, {"X": close}, {"X": high}, {"X": low}, {"X": vol}, days, True
    )


# --------------------------------------------------------------------------
# 1. look-ahead
# --------------------------------------------------------------------------


def test_window_never_returns_a_bar_after_the_decision_date():
    p = _panel()
    day = p.calendar[40]
    high, low, close, vol = p.window("X", day, 200)
    assert close.size == 41
    assert float(close[-1]) == pytest.approx(float(p.close["X"][40]))
    # the 41-bar slice is exactly bars 0..40; nothing from 41.. leaked in
    assert np.array_equal(close, p.close["X"][:41])


def test_gate_evidence_is_invariant_to_future_bars():
    """Truncating the panel after the decision date must not change the gate."""
    full = _panel()
    day = full.calendar[40]
    before = evaluate_gate(full, "X", day, 8.0)

    cut = PricePanel(
        "kr",
        {"X": full.dates["X"][:41]},
        {"X": full.close["X"][:41]},
        {"X": full.high["X"][:41]},
        {"X": full.low["X"][:41]},
        {"X": full.volume["X"][:41]},
        full.calendar[:41],
        True,
    )
    after = evaluate_gate(cut, "X", day, 8.0)
    assert before == after


def test_scoring_excludes_the_entry_bar_and_stops_at_the_horizon():
    p = _panel()
    day = p.calendar[10]
    out = scoring.score(p, "X", day, 5)
    entry = float(p.close["X"][10])
    fwd = p.close["X"][11:16]
    assert out.status == "full"
    assert out.entry == pytest.approx(entry)
    assert out.ret == pytest.approx(float(fwd[-1] / entry - 1))
    assert out.mfe == pytest.approx(float(fwd.max() / entry - 1))
    assert out.mae == pytest.approx(float(fwd.min() / entry - 1))
    # bar 16 and beyond must not influence anything
    p.close["X"][16:] = 1e9
    p.high["X"][16:] = 1e9
    again = scoring.score(p, "X", day, 5)
    assert again.ret == pytest.approx(out.ret)
    assert again.ret_hl_mfe == pytest.approx(out.ret_hl_mfe)


def test_scoring_flags_truncation_instead_of_silently_shortening():
    p = _panel(n=20)
    day = p.calendar[17]
    out = scoring.score(p, "X", day, 5)
    assert out.status == "truncated"
    assert out.bars_used == 2


# --------------------------------------------------------------------------
# 2. fidelity against the production indicator functions
# --------------------------------------------------------------------------


def _frame(p: PricePanel, upto: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [d.isoformat() for d in p.dates["X"][: upto + 1]],
            "open": p.close["X"][: upto + 1],
            "high": p.high["X"][: upto + 1],
            "low": p.low["X"][: upto + 1],
            "close": p.close["X"][: upto + 1],
            "volume": p.volume["X"][: upto + 1],
        }
    )


def test_rsi_matches_production():
    from app.mcp_server.tooling.market_data_indicators import _calculate_rsi

    p = _panel()
    frame = _frame(p, 50)
    expected = _calculate_rsi(frame["close"])["14"]
    assert ind.rsi_wilder(frame["close"].to_numpy(dtype=float)) == pytest.approx(
        expected
    )


def test_rsi_series_matches_production_prefix_calls():
    """Each series index equals production ``_calculate_rsi`` on close[:i+1]."""
    from app.mcp_server.tooling.market_data_indicators import _calculate_rsi

    p = _panel()
    close = p.close["X"]
    series = ind.rsi_wilder_series(close)
    for i in range(14, close.size):
        expected = _calculate_rsi(pd.Series(close[: i + 1]))["14"]
        if expected is None:
            assert np.isnan(series[i])
        else:
            assert series[i] == pytest.approx(expected)
    # last value also matches the scalar helper
    assert series[-1] == pytest.approx(ind.rsi_wilder(close))


def test_bollinger_matches_production():
    from app.mcp_server.tooling.market_data_indicators import _calculate_bollinger

    p = _panel()
    close = p.close["X"]
    expected = _calculate_bollinger(pd.Series(close))
    upper, middle, lower = ind.bollinger(close)
    assert upper == pytest.approx(expected["upper"])
    assert middle == pytest.approx(expected["middle"])
    assert lower == pytest.approx(expected["lower"])


def test_fibonacci_levels_match_production():
    from app.mcp_server.tooling.market_data_indicators import _calculate_fibonacci

    p = _panel()
    frame = _frame(p, 50)
    current = float(frame["close"].iloc[-1])
    expected = _calculate_fibonacci(frame, current)["levels"]
    got = ind.fibonacci_levels(
        frame["high"].to_numpy(dtype=float), frame["low"].to_numpy(dtype=float)
    )
    assert {str(k): v for k, v in got.items()} == expected


def test_support_resistance_matches_production_quick_builder():
    from app.mcp_server.tooling.analysis_quick import _build_support_resistance

    p = _panel()
    frame = _frame(p, 50)
    current = float(frame["close"].iloc[-1])
    exp_sup, exp_res = _build_support_resistance(frame, current)
    got_sup, got_res = ind.support_resistance(
        frame["high"].to_numpy(dtype=float),
        frame["low"].to_numpy(dtype=float),
        frame["close"].to_numpy(dtype=float),
        frame["volume"].to_numpy(dtype=float),
        current,
    )
    assert [(s["price"], s["strength"], s["sources"]) for s in got_sup] == [
        (s["price"], s["strength"], s["sources"]) for s in exp_sup
    ]
    assert [(r["price"], r["strength"], r["sources"]) for r in got_res] == [
        (r["price"], r["strength"], r["sources"]) for r in exp_res
    ]


def test_support_family_mapping_matches_production_fanout_aliases():
    """Pin research aliases to the live fanout ``_support_family`` helper."""
    from app.mcp_server.tooling.buy_candidate_fanout import _support_family

    samples = [
        "fib_38.2",
        "fib_50",
        "bb_lower",
        "volume_poc",
        "volume_profile_va",
        "bb_upper",
        "bb_middle",
    ]
    production = {src: _support_family(src) for src in samples}
    research = {}
    for src in samples:
        families = ind.source_families([src])
        research[src] = next(iter(families)) if families else None
    assert research == production
    assert ind.source_families(["fib_38.2", "bb_lower", "volume_poc"]) == {
        "fib",
        "bb_lower",
        "volume_profile",
    }
    assert ind.source_families(["bb_upper", "bb_middle"]) == set()
