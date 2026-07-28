"""§3 / §5 / §6 reducer tests, including hand-computed closed forms."""

from __future__ import annotations

from fractions import Fraction

import pytest

from research.krb1c_reducer_indep.cli import TABLES
from research.krb1c_reducer_indep.reducer import (
    CostRecord,
    MarketCostInput,
    ReducerFailClosed,
    SellTaxComponent,
    bp_to_decimal_string,
    candidate_at,
    ceil_to_bp,
    reduce_c_stress_cap,
    reduce_market,
    reduce_records,
    rho_exit_of,
)
from research.krb1c_reducer_indep.sealed_input import sealed_cost_inputs
from research.krb1c_reducer_indep.tick import KOSPI_TICK_TABLE

B = Fraction(150_000_000, 10**12)  # 0.015%
S = Fraction(150_000_000, 10**12)  # 0.015%
TAU = Fraction(2_000_000_000, 10**12)  # 0.20%


# --- §3 -------------------------------------------------------------------


def test_sealed_cost_inputs_match_binding() -> None:
    costs = sealed_cost_inputs()
    assert set(costs) == {"KOSPI", "KOSDAQ"}
    for cost in costs.values():
        assert cost.b == B == Fraction(3, 20_000)
        assert cost.s == S
        assert cost.tau == TAU == Fraction(1, 500)
        assert cost.a == Fraction(43, 20_000)
    # KOSPI's 0.05% + 0.15% must sum to exactly KOSDAQ's single 0.20%
    assert costs["KOSPI"].sell_tax_rate_e12 == costs["KOSDAQ"].sell_tax_rate_e12
    assert costs["KOSPI"].sell_tax_rate_e12 == 2_000_000_000


def _rec(**kwargs) -> CostRecord:
    base = {
        "market": "KOSPI",
        "buy_commission_rate_e12": 100,
        "sell_commission_rate_e12": 200,
        "sell_tax_components": (SellTaxComponent("T", 300),),
        "source_snapshot_sha256": "x" * 64,
    }
    base.update(kwargs)
    return CostRecord(**base)  # type: ignore[arg-type]


def test_period_maxima_are_taken_independently() -> None:
    """§3 — B/S/A each maximised on their own; not tied to one record."""
    records = [
        _rec(
            buy_commission_rate_e12=900,
            sell_commission_rate_e12=1,
            sell_tax_components=(SellTaxComponent("T", 1),),
        ),
        _rec(
            buy_commission_rate_e12=1,
            sell_commission_rate_e12=800,
            sell_tax_components=(SellTaxComponent("T", 1),),
        ),
        _rec(
            buy_commission_rate_e12=1,
            sell_commission_rate_e12=1,
            sell_tax_components=(SellTaxComponent("T", 700),),
        ),
    ]
    reduced = reduce_records("KOSPI", records)
    assert (reduced.buy_rate_e12, reduced.sell_rate_e12, reduced.sell_tax_rate_e12) == (
        900,
        800,
        700,
    )


def test_mock_tariff_basis_is_rejected() -> None:
    with pytest.raises(ReducerFailClosed) as exc:
        reduce_records("KOSPI", [_rec(cost_basis="MOCK_DISPLAY_TARIFF")])
    assert "REAL_TRADING_TARIFF" in str(exc.value)


def test_missing_snapshot_sha_is_rejected() -> None:
    with pytest.raises(ReducerFailClosed):
        reduce_records("KOSPI", [_rec(source_snapshot_sha256=None)])


def test_non_pass_probe_is_rejected() -> None:
    with pytest.raises(ReducerFailClosed):
        reduce_records("KOSPI", [_rec(probe_reconciliation_status="FAIL")])


def test_duplicate_tax_component_is_rejected() -> None:
    with pytest.raises(ReducerFailClosed):
        reduce_records(
            "KOSPI",
            [
                _rec(
                    sell_tax_components=(
                        SellTaxComponent("T", 1),
                        SellTaxComponent("T", 2),
                    )
                )
            ],
        )


def test_negative_rate_is_rejected() -> None:
    with pytest.raises(ReducerFailClosed):
        reduce_records("KOSPI", [_rec(buy_commission_rate_e12=-1)])


def test_no_records_is_rejected() -> None:
    with pytest.raises(ReducerFailClosed):
        reduce_records("KOSPI", [])


# --- §4.8 / §5 ------------------------------------------------------------


@pytest.mark.parametrize(
    "price,expected_rho_exit,expected_witness",
    [
        # inside [5k,20k): own ratio <= 1/500, beaten by 20,000's 50/20000=1/400
        (5_000, Fraction(1, 400), 20_000),
        (19_990, Fraction(1, 400), 20_000),
        # at 20,000 the own ratio ties 1/400 and P itself is the least Q
        (20_000, Fraction(1, 400), 20_000),
        # inside [20k,50k): own ratio < 1/400, 200,000 gives 500/200000=1/400
        (30_000, Fraction(1, 400), 200_000),
        # inside [50k,200k): own ratio <= 1/500, 200,000 wins
        (50_000, Fraction(1, 400), 200_000),
        # at 200,000 P itself attains 1/400
        (200_000, Fraction(1, 400), 200_000),
        # at exactly 250,000 own ratio 500/250000 == 1/500 ties 500,000's
        # 1000/500000; the least Q wins the tie
        (250_000, Fraction(1, 500), 250_000),
        # above 250,000 own ratio drops below 1/500, so 500,000 takes over
        (250_500, Fraction(1, 500), 500_000),
        (400_000, Fraction(1, 500), 500_000),
    ],
)
def test_rho_exit_and_witness(
    price: int, expected_rho_exit: Fraction, expected_witness: int
) -> None:
    rho, witness = rho_exit_of(KOSPI_TICK_TABLE, price)
    assert rho == expected_rho_exit
    assert witness == expected_witness


def test_candidate_closed_form_at_20000() -> None:
    cost = sealed_cost_inputs()["KOSPI"]
    row = candidate_at(KOSPI_TICK_TABLE, cost, 20_000)

    assert row.rho_entry == Fraction(50, 20_000) == Fraction(1, 400)
    assert row.rho_exit == Fraction(1, 400)
    assert row.entry_multiplier == 1 + B + Fraction(1, 400)
    assert row.exit_multiplier_cap == 1 - S - TAU - Fraction(1, 400)
    expected = (1 + B + Fraction(1, 400)) / (1 - S - TAU - Fraction(1, 400)) - 1
    assert row.c == expected


def test_multiplicative_formula_is_not_the_additive_approximation() -> None:
    """§5 — the additive approximation b+s+tau+2*tick/P is explicitly
    non-binding, so the two must differ at the witness."""
    cost = sealed_cost_inputs()["KOSPI"]
    row = candidate_at(KOSPI_TICK_TABLE, cost, 20_000)
    additive = B + S + TAU + Fraction(1, 400) + Fraction(1, 400)
    assert row.c != additive
    assert row.c > additive  # the exact form is strictly more conservative


def test_exit_multiplier_cap_positive_everywhere() -> None:
    for market, cost in sealed_cost_inputs().items():
        table = TABLES[market]
        for price in table.valid_prices():
            row = candidate_at(table, cost, price)
            assert row.exit_multiplier_cap > 0


def test_exit_multiplier_cap_non_positive_fails_closed() -> None:
    """§8.1(k) — a tariff that eats the whole exit must fail, not clamp."""
    hostile = MarketCostInput(
        market="KOSPI",
        buy_rate_e12=0,
        sell_rate_e12=999_000_000_000,
        sell_tax_rate_e12=0,
    )
    with pytest.raises(ReducerFailClosed) as exc:
        candidate_at(KOSPI_TICK_TABLE, hostile, 20_000)
    assert "8.1(k)" in str(exc.value)


# --- §6 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (Fraction(0), 0),
        (Fraction(1, 10_000), 1),  # exactly on a bp — no extra
        (Fraction(1, 20_000), 1),  # strictly between — up
        (Fraction(15_000, 10_000), 15_000),
        (Fraction(1, 10_000_000), 1),  # tiny positive still rounds up
        (Fraction(29_999, 10_000_000), 30),
    ],
)
def test_ceil_to_bp(value: Fraction, expected: int) -> None:
    assert ceil_to_bp(value) == expected


def test_ceil_to_bp_never_rounds_to_nearest() -> None:
    # 0.400 01 bp would round to 0 under nearest; the clause demands 1
    assert ceil_to_bp(Fraction(400_001, 10_000_000_000)) == 1


@pytest.mark.parametrize(
    "bp,expected",
    [(0, "0.0000"), (74, "0.0074"), (10_000, "1.0000"), (12_345, "1.2345")],
)
def test_bp_to_decimal_string(bp: int, expected: str) -> None:
    assert bp_to_decimal_string(bp) == expected


def test_market_c_raw_witness_is_least_price_on_tie() -> None:
    cost = sealed_cost_inputs()["KOSPI"]
    result = reduce_market(KOSPI_TICK_TABLE, cost)
    tied = [row.price for row in result.candidates if row.c == result.c_raw]
    assert len(tied) > 1, "expected a genuine tie to exercise the rule"
    assert result.witness_price == min(tied)


def test_full_reduction_closed_form() -> None:
    result = reduce_c_stress_cap(TABLES, sealed_cost_inputs())

    expected_c_raw = (1 + B + Fraction(1, 400)) / (1 - S - TAU - Fraction(1, 400)) - 1
    assert result.c_raw == expected_c_raw
    assert result.c_stress_cap_bp == ceil_to_bp(expected_c_raw)
    assert result.c_stress_cap == Fraction(result.c_stress_cap_bp, 10_000)
    assert result.witness_market == "KOSPI"  # tie between the two markets
    assert result.all_target_checks_passed
    assert result.enumerated_count == 8_002
    assert result.target_check_count == 8_002


def test_both_markets_agree_under_identical_binding() -> None:
    """The sealed binding gives both markets the same three rates and the same
    tick table, so their C_raw_m must coincide exactly."""
    result = reduce_c_stress_cap(TABLES, sealed_cost_inputs())
    assert result.markets["KOSPI"].c_raw == result.markets["KOSDAQ"].c_raw
    assert (
        result.markets["KOSPI"].witness_price == result.markets["KOSDAQ"].witness_price
    )


def test_market_set_mismatch_fails_closed() -> None:
    costs = sealed_cost_inputs()
    del costs["KOSDAQ"]
    with pytest.raises(ReducerFailClosed):
        reduce_c_stress_cap(TABLES, costs)


def test_target_check_holds_at_every_price() -> None:
    """§6.9 — the self-check, re-derived row by row from the published cap."""
    result = reduce_c_stress_cap(TABLES, sealed_cost_inputs())
    cap = result.c_stress_cap
    for market, res in result.markets.items():
        table = TABLES[market]
        cost = res.cost
        assert len(res.target_checks) == len(res.candidates)
        for check in res.target_checks:
            assert check.target == table.tick_ceil(Fraction(check.price) * (1 + cap))
            assert check.target >= check.price
            lhs = check.target * (
                1 - cost.s - cost.tau - Fraction(table.tick(check.target), check.target)
            )
            rhs = check.price * (
                1 + cost.b + Fraction(table.tick(check.price), check.price)
            )
            assert check.lhs == lhs
            assert check.rhs == rhs
            assert lhs >= rhs


def test_target_check_can_actually_fail() -> None:
    """The §6.9 check must not be vacuously true — a zero cap has to break it.

    With cap = 0, T = tick_ceil(P) = P and the inequality reduces to
    P(1 - s - tau - tick(P)/P) >= P(1 + b + tick(P)/P), which is false for any
    positive cost. This pins the check as a live assertion rather than a
    tautology of the implementation.
    """
    from research.krb1c_reducer_indep.reducer import run_target_checks

    rows = run_target_checks(
        KOSPI_TICK_TABLE,
        sealed_cost_inputs()["KOSPI"],
        Fraction(0),
        [5_000, 20_000, 200_000, 400_000],
    )
    assert rows
    assert all(row.passed is False for row in rows)
    assert all(row.target == row.price for row in rows)


def test_target_check_is_not_a_tight_inverse_of_the_cap() -> None:
    """Recorded property, not a defect.

    A cap one bp *below* the reduced value still passes §6.9 at the witness,
    because tick_ceil lifts T past the exact break-even point and donates
    headroom. So §6.9 is a necessary self-check, not a sufficient derivation of
    the cap — consistent with the sealed math verification's finding that §6.9
    is algebraically implied and that observed failures signal table/rate/
    rounding/implementation defects rather than a normal case.
    """
    from research.krb1c_reducer_indep.reducer import run_target_checks

    result = reduce_c_stress_cap(TABLES, sealed_cost_inputs())
    understated = Fraction(result.c_stress_cap_bp - 1, 10_000)
    assert understated < result.c_raw  # genuinely below the exact break-even
    rows = run_target_checks(
        KOSPI_TICK_TABLE,
        sealed_cost_inputs()["KOSPI"],
        understated,
        [result.witness_price],
    )
    assert rows[0].passed is True
    assert rows[0].target == 20_150
