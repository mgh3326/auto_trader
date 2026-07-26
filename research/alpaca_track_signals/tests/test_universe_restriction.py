"""ROB-1061 H3 adversarial-verification remediation -- SPEC DEFECT 1 (blocking).

Run A §6 rule 7: "N_t < 18 -> 신규 진입 중단, 기존 포지션 청산만" (new entries
stop, existing positions may still exit). H1 already supplies
``pit_universe_alpaca.universe_state``/``UniverseSnapshot.meets_min_universe_size``
for exactly this purpose -- prior to this remediation, neither H3 engine
referenced either one (zero occurrences), which the adversarial verifier
demonstrated live: a ``n_t=1`` universe (``meets_min_universe_size=False``,
``universe_state(n_t=1) == "restricted_exits_only"``) still produced a live
``ENTER``/``ENTRY_ACCEPTED`` record.

This file is the "유니버스 미달" (below-minimum-universe) fixture AC25
requires: both engines' entry paths are proven BLOCKED below the minimum,
and both engines' EXIT paths are proven UNAFFECTED by the same restriction
(§6 rule 7's "기존 포지션 청산만" half).
"""

from __future__ import annotations

from datetime import UTC, datetime

import configs as cfg
import dats_engine
import decision_calendar as dc
import indicators as ind
import pit_universe_alpaca as pu
import wcmb_engine
from daily_bars import DAY_MS, DailyBar

AP_A1_00 = cfg.build_ap_a1_configs()[0]  # f=14, s=56, m=28, threshold=0.005
AP_A2_00 = cfg.build_ap_a2_configs()[0]  # L=14, k=5, b=1


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


DECISION_TS = _ms(2026, 7, 20, 0, 5, 0)  # a Monday (valid for both AP-A1/AP-A2)
WINDOW_END = dc.prior_completed_day_window(DECISION_TS)[1]


def _bars_ending_at_window(closes: list[float]) -> tuple[DailyBar, ...]:
    n = len(closes)
    bars = []
    for i, close in enumerate(closes):
        day_start = WINDOW_END - (n - i) * DAY_MS
        bars.append(
            DailyBar(
                day_start_ms=day_start,
                day_end_ms=day_start + DAY_MS,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=0.0,
                minute_count_observed=1440,
                imputed_minutes=0,
                max_gap_minutes=0,
                gap_in_last_60min=False,
                is_valid=True,
                is_segment_start=(i == 0),
            )
        )
    return tuple(bars)


def _restricted_snapshot(eligible: tuple[str, ...]) -> pu.UniverseSnapshot:
    """DELIBERATELY below the N_t >= 18 minimum -- unlike every other H3 test
    file's ``_snapshot`` helper (which pads to >= 18 so unrelated tests don't
    trip this gate), this file's whole point IS the below-minimum universe."""
    return pu.UniverseSnapshot(
        decision_ts_ms=DECISION_TS,
        eligible_symbols=tuple(sorted(eligible)),
        per_symbol=(),
        n_t=len(eligible),
        meets_min_universe_size=len(eligible) >= 18,
    )


def _uptrend_closes(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    return [start * (1 + step) ** i for i in range(n)]


def test_universe_state_below_minimum_is_restricted_exits_only_h1_reference():
    # Sanity/documentation: the exact H1 call this file's whole premise rests
    # on, reproducing the verifier's own live demonstration numbers.
    snapshot = _restricted_snapshot(("AAA/USD",))
    assert snapshot.n_t == 1
    assert snapshot.meets_min_universe_size is False
    assert pu.universe_state(snapshot.n_t) == "restricted_exits_only"


# --------------------------------------------------------------------------- #
# AP-A1 (dats_engine)
# --------------------------------------------------------------------------- #


def test_ap_a1_blocks_a_new_entry_when_the_universe_is_below_the_minimum_size():
    # Same strong-uptrend fixture the (unrestricted) ENTRY_ACCEPTED test
    # uses -- D/R both clear the entry bar -- but with a single-symbol
    # (n_t=1) universe. Before this remediation this produced a live
    # ENTER/ENTRY_ACCEPTED record (the verifier's exact finding).
    closes = _uptrend_closes(120, start=100.0, step=0.015)
    d = ind.compute_trend_d(closes, f=14, s=56)
    r = ind.compute_momentum_r(closes, m=28)
    assert d >= 0.005 and r > 0.0  # sanity: this really is an entry signal

    result = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_restricted_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_state={},
    )
    record = next(r for r in result.records if r.symbol == "AAA/USD")
    assert record.reason_code == "UNIVERSE_RESTRICTED_NEW_ENTRY_BLOCKED"
    assert record.action == "NO_ACTION"
    assert record.target_notional == 0.0
    assert result.new_state["AAA/USD"].state == "flat"


def test_ap_a1_exit_is_unaffected_by_a_below_minimum_universe():
    # §6 rule 7's OTHER half: "기존 포지션 청산만" -- an existing long
    # position's EXIT must fire normally even while the universe is
    # restricted (only NEW entries stop).
    closes = [100.0 * (0.97**i) for i in range(120)]  # sharp, sustained decline
    d = ind.compute_trend_d(closes, f=14, s=56)
    r = ind.compute_momentum_r(closes, m=28)
    assert d <= -0.005 or r <= 0.0  # sanity: this really triggers exit

    result = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_restricted_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_state={
            "AAA/USD": dats_engine.AP_A1_PositionState(
                state="long", committed_notional=50.0
            )
        },
    )
    record = next(r for r in result.records if r.symbol == "AAA/USD")
    assert record.reason_code == "EXIT_TRIGGERED"
    assert record.action == "EXIT"
    assert result.new_state["AAA/USD"].state == "flat"


# --------------------------------------------------------------------------- #
# AP-A2 (wcmb_engine)
# --------------------------------------------------------------------------- #


def test_ap_a2_blocks_a_new_entry_when_the_universe_is_below_the_minimum_size():
    closes = _uptrend_closes(30, step=0.01)
    score = ind.compute_score(closes, ell=14)
    assert score > 0.0  # sanity: this really is a buy candidate

    result = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=_restricted_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_held={},
    )
    record = next(r for r in result.records if r.symbol == "AAA/USD")
    assert record.reason_code == "UNIVERSE_RESTRICTED_NEW_ENTRY_BLOCKED"
    assert record.action == "NO_ACTION"
    assert "AAA/USD" not in result.new_held


def test_ap_a2_exit_is_unaffected_by_a_below_minimum_universe():
    # A held symbol ranked beyond k+b must still exit even while the
    # universe overall is restricted.
    n = 30
    prior_held = {
        "KEEP/USD": wcmb_engine.AP_A2_HeldState(committed_notional=1900.0),
        "H0/USD": wcmb_engine.AP_A2_HeldState(committed_notional=90.0),
    }
    bars_by_symbol = {
        "KEEP/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.05)),
        "H0/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.001)),
    }
    for idx, step in enumerate((0.014, 0.013, 0.012, 0.011, 0.010)):
        bars_by_symbol[f"F{idx}/USD"] = _bars_ending_at_window(
            _uptrend_closes(n, step=step)
        )
    # Deliberately restricted: only 7 symbols total, well below N_t >= 18.
    universe = _restricted_snapshot(tuple(bars_by_symbol))
    assert universe.n_t < 18

    result = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held=prior_held,
    )
    by_symbol = {r.symbol: r for r in result.records}
    # H0/USD still exits (rank 7 > k+b=6) -- restriction never blocks exits.
    assert by_symbol["H0/USD"].reason_code == "RANK_EXCEEDS_BUFFER_EXIT"
    assert by_symbol["H0/USD"].action == "EXIT"
    assert "H0/USD" not in result.new_held
    # But every unheld candidate that would otherwise have bought (F0..F4)
    # is blocked instead of entering.
    for idx in range(5):
        symbol = f"F{idx}/USD"
        assert by_symbol[symbol].reason_code == "UNIVERSE_RESTRICTED_NEW_ENTRY_BLOCKED"
        assert symbol not in result.new_held
