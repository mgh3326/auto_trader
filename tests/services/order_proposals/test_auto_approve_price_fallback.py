"""#1067 -- a Toss preview without current_price uses a fresh KIS quote.

Pure-layer evidence: the quote freshness contract, the evaluator's
"substitute the input, never the gate" property, and the audit projection.
Dispatch/retry lifecycle evidence lives in
``test_auto_approve_price_retry_dispatch.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)
from app.services.order_proposals.auto_approve_audit import (
    append_auto_approve_rejection_attempt,
    build_auto_approve_rejection_card_block,
    project_auto_approve_rejections,
)
from app.services.order_proposals.auto_approve_price_fallback import (
    PRICE_FALLBACK_FAILURE_REASONS,
    PriceFallback,
    classify_kis_quote,
    fetch_kis_quote_fallback,
    preview_current_price_absent,
)

_PRICE_CONTEXT_MESSAGE = "Failed to retrieve current price for 035720: boom"

# The 09-30 09:13 case: Toss 035720 buy 3 @ 32,750 (98,250 KRW), mode expanded.
_LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("2000000"),
    daily_cap=Decimal("5000000"),
    policy_version="2026-09-30.1",
    mode="expanded",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("90"),
)


@pytest.fixture(autouse=True)
def _toss_veto_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", True)


def _group(**overrides):
    values = {
        "symbol": "035720",
        "market": "equity_kr",
        "account_mode": "toss_live",
        "broker_account_id": "toss-acct-1",
        "order_type": "limit",
        "action": "place",
        "exit_intent": None,
        "thesis": "underwater_support_net add at support",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "rung_index": 0,
        "side": "buy",
        "limit_price": Decimal("32750"),
        "quantity": Decimal("3"),
        "notional": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fresh_quote(**overrides):
    quote = {
        "symbol": "035720",
        "instrument_type": "equity_kr",
        "price": 33800.0,
        "source": "kis",
        "is_stale_price": False,
        "price_freshness": "fresh",
        "data_state": "fresh",
    }
    quote.update(overrides)
    return quote


def _decide(*, group=None, rung=None, preview, daily="0", fallback=None, limits=None):
    return evaluate_auto_approve_eligibility(
        group=group or _group(),
        rung=rung or _rung(),
        preview=preview,
        limits=limits or _LIMITS,
        daily_notional=Decimal(daily),
        price_fallback=fallback,
    )


def _stored_inputs(decision):
    source_asof = append_auto_approve_rejection_attempt(
        {},
        decisions=[
            {
                "rung_index": 0,
                "eligible": decision.eligible,
                "reason": decision.reason,
                **decision.details,
            }
        ],
        now=datetime(2026, 9, 30, 0, 13, tzinfo=UTC),
    )
    stored = json.loads(json.dumps(source_asof))
    [attempt] = project_auto_approve_rejections(stored)
    return stored, attempt["rungs"][0]


# ---------------------------------------------------------------------------
# Quote freshness contract
# ---------------------------------------------------------------------------


def test_fresh_same_symbol_kis_quote_is_accepted():
    fallback = classify_kis_quote(_fresh_quote(), symbol="035720", market="equity_kr")
    assert fallback == PriceFallback.observed(Decimal("33800.0"))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"symbol": "035420"}, "symbol_mismatch"),
        ({"instrument_type": "equity_us"}, "market_mismatch"),
        ({"source": "yahoo"}, "source_mismatch"),
        ({"is_stale_price": True}, "quote_stale"),
        # Absent / non-bool freshness is never "fresh".
        ({"is_stale_price": None}, "freshness_unavailable"),
        ({"is_stale_price": "false"}, "freshness_unavailable"),
        ({"is_stale_price": 0}, "freshness_unavailable"),
        # Today's candle after the close is not a current price.
        ({"data_state": "market_closed"}, "session_not_live"),
        ({"data_state": "premarket_unavailable"}, "session_not_live"),
        ({"price": 0}, "price_invalid"),
        ({"price": -1}, "price_invalid"),
        ({"price": None}, "price_invalid"),
        ({"price": True}, "price_invalid"),
        ({"price": float("nan")}, "price_invalid"),
        ({"price": "abc"}, "price_invalid"),
        ({"error": "KIS down"}, "quote_unavailable"),
    ],
)
def test_unusable_quotes_fail_closed_with_a_closed_reason(overrides, reason):
    fallback = classify_kis_quote(
        _fresh_quote(**overrides), symbol="035720", market="equity_kr"
    )
    assert fallback == PriceFallback.failed(reason)


def test_missing_freshness_flag_is_not_fresh():
    quote = _fresh_quote()
    del quote["is_stale_price"]
    assert classify_kis_quote(
        quote, symbol="035720", market="equity_kr"
    ) == PriceFallback.failed("freshness_unavailable")


def test_non_mapping_quote_is_unavailable():
    for bad in (None, [], "33800"):
        assert classify_kis_quote(
            bad, symbol="035720", market="equity_kr"
        ) == PriceFallback.failed("quote_unavailable")


def test_unknown_failure_reason_cannot_be_constructed():
    with pytest.raises(ValueError):
        PriceFallback.failed("looks_fine")


@pytest.mark.asyncio
async def test_fetch_reads_same_symbol_and_market_through_quote_fn():
    calls = []

    async def quote_fn(symbol, market):
        calls.append((symbol, market))
        return _fresh_quote()

    fallback = await fetch_kis_quote_fallback(
        symbol="035720", market="equity_kr", quote_fn=quote_fn
    )
    assert calls == [("035720", "equity_kr")]
    assert fallback.price == Decimal("33800.0")


@pytest.mark.asyncio
async def test_fetch_us_is_unsupported_without_any_read():
    async def quote_fn(symbol, market):
        raise AssertionError("US get_quote has no is_stale_price; never read it")

    fallback = await fetch_kis_quote_fallback(
        symbol="AAPL", market="equity_us", quote_fn=quote_fn
    )
    assert fallback == PriceFallback.failed("market_unsupported")


@pytest.mark.asyncio
async def test_fetch_timeout_and_exception_are_closed_reasons():
    async def slow(symbol, market):
        await asyncio.sleep(5)

    async def broken(symbol, market):
        raise RuntimeError("socket closed")

    assert await fetch_kis_quote_fallback(
        symbol="035720", market="equity_kr", quote_fn=slow, timeout_seconds=0.01
    ) == PriceFallback.failed("quote_timeout")
    assert await fetch_kis_quote_fallback(
        symbol="035720", market="equity_kr", quote_fn=broken
    ) == PriceFallback.failed("quote_unavailable")


@pytest.mark.parametrize(
    ("preview", "absent"),
    [
        ({"success": True}, True),
        ({"success": True, "current_price": None}, True),
        ({"success": True, "current_price": ""}, True),
        ({"success": True, "current_price": "  "}, True),
        ({"success": True, "current_price": "33800"}, False),
        # Present but malformed is *not* absent: it rejects as before.
        ({"success": True, "current_price": "0"}, False),
        ({"success": True, "current_price": "abc"}, False),
        (None, False),
    ],
)
def test_preview_current_price_absent(preview, absent):
    assert preview_current_price_absent(preview) is absent


# ---------------------------------------------------------------------------
# AC4 golden: a present preview price is untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fallback",
    [
        None,
        PriceFallback.observed(Decimal("40000")),
        PriceFallback.observed(Decimal("32000")),
        PriceFallback.failed("quote_stale"),
    ],
)
def test_golden_present_preview_price_ignores_any_fallback(fallback):
    preview = {"success": True, "current_price": "33800"}
    decision = _decide(preview=preview, fallback=fallback)

    assert decision.eligible is True
    assert decision.reason == "eligible"
    assert decision.details == {
        "policy_version": "2026-09-30.1",
        "mode": "expanded",
        "action": "place",
        "price_source": "toss_preview",
        "current_price": "33800",
        "limit_price": "32750",
        "distance_pct": _decide(preview=preview).details["distance_pct"],
        "min_distance_pct": "3",
        "notional": "98250",
        "daily_notional_before": "0",
        "daily_notional_after": "98250",
        "daily_cap_exempt": False,
        "per_order_cap": "2000000",
        "daily_cap": "5000000",
        "loss_guard": "not_applicable",
    }
    assert decision == _decide(preview=preview)


@pytest.mark.parametrize("account_mode", ["kis_live", "upbit"])
def test_non_toss_rungs_are_byte_identical_and_never_use_the_fallback(
    account_mode,
):
    market = "crypto" if account_mode == "upbit" else "equity_kr"
    group = _group(account_mode=account_mode, market=market, symbol="KRW-BTC")
    present = {"success": True, "current_price": "33800"}
    absent = {"success": True, "price_context_message": _PRICE_CONTEXT_MESSAGE}
    observed = PriceFallback.observed(Decimal("33800"))

    with_price = _decide(group=group, preview=present, fallback=observed)
    assert with_price == _decide(group=group, preview=present)
    assert "price_source" not in with_price.details

    without_price = _decide(group=group, preview=absent, fallback=observed)
    assert without_price == _decide(group=group, preview=absent)
    assert without_price.reason == "price_or_quantity_missing"
    assert "price_fallback_reason" not in without_price.details


def test_present_but_invalid_preview_price_does_not_use_the_fallback():
    decision = _decide(
        preview={"success": True, "current_price": "0"},
        fallback=PriceFallback.observed(Decimal("33800")),
    )
    assert (decision.eligible, decision.reason) == (
        False,
        "price_or_quantity_missing",
    )
    assert decision.details["price_source"] == "toss_preview"
    assert "price_fallback_reason" not in decision.details


# ---------------------------------------------------------------------------
# AC1 / AC3: the fallback price runs every gate unchanged
# ---------------------------------------------------------------------------


def test_incident_case_is_eligible_on_the_kis_fallback_and_records_source():
    decision = _decide(
        preview={"success": True, "price_context_message": _PRICE_CONTEXT_MESSAGE},
        fallback=PriceFallback.observed(Decimal("33800")),
    )

    assert (decision.eligible, decision.reason) == (True, "eligible")
    assert decision.details["price_source"] == "kis_quote_fallback"
    assert decision.details["current_price"] == "33800"
    assert decision.details["notional"] == "98250"


_EQUIVALENCE_CASES = [
    # (label, rung overrides, daily_notional, price X)
    ("eligible resting buy", {}, "0", "33800"),
    ("buy at market is marketable", {}, "0", "32750"),
    ("buy above market is marketable", {}, "0", "32000"),
    ("per-order cap", {"quantity": Decimal("70")}, "0", "33800"),
    ("daily cap", {}, "4950000", "33800"),
    (
        "resting profit sell",
        {"side": "sell", "limit_price": Decimal("36000")},
        "0",
        "33800",
    ),
    (
        "marketable profit sell",
        {"side": "sell", "limit_price": Decimal("33000")},
        "0",
        "33800",
    ),
    (
        "sell inside break-even band",
        {"side": "sell", "limit_price": Decimal("30100")},
        "0",
        "29000",
    ),
    (
        "sell at a loss",
        {"side": "sell", "limit_price": Decimal("28000")},
        "0",
        "27000",
    ),
]


@pytest.mark.parametrize(
    ("label", "rung_overrides", "daily", "price"),
    _EQUIVALENCE_CASES,
    ids=[case[0] for case in _EQUIVALENCE_CASES],
)
def test_fallback_price_x_decides_exactly_like_preview_price_x(
    label, rung_overrides, daily, price
):
    rung = _rung(**rung_overrides)
    # Sell cases carry the preview's avg cost so the loss/profit gates run.
    extras = {"avg_buy_price": "30000"} if rung_overrides.get("side") == "sell" else {}
    from_preview = _decide(
        rung=rung,
        daily=daily,
        preview={"success": True, "current_price": price, **extras},
    )
    from_fallback = _decide(
        rung=rung,
        daily=daily,
        preview={"success": True, **extras},
        fallback=PriceFallback.observed(Decimal(price)),
    )

    assert from_fallback.eligible == from_preview.eligible, label
    assert from_fallback.reason == from_preview.reason, label
    expected = dict(from_preview.details)
    expected["price_source"] = "kis_quote_fallback"
    expected["current_price"] = expected.get("current_price", price)
    assert from_fallback.details == expected, label


def test_fallback_price_that_fails_a_gate_rejects():
    decision = _decide(
        preview={"success": True},
        fallback=PriceFallback.observed(Decimal("32700")),
    )
    assert (decision.eligible, decision.reason) == (False, "marketable_not_resting")
    assert decision.details["price_source"] == "kis_quote_fallback"


def test_tag_and_loss_cut_gates_still_run_before_the_price_is_read():
    observed = PriceFallback.observed(Decimal("33800"))
    loss_cut = _decide(
        group=_group(exit_intent="loss_cut"),
        preview={"success": True},
        fallback=observed,
    )
    assert loss_cut.reason == "loss_cut_intent"
    tagged = _decide(
        group=_group(thesis="policy_deviation: add anyway"),
        preview={"success": True},
        fallback=observed,
    )
    assert tagged.reason == "approval_required_tag"
    failed_preview = _decide(
        preview={"success": False, "error": "guard"}, fallback=observed
    )
    assert failed_preview.reason == "preview_guard_failed"


# ---------------------------------------------------------------------------
# AC2: a failed fallback keeps both diagnostics on the rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(PRICE_FALLBACK_FAILURE_REASONS))
def test_failed_fallback_rejection_keeps_preview_message_and_reason(reason):
    decision = _decide(
        preview={"success": True, "price_context_message": _PRICE_CONTEXT_MESSAGE},
        fallback=PriceFallback.failed(reason),
    )

    assert (decision.eligible, decision.reason) == (
        False,
        "price_or_quantity_missing",
    )
    assert decision.details["missing_inputs"] == ["current_price"]
    assert decision.details["price_fallback_reason"] == reason
    assert decision.details["price_context_message"] == _PRICE_CONTEXT_MESSAGE
    assert "price_source" not in decision.details

    stored, rung = _stored_inputs(decision)
    assert rung["reason_code"] == "price_or_quantity_missing"
    assert rung["inputs"]["missing_inputs"] == ["current_price"]
    assert rung["inputs"]["price_fallback_reason"] == reason
    assert rung["inputs"]["price_context_message"] == _PRICE_CONTEXT_MESSAGE
    card = build_auto_approve_rejection_card_block(stored)
    assert f"`price_or_quantity_missing` / `{reason}`" in card


def test_malformed_fallback_object_is_recorded_as_unrecognized():
    decision = _decide(
        preview={"success": True},
        fallback=PriceFallback(price=None, failure_reason=None),
    )
    assert decision.reason == "price_or_quantity_missing"
    assert decision.details["price_fallback_reason"] == "unrecognized"
    _stored, rung = _stored_inputs(decision)
    assert rung["inputs"]["price_fallback_reason"] == "unrecognized"


def test_audit_projects_price_source_and_retry_flag_only_from_closed_values():
    decision = {
        "rung_index": 0,
        "eligible": False,
        "reason": "marketable_not_resting",
        "price_source": "kis_quote_fallback",
        "price_retry_reevaluation": True,
    }
    bogus = {
        "rung_index": 1,
        "eligible": False,
        "reason": "price_or_quantity_missing",
        "price_source": "made_up",
        "price_fallback_reason": "<script>",
        "price_retry_reevaluation": "yes",
    }
    stored = append_auto_approve_rejection_attempt(
        {}, decisions=[decision, bogus], now=datetime(2026, 9, 30, tzinfo=UTC)
    )
    [attempt] = project_auto_approve_rejections(stored)
    first, second = attempt["rungs"]
    assert first["inputs"]["price_source"] == "kis_quote_fallback"
    assert first["inputs"]["price_retry_reevaluation"] is True
    assert "price_source" not in second["inputs"]
    assert "price_fallback_reason" not in second["inputs"]
    assert "price_retry_reevaluation" not in second["inputs"]
