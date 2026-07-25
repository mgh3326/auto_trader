"""ROB-1059 H1 (spec §6/§9) — PIT eligible-universe (U_t) evaluation.

Boundary cases required by AC14-18/AC22: §6 rule order 1-7; 180-day warm-up
boundary 179/180/181; N_t 17/18/19; UNIVERSE_OUTAGE (2 consecutive weekly evals
or 7 consecutive days of N_t<18); alpaca_first_daily proxy provenance never
recorded as an actual listing date; MATIC/POL (etc.) never stitched.
"""

import pit_universe_alpaca as pu
import pytest

DAY_MS = pu.DAY_MS
DECISION_T = 1_735_689_900_000  # arbitrary decision timestamp (ms)


def _candidate(
    symbol: str,
    base: str | None = None,
    *,
    active: bool = True,
    tradable: bool = True,
    usd_pair: bool = True,
    quote_mode: str = "USDC",
    history_days: int | None = 400,
    valid_daily: bool = True,
    no_gap_60: bool = True,
) -> pu.SymbolCandidate:
    first_daily = None if history_days is None else DECISION_T - history_days * DAY_MS
    return pu.SymbolCandidate(
        symbol=symbol,
        base=base or symbol,
        alpaca_active=active,
        alpaca_tradable=tradable,
        is_usd_pair=usd_pair,
        binance_quote_mode=quote_mode,
        alpaca_first_daily_ms=first_daily,
        all_valid_daily_bars_in_lookback=valid_daily,
        no_gap_in_last_60min=no_gap_60,
    )


# --------------------------------------------------------------------------- #
# rule order 1-7, each independently triggerable
# --------------------------------------------------------------------------- #
def test_rule1_inactive_alpaca_symbol_excluded():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("BTC/USD", active=False)])
    assert snap.per_symbol[0].eligible is False
    assert snap.per_symbol[0].fail_reason == "alpaca_not_active_tradable_usd"


def test_rule1_non_tradable_excluded():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("BTC/USD", tradable=False)])
    assert snap.per_symbol[0].fail_reason == "alpaca_not_active_tradable_usd"


def test_rule1_non_usd_pair_excluded():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("BTC/USD", usd_pair=False)])
    assert snap.per_symbol[0].fail_reason == "alpaca_not_active_tradable_usd"


def test_rule2_stablecoin_and_paxg_excluded():
    for base in sorted(pu.EXCLUDED_STABLE_AND_PAXG_BASES):
        snap = pu.evaluate_universe(DECISION_T, [_candidate(f"{base}/USD", base=base)])
        assert snap.per_symbol[0].fail_reason == "stable_or_paxg_excluded", base


def test_rule3_no_binance_stable_pair_excluded():
    snap = pu.evaluate_universe(
        DECISION_T, [_candidate("HYPE/USD", quote_mode="NO_MAPPING")]
    )
    assert snap.per_symbol[0].fail_reason == "no_binance_stable_pair"


def test_rule5_invalid_daily_bar_in_lookback_excluded():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("BTC/USD", valid_daily=False)])
    assert snap.per_symbol[0].fail_reason == "invalid_daily_bar_in_lookback"


def test_rule6_gap_in_last_60min_excluded():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("BTC/USD", no_gap_60=False)])
    assert snap.per_symbol[0].fail_reason == "gap_in_last_60min"


def test_all_rules_pass_symbol_is_eligible():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("BTC/USD")])
    assert snap.per_symbol[0].eligible is True
    assert snap.per_symbol[0].fail_reason is None


# --------------------------------------------------------------------------- #
# 180-day PIT warm-up boundary: 179 / 180 / 181
# --------------------------------------------------------------------------- #
def test_warmup_179_days_is_insufficient():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("X/USD", history_days=179)])
    assert snap.per_symbol[0].fail_reason == "insufficient_pit_history"


def test_warmup_180_days_is_exactly_sufficient():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("X/USD", history_days=180)])
    assert snap.per_symbol[0].eligible is True


def test_warmup_181_days_is_sufficient():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("X/USD", history_days=181)])
    assert snap.per_symbol[0].eligible is True


def test_unknown_listing_date_is_insufficient_history_not_a_crash():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("X/USD", history_days=None)])
    assert snap.per_symbol[0].fail_reason == "insufficient_pit_history"
    assert snap.per_symbol[0].pit_history_days is None


# --------------------------------------------------------------------------- #
# alpaca_first_daily PIT-proxy provenance: explicit, never an actual listing date
# --------------------------------------------------------------------------- #
def test_listing_proxy_source_is_explicit_alpaca_first_daily_proxy_tag():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("X/USD", history_days=200)])
    assert snap.per_symbol[0].listing_proxy_source == pu.ALPACA_FIRST_DAILY_PROXY


def test_listing_proxy_source_is_none_when_no_first_daily_known():
    snap = pu.evaluate_universe(DECISION_T, [_candidate("X/USD", history_days=None)])
    assert snap.per_symbol[0].listing_proxy_source is None


# --------------------------------------------------------------------------- #
# N_t boundary: 17 / 18 / 19
# --------------------------------------------------------------------------- #
def _n_candidates(n: int) -> list[pu.SymbolCandidate]:
    return [_candidate(f"SYM{i:02d}/USD") for i in range(n)]


def test_n_t_17_fails_min_universe_size():
    snap = pu.evaluate_universe(DECISION_T, _n_candidates(17))
    assert snap.n_t == 17
    assert snap.meets_min_universe_size is False


def test_n_t_18_meets_min_universe_size():
    snap = pu.evaluate_universe(DECISION_T, _n_candidates(18))
    assert snap.n_t == 18
    assert snap.meets_min_universe_size is True


def test_n_t_19_meets_min_universe_size():
    snap = pu.evaluate_universe(DECISION_T, _n_candidates(19))
    assert snap.n_t == 19
    assert snap.meets_min_universe_size is True


# --------------------------------------------------------------------------- #
# universe state / UNIVERSE_OUTAGE
# --------------------------------------------------------------------------- #
def test_universe_state_normal_when_nt_ok_and_no_outage_history():
    assert pu.universe_state(20) == "normal"


def test_universe_state_restricted_when_nt_below_min_but_no_outage_yet():
    assert (
        pu.universe_state(10, recent_daily_n_lt_min=[True] * 3)
        == "restricted_exits_only"
    )


def test_universe_outage_after_2_consecutive_weekly_evals_below_min():
    assert pu.is_universe_outage_weekly([True, True]) is True
    assert pu.is_universe_outage_weekly([False, True]) is False
    assert pu.is_universe_outage_weekly([True]) is False
    assert (
        pu.universe_state(10, recent_weekly_n_lt_min=[True, True]) == "universe_outage"
    )


def test_universe_outage_after_7_consecutive_days_below_min():
    assert pu.is_universe_outage_daily([True] * 7) is True
    assert pu.is_universe_outage_daily([True] * 6) is False
    assert pu.universe_state(10, recent_daily_n_lt_min=[True] * 7) == "universe_outage"


def test_universe_outage_daily_boundary_6_days_is_not_outage_7_days_is():
    six = [True] * 6
    seven = [True] * 7
    assert pu.universe_state(10, recent_daily_n_lt_min=six) == "restricted_exits_only"
    assert pu.universe_state(10, recent_daily_n_lt_min=seven) == "universe_outage"


def test_universe_outage_broken_by_one_good_day_resets_the_run():
    recent = [True, True, True, True, True, True, False, True]  # last 7 not all True
    assert pu.is_universe_outage_daily(recent) is False


# --------------------------------------------------------------------------- #
# canonical order + determinism
# --------------------------------------------------------------------------- #
def test_eligible_symbols_are_lexicographic_regardless_of_input_order():
    candidates = [_candidate("ZED/USD"), _candidate("AAA/USD"), _candidate("MMM/USD")]
    snap = pu.evaluate_universe(DECISION_T, candidates)
    assert snap.eligible_symbols == ("AAA/USD", "MMM/USD", "ZED/USD")
    assert tuple(e.symbol for e in snap.per_symbol) == (
        "AAA/USD",
        "MMM/USD",
        "ZED/USD",
    )


def test_duplicate_symbol_is_rejected():
    with pytest.raises(ValueError):
        pu.evaluate_universe(DECISION_T, [_candidate("BTC/USD"), _candidate("BTC/USD")])


# --------------------------------------------------------------------------- #
# migration non-stitching: MATIC and POL are fully independent candidates
# --------------------------------------------------------------------------- #
def test_matic_and_pol_are_independent_symbols_never_stitched():
    assert pu.KNOWN_MIGRATIONS["MATIC"] == "POL"
    matic = _candidate("MATIC/USD", history_days=1000)
    pol = _candidate("POL/USD", history_days=50)  # too young on its own
    snap = pu.evaluate_universe(DECISION_T, [matic, pol])
    matic_result = next(e for e in snap.per_symbol if e.symbol == "MATIC/USD")
    pol_result = next(e for e in snap.per_symbol if e.symbol == "POL/USD")
    # POL's shortfall must not be masked/filled by MATIC's long history, and
    # MATIC's eligibility must not be affected by POL's presence either.
    assert matic_result.eligible is True
    assert pol_result.eligible is False
    assert pol_result.fail_reason == "insufficient_pit_history"
    assert pol_result.pit_history_days == 50
