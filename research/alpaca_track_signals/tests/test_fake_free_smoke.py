"""ROB-1061 H3 (AC26) — fake-free smoke test.

Real synthetic 1-minute rows -> the REAL H1 ``daily_bars.build_daily_series``
-> the REAL H1 ``pit_universe_alpaca.evaluate_universe`` -> the REAL H3
``dats_engine.run_ap_a1_decision`` / ``wcmb_engine.run_ap_a2_decision``,
chained end to end. The ONLY fake thing in this test is the raw synthetic
minute-bar price content; every processing stage after that is the real,
named module function — no stage is replaced by a mock/stub/behavior
callback (mirrors ``research/alpaca_track/tests/test_fake_free_smoke.py``'s
own discipline, AC26 requires exactly this shape for H3).
"""

from __future__ import annotations

from datetime import UTC, datetime

import configs as cfg
import daily_bars as db
import dats_engine
import decision_calendar as dc
import pit_universe_alpaca as pu
import wcmb_engine

MIN_MS = 60_000
DAY_MS = db.DAY_MS
N_DAYS = 200


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


def _minute_rows_for_uptrend(window_start_ms: int, n_days: int, step: float):
    rows = []
    price = 100.0
    for day in range(n_days):
        day_start = window_start_ms + day * DAY_MS
        for minute in range(1440):
            rows.append(
                db.SpotMinute(
                    open_time_ms=day_start + minute * MIN_MS,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=1.0,
                )
            )
        price *= (1 + step) ** 1440  # apply the daily drift once per day-close
    return rows


def _minute_rows_flat(window_start_ms: int, n_days: int, price: float = 100.0):
    rows = []
    for day in range(n_days):
        day_start = window_start_ms + day * DAY_MS
        for minute in range(1440):
            rows.append(
                db.SpotMinute(
                    open_time_ms=day_start + minute * MIN_MS,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=1.0,
                )
            )
    return rows


def test_fake_free_end_to_end_daily_bars_pit_universe_and_both_h3_engines():
    # A Monday decision, 200 real UTC days of history behind it (plenty for
    # both AP-A1's s<=84/m<=56 lookback and PIT's 180-day warm-up).
    decision_day = _ms(2026, 7, 20, 0, 0, 0)  # a Monday's own 00:00 UTC
    decision_ts = decision_day + 5 * MIN_MS
    window_start = decision_day - N_DAYS * DAY_MS
    window_end = decision_day

    strong_rows = _minute_rows_for_uptrend(window_start, N_DAYS, step=0.0006)
    flat_rows = _minute_rows_flat(window_start, N_DAYS)

    strong_bars = db.build_daily_series(
        strong_rows, window_start_ms=window_start, window_end_ms=window_end
    )
    flat_bars = db.build_daily_series(
        flat_rows, window_start_ms=window_start, window_end_ms=window_end
    )
    assert len(strong_bars) == N_DAYS
    assert all(bar.is_valid for bar in strong_bars)
    assert all(bar.is_valid for bar in flat_bars)

    def _candidate(symbol: str, bars) -> pu.SymbolCandidate:
        return pu.SymbolCandidate(
            symbol=symbol,
            base=symbol.split("/")[0],
            alpaca_active=True,
            alpaca_tradable=True,
            is_usd_pair=True,
            binance_quote_mode="USDC",
            alpaca_first_daily_ms=window_start - 400 * DAY_MS,
            all_valid_daily_bars_in_lookback=all(b.is_valid for b in bars),
            no_gap_in_last_60min=not bars[-1].gap_in_last_60min,
        )

    # H1's own real PIT universe builder requires N_t >= 18 to mark the
    # snapshot as meeting the minimum universe size -- pad with 16 more
    # (flat, thus score/D-neutral) real candidates so this end-to-end smoke
    # exercises a REALISTIC universe shape, not a 2-symbol toy that would
    # never occur alongside the real gate.
    padding_bars = {
        f"PAD{i}/USD": db.build_daily_series(
            _minute_rows_flat(window_start, N_DAYS, price=50.0 + i),
            window_start_ms=window_start,
            window_end_ms=window_end,
        )
        for i in range(16)
    }
    all_bars = {"STRONG/USD": strong_bars, "FLAT/USD": flat_bars, **padding_bars}
    candidates = [_candidate(symbol, bars) for symbol, bars in all_bars.items()]
    universe = pu.evaluate_universe(decision_ts, candidates)
    assert universe.meets_min_universe_size
    assert "STRONG/USD" in universe.eligible_symbols
    assert "FLAT/USD" in universe.eligible_symbols

    ap_a1_config = cfg.build_ap_a1_configs()[0]
    assert dc.is_ap_a1_decision_ts(decision_ts)
    ap_a1_result = dats_engine.run_ap_a1_decision(
        decision_ts_ms=decision_ts,
        config=ap_a1_config,
        universe=universe,
        bars_by_symbol=all_bars,
        prior_state={},
    )
    ap_a1_by_symbol = {r.symbol: r for r in ap_a1_result.records}
    assert ap_a1_by_symbol["STRONG/USD"].reason_code == "ENTRY_ACCEPTED"
    assert ap_a1_by_symbol["STRONG/USD"].action == "ENTER"
    assert ap_a1_by_symbol["FLAT/USD"].reason_code == "NO_ENTRY_SIGNAL"
    assert sum(ap_a1_result.reason_histogram.values()) == len(ap_a1_result.records)
    assert len(ap_a1_result.records) > 0  # non-empty evidence, AC26

    ap_a2_config = cfg.build_ap_a2_configs()[0]
    assert dc.is_ap_a2_decision_ts(decision_ts)
    ap_a2_result = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=decision_ts,
        config=ap_a2_config,
        universe=universe,
        bars_by_symbol=all_bars,
        prior_held={},
    )
    ap_a2_by_symbol = {r.symbol: r for r in ap_a2_result.records}
    assert ap_a2_by_symbol["STRONG/USD"].reason_code == "RANK_BUY_ACCEPTED"
    assert ap_a2_by_symbol["FLAT/USD"].reason_code == "SCORE_NOT_POSITIVE"
    assert sum(ap_a2_result.reason_histogram.values()) == len(ap_a2_result.records)
    assert len(ap_a2_result.records) > 0  # non-empty evidence, AC26

    # Both an entry AND a rejection must be present in evidence for BOTH
    # strategies -- AC26's "non-empty entries and rejections" requirement.
    ap_a1_reasons = set(ap_a1_result.reason_histogram)
    ap_a2_reasons = set(ap_a2_result.reason_histogram)
    assert "ENTRY_ACCEPTED" in ap_a1_reasons
    assert len(ap_a1_reasons - {"ENTRY_ACCEPTED"}) > 0
    assert "RANK_BUY_ACCEPTED" in ap_a2_reasons
    assert len(ap_a2_reasons - {"RANK_BUY_ACCEPTED"}) > 0
