"""Walk-forward / PIT lookahead boundary tests.

Injecting a future row into a decision context must go RED
(``LookaheadViolation``), not silently succeed.
"""

from __future__ import annotations

from datetime import date

import pytest
from pit import Bar, LookaheadViolation, assert_no_lookahead, bars_available_at


def _bar(symbol: str, session: str, close: int = 100) -> Bar:
    d = date.fromisoformat(session)
    return Bar(
        symbol=symbol,
        session_date=d,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000,
        trading_value=1_000_000,
        market="KOSPI",
        price_mode="adjusted",
        source_product="test",
    )


def test_bars_available_at_excludes_future_rows():
    bars = [
        _bar("A", "2023-01-02"),
        _bar("A", "2023-01-03"),
        _bar("A", "2023-01-04"),  # future relative to decision 01-03
    ]
    available = bars_available_at(bars, "2023-01-03")
    assert [b.session_date.isoformat() for b in available] == [
        "2023-01-02",
        "2023-01-03",
    ]


def test_assert_no_lookahead_passes_when_all_bars_le_decision():
    bars = [_bar("A", "2023-01-02"), _bar("A", "2023-01-03")]
    assert_no_lookahead(bars, "2023-01-03")  # no raise


def test_assert_no_lookahead_goes_red_on_future_row():
    """Future row injected into used set → LookaheadViolation (RED)."""
    decision = date(2023, 1, 3)
    used = [
        _bar("A", "2023-01-02"),
        _bar("A", "2023-01-03"),
        _bar("A", "2023-01-04"),  # future — must RED
    ]
    with pytest.raises(LookaheadViolation) as exc_info:
        assert_no_lookahead(used, decision)
    msg = str(exc_info.value)
    assert "2023-01-04" in msg
    assert "lookahead" in msg.lower()


def test_assert_no_lookahead_goes_red_on_far_future_row():
    used = [_bar("005930", "2024-06-01", close=999.0)]
    with pytest.raises(LookaheadViolation):
        assert_no_lookahead(used, "2023-01-03")


def test_decision_boundary_same_day_is_allowed():
    """Bar on the decision session itself is available (close-of-day model)."""
    used = [_bar("A", "2023-01-03")]
    assert_no_lookahead(used, "2023-01-03")
