"""hk 7119 — the pure daily-loss calculator (6939 I3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.daily_loss_monitor import rules
from app.services.daily_loss_monitor.rules import (
    DAILY_LOSS_LIMIT_KRW,
    Fill,
    LossResult,
    Mark,
    compute_daily_pnl,
    should_freeze,
)

NOW = datetime(2026, 11, 28, 5, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)


def fill(
    side,
    qty,
    price,
    *,
    fee="0",
    at=NOW - timedelta(hours=1),
    symbol="005930",
    currency="KRW",
    fx=None,
    tax=None,
):
    return Fill(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=None if fee is None else Decimal(fee),
        tax=None if tax is None else Decimal(tax),
        fee_currency=currency,
        currency=currency,
        filled_at=at,
        fx_krw=None if fx is None else Decimal(fx),
    )


def mark(price, *, age=timedelta(minutes=1), fx=None):
    return Mark(Decimal(price), NOW - age, None if fx is None else Decimal(fx))


def test_limit_is_locked_at_500k() -> None:
    assert DAILY_LOSS_LIMIT_KRW == Decimal("500000")
    assert rules.QUOTE_MAX_AGE == timedelta(minutes=10)


def test_loss_of_the_limit_freezes() -> None:
    # Carried 100 at 60,000; quote 55,000 -> -500,000 exactly.
    result = compute_daily_pnl(
        currency="KRW",
        history=[fill("buy", "100", "70000", at=YESTERDAY)],
        today=[],
        reference={"005930": mark("60000")},
        quotes={"005930": mark("55000")},
        now=NOW,
    )
    assert result.pnl_krw == Decimal("-500000")
    assert should_freeze(result)


def test_loss_below_the_limit_does_not_freeze() -> None:
    result = compute_daily_pnl(
        currency="KRW",
        history=[fill("buy", "100", "70000", at=YESTERDAY)],
        today=[],
        reference={"005930": mark("60000")},
        quotes={"005930": mark("55001")},
        now=NOW,
    )
    assert result.pnl_krw == Decimal("-499900")
    assert not should_freeze(result)


def test_realized_fifo_with_fees_counts() -> None:
    # Buy today 10 @ 100,000 fee 1,000; sell 10 @ 50,000 fee 500, tax 1,000.
    result = compute_daily_pnl(
        currency="KRW",
        history=[],
        today=[
            fill("buy", "10", "100000", fee="1000", at=NOW - timedelta(hours=2)),
            fill("sell", "10", "50000", fee="500", tax="1000"),
        ],
        reference={},
        quotes={},
        now=NOW,
    )
    assert result.pnl_krw == Decimal("-502500")
    assert should_freeze(result)


def test_unmeasurable_mtm_freezes() -> None:
    for quotes, reason in (
        ({}, "quote_unavailable"),
        ({"005930": mark("70000", age=timedelta(minutes=11))}, "quote_stale"),
    ):
        result = compute_daily_pnl(
            currency="KRW",
            history=[fill("buy", "1", "70000", at=YESTERDAY)],
            today=[],
            reference={"005930": mark("70000")},
            quotes=quotes,
            now=NOW,
        )
        assert result.pnl_krw is None and result.reasons == (reason,)
        assert should_freeze(result)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"today": [fill("buy", "1", "1", fee=None)]}, "fee_estimate_unavailable"),
        ({"today": [fill("sell", "1", "1")]}, "position_oversold"),
        (
            {"history": [fill("buy", "1", "1", at=YESTERDAY)], "reference": {}},
            "reference_price_unavailable",
        ),
        ({"provisional_unreconciled": True}, "provisional_fill_unreconciled"),
        (
            {"today": [fill("buy", "1", "1", currency="USD")]},
            "currency_mismatch",
        ),
    ],
)
def test_each_unmeasurable_part_freezes(kwargs, reason) -> None:
    base = {
        "history": [],
        "today": [],
        "reference": {},
        "quotes": {"005930": mark("1")},
    }
    base.update(kwargs)
    result = compute_daily_pnl(currency="KRW", now=NOW, **base)
    assert reason in result.reasons and result.pnl_krw is None
    assert reason in rules.REASONS
    assert should_freeze(result)


def test_usd_needs_fx_and_converts_at_fill_fx() -> None:
    today = [fill("buy", "10", "100", currency="USD", fx="1400")]
    missing = compute_daily_pnl(
        currency="USD",
        history=[],
        today=[fill("buy", "10", "100", currency="USD")],
        reference={},
        quotes={"005930": mark("100", fx="1400")},
        now=NOW,
    )
    assert missing.reasons == ("fx_unavailable",)
    result = compute_daily_pnl(
        currency="USD",
        history=[],
        today=today,
        reference={},
        quotes={"005930": mark("60", fx="1300")},
        now=NOW,
    )
    # cost 10*100*1400 = 1,400,000; value 10*60*1300 = 780,000.
    assert result.pnl_krw == Decimal("-620000")
    assert should_freeze(result)


def test_flat_day_does_not_freeze() -> None:
    result = compute_daily_pnl(
        currency="KRW", history=[], today=[], reference={}, quotes={}, now=NOW
    )
    assert result == LossResult(pnl_krw=Decimal(0), reasons=())
    assert not should_freeze(result)


# --- ESC 703542 answer: conservative bases instead of unknown ---------------

ESTIMATE = rules.CostEstimate(
    round_trip_cost_bps=Decimal("47.4"),
    sell_tax_bps=rules.KR_SELL_TAX_BPS,
    policy_version="v",
    policy_content_hash="h",
)


def test_unreported_fee_uses_the_policy_estimate_and_overstates_the_loss() -> None:
    today = [
        fill("buy", "10", "100000", fee=None, at=NOW - timedelta(hours=2)),
        fill("sell", "10", "100000", fee=None),
    ]
    result = compute_daily_pnl(
        currency="KRW",
        history=[],
        today=today,
        reference={},
        quotes={},
        now=NOW,
        cost_estimate=ESTIMATE,
        sell_tax_bps=rules.KR_SELL_TAX_BPS,
    )
    # Each leg: 1,000,000 * 47.4bp = 4,740; sell tax 18bp = 1,800.
    assert result.pnl_krw == Decimal("-11280")
    assert result.fee_bases == ("estimated",)
    assert result.measured and not should_freeze(result)


def test_estimated_fee_can_push_a_day_over_the_limit() -> None:
    today = [
        fill("buy", "100", "1000000", fee=None, at=NOW - timedelta(hours=2)),
        fill("sell", "100", "1000000", fee=None),
    ]
    result = compute_daily_pnl(
        currency="KRW",
        history=[],
        today=today,
        reference={},
        quotes={},
        now=NOW,
        cost_estimate=ESTIMATE,
        sell_tax_bps=rules.KR_SELL_TAX_BPS,
    )
    assert result.pnl_krw == Decimal("-1128000")
    assert should_freeze(result)


def test_recorded_fee_is_used_as_recorded() -> None:
    today = [
        fill("buy", "1", "100", fee="3", at=NOW - timedelta(hours=2)),
        fill("sell", "1", "100", fee="2", tax="1"),
    ]
    result = compute_daily_pnl(
        currency="KRW",
        history=[],
        today=today,
        reference={},
        quotes={},
        now=NOW,
        cost_estimate=ESTIMATE,
        sell_tax_bps=rules.KR_SELL_TAX_BPS,
    )
    assert result.pnl_krw == Decimal("-6")
    assert result.fee_bases == ("recorded",)


def test_last_session_close_mark_is_accepted_without_the_quote_age_rule() -> None:
    close = Mark(
        Decimal("55000"), NOW - timedelta(hours=14), None, basis="last_session_close"
    )
    result = compute_daily_pnl(
        currency="KRW",
        history=[fill("buy", "100", "70000", at=YESTERDAY)],
        today=[],
        reference={"005930": mark("60000")},
        quotes={"005930": close},
        now=NOW,
    )
    assert result.pnl_krw == Decimal("-500000")
    assert result.mark_bases == ("last_session_close",)
    assert should_freeze(result)


def test_unknown_mark_basis_is_unavailable() -> None:
    odd = Mark(Decimal("1"), NOW, None, basis="guess")
    result = compute_daily_pnl(
        currency="KRW",
        history=[fill("buy", "1", "1", at=YESTERDAY)],
        today=[],
        reference={"005930": mark("1")},
        quotes={"005930": odd},
        now=NOW,
    )
    assert result.reasons == ("quote_unavailable",)


# --- review round (PR #2714) ------------------------------------------------


def test_history_oversell_is_caught_even_when_later_netted_back() -> None:
    result = compute_daily_pnl(
        currency="KRW",
        history=[
            fill("sell", "10", "1", at=YESTERDAY - timedelta(hours=2)),
            fill("buy", "10", "1", at=YESTERDAY),
        ],
        today=[],
        reference={},
        quotes={},
        now=NOW,
    )
    assert "position_oversold" in result.reasons and should_freeze(result)


def test_recorded_fee_without_currency_is_unknown() -> None:
    f = Fill(
        "005930",
        "buy",
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        None,
        None,
        "KRW",
        NOW - timedelta(hours=1),
        None,
    )
    result = compute_daily_pnl(
        currency="KRW",
        history=[],
        today=[f],
        reference={},
        quotes={"005930": mark("1")},
        now=NOW,
    )
    assert result.reasons == ("fee_currency_unknown",)


def test_missing_sell_tax_is_estimated_even_with_a_recorded_fee() -> None:
    today = [
        fill("buy", "1", "1500000", at=NOW - timedelta(hours=2)),
        fill("sell", "1", "1001000", fee="0", tax=None),
    ]
    result = compute_daily_pnl(
        currency="KRW",
        history=[],
        today=today,
        reference={},
        quotes={},
        now=NOW,
        sell_tax_bps=rules.KR_SELL_TAX_BPS,
    )
    # pre-tax loss 499,000; 18bp of 1,001,000 = 1,801.8 -> over the limit.
    assert result.pnl_krw == Decimal("-500801.8")
    assert should_freeze(result)


def test_input_reasons_make_the_day_unmeasured() -> None:
    result = compute_daily_pnl(
        currency="KRW",
        history=[],
        today=[],
        reference={},
        quotes={},
        now=NOW,
        input_reasons=("reconcile_unproven",),
    )
    assert result.reasons == ("reconcile_unproven",) and should_freeze(result)
