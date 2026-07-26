from __future__ import annotations

from datetime import UTC, datetime

import configs as cfg
import decision_calendar as dc
import indicators as ind
import pit_universe_alpaca as pu
import pytest
import seal_consumption as sc
import sizing
import wcmb_engine as eng
from daily_bars import DAY_MS, DailyBar

AP_A2_00 = cfg.build_ap_a2_configs()[0]  # L=14, k=5, b=1


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


DECISION_TS = _ms(2026, 7, 20, 0, 5, 0)  # a Monday
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
    # tests not specifically about the restricted-entry gate
    # (test_universe_restriction.py) must not accidentally trip it via an
    # unrealistically tiny fixture universe.
    padding = tuple(f"PAD{i}/USD" for i in range(max(0, 18 - len(eligible))))
    all_eligible = tuple(sorted(set(eligible) | set(padding)))
    return pu.UniverseSnapshot(
        decision_ts_ms=DECISION_TS,
        eligible_symbols=all_eligible,
        per_symbol=(),
        n_t=len(all_eligible),
        meets_min_universe_size=len(all_eligible) >= 18,
    )


def _uptrend_closes(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    return [start * (1 + step) ** i for i in range(n)]


def _downtrend_closes(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    return [start * (1 - step) ** i for i in range(n)]


def test_rejects_a_non_ap_a2_config():
    ap_a1 = cfg.build_ap_a1_configs()[0]
    with pytest.raises(ValueError, match="AP-A2"):
        eng.run_ap_a2_decision(
            decision_ts_ms=DECISION_TS,
            config=ap_a1,
            universe=_snapshot(()),
            bars_by_symbol={},
            prior_held={},
        )


def test_rejects_a_non_monday_decision_timestamp():
    tuesday = DECISION_TS + 24 * 60 * 60 * 1000
    with pytest.raises(ValueError, match="decision"):
        eng.run_ap_a2_decision(
            decision_ts_ms=tuesday,
            config=AP_A2_00,
            universe=_snapshot(()),
            bars_by_symbol={},
            prior_held={},
        )


def test_rejects_a_forged_config_reusing_a_real_config_id_with_a_relaxed_k():
    # SPEC DEFECT 3 (ROB-1061 adversarial-verification) -- see
    # test_dats_engine.py's matching AP-A1 test for the full rationale.
    forged_params = dict(AP_A2_00.params)
    forged_params["k"] = 50  # relaxed from the sealed 5 -- inflates entries
    forged = cfg.ConfigSpec(
        config_id=AP_A2_00.config_id,
        family=AP_A2_00.family,
        params=forged_params,
        canonical_hash=cfg.canonical_config_hash(
            AP_A2_00.config_id, AP_A2_00.family, forged_params
        ),
    )
    with pytest.raises(sc.ConfigNotSealedError, match=AP_A2_00.config_id):
        eng.run_ap_a2_decision(
            decision_ts_ms=DECISION_TS,
            config=forged,
            universe=_snapshot(()),
            bars_by_symbol={},
            prior_held={},
        )


def test_min_target_notional_floor_rejects_a_sub_25_buy_at_the_engine_boundary():
    # V5 (ROB-1061 adversarial-verification): the AP-A2 `$25` floor gate can
    # be deleted at the ENGINE call site with zero prior test noticing --
    # no engine-level test previously exercised an `uncapped` target_notional
    # actually landing below the floor (as opposed to being cash-capped
    # above it). Same wild-oscillation fixture shape as the AP-A1 sibling
    # test: Score > 0 (qualifies for a buy), but the huge annualized sigma20
    # clamps the AP-A2 k=5 base slot ($400) down to well under $25.
    closes = [100.0 * (1.02**i) * (1.25 if i % 2 == 0 else 0.75) for i in range(120)]
    score = ind.compute_score(closes, ell=14)
    sigma20 = ind.annualized_sigma20(closes)
    vol_scale = sizing.compute_vol_scale(sigma20)
    uncapped = 400.0 * vol_scale
    assert score > 0.0  # sanity: this really is a buy candidate
    assert uncapped < sc.min_strategy_target_usd()  # sanity: below the $25 floor

    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_held={},
    )
    assert len(result.records) == 18  # AAA/USD + 17 N_t>=18 padding fillers
    record = next(r for r in result.records if r.symbol == "AAA/USD")
    assert record.reason_code == "MIN_TARGET_NOTIONAL"
    assert record.action == "NO_ACTION"
    assert "AAA/USD" not in result.new_held


def test_min_target_notional_floor_also_gates_the_cash_capped_branch():
    # The SECOND `meets_min_target_notional` call site in the AP-A2 buy
    # loop (capped by available_cash, not by vol_scale) -- a distinct
    # deletable gate from the one above. Almost all $2,000 equity is
    # committed to KEEP/USD, leaving only $20 available -- below the $25
    # floor -- for AAA/USD's buy attempt, which must be rejected as
    # INSUFFICIENT_CASH (not silently funded at a sub-floor $20).
    closes = _uptrend_closes(30, step=0.01)
    prior_held = {"KEEP/USD": eng.AP_A2_HeldState(committed_notional=1980.0)}
    keep_bars = _bars_ending_at_window(_uptrend_closes(30, step=0.001))
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=_snapshot(("AAA/USD", "KEEP/USD")),
        bars_by_symbol={
            "AAA/USD": _bars_ending_at_window(closes),
            "KEEP/USD": keep_bars,
        },
        prior_held=prior_held,
    )
    assert len(result.records) == 18  # AAA/USD + KEEP/USD + 16 padding fillers
    record = next(r for r in result.records if r.symbol == "AAA/USD")
    assert record.reason_code == "INSUFFICIENT_CASH"
    assert record.action == "NO_ACTION"
    assert "AAA/USD" not in result.new_held
    assert result.new_held["KEEP/USD"].committed_notional == pytest.approx(1980.0)


def test_unheld_negative_score_symbol_is_rejected_score_not_positive():
    closes = _downtrend_closes(30)
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_held={},
    )
    assert len(result.records) == 18  # AAA/USD + 17 padding fillers
    record = next(r for r in result.records if r.symbol == "AAA/USD")
    assert record.reason_code == "SCORE_NOT_POSITIVE"
    assert record.action == "NO_ACTION"


def test_unheld_positive_score_symbol_is_bought_when_a_slot_is_free():
    closes = _uptrend_closes(30, step=0.01)
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_held={},
    )
    assert len(result.records) == 18  # AAA/USD + 17 padding fillers
    record = next(r for r in result.records if r.symbol == "AAA/USD")
    score = ind.compute_score(closes, ell=14)
    sigma20 = ind.annualized_sigma20(closes)
    # Independent oracle: the literal SS12.5 formula (equity=$2,000, k=5,
    # vol_target=0.50), NOT a re-derivation through `sizing.compute_vol_scale`/
    # `sizing.ap_a2_base_slot_usd` -- those are the SAME functions the engine
    # itself calls, so reusing them here would let a shared bug in either
    # hide from this test.
    expected = (2000.0 / 5) * min(1.0, 0.50 / sigma20)
    assert score > 0.0
    assert record.reason_code == "RANK_BUY_ACCEPTED"
    assert record.action == "ENTER"
    assert record.target_notional == pytest.approx(expected)
    assert result.new_held["AAA/USD"].committed_notional == pytest.approx(expected)


def test_held_symbol_beyond_k_plus_b_rank_is_exited_and_frees_cash_for_a_buy():
    # KEEP/USD holds $1900 (rank 1, by far the strongest score -- never at
    # risk). H0/USD holds $90 but is the WEAKEST-scoring symbol of the whole
    # set (5 fillers all outrank it), landing it at rank 7 > k+b=6 -- it must
    # exit. Without that exit, available cash is only $2000-1900-90=$10
    # (below the $25 floor, every buy candidate would be rejected). With the
    # exit processed FIRST (step (2) before step (3)), available cash
    # becomes exactly $100, letting the single strongest filler (F0/USD)
    # buy at exactly $100 (cash-capped) while the next-ranked filler is
    # INSUFFICIENT_CASH. This is only correct if exits are applied before
    # cash is computed for buys -- reordering steps (2)/(3) would leave
    # cash at $10 and reject every buy.
    n = 30
    prior_held = {
        "KEEP/USD": eng.AP_A2_HeldState(committed_notional=1900.0),
        "H0/USD": eng.AP_A2_HeldState(committed_notional=90.0),
    }
    bars_by_symbol = {
        "KEEP/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.05)),
        "H0/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.001)),
    }
    for idx, step in enumerate((0.014, 0.013, 0.012, 0.011, 0.010)):
        bars_by_symbol[f"F{idx}/USD"] = _bars_ending_at_window(
            _uptrend_closes(n, step=step)
        )

    universe = _snapshot(tuple(bars_by_symbol))
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held=prior_held,
    )
    assert len(result.records) == 18  # KEEP/USD + H0/USD + F0..F4 + 11 padding
    by_symbol = {r.symbol: r for r in result.records}
    assert by_symbol["H0/USD"].reason_code == "RANK_EXCEEDS_BUFFER_EXIT"
    assert by_symbol["H0/USD"].action == "EXIT"
    assert by_symbol["KEEP/USD"].reason_code == "RANK_BUFFER_HOLD"
    assert by_symbol["F0/USD"].reason_code == "RANK_BUY_ACCEPTED"
    assert by_symbol["F0/USD"].target_notional == pytest.approx(100.0)
    assert by_symbol["F1/USD"].reason_code == "INSUFFICIENT_CASH"
    assert "H0/USD" not in result.new_held
    assert result.new_held["F0/USD"].committed_notional == pytest.approx(100.0)
    # AC16 verbatim ("no restoration of existing holdings' weight"): a HELD
    # symbol that lands in RANK_BUFFER_HOLD keeps its ORIGINAL entry-time
    # committed_notional untouched -- it must NOT be re-normalized/
    # equal-weighted just because a decision happened. KEEP/USD entered at
    # $1900; a mutant that recomputes it to the equal-weight base_slot
    # ($400 for k=5) on every hold would leave this specific number
    # unchecked without this assertion.
    assert result.new_held["KEEP/USD"].committed_notional == pytest.approx(1900.0)


def test_an_exited_symbol_is_never_reconsidered_as_a_same_decision_buy_candidate():
    # Regression for the double-record bug this fixture originally caught:
    # H0/USD (rank 7 > k+b=6) exits. It ALSO has a positive Score, so a
    # naive "unheld = not in new_held (post-exit)" buy-candidate filter would
    # let it compete again as a buy candidate in the SAME decision -- and
    # since only one record per symbol survives, that would silently erase
    # its own EXIT record. There must be EXACTLY ONE record for H0/USD, and
    # it must be the EXIT.
    n = 30
    prior_held = {
        "KEEP/USD": eng.AP_A2_HeldState(committed_notional=1900.0),
        "H0/USD": eng.AP_A2_HeldState(committed_notional=90.0),
    }
    bars_by_symbol = {
        "KEEP/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.05)),
        "H0/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.001)),
    }
    for idx, step in enumerate((0.014, 0.013, 0.012, 0.011, 0.010)):
        bars_by_symbol[f"F{idx}/USD"] = _bars_ending_at_window(
            _uptrend_closes(n, step=step)
        )
    universe = _snapshot(tuple(bars_by_symbol))
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held=prior_held,
    )
    assert len(result.records) == 18  # KEEP/USD + H0/USD + F0..F4 + 11 padding
    h0_records = [r for r in result.records if r.symbol == "H0/USD"]
    assert len(h0_records) == 1
    assert h0_records[0].reason_code == "RANK_EXCEEDS_BUFFER_EXIT"


def test_held_symbol_at_rank_k_plus_b_holds_not_exit():
    # Construct exactly k+b=6 held symbols, all with strictly decreasing
    # scores (so ranks are 1..6 with no ties) -- rank 6 (== k+b) must HOLD.
    n = 30
    prior_held = {
        f"H{i}/USD": eng.AP_A2_HeldState(committed_notional=100.0) for i in range(6)
    }
    bars_by_symbol = {
        f"H{i}/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.05 - i * 0.005))
        for i in range(6)
    }
    universe = _snapshot(tuple(bars_by_symbol))
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held=prior_held,
    )
    assert len(result.records) == 18  # H0..H5 + 12 padding fillers
    rank6_symbol = "H5/USD"  # smallest step -> smallest score -> rank 6
    record = next(r for r in result.records if r.symbol == rank6_symbol)
    assert record.reason_code == "RANK_BUFFER_HOLD"
    assert record.action == "HOLD"


def test_rank_slots_full_once_k_symbols_are_held():
    # k=5: exactly 5 unheld candidates all qualify, but only 5 slots exist in
    # total (0 pre-held) -- all 5 should buy since none are held yet. Add a
    # 6th, weaker candidate that must be rejected as RANK_SLOTS_FULL.
    n = 30
    bars_by_symbol = {
        f"C{i}/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.05 - i * 0.005))
        for i in range(6)
    }
    universe = _snapshot(tuple(bars_by_symbol))
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held={},
    )
    bought = {r.symbol for r in result.records if r.reason_code == "RANK_BUY_ACCEPTED"}
    full = {r.symbol for r in result.records if r.reason_code == "RANK_SLOTS_FULL"}
    assert bought == {f"C{i}/USD" for i in range(5)}
    assert full == {"C5/USD"}


def test_rank_slots_full_when_the_decision_starts_already_at_k_held_symbols():
    # A7 (ROB-1061 adversarial-verification): EVERY existing k-cap test
    # (including the one immediately above) uses `prior_held={}` -- the
    # decision always STARTS with zero held slots and fills up to k=5 DURING
    # the decision. A mutant that relaxes the init-time cap check
    # (`slots_full = len(new_held) >= k` -> `> k`) never gets exercised at
    # the boundary that actually matters in production: a decision that
    # STARTS already at k held symbols. Under the `> k` mutant, a single
    # unheld positive-Score candidate would be accepted (`len(new_held)=5
    # >= k=5` is False under `>`, i.e. slots_full stays False), breaching the
    # k=5 cap to 6 held symbols. Under the correct `>= k` this candidate must
    # be rejected RANK_SLOTS_FULL and the held count must stay exactly 5.
    n = 30
    # 5 held symbols, all comfortably scored/ranked so none exit (all rank
    # within k+b=6 of each other -- none is beyond the buffer).
    prior_held = {
        f"H{i}/USD": eng.AP_A2_HeldState(committed_notional=100.0) for i in range(5)
    }
    bars_by_symbol = {
        f"H{i}/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.05 - i * 0.005))
        for i in range(5)
    }
    # A single NEW, unheld, strongly-positive-Score candidate that would
    # outrank every held symbol if a slot were free.
    bars_by_symbol["NEW/USD"] = _bars_ending_at_window(_uptrend_closes(n, step=0.10))
    universe = _snapshot(tuple(bars_by_symbol))
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held=prior_held,
    )
    record = next(r for r in result.records if r.symbol == "NEW/USD")
    assert record.reason_code == "RANK_SLOTS_FULL"
    assert record.action == "NO_ACTION"
    assert record.target_notional == 0.0
    assert "NEW/USD" not in result.new_held
    assert len(result.new_held) == 5  # cap never breached past k=5


def test_look_ahead_is_structurally_impossible_appending_future_bars_never_changes_output():
    closes = _uptrend_closes(30, step=0.02)
    base_bars = _bars_ending_at_window(closes)
    future_bars = tuple(
        DailyBar(
            day_start_ms=WINDOW_END + i * DAY_MS,
            day_end_ms=WINDOW_END + (i + 1) * DAY_MS,
            open=1_000_000.0,
            high=1_000_000.0,
            low=1_000_000.0,
            close=1_000_000.0,
            volume=0.0,
            minute_count_observed=1440,
            imputed_minutes=0,
            max_gap_minutes=0,
            gap_in_last_60min=False,
            is_valid=True,
            is_segment_start=False,
        )
        for i in range(5)
    )
    universe = _snapshot(("AAA/USD",))
    baseline = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol={"AAA/USD": base_bars},
        prior_held={},
    )
    tampered = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=universe,
        bars_by_symbol={"AAA/USD": base_bars + future_bars},
        prior_held={},
    )
    assert baseline.records == tampered.records
    assert dict(baseline.new_held) == dict(tampered.new_held)


def test_reason_histogram_reconciles_to_the_total_record_count():
    closes_up = _uptrend_closes(30, step=0.02)
    closes_down = _downtrend_closes(30, step=0.01)
    result = eng.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=_snapshot(("AAA/USD", "BBB/USD")),
        bars_by_symbol={
            "AAA/USD": _bars_ending_at_window(closes_up),
            "BBB/USD": _bars_ending_at_window(closes_down),
        },
        prior_held={},
    )
    assert sum(result.reason_histogram.values()) == len(result.records)
