"""ROB-942 R1 M1 fix — non-finite (NaN/+Inf/-Inf) input fail-closed RED fixtures.

R1 verifier finding M1: ``SignalEvent.__post_init__``'s ``<= 0`` checks let NaN
through (``nan <= 0`` is ``False``), and ``Bar1m``/``CostScenario``/
``FundingCrossing`` had no validation at all, so a non-finite value could ride
all the way to ``TradeRecord``/``ledger_hash`` before failing (if it failed at
all). This module pins that every non-finite entry point raises ``ValueError``
at construction/call time, BEFORE any ledger (TradeRecord) is built — not
deferred to hashing. Existing "price must be positive" / "distance must be
positive" contracts are preserved unchanged; this only closes the NaN/Inf gap.
"""

import math

import pytest
import rob940_cost_model as cm
from rob940_bars_agg import Bar1m
from rob940_engine import SignalEvent

_NONFINITE = (math.nan, math.inf, -math.inf)


# --------------------------------------------------------------------------- #
# SignalEvent: sl_distance_bps / tp_distance_bps / tp_target_price
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", _NONFINITE)
def test_signal_event_rejects_nonfinite_sl_distance_bps(bad):
    with pytest.raises(ValueError):
        SignalEvent(
            strategy="s",
            config_id="c",
            symbol="X",
            signal_ts=0,
            side="long",
            sl_distance_bps=bad,
            tp_distance_bps=100.0,
        )


@pytest.mark.parametrize("bad", _NONFINITE)
def test_signal_event_rejects_nonfinite_tp_distance_bps(bad):
    with pytest.raises(ValueError):
        SignalEvent(
            strategy="s",
            config_id="c",
            symbol="X",
            signal_ts=0,
            side="long",
            sl_distance_bps=50.0,
            tp_distance_bps=bad,
        )


@pytest.mark.parametrize("bad", _NONFINITE)
def test_signal_event_rejects_nonfinite_tp_target_price(bad):
    with pytest.raises(ValueError):
        SignalEvent(
            strategy="s",
            config_id="c",
            symbol="X",
            signal_ts=0,
            side="long",
            sl_distance_bps=50.0,
            tp_target_price=bad,
        )


# --------------------------------------------------------------------------- #
# Bar1m OHLCV
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", _NONFINITE)
@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_bar1m_rejects_nonfinite_ohlcv_field(field, bad):
    kwargs = {
        "ts": 0,
        "open": 100.0,
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
        "volume": 1.0,
    }
    kwargs[field] = bad
    with pytest.raises(ValueError):
        Bar1m(**kwargs)


# --------------------------------------------------------------------------- #
# CostScenario / FundingCrossing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", _NONFINITE)
def test_cost_scenario_rejects_nonfinite_all_in_bps(bad):
    with pytest.raises(ValueError):
        cm.CostScenario("bad", bad)


@pytest.mark.parametrize("bad", _NONFINITE)
def test_funding_crossing_rejects_nonfinite_rate_bps(bad):
    with pytest.raises(ValueError):
        cm.FundingCrossing(ts=0, rate_bps=bad)


# --------------------------------------------------------------------------- #
# gross_bps / net_bps boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", _NONFINITE)
def test_gross_bps_rejects_nonfinite_entry_price(bad):
    with pytest.raises(ValueError):
        cm.gross_bps("long", bad, 100.0)


@pytest.mark.parametrize("bad", _NONFINITE)
def test_gross_bps_rejects_nonfinite_exit_price(bad):
    with pytest.raises(ValueError):
        cm.gross_bps("long", 100.0, bad)


@pytest.mark.parametrize("bad", _NONFINITE)
def test_net_bps_rejects_nonfinite_gross(bad):
    with pytest.raises(ValueError):
        cm.net_bps(bad, cm.COST_SCENARIO_PRIMARY_STRESS, 0.0)


@pytest.mark.parametrize("bad", _NONFINITE)
def test_net_bps_rejects_nonfinite_funding(bad):
    with pytest.raises(ValueError):
        cm.net_bps(50.0, cm.COST_SCENARIO_PRIMARY_STRESS, bad)


# --------------------------------------------------------------------------- #
# existing positive-price / positive-distance contracts must still hold
# --------------------------------------------------------------------------- #
def test_gross_bps_still_rejects_nonpositive_finite_entry():
    with pytest.raises(ValueError):
        cm.gross_bps("long", 0.0, 10.0)
    with pytest.raises(ValueError):
        cm.gross_bps("long", -5.0, 10.0)


def test_signal_event_still_rejects_nonpositive_finite_sl_distance():
    with pytest.raises(ValueError):
        SignalEvent(
            strategy="s",
            config_id="c",
            symbol="X",
            signal_ts=0,
            side="long",
            sl_distance_bps=0.0,
            tp_distance_bps=100.0,
        )
