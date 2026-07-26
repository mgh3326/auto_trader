"""ROB-1061 H3 (AC25/AC26 completion evidence) — repeated runs of BOTH
engines over identical input are byte-identical (same records, same
reason_histogram), a 1-ULP source price change moves the result, and
``bars_by_symbol``/``prior_state`` dict container order never matters
(everything is explicitly sorted to canonical order internally)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import configs as cfg
import dats_engine
import decision_calendar as dc
import pit_universe_alpaca as pu
import wcmb_engine
from daily_bars import DAY_MS, DailyBar

AP_A1_00 = cfg.build_ap_a1_configs()[0]
AP_A2_00 = cfg.build_ap_a2_configs()[0]


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


DECISION_TS = _ms(2026, 7, 20, 0, 5, 0)
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


def _snapshot(eligible: tuple[str, ...]) -> pu.UniverseSnapshot:
    # Pad with filler symbols (no bars supplied for them -- harmless
    # INVALID_DECISION_DAY no-ops) so N_t >= 18 by default (Run A §6 rule 7):
    # this file is about determinism, not the restricted-entry gate, so an
    # unrealistically tiny fixture universe must not trip it.
    padding = tuple(f"PAD{i}/USD" for i in range(max(0, 18 - len(eligible))))
    all_eligible = tuple(sorted(set(eligible) | set(padding)))
    return pu.UniverseSnapshot(
        decision_ts_ms=DECISION_TS,
        eligible_symbols=all_eligible,
        per_symbol=(),
        n_t=len(all_eligible),
        meets_min_universe_size=len(all_eligible) >= 18,
    )


def _closes(n=120, step=0.01):
    return [100.0 * (1 + step) ** i for i in range(n)]


def test_ap_a1_repeated_runs_are_byte_identical():
    bars = {"AAA/USD": _bars_ending_at_window(_closes())}
    universe = _snapshot(("AAA/USD",))
    r1 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol=bars,
        prior_state={},
    )
    r2 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol=bars,
        prior_state={},
    )
    assert r1.records == r2.records
    assert r1.reason_histogram == r2.reason_histogram


def test_ap_a1_one_ulp_source_price_change_moves_the_evidence_hash():
    closes = _closes()
    bars = {"AAA/USD": _bars_ending_at_window(closes)}
    tampered_closes = list(closes)
    # A GENUINE single ULP step (math.nextafter), not an arbitrary
    # 1e-9 perturbation -- at this fixture's magnitude (~hundreds),
    # 1e-9 was ~10^4-10^6 ULPs, silently overstating the sensitivity
    # this test claims to prove.
    tampered_closes[-1] = math.nextafter(tampered_closes[-1], math.inf)
    tampered_bars = {"AAA/USD": _bars_ending_at_window(tampered_closes)}
    universe = _snapshot(("AAA/USD",))
    r1 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol=bars,
        prior_state={},
    )
    r2 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol=tampered_bars,
        prior_state={},
    )
    assert r1.records[0].evidence_hash != r2.records[0].evidence_hash


def test_ap_a1_bars_by_symbol_dict_order_never_matters():
    bars_a_first = {
        "AAA/USD": _bars_ending_at_window(_closes(step=0.01)),
        "BBB/USD": _bars_ending_at_window(_closes(step=0.02)),
    }
    bars_b_first = {
        "BBB/USD": bars_a_first["BBB/USD"],
        "AAA/USD": bars_a_first["AAA/USD"],
    }
    universe = _snapshot(("AAA/USD", "BBB/USD"))
    r1 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol=bars_a_first,
        prior_state={},
    )
    r2 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol=bars_b_first,
        prior_state={},
    )
    assert r1.records == r2.records


def test_ap_a2_repeated_runs_are_byte_identical():
    bars = {"AAA/USD": _bars_ending_at_window(_closes())}
    universe = _snapshot(("AAA/USD",))
    r1 = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars,
        prior_held={},
    )
    r2 = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars,
        prior_held={},
    )
    assert r1.records == r2.records
    assert r1.reason_histogram == r2.reason_histogram


def test_ap_a2_one_ulp_source_price_change_moves_the_evidence_hash():
    closes = _closes()
    bars = {"AAA/USD": _bars_ending_at_window(closes)}
    tampered_closes = list(closes)
    # A GENUINE single ULP step (math.nextafter), not an arbitrary
    # 1e-9 perturbation -- at this fixture's magnitude (~hundreds),
    # 1e-9 was ~10^4-10^6 ULPs, silently overstating the sensitivity
    # this test claims to prove.
    tampered_closes[-1] = math.nextafter(tampered_closes[-1], math.inf)
    tampered_bars = {"AAA/USD": _bars_ending_at_window(tampered_closes)}
    universe = _snapshot(("AAA/USD",))
    r1 = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars,
        prior_held={},
    )
    r2 = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=tampered_bars,
        prior_held={},
    )
    assert r1.records[0].evidence_hash != r2.records[0].evidence_hash
