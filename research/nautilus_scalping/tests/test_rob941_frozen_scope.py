"""ROB-941 (AC2/AC3) — frozen universe + UTC half-open window, single source of truth.

Editing these constants after a manifest/campaign exists starts a new lineage; this
module IS the frozen scope other rob941_* modules and the manifest consult.
"""

import pytest
import rob941_frozen_scope as scope


def test_universe_is_exactly_four_symbols_in_frozen_order():
    assert scope.UNIVERSE == ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")


def test_window_iso_strings_are_frozen():
    assert scope.WINDOW_START_ISO == "2025-07-01T00:00:00Z"
    assert scope.WINDOW_END_ISO == "2026-07-01T00:00:00Z"


def test_window_is_half_open_utc_epoch_ms():
    # 2025-07-01T00:00:00Z and 2026-07-01T00:00:00Z, epoch ms
    assert scope.WINDOW_START_MS == 1751328000000
    assert scope.WINDOW_END_MS == 1782864000000
    assert scope.WINDOW_START_MS < scope.WINDOW_END_MS


def test_btc_is_historical_only_and_demo_ineligible_with_fixed_reason():
    e = scope.eligibility("BTCUSDT")
    assert e == {
        "historical_only": True,
        "demo_execution_eligible": False,
        "reason": "min_notional_50_exceeds_demo_cap_10",
    }


@pytest.mark.parametrize("symbol", ["XRPUSDT", "DOGEUSDT", "SOLUSDT"])
def test_non_btc_symbols_are_demo_eligible_with_no_reason(symbol):
    e = scope.eligibility(symbol)
    assert e == {
        "historical_only": False,
        "demo_execution_eligible": True,
        "reason": None,
    }


def test_eligibility_rejects_symbol_outside_frozen_universe():
    with pytest.raises(ValueError, match="ETHUSDT"):
        scope.eligibility("ETHUSDT")


def test_months_in_window_is_twelve_full_calendar_months():
    months = scope.months_in_window()
    assert months == [
        (2025, 7),
        (2025, 8),
        (2025, 9),
        (2025, 10),
        (2025, 11),
        (2025, 12),
        (2026, 1),
        (2026, 2),
        (2026, 3),
        (2026, 4),
        (2026, 5),
        (2026, 6),
    ]


def test_months_in_window_does_not_track_latest():
    # regression guard: the frozen window must never be computed from "now" —
    # it is a literal constant, not derived from datetime.now()/utcnow().
    import inspect

    src = inspect.getsource(scope)
    assert "datetime.now(" not in src
    assert "utcnow(" not in src
