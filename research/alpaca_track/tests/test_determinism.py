"""ROB-1059 H1 (AC19-21) — canonical (decision_ts, symbol) output order,
dict/file-iteration-order independence, exact int/float discipline, and
hash sensitivity to a genuine one-ULP/gap/contract/symbol-order change.
"""

import math

import canonical_hash
import daily_bars as db
import pit_universe_alpaca as pu
import pytest

DAY_MS = 86_400_000


def _snapshot(decision_ts_ms: int, symbols: list[str]) -> pu.UniverseSnapshot:
    candidates = [
        pu.SymbolCandidate(
            symbol=s,
            base=s,
            alpaca_active=True,
            alpaca_tradable=True,
            is_usd_pair=True,
            binance_quote_mode="USDC",
            alpaca_first_daily_ms=decision_ts_ms - 400 * DAY_MS,
            all_valid_daily_bars_in_lookback=True,
            no_gap_in_last_60min=True,
        )
        for s in symbols
    ]
    return pu.evaluate_universe(decision_ts_ms, candidates)


# --------------------------------------------------------------------------- #
# AC19: canonical (decision_ts, symbol) order
# --------------------------------------------------------------------------- #
def test_canonical_snapshot_sequence_accepts_strictly_increasing_decision_ts():
    s1 = _snapshot(1_000, ["BBB", "AAA"])
    s2 = _snapshot(2_000, ["CCC"])
    out = pu.canonical_snapshot_sequence([s1, s2])
    assert out == (s1, s2)
    # within each snapshot, symbol order is already canonical lexicographic
    assert s1.eligible_symbols == ("AAA", "BBB")


def test_canonical_snapshot_sequence_rejects_out_of_order_decision_ts():
    s1 = _snapshot(2_000, ["AAA"])
    s2 = _snapshot(1_000, ["BBB"])  # out of order
    with pytest.raises(ValueError):
        pu.canonical_snapshot_sequence([s1, s2])


def test_canonical_snapshot_sequence_rejects_duplicate_decision_ts():
    s1 = _snapshot(1_000, ["AAA"])
    s2 = _snapshot(1_000, ["BBB"])
    with pytest.raises(ValueError):
        pu.canonical_snapshot_sequence([s1, s2])


# --------------------------------------------------------------------------- #
# dict/file iteration order must not change bytes or hashes
# --------------------------------------------------------------------------- #
def test_dict_insertion_order_does_not_affect_canonical_hash():
    a = {"z": 1, "a": 2, "m": 3.5}
    b = {"a": 2, "m": 3.5, "z": 1}  # same content, different insertion order
    assert canonical_hash.canonical_sha256(a) == canonical_hash.canonical_sha256(b)


def test_candidate_input_order_does_not_affect_universe_snapshot_hash():
    forward = _snapshot(1_000, ["AAA", "BBB", "CCC"])
    reversed_input = _snapshot(1_000, ["CCC", "BBB", "AAA"])
    assert forward.eligible_symbols == reversed_input.eligible_symbols
    assert canonical_hash.canonical_sha256(
        [e.to_dict() for e in forward.per_symbol]
    ) == canonical_hash.canonical_sha256(
        [e.to_dict() for e in reversed_input.per_symbol]
    )


# --------------------------------------------------------------------------- #
# a real content change (one ULP / a gap / a symbol-order change) MUST change
# the hash; re-execution and irrelevant permutations must NOT.
# --------------------------------------------------------------------------- #
def test_one_ulp_source_mutation_changes_the_hash():
    base_close = 100.0
    bumped_close = math.nextafter(base_close, math.inf)  # one ULP higher
    assert base_close != bumped_close
    h1 = canonical_hash.canonical_sha256({"close": base_close})
    h2 = canonical_hash.canonical_sha256({"close": bumped_close})
    assert h1 != h2


def test_one_minute_gap_changes_the_daily_bar_hash():
    day0 = 1_719_878_400_000
    full_rows = [
        db.SpotMinute(day0 + m * 60_000, 100.0, 101.0, 99.0, 100.5, 1.0)
        for m in range(1440)
    ]
    gapped_rows = [r for r in full_rows if r.open_time_ms != day0 + 500 * 60_000]
    bar_full = db.build_utc_day(
        day0, full_rows, prior_close=99.0, is_segment_start=True
    )
    bar_gapped = db.build_utc_day(
        day0, gapped_rows, prior_close=99.0, is_segment_start=True
    )
    h_full = canonical_hash.canonical_sha256(bar_full.__dict__)
    h_gapped = canonical_hash.canonical_sha256(bar_gapped.__dict__)
    assert h_full != h_gapped


def test_symbol_order_change_in_candidate_list_does_not_change_snapshot_content_but_a_genuine_membership_change_does():
    same_a = _snapshot(1_000, ["AAA", "BBB"])
    same_b = _snapshot(1_000, ["BBB", "AAA"])  # pure input-order permutation
    assert same_a.eligible_symbols == same_b.eligible_symbols

    different = _snapshot(1_000, ["AAA", "ZZZ"])  # genuine membership change
    assert same_a.eligible_symbols != different.eligible_symbols


# --------------------------------------------------------------------------- #
# re-execution and semantically-irrelevant permutations stay byte-identical
# --------------------------------------------------------------------------- #
def test_re_execution_is_byte_identical():
    s1 = _snapshot(1_000, ["AAA", "BBB", "CCC"])
    s2 = _snapshot(1_000, ["AAA", "BBB", "CCC"])
    assert s1 == s2
    assert canonical_hash.canonical_sha256(
        [e.to_dict() for e in s1.per_symbol]
    ) == canonical_hash.canonical_sha256([e.to_dict() for e in s2.per_symbol])


# --------------------------------------------------------------------------- #
# exact int/float discipline (bool rejected, non-finite rejected, empty sum
# is exact float 0.0) — cross-module reaffirmation of AC20
# --------------------------------------------------------------------------- #
def test_universe_snapshot_rejects_bool_for_decision_ts_ms():
    with pytest.raises(TypeError):
        pu.UniverseSnapshot(
            decision_ts_ms=True,
            eligible_symbols=(),
            per_symbol=(),
            n_t=0,
            meets_min_universe_size=False,
        )


def test_daily_bar_volume_is_exact_float_never_int_zero():
    bar = db.build_utc_day(
        1_719_878_400_000, [], prior_close=None, is_segment_start=True
    )
    assert type(bar.volume) is float
    assert bar.volume == 0.0
    assert (
        bar.volume is not False
    )  # int/bool 0 would be falsy-equal to 0.0 but wrong type
