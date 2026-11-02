"""ROB-1470 report: golden output on fixture data (Acceptance 5)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.strategy_sleeves.book import compute_sleeve_book
from app.services.strategy_sleeves.canary import HorizonMark, build_cohort
from app.services.strategy_sleeves.report import (
    budget_at_signal,
    build_report_row,
    currency_totals,
    render_text,
)
from tests.services.strategy_sleeves.support import (
    NOW,
    D,
    fill,
    order,
    sleeve,
    version_row,
)

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).with_name("report_golden.txt")


def _rows():
    us = sleeve()
    us_orders = [
        order(1, limit_price="100", signal_key="sig-a"),
        order(2, limit_price="120", signal_key="sig-a"),  # same signal, a ladder
        order(3, side="sell", signal_key="sig-b"),
        order(4, state="resting", quantity="2", limit_price="95", signal_key="sig-c"),
    ]
    us_fills = [
        fill(1, 1, quantity="10", price="100", minutes=0),
        fill(2, 2, quantity="10", price="120", minutes=1),
        fill(3, 3, side="sell", quantity="15", price="130", fee="2", minutes=2),
    ]
    us_versions = [
        version_row(version=1, state="shadow", set_at=NOW - timedelta(days=10)),
        version_row(set_at=NOW - timedelta(days=5)),
    ]
    us_cohort = build_cohort(
        orders=us_orders,
        fills=us_fills,
        marks={
            "sig-a": HorizonMark(True, D("115")),
            "sig-b": HorizonMark(True, D("125")),
            "sig-c": HorizonMark(False),
        },
        budget_at={"sig-a": D("1000"), "sig-b": D("1000"), "sig-c": D("1000")},
    )
    kr = sleeve(
        id=8,
        sleeve_key="kr-cut-shadow",
        rule_id="rob1458.kr_cut_minus20",
        market="equity_kr",
        currency="KRW",
    )
    kr_versions = [
        version_row(
            sleeve_id=8,
            version=1,
            state="shadow",
            prereg_metric=None,
            prereg_min_effect_pct=None,
            prereg_horizon_sessions=None,
            prereg_max_duration_days=None,
        )
    ]
    empty = compute_sleeve_book(orders=[], fills=[], marks={})
    return [
        build_report_row(
            sleeve=us,
            versions=us_versions,
            book=compute_sleeve_book(
                orders=us_orders, fills=us_fills, marks={"AAA": D("110")}
            ),
            cohort=us_cohort,
            diagnostics={"no_matching_order": 1},
            now=NOW,
        ),
        build_report_row(
            sleeve=kr,
            versions=kr_versions,
            book=empty,
            cohort=build_cohort(orders=[], fills=[], marks={}, budget_at={}),
            diagnostics={},
            now=NOW,
        ),
    ]


def test_report_text_matches_the_golden_output() -> None:
    assert render_text(_rows()) == GOLDEN.read_text("utf-8")


def test_report_row_numbers() -> None:
    [us, kr] = _rows()
    assert us["used"] == "790"  # held 5 @ 120 + open 2 @ 95
    assert us["budget_left"] == "210"
    assert us["net_pnl"] == "298"  # 350 realized - 50 unrealized - 2 fees
    assert us["safety_state"] == "ok"
    canary = us["canary"]
    # sig-a: 10 x 15 + 10 x -5 = 100 -> 10%; sig-b (sell): 15 x 5 - 2 = 73 -> 7.3%
    assert canary["signals"] == 3
    assert (canary["matured"], canary["open"]) == (2, 1)
    assert canary["mean_result_pct_of_budget"] == "8.65"
    assert canary["verdict_state"] == "collecting"
    assert canary["baseline"] == "not_recorded"
    assert canary["mean_d_pct_of_budget"] is None
    assert canary["preregistration"]["complete"] is True
    assert kr["canary"]["verdict_state"] == "not_preregistered"
    assert kr["safety_state"] == "shadow"
    json.dumps([us, kr])  # the --json output is serializable


def test_totals_keep_currencies_apart() -> None:
    totals = currency_totals(_rows())
    assert set(totals) == {"KRW", "USD"}
    assert totals["USD"]["used"] == "790"
    assert totals["KRW"]["used"] == "0"


def test_budget_at_signal_uses_the_version_in_effect() -> None:
    versions = [
        version_row(version=1, budget_cap=D("100"), set_at=NOW - timedelta(days=3)),
        version_row(version=2, budget_cap=D("500"), set_at=NOW - timedelta(days=1)),
    ]
    assert budget_at_signal(versions, NOW - timedelta(days=2)) == D("100")
    assert budget_at_signal(versions, NOW) == D("500")
    assert budget_at_signal(versions, NOW - timedelta(days=9)) == D("100")


def test_report_says_no_sleeves_when_empty() -> None:
    assert render_text([]) == "no sleeves\n"
