"""ROB-1062 H4 (AC29/AC30 spirit — first RED/first real run must be
synthetic) — fake-free smoke test.

Real synthetic daily bars -> the REAL H1 ``pit_universe_alpaca.
evaluate_universe`` -> the REAL H3 ``dats_engine.run_ap_a1_decision`` -> the
REAL H4 ``fill_model``/``trade_ledger``/``pnl_views``/``oos_mask``, chained
end to end for TWO real decisions (an entry, then an exit) hand-picked from
the synthetic price path — mirrors ``alpaca_track_signals/tests/
test_fake_free_smoke.py``'s own discipline (every processing stage is the
REAL named function; only the raw price content is fake). Deliberately does
NOT go through the full ``runner.run_family_fold`` continuous loop (that is
covered, expensively, by ``test_runner.py``/``test_golden_digest.py``) — this
test exists to be FAST and to prove the wiring is real, not mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import daily_bars as db
import dats_engine
import oos_mask as om
import pit_universe_alpaca as pu
import pnl_views as pv
import trade_ledger as tl
import wf_seal_consumption as wf_seal

MIN_MS = 60_000
DAY_MS = db.DAY_MS
N_DAYS = 200


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


def _minute_rows(window_start_ms: int, closes: list[float]) -> list[db.SpotMinute]:
    rows = []
    for day, price in enumerate(closes):
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


def _candidate(symbol: str, bars, window_start_ms: int) -> pu.SymbolCandidate:
    return pu.SymbolCandidate(
        symbol=symbol,
        base=symbol.split("/")[0],
        alpaca_active=True,
        alpaca_tradable=True,
        is_usd_pair=True,
        binance_quote_mode="USDC",
        alpaca_first_daily_ms=window_start_ms - 400 * DAY_MS,
        all_valid_daily_bars_in_lookback=all(b.is_valid for b in bars),
        no_gap_in_last_60min=not bars[-1].gap_in_last_60min,
    )


def test_fake_free_entry_then_exit_through_the_full_h4_pipeline():
    entry_decision_day = _ms(2026, 7, 20, 0, 0, 0)  # a Monday
    window_start = entry_decision_day - N_DAYS * DAY_MS

    # Uptrend for N_DAYS-ish, then a sharp reversal in the last ~10 days so
    # AP-A1's D flips negative and an EXIT fires within a short horizon.
    closes = [100.0 * (1.0006**i) for i in range(N_DAYS)]
    peak = closes[-1]
    for i in range(10):
        closes.append(peak * (0.985 ** (i + 1)))

    strong_rows = _minute_rows(window_start, closes)
    strong_bars = db.build_daily_series(
        strong_rows,
        window_start_ms=window_start,
        window_end_ms=window_start + len(closes) * DAY_MS,
    )
    padding_bars = {
        f"PAD{i}/USD": db.build_daily_series(
            _minute_rows(window_start, [50.0 + i] * len(closes)),
            window_start_ms=window_start,
            window_end_ms=window_start + len(closes) * DAY_MS,
        )
        for i in range(19)
    }
    all_bars = {"STRONG/USD": strong_bars, **padding_bars}
    candidates = [_candidate(s, b, window_start) for s, b in all_bars.items()]

    import seal_consumption as h3_seal

    bundle = h3_seal.load_sealed_configs_and_params()
    config = next(c for c in bundle.configs if c.family == "AP-A1")

    universe = pu.evaluate_universe(entry_decision_day + 5 * MIN_MS, candidates)
    assert universe.meets_min_universe_size

    entry_result = dats_engine.run_ap_a1_decision(
        decision_ts_ms=entry_decision_day + 5 * MIN_MS,
        config=config,
        universe=universe,
        bars_by_symbol=all_bars,
        prior_state={},
    )
    entry_record = next(r for r in entry_result.records if r.symbol == "STRONG/USD")
    assert entry_record.action == "ENTER"
    assert entry_record.reason_code == "ENTRY_ACCEPTED"

    exit_decision_day = window_start + (N_DAYS + 8) * DAY_MS
    universe_2 = pu.evaluate_universe(exit_decision_day + 5 * MIN_MS, candidates)
    exit_result = dats_engine.run_ap_a1_decision(
        decision_ts_ms=exit_decision_day + 5 * MIN_MS,
        config=config,
        universe=universe_2,
        bars_by_symbol=all_bars,
        prior_state=entry_result.new_state,
    )
    exit_record = next(r for r in exit_result.records if r.symbol == "STRONG/USD")
    assert exit_record.action == "EXIT"
    assert exit_record.reason_code == "EXIT_TRIGGERED"

    # Real fill_model, on both legs.
    entry_ref = strong_bars[N_DAYS - 1].close
    exit_ref = [b for b in strong_bars if b.day_end_ms == exit_decision_day][0].close
    entry_minutes = [
        db.SpotMinute(
            open_time_ms=entry_record.decision_ts_ms,
            open=entry_ref,
            high=entry_ref + 1,
            low=entry_ref - 1,
            close=entry_ref,
            volume=1.0,
        ),
        db.SpotMinute(
            open_time_ms=entry_record.decision_ts_ms + 60_000,
            open=entry_ref,
            high=entry_ref + 1,
            low=entry_ref - 1,
            close=entry_ref,
            volume=1.0,
        ),
    ]
    exit_minutes = [
        db.SpotMinute(
            open_time_ms=exit_record.decision_ts_ms,
            open=exit_ref,
            high=exit_ref + 1,
            low=max(exit_ref - 1, 0.01),
            close=exit_ref,
            volume=1.0,
        ),
        db.SpotMinute(
            open_time_ms=exit_record.decision_ts_ms + 60_000,
            open=exit_ref,
            high=exit_ref + 1,
            low=max(exit_ref - 1, 0.01),
            close=exit_ref,
            volume=1.0,
        ),
    ]

    entry_attempt, open_leg = tl.process_entry_signal(
        entry_record, reference_close=entry_ref, minute_bars=entry_minutes
    )
    assert entry_attempt.outcome.filled is True
    assert open_leg is not None

    exit_attempt, trade = tl.process_exit_signal(
        exit_record, open_leg, reference_close=exit_ref, minute_bars=exit_minutes
    )
    assert exit_attempt.outcome.filled is True
    assert trade is not None

    # Real pnl_views, real sealed cost scenarios (via H4's own seal gateway).
    trade_fill = pv.TradeFill(
        entry_reference_close=trade.entry_reference_close,
        entry_fill_price=trade.entry_fill.fill_price,
        exit_reference_close=trade.exit_reference_close,
        exit_fill_price=trade.exit_fill.fill_price,
    )
    real_scenarios = wf_seal.cost_scenarios_bp()
    three_view = pv.three_view_pnl_bp(trade_fill, cost_scenarios_bp=real_scenarios)
    assert three_view.shadow_net_bp_by_scenario["C120"] < three_view.actual_fill_bp

    # Real oos_mask -- masked by default, unmaskable only with matching PASS evidence.
    masked = om.mask(
        three_view, fold_id="smoke-fold", family="AP-A1", config_id=config.config_id
    )
    evidence = om.DryCountPassEvidence(
        fold_id="smoke-fold",
        family="AP-A1",
        config_id=config.config_id,
        modeled_entries=5,
        min_modeled_entries_per_fold=5,
        passed=True,
    )
    unmasked = om.unmask(masked, evidence)
    assert unmasked is three_view
