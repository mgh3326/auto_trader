"""Parent normalisation, exclusion reasons and episode dedupe (prereg §1)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from research.rob1391_us_resubmit_shadow.parents import (
    assemble_orders,
    build_episodes,
    normalise,
)
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    CAL,
    et,
    kis_row,
    rung_for,
    rung_row,
    toss_row,
    universe_row,
)

pytestmark = pytest.mark.unit

D0 = date(2025, 11, 17)


def one(row, *, rungs=None, universe=None, deviations=frozenset()):
    """Default: the order carries its approved, policy-stamped rung."""

    rungs = [rung_for(row)] if rungs is None else list(rungs)
    orders = assemble_orders([row], rungs, [universe or universe_row(row["symbol"])])
    return normalise(orders[0], CAL, deviations=deviations)


def test_toss_expired_partial_fill_becomes_candidate_with_residual():
    cand, reasons = one(
        toss_row(
            1,
            "SYMA",
            ordered_at=et(D0, 10, 5),
            price="99.50",
            qty=3,
            filled=1,
            avg="99.40",
        ),
        rungs=[rung_row("TOSSORD00000001", qty=3, price="99.50", notional="310")],
    )
    assert reasons == []
    assert cand.d0 == D0 and cand.q == 3 and cand.filled_d0 == 1 and cand.remaining == 2
    assert cand.budget == Decimal("310") and cand.budget_basis == "rung_notional"
    assert cand.expiry_basis == "toss_expired_at"


def test_ledger_budget_basis_without_rung():
    cand, _ = one(
        toss_row(2, "SYMA", ordered_at=et(D0, 10, 5), price="50.25", qty=2),
        rungs=[],
        deviations=frozenset(
            {"D6_ledger_budget_basis", "D4_policy_epoch_invariant_basis"}
        ),
    )
    assert (
        cand.budget == Decimal("100.50")
        and cand.budget_basis == "ledger_price_x_quantity"
    )


def test_kis_unfilled_expiry_inferred_at_d0_close_and_partial_is_unproven():
    cand, reasons = one(
        kis_row(3, "SYMB", ordered_at=et(D0, 9, 45), price="412.10", qty=1, filled="0")
    )
    assert reasons == [] and cand.expiry_basis == "kis_day_close_inferred"
    assert cand.expiry_at == CAL.session(D0).close_utc
    _, reasons = one(
        kis_row(
            4,
            "SYMB",
            ordered_at=et(D0, 9, 45),
            price="412.10",
            qty=3,
            status="partial",
            filled="1",
        )
    )
    assert reasons == ["kis_partial_expiry_unproven"]


@pytest.mark.parametrize(
    ("over", "universe_over", "reason"),
    [
        ({"quantity": "0.50000000"}, {}, "fractional_order"),
        ({"currency": "KRW"}, {}, "currency_not_usd"),
        ({"order_type": "market"}, {}, "not_limit"),
        ({"requires_manual_review": True}, {}, "unclear_ledger_manual_review"),
        ({"price": "99.50500000"}, {}, "price_off_tick"),
        ({"time_in_force": "gtc"}, {}, "not_day_tif"),
        ({}, {"exchange": "OTC"}, "otc"),
        (
            {},
            {
                "security_type": "ETF",
                "is_common_stock": False,
                "is_common_share": False,
                "leverage_factor": "3",
            },
            "leveraged_or_inverse_etf",
        ),
        (
            {},
            {
                "security_type": "ETF",
                "is_common_stock": False,
                "is_common_share": False,
                "leverage_factor": "-1",
            },
            "leveraged_or_inverse_etf",
        ),
        (
            {},
            {
                "security_type": "ETF",
                "is_common_stock": False,
                "is_common_share": False,
                "name_en": "Daily Semis Bull 3X Shares",
            },
            "leveraged_or_inverse_etf_by_name",
        ),
        (
            {},
            {
                "security_type": "PREFERRED",
                "is_common_stock": False,
                "is_common_share": False,
            },
            "not_common_stock_or_etf",
        ),
        ({}, {"listing_status": "DELISTED"}, "inactive_listing"),
    ],
)
def test_exclusion_reasons_are_recorded(over, universe_over, reason):
    row = toss_row(5, "SYMC", ordered_at=et(D0, 10, 5), price="20.00", qty=5)
    row.update(over)
    cand, reasons = one(row, universe=universe_row("SYMC", **universe_over))
    assert cand is None and reason in reasons


def test_option_symbol_and_missing_universe_row():
    row = toss_row(
        6, "SYMD251219C00100000", ordered_at=et(D0, 10, 5), price="1.00", qty=1
    )
    _, reasons = normalise(assemble_orders([row], [], [])[0], CAL)
    assert "option" in reasons and "instrument_unverified_no_universe_row" in reasons


def test_plain_etf_is_allowed():
    cand, reasons = one(
        toss_row(7, "SYME", ordered_at=et(D0, 10, 5), price="20.00", qty=5),
        universe=universe_row(
            "SYME",
            security_type="ETF",
            is_common_stock=False,
            is_common_share=False,
            leverage_factor="1",
            name_en="Broad Market Index ETF",
        ),
    )
    assert reasons == [] and cand is not None


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("filled", ["no_residual_filled"]),
        # D8: a bare Toss cancelled row carries no sweep evidence, so the
        # broker-status guard and the canceledAt guard both fail closed.
        (
            "cancelled",
            ["not_day_expiry_cancelled_broker_status", "toss_cancel_timestamp_missing"],
        ),
        ("accepted", ["unreconciled"]),
        ("replaced", ["not_day_expiry_replaced"]),
    ],
)
def test_non_expiry_statuses(status, reason):
    _, reasons = one(
        toss_row(
            8,
            "SYMA",
            ordered_at=et(D0, 10, 5),
            price="10.00",
            qty=2,
            status=status,
            expired_at=None,
        )
    )
    assert reasons == reason


def test_expiry_must_fall_between_d0_close_and_next_open():
    late = CAL.session(CAL.offset(D0, 1)).open_utc
    _, reasons = one(
        toss_row(
            9, "SYMA", ordered_at=et(D0, 10, 5), price="10.00", qty=2, expired_at=late
        )
    )
    assert reasons == ["expiry_outside_d0_window"]
    early = CAL.session(D0).close_utc.replace(minute=0, hour=19)
    _, reasons = one(
        toss_row(
            10, "SYMA", ordered_at=et(D0, 10, 5), price="10.00", qty=2, expired_at=early
        )
    )
    assert reasons == ["expiry_outside_d0_window"]


def test_quantity_and_budget_mismatches():
    _, reasons = one(
        toss_row(
            11,
            "SYMA",
            ordered_at=et(D0, 10, 5),
            price="10.00",
            qty=2,
            filled=3,
            avg="9.99",
        )
    )
    assert reasons == ["quantity_mismatch_filled_gt_ordered"]
    _, reasons = one(
        toss_row(12, "SYMA", ordered_at=et(D0, 10, 5), price="10.00", qty=2),
        rungs=[rung_row("TOSSORD00000012", qty=2, price="10.00", notional="15")],
    )
    assert reasons == ["budget_below_order_notional"]
    _, reasons = one(
        toss_row(13, "SYMA", ordered_at=et(D0, 10, 5), price="10.00", qty=2),
        rungs=[rung_row("TOSSORD00000013", qty=2, price="10.01")],
    )
    assert reasons == ["rung_price_mismatch"]


def test_duplicate_ledger_rows_are_unclear():
    a = toss_row(14, "SYMA", ordered_at=et(D0, 10, 5), price="10.00", qty=2)
    b = dict(a, source_id="15")
    orders = assemble_orders([a, b], [], [universe_row("SYMA")])
    assert all("duplicate_ledger_or_rung" in normalise(o, CAL)[1] for o in orders)


def test_rung_from_other_account_mode_never_joins():
    row = toss_row(16, "SYMA", ordered_at=et(D0, 10, 5), price="10.00", qty=2)
    orders = assemble_orders(
        [row],
        [
            rung_row(
                "TOSSORD00000016",
                qty=2,
                price="10.00",
                notional="50",
                account_mode="kis_live",
            )
        ],
        [universe_row("SYMA")],
    )
    assert orders[0].rung is None


def test_kis_kr_table_us_row_is_surfaced_as_unexpected():
    row = kis_row(
        17,
        "SYMA",
        ordered_at=et(D0, 10, 5),
        price="10.00",
        qty=1,
        source="kis_live_order_ledger",
    )
    assert one(row)[1] == ["unexpected_source_kis_live_order_ledger_equity_us"]


# ----------------------------------------------------------------- dedupe


def orders_at(*specs):
    rows = [
        toss_row(100 + i, sym, ordered_at=et(day, 8, 5), price=price, qty=2)
        for i, (sym, day, price) in enumerate(specs)
    ]
    return assemble_orders(rows, [], [universe_row(s[0]) for s in specs])


def test_dedupe_session_gap_boundary_five_joins_six_splits():
    d5, d6 = CAL.offset(D0, 5), CAL.offset(D0, 6)
    ep = build_episodes(orders_at(("SYMA", D0, "100.00"), ("SYMA", d5, "100.00")), CAL)
    assert len(ep.episodes) == 1
    ep = build_episodes(orders_at(("SYMA", D0, "100.00"), ("SYMA", d6, "100.00")), CAL)
    assert len(ep.episodes) == 2


def test_dedupe_gap_is_from_previous_order_not_anchor():
    days = [D0, CAL.offset(D0, 5), CAL.offset(D0, 10)]
    ep = build_episodes(orders_at(*[("SYMA", d, "100.00") for d in days]), CAL)
    assert len(ep.episodes) == 1


def test_dedupe_anchor_band_boundary_five_percent():
    d1 = CAL.offset(D0, 1)
    ep = build_episodes(orders_at(("SYMA", D0, "100.00"), ("SYMA", d1, "105.00")), CAL)
    assert len(ep.episodes) == 1
    ep = build_episodes(orders_at(("SYMA", D0, "100.00"), ("SYMA", d1, "105.01")), CAL)
    assert len(ep.episodes) == 2
    ep = build_episodes(orders_at(("SYMA", D0, "100.00"), ("SYMA", d1, "95.00")), CAL)
    assert len(ep.episodes) == 1


def test_band_is_against_first_anchor_not_drifting():
    days = [D0, CAL.offset(D0, 1), CAL.offset(D0, 2)]
    ep = build_episodes(
        orders_at(
            ("SYMA", days[0], "100.00"),
            ("SYMA", days[1], "104.00"),
            ("SYMA", days[2], "108.00"),
        ),
        CAL,
    )
    assert len(ep.episodes) == 2


def test_different_symbols_never_merge():
    ep = build_episodes(orders_at(("SYMA", D0, "100.00"), ("SYMB", D0, "100.00")), CAL)
    assert len(ep.episodes) == 2


def test_ladder_children_share_episode_regardless_of_price():
    rows = [
        toss_row(200, "SYMA", ordered_at=et(D0, 10, 5), price="100.00", qty=1),
        toss_row(201, "SYMA", ordered_at=et(D0, 10, 6), price="90.00", qty=1),
    ]
    rungs = [
        rung_row("TOSSORD00000200", qty=1, price="100.00", root="root-1"),
        rung_row("TOSSORD00000201", qty=1, price="90.00", root="root-1", rung_index=1),
    ]
    ep = build_episodes(assemble_orders(rows, rungs, [universe_row("SYMA")]), CAL)
    assert len(ep.episodes) == 1
    (only,) = ep.episodes.values()
    assert only.anchor.source_id == "200" and len(only.members) == 2


def test_missing_anchor_price_is_quarantined_not_a_new_parent():
    rows = [toss_row(300, "SYMA", ordered_at=et(D0, 10, 5), price=None, qty=1)]
    ep = build_episodes(assemble_orders(rows, [], []), CAL)
    assert ep.episodes == {} and ep.quarantined == {
        "toss_live_order_ledger:300": "anchor_price_missing"
    }


def test_episode_id_is_deterministic_and_opaque():
    a = build_episodes(orders_at(("SYMA", D0, "100.00")), CAL)
    b = build_episodes(orders_at(("SYMA", D0, "100.00")), CAL)
    assert list(a.episodes) == list(b.episodes)
    assert next(iter(a.episodes)).startswith("ep-") and "TOSSORD" not in next(
        iter(a.episodes)
    )
