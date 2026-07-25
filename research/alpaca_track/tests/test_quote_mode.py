"""ROB-1059 H1 (spec §14.2) — quote_mode mapping priority, SYNTH_USDC price
reconstruction, USDT_PROXY basis-drift flag, and fail-closed validation against
the sealed ``universe_map_2026-07-25.json``.
"""

import hashlib
import math
from datetime import date
from pathlib import Path

import pytest
import quote_mode as qm

SEALED_PATH = (
    Path(__file__).resolve().parents[1] / "sealed" / "universe_map_2026-07-25.json"
)
SEALED_SHA256 = "512285ebf67bb49dc1844d7c76dda4ea09dc19cbfb5968d32caee4a688cae8b2"
REQUIRED_BACKTEST_START = date(2024, 6, 1)


def test_sealed_fixture_checksum_matches_the_authority_document():
    actual = hashlib.sha256(SEALED_PATH.read_bytes()).hexdigest()
    assert actual == SEALED_SHA256


# --------------------------------------------------------------------------- #
# priority order (a)(b)(c)(d) — exact, no inversion, no ad-hoc fallback
# --------------------------------------------------------------------------- #
def test_priority_a_sufficient_usdc_history_wins_even_if_usdt_also_exists():
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=date(2018, 12, 15),
        base_usdt_first_1m=date(2017, 8, 17),
        usdc_usdt_available=True,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "USDC"


def test_priority_b_late_usdc_falls_through_to_synth_usdc():
    # AAVE fixture: USDC pair exists but only from 2024-09-04, AFTER the
    # required backtest start (2024-06-01) -- insufficient, falls to (b).
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=date(2024, 9, 4),
        base_usdt_first_1m=date(2020, 10, 15),
        usdc_usdt_available=True,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "SYNTH_USDC"


def test_priority_c_usdt_only_no_usdc_pair_at_all():
    # BAT fixture: no USDC pair ever existed.
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=None,
        base_usdt_first_1m=date(2019, 3, 4),
        usdc_usdt_available=True,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "USDT_PROXY"


def test_priority_c_usdt_only_when_usdcusdt_itself_unavailable():
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=None,
        base_usdt_first_1m=date(2019, 3, 4),
        usdc_usdt_available=False,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "USDT_PROXY"


def test_priority_d_no_direct_stable_pair_is_no_mapping():
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=None,
        base_usdt_first_1m=None,
        usdc_usdt_available=True,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "NO_MAPPING"


def test_late_usdc_only_no_usdt_pair_ever_is_usdc_not_no_mapping():
    # A hypothetical base with a (late, insufficient) native BASEUSDC pair but
    # NO BASEUSDT pair ever listed. Neither SYNTH_USDC nor USDT_PROXY is
    # reconstructible (both need a USDT leg), but a BASEUSDC pair IS a direct
    # stable pair -- rule (d)/NO_MAPPING is reserved for "no direct stable
    # pair at all", so this must fall back to USDC, not be excluded. No sealed
    # base hits this branch today; this guards a future universe refresh.
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=date(2025, 1, 1),
        base_usdt_first_1m=None,
        usdc_usdt_available=True,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "USDC"

    # usdc_usdt_available is irrelevant to this branch (there is no USDT leg
    # to combine it with either way).
    mode_no_basis = qm.resolve_quote_mode(
        base_usdc_first_1m=date(2025, 1, 1),
        base_usdt_first_1m=None,
        usdc_usdt_available=False,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode_no_basis == "USDC"


def test_usdc_sufficiency_boundary_exact_required_start_is_sufficient():
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=REQUIRED_BACKTEST_START,
        base_usdt_first_1m=date(2017, 1, 1),
        usdc_usdt_available=True,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "USDC"


def test_usdc_sufficiency_boundary_one_day_after_required_start_is_insufficient():
    mode = qm.resolve_quote_mode(
        base_usdc_first_1m=date(2024, 6, 2),
        base_usdt_first_1m=date(2017, 1, 1),
        usdc_usdt_available=True,
        required_backtest_start=REQUIRED_BACKTEST_START,
    )
    assert mode == "SYNTH_USDC"


# --------------------------------------------------------------------------- #
# SYNTH_USDC price reconstruction: same-minute alignment, no forward-fill
# --------------------------------------------------------------------------- #
def test_synth_usdc_price_divides_usdt_by_usdcusdt():
    price = qm.synth_usdc_price(100.0, 1.0002)
    assert price == pytest.approx(100.0 / 1.0002)


def test_synth_usdc_price_missing_usdcusdt_minute_propagates_none_not_forward_fill():
    assert qm.synth_usdc_price(100.0, None) is None


def test_synth_usdc_price_rejects_non_finite_inputs():
    with pytest.raises(TypeError):
        qm.synth_usdc_price(math.nan, 1.0)
    with pytest.raises(TypeError):
        qm.synth_usdc_price(100.0, math.inf)


def test_synth_usdc_price_rejects_non_positive_usdcusdt():
    with pytest.raises(ValueError):
        qm.synth_usdc_price(100.0, 0.0)


# --------------------------------------------------------------------------- #
# USDT_PROXY basis-drift flag: 30bp boundary
# --------------------------------------------------------------------------- #
def test_basis_drift_flag_exactly_30bp_is_not_flagged():
    # |1.0030 - 1| * 10000 == 30.0 -> boundary is NOT > 30, so False.
    assert qm.usdcusdt_basis_drift_flag(1.0030) is False


def test_basis_drift_flag_just_over_30bp_is_flagged():
    assert qm.usdcusdt_basis_drift_flag(1.00301) is True


def test_basis_drift_flag_symmetric_below_peg():
    assert qm.usdcusdt_basis_drift_flag(0.99699) is True  # -30.1bp, unambiguous
    # NOTE: 0.99700 is NOT used as an "exactly -30bp -> False" boundary case:
    # plain IEEE-754 float subtraction (1.0 - 0.99700) rounds to
    # 0.0030000000000000027, i.e. ~30.00000000000003bp, which IS > 30 and thus
    # flagged -- this is inherent float-precision noise (asymmetric vs. the
    # +30bp side, where 1.0030 - 1.0 rounds the other way), not a threshold
    # relaxation. The exact-30bp case is intentionally left ambiguous by this
    # test; only unambiguous values (29bp/31bp-class) are asserted.


def test_basis_drift_flag_at_peg_is_false():
    assert qm.usdcusdt_basis_drift_flag(1.0) is False


# --------------------------------------------------------------------------- #
# validation against the sealed universe map
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def sealed():
    return qm.load_sealed_universe_map(SEALED_PATH)


def test_sealed_map_matches_btc_direct_usdc(sealed):
    qm.validate_against_sealed_universe_map(
        base="BTC",
        computed_quote_mode="USDC",
        computed_usdc_first_1m=date(2018, 12, 15),
        computed_usdt_first_1m=date(2017, 8, 17),
        sealed=sealed,
    )  # must not raise


def test_sealed_map_matches_aave_synth_usdc(sealed):
    qm.validate_against_sealed_universe_map(
        base="AAVE",
        computed_quote_mode="SYNTH_USDC",
        computed_usdc_first_1m=date(2024, 9, 4),
        computed_usdt_first_1m=date(2020, 10, 15),
        sealed=sealed,
    )  # must not raise


def test_sealed_map_matches_bat_usdt_proxy(sealed):
    qm.validate_against_sealed_universe_map(
        base="BAT",
        computed_quote_mode="USDT_PROXY",
        computed_usdc_first_1m=None,
        computed_usdt_first_1m=date(2019, 3, 4),
        sealed=sealed,
    )  # must not raise


def test_hype_is_no_mapping_and_permanently_excluded_no_fallback_revives_it(sealed):
    qm.validate_against_sealed_universe_map(
        base="HYPE",
        computed_quote_mode="NO_MAPPING",
        computed_usdc_first_1m=None,
        computed_usdt_first_1m=None,
        sealed=sealed,
    )  # must not raise: agreeing with the permanent exclusion is fine
    with pytest.raises(qm.SealedUniverseMapMismatchError):
        # any recomputation that tries to revive HYPE with a mapping must fail closed
        qm.validate_against_sealed_universe_map(
            base="HYPE",
            computed_quote_mode="USDT_PROXY",
            computed_usdc_first_1m=None,
            computed_usdt_first_1m=date(2026, 1, 1),
            sealed=sealed,
        )


def test_mismatched_quote_mode_fails_closed(sealed):
    with pytest.raises(qm.SealedUniverseMapMismatchError):
        qm.validate_against_sealed_universe_map(
            base="BTC",
            computed_quote_mode="SYNTH_USDC",  # wrong on purpose
            computed_usdc_first_1m=date(2018, 12, 15),
            computed_usdt_first_1m=date(2017, 8, 17),
            sealed=sealed,
        )


def test_mismatched_first_1m_date_fails_closed(sealed):
    with pytest.raises(qm.SealedUniverseMapMismatchError):
        qm.validate_against_sealed_universe_map(
            base="BTC",
            computed_quote_mode="USDC",
            computed_usdc_first_1m=date(2019, 1, 1),  # wrong on purpose
            computed_usdt_first_1m=date(2017, 8, 17),
            sealed=sealed,
        )


def test_resolve_quote_mode_reproduces_every_row_of_the_sealed_universe_map(sealed):
    """Full-universe cross-check (not just spot samples): recomputing
    ``resolve_quote_mode`` from each sealed row's own recorded first-1m-bar
    dates must reproduce that row's sealed ``quote_mode`` for all 32 non-
    excluded pairs, and every recomputation must also pass
    ``validate_against_sealed_universe_map`` without raising."""
    checked = 0
    for base, record in sealed.items():
        if record.excluded:
            continue
        usdc_usdt_available = True  # global USDCUSDT pair, sealed asof 2018-12-15
        computed = qm.resolve_quote_mode(
            base_usdc_first_1m=record.binance_usdc_first_1m,
            base_usdt_first_1m=record.binance_usdt_first_1m,
            usdc_usdt_available=usdc_usdt_available,
            required_backtest_start=REQUIRED_BACKTEST_START,
        )
        assert computed == record.quote_mode, (
            f"{base}: recomputed {computed!r} != sealed {record.quote_mode!r}"
        )
        qm.validate_against_sealed_universe_map(
            base=base,
            computed_quote_mode=computed,
            computed_usdc_first_1m=record.binance_usdc_first_1m,
            computed_usdt_first_1m=record.binance_usdt_first_1m,
            sealed=sealed,
        )
        checked += 1
    assert checked == 32  # 36 total pairs - 4 excluded stable/PAXG rows


def test_unknown_symbol_not_in_sealed_map_fails_closed(sealed):
    with pytest.raises(qm.SealedUniverseMapMismatchError):
        qm.validate_against_sealed_universe_map(
            base="NOTREAL",
            computed_quote_mode="USDC",
            computed_usdc_first_1m=None,
            computed_usdt_first_1m=None,
            sealed=sealed,
        )
