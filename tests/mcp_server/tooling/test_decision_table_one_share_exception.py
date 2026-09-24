"""§664 — KR new-entry one-share exception in the decision-table validator.

``buy.per_symbol_notional_krw_range`` is [200,000, 400,000] KRW for new
entries; its ``one_share_exception`` admits exactly one share of a symbol whose
single share already exceeds 400,000, up to ``absolute_ceiling_krw``
(10,000,000), with at most ``max_deep_rungs`` (1) buy rung per symbol. The
per-order auto-approve cap (2,000,000) is not relaxed: an exception order
above it is demoted to a human card, not rejected.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest

from app.services.decision_table_validate import decision_table_validate
from app.services.decision_table_validate import validator as validator_module
from app.services.decision_table_validate.one_share_exception import (
    DENIED_ABOVE_CEILING,
    DENIED_HELD_POSITION,
    DENIED_NO_FLAT_PROOF,
    DENIED_NOT_A_SINGLE_SHARE,
    DENIED_NOT_ONE_SYMBOL,
    DENIED_PARKING_SYMBOL,
    DENIED_SHARE_WITHIN_BAND,
    OneShareException,
    one_share_exception_denial,
    one_share_exception_for,
    row_has_position_evidence,
)
from app.services.order_proposals.auto_approve import (
    evaluate_auto_approve_eligibility,
    limits_for_market,
)
from app.services.trading_policy_service import get_policy_for, load_trading_policy

pytestmark = pytest.mark.unit

_KR_BAND = "buy.per_symbol_notional_krw_range"


def _canonical_hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _buy_row(
    *,
    scenario_id: str = "new-entry-000660",
    symbol: str = "000660",
    rungs: list[dict[str, Any]] | None = None,
    account_mode: str = "toss_live",
    side: str = "buy",
    conditions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "priority": 1,
        "symbols": [symbol],
        "conditions": conditions
        if conditions is not None
        else [
            {
                "metric": "live_price_band",
                "source": "get_quote(symbol,market='kr').price",
                "operator": "between",
                "value": {"min_inclusive": 1700000, "max_exclusive": 1900000},
                "max_age_seconds": 300,
            },
            _flat(symbol),
        ],
        "action": {
            "proposal_action": "place",
            "account_mode": account_mode,
            "side": side,
            "order_type": "limit",
            "rungs": rungs
            if rungs is not None
            else [_rung(1776000, 1776000, qty=1, tick=1000)],
        },
        "invalidation": [],
    }


def _flat(symbol: str = "000660", account_mode: str = "toss_live") -> dict[str, Any]:
    """The affirmative new-entry proof the exception requires."""

    return {
        "metric": "position_quantity",
        "source": (
            f"get_holdings.accounts[{account_mode}].positions[{symbol}].quantity"
        ),
        "operator": "eq",
        "value": 0,
        "max_age_seconds": 300,
    }


def _rung(price_min: int, price_max: int, *, qty: int, tick: int, n: int = 1):
    return {
        "rung": n,
        "price_min": price_min,
        "price_max": price_max,
        "qty": qty,
        "tick": tick,
    }


def _envelope(*rows: dict[str, Any], market: str = "kr") -> dict[str, Any]:
    decision_table = {"no_match_action": "no_proposal", "rows": list(rows)}
    return {
        "schema_version": "kr-nxt-decision-table/v1.1",
        "correlation_id": "kr-nxt-prep-2026-09-28",
        "trading_date": "2026-09-28",
        "market": market,
        "decision_table": decision_table,
        "decision_table_hash": _canonical_hash(decision_table),
    }


def _validate(*rows: dict[str, Any], market: str = "kr") -> dict[str, Any]:
    return decision_table_validate(_envelope(*rows, market=market), market)


def _rules(result: dict[str, Any]) -> list[str]:
    return [item["rule"] for item in result["violations"]]


def _sizing(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in result["violations"] if item["rule"] == "sizing_band_violation"
    ]


# --------------------------------------------------------------------------
# The shipped policy.
# --------------------------------------------------------------------------


def test_shipped_kr_band_declares_the_exception_and_keeps_every_cap():
    document = load_trading_policy()
    band = document.thresholds[_KR_BAND]
    assert band.value == [200000, 400000]
    assert band.one_share_exception is not None
    assert band.one_share_exception.enabled is True
    assert band.one_share_exception.absolute_ceiling_krw == 10000000
    assert band.one_share_exception.absolute_ceiling_usd is None
    assert band.one_share_exception.max_deep_rungs == 1
    auto_approve = document.order_proposals.auto_approve
    assert auto_approve.per_order_cap["kr"] == 2000000
    assert auto_approve.daily_cap["kr"] == 5000000


def test_get_trading_policy_projects_only_the_krw_ceiling_for_kr():
    kr = get_policy_for("kr", "buy")["thresholds"][_KR_BAND]["one_share_exception"]
    us = get_policy_for("us", "buy")["thresholds"]["buy.per_symbol_notional_usd_range"][
        "one_share_exception"
    ]
    assert kr == {
        "enabled": True,
        "absolute_ceiling_krw": 10000000.0,
        "max_deep_rungs": 1,
    }
    # The US projection keeps its pre-§664 keyset exactly.
    assert us == {"enabled": True, "absolute_ceiling_usd": 10000.0, "max_deep_rungs": 1}


# --------------------------------------------------------------------------
# AC (2): allowed / rejected cases.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "tick"),
    [(400500, 500), (1776000, 1000), (2500000, 1000), (10000000, 1000)],
)
def test_one_share_above_the_band_is_admitted_up_to_the_ceiling(price, tick):
    result = _validate(_buy_row(rungs=[_rung(price, price, qty=1, tick=tick)]))
    assert result["valid"] is True, result["violations"]
    assert _sizing(result) == []


def test_one_share_one_tick_over_the_ceiling_is_rejected():
    result = _validate(_buy_row(rungs=[_rung(10001000, 10001000, qty=1, tick=1000)]))
    assert result["valid"] is False
    (violation,) = _sizing(result)
    assert DENIED_ABOVE_CEILING in violation["expected"]


def test_ceiling_is_judged_at_price_max_not_price_min():
    result = _validate(_buy_row(rungs=[_rung(9999000, 10001000, qty=1, tick=1000)]))
    assert result["valid"] is False
    assert DENIED_ABOVE_CEILING in _sizing(result)[0]["expected"]


@pytest.mark.parametrize(("qty", "price"), [(2, 450000), (2, 1776000), (3, 400500)])
def test_more_than_one_share_over_the_band_is_rejected(qty, price):
    tick = 500 if price < 500000 else 1000
    result = _validate(_buy_row(rungs=[_rung(price, price, qty=qty, tick=tick)]))
    assert result["valid"] is False
    assert DENIED_NOT_A_SINGLE_SHARE in _sizing(result)[0]["expected"]


def test_in_band_single_share_needs_no_exception_and_below_band_is_not_covered():
    in_band = _validate(_buy_row(rungs=[_rung(350000, 350000, qty=1, tick=500)]))
    assert in_band["valid"] is True, in_band["violations"]

    below = _validate(_buy_row(rungs=[_rung(150000, 150000, qty=1, tick=100)]))
    assert below["valid"] is False
    (violation,) = _sizing(below)
    # A below-band order is never an exception candidate.
    assert "one_share_exception" not in violation["expected"]


def test_over_per_order_cap_one_share_is_valid_and_then_carded_not_rejected():
    """AC: > 2,000,000 → human card via per_order_cap_exceeded, not a block."""

    result = _validate(_buy_row(rungs=[_rung(2001000, 2001000, qty=1, tick=1000)]))
    assert result["valid"] is True, result["violations"]

    class _Group:
        action = "place"
        order_type = "limit"
        exit_intent = None
        account_mode = "kis_live"
        market = "equity_kr"
        side = "buy"
        symbol = "000660"
        thesis = "one-share new entry"
        strategy = "one_share_exception"
        rationale = None
        lot_context = None
        proposer = "session"
        target_broker_order_id = None

    class _OneShare:
        side = "buy"
        quantity = Decimal("1")
        limit_price = Decimal("2001000")

    limits = limits_for_market("equity_kr")
    assert limits is not None
    assert limits.per_order_cap == Decimal("2000000")
    decision = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_OneShare(),
        preview={"success": True, "current_price": "2100000"},
        limits=limits,
        daily_notional=Decimal("0"),
    )
    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"

    class _UnderCap(_OneShare):
        limit_price = Decimal("1776000")

    under = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_UnderCap(),
        preview={"success": True, "current_price": "1850000"},
        limits=limits,
        daily_notional=Decimal("0"),
    )
    assert under.reason != "per_order_cap_exceeded"


# --------------------------------------------------------------------------
# Tester-directed surfaces: sells, held symbols, rung count, scope.
# --------------------------------------------------------------------------


def test_sell_rows_never_consult_the_exception(monkeypatch):
    def _boom(**_kwargs):
        raise AssertionError("one_share_exception consulted for a sell")

    monkeypatch.setattr(validator_module, "one_share_exception_denial", _boom)
    sell = _buy_row(side="sell", rungs=[_rung(1776000, 1776000, qty=1, tick=1000)])
    result = _validate(sell)
    assert _sizing(result) == []


@pytest.mark.parametrize(
    "held_condition",
    [
        {"metric": "position_quantity", "operator": "eq", "value": 3},
        {"metric": "position_quantity", "operator": "gte", "value": 0},
        {"metric": "position_avg_buy_price", "operator": "eq", "value": 1500000},
        {"metric": "holding_quantity", "operator": "gt", "value": 0},
    ],
)
def test_rows_with_held_position_evidence_are_denied(held_condition):
    """Held evidence wins even next to a valid flat proof."""

    condition = {"source": "get_holdings", "max_age_seconds": 300, **held_condition}
    result = _validate(_buy_row(conditions=[_flat(), condition]))
    assert result["valid"] is False
    assert DENIED_HELD_POSITION in _sizing(result)[0]["expected"]


@pytest.mark.parametrize(
    "held_condition",
    [
        # tester round 1, BLOCKER 1 — spellings outside the old closed list
        {"metric": "held_qty", "source": "get_holdings", "operator": "gt", "value": 0},
        {"metric": "lot_size_now", "source": "x", "operator": "gt", "value": 0},
        {"metric": "Shares-Owned", "source": "x", "operator": "gt", "value": 0},
        {"metric": "live_price_band", "source": "get_holdings.positions[000660]"},
        {"metric": "ＨＥＬＤ", "source": "x", "operator": "gt", "value": 0},
    ],
)
def test_any_holding_shaped_metric_or_source_is_held_evidence(held_condition):
    condition = {"max_age_seconds": 300, **held_condition}
    result = _validate(_buy_row(conditions=[_flat(), condition]))
    assert result["valid"] is False
    assert DENIED_HELD_POSITION in _sizing(result)[0]["expected"]


@pytest.mark.parametrize("key", ["position_qty", "held_quantity", "lot_context"])
def test_any_holding_shaped_action_or_row_key_is_held_evidence(key):
    on_action = _buy_row()
    on_action["action"][key] = 3
    on_row = _buy_row()
    on_row[key] = 3
    for row in (on_action, on_row):
        result = _validate(row)
        assert result["valid"] is False
        assert DENIED_HELD_POSITION in _sizing(result)[0]["expected"]


@pytest.mark.parametrize(
    "flat_override",
    [
        {"value": "0"},
        {"value": False},
        {"value": 1},
        {"operator": "lte"},
        {"metric": "Position_Quantity"},
        {"source": "get_holdings.accounts[toss_live].positions[005930].quantity"},
        {"source": None},
    ],
)
def test_flat_proof_must_be_exact_and_about_this_symbol(flat_override):
    live_band = _buy_row()["conditions"][0]
    flat = {**_flat(), **flat_override}
    result = _validate(_buy_row(conditions=[live_band, flat]))
    assert result["valid"] is False


def test_silence_about_holdings_is_not_a_new_entry():
    live_band = _buy_row()["conditions"][0]
    result = _validate(_buy_row(conditions=[live_band]))
    assert result["valid"] is False
    assert DENIED_NO_FLAT_PROOF in _sizing(result)[0]["expected"]


def test_avg_price_on_the_action_is_held_evidence():
    row = _buy_row()
    row["action"]["avg_price"] = 1500000
    result = _validate(row)
    assert result["valid"] is False
    assert DENIED_HELD_POSITION in _sizing(result)[0]["expected"]


def test_explicit_flat_position_is_a_new_entry():
    result = _validate(_buy_row(conditions=[_flat()]))
    assert result["valid"] is True, result["violations"]
    float_zero = {**_flat(), "value": 0.0}
    result = _validate(_buy_row(conditions=[float_zero]))
    assert result["valid"] is True, result["violations"]


def test_two_rungs_in_one_row_exceed_max_deep_rungs():
    rungs = [
        _rung(1776000, 1776000, qty=1, tick=1000, n=1),
        _rung(1700000, 1700000, qty=1, tick=1000, n=2),
    ]
    result = _validate(_buy_row(rungs=rungs))
    assert result["valid"] is False
    assert "one_share_exception_rung_limit" in _rules(result)


def test_same_symbol_across_rows_and_accounts_shares_one_rung_budget():
    first = _buy_row(scenario_id="a")
    second = _buy_row(
        scenario_id="b",
        account_mode="kis_live",
        rungs=[_rung(1700000, 1700000, qty=1, tick=1000)],
    )
    result = _validate(first, second)
    assert result["valid"] is False
    limited = [
        item["row"]
        for item in result["violations"]
        if item["rule"] == "one_share_exception_rung_limit"
    ]
    assert limited == [0, 1]


def test_an_in_band_rung_of_an_exception_symbol_still_counts():
    first = _buy_row(scenario_id="a")
    second = _buy_row(scenario_id="b", rungs=[_rung(350000, 350000, qty=1, tick=500)])
    result = _validate(first, second)
    assert "one_share_exception_rung_limit" in _rules(result)


def test_different_symbols_each_get_their_own_rung():
    first = _buy_row(scenario_id="a", symbol="000660")
    second = _buy_row(scenario_id="b", symbol="005930")
    result = _validate(first, second)
    assert result["valid"] is True, result["violations"]


@pytest.mark.parametrize("parking_symbol", ["459580", "357870"])
def test_cash_parking_symbols_never_take_the_exception(parking_symbol):
    """Tester round 1, BLOCKER 2: parking's raised 10M cap must not combine."""

    row = _buy_row(
        symbol=parking_symbol,
        rungs=[_rung(2500000, 2500000, qty=1, tick=1000)],
    )
    result = _validate(row)
    assert result["valid"] is False
    assert DENIED_PARKING_SYMBOL in _sizing(result)[0]["expected"]


@pytest.mark.parametrize("variant", ["000660 ", " 000660", "０００６６０", "000660\t"])
def test_padded_or_widened_symbol_shares_the_rung_budget(variant):
    """Tester round 1, BLOCKER 3: the ledger key is normalized."""

    first = _buy_row(scenario_id="a")
    second = _buy_row(
        scenario_id="b",
        account_mode="kis_live",
        symbol=variant,
        rungs=[_rung(1700000, 1700000, qty=1, tick=1000)],
    )
    result = _validate(first, second)
    assert result["valid"] is False
    assert "one_share_exception_rung_limit" in _rules(result)


@pytest.mark.parametrize(
    "symbols", [["000660", "000660"], ["000660 "], ["0006600"], [660], []]
)
def test_exception_needs_exactly_one_canonical_symbol(symbols):
    row = _buy_row()
    row["symbols"] = symbols
    result = _validate(row)
    assert result["valid"] is False
    assert DENIED_NOT_ONE_SYMBOL in _sizing(result)[0]["expected"]


def test_multi_symbol_row_is_denied():
    row = _buy_row()
    row["symbols"] = ["000660", "005930"]
    result = _validate(row)
    assert result["valid"] is False
    assert DENIED_NOT_ONE_SYMBOL in _sizing(result)[0]["expected"]


def test_us_band_exception_is_not_honoured_by_this_change():
    row = _buy_row(symbol="AVGO", rungs=[_rung(600, 600, qty=1, tick=1)])
    result = _validate(row, market="us")
    assert "sizing_band_violation" in _rules(result)


def test_disabled_or_missing_exception_falls_back_to_the_band(monkeypatch):
    real = get_policy_for("kr", "buy")

    def _patched(exception):
        def _get(market, lane):
            policy = deepcopy(real)
            policy["thresholds"][_KR_BAND]["one_share_exception"] = exception
            return policy

        return _get

    for exception in (
        None,
        {"enabled": False, "absolute_ceiling_krw": 10000000, "max_deep_rungs": 1},
        {"enabled": True, "absolute_ceiling_usd": 10000000, "max_deep_rungs": 1},
    ):
        monkeypatch.setattr(validator_module, "get_policy_for", _patched(exception))
        result = _validate(_buy_row())
        assert result["valid"] is False, exception
        assert "sizing_band_violation" in _rules(result)


# --------------------------------------------------------------------------
# Pure helpers.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": "true", "absolute_ceiling_krw": 10000000, "max_deep_rungs": 1},
        {"enabled": True, "absolute_ceiling_krw": "10000000", "max_deep_rungs": 1},
        {"enabled": True, "absolute_ceiling_krw": float("inf"), "max_deep_rungs": 1},
        {"enabled": True, "absolute_ceiling_krw": 0, "max_deep_rungs": 1},
        {"enabled": True, "absolute_ceiling_krw": 10000000, "max_deep_rungs": 0},
        {"enabled": True, "absolute_ceiling_krw": 10000000, "max_deep_rungs": True},
        {"enabled": True, "absolute_ceiling_krw": True, "max_deep_rungs": 1},
    ],
)
def test_malformed_exception_fails_closed(raw):
    assert one_share_exception_for("kr", {"one_share_exception": raw}) is None


def test_exception_reader_is_kr_only():
    raw = {"enabled": True, "absolute_ceiling_krw": 10000000, "max_deep_rungs": 1}
    assert one_share_exception_for("kr", {"one_share_exception": raw}) == (
        OneShareException(ceiling=Decimal("10000000"), max_deep_rungs=1)
    )
    # A US-shaped block (the real shipped one) must still read as "no exception".
    us_block = get_policy_for("us", "buy")["thresholds"][
        "buy.per_symbol_notional_usd_range"
    ]
    assert us_block["one_share_exception"]["enabled"] is True
    assert one_share_exception_for("us", us_block) is None
    both = {**raw, "absolute_ceiling_usd": 10000}
    for market in ("us", "crypto", None):
        assert one_share_exception_for(market, {"one_share_exception": both}) is None


def test_row_without_conditions_has_no_position_evidence():
    assert row_has_position_evidence({"action": {}}) is False


def test_unreadable_price_max_cannot_prove_the_ceiling():
    """Scalar rungs are blocked anyway, but the exception must not ride along."""

    row = _buy_row()
    row["action"]["rungs"] = {"price_min": 1776000, "price_max": "n/a", "qty": 1}
    result = _validate(row)
    assert DENIED_ABOVE_CEILING in _sizing(result)[0]["expected"]


def test_predicate_denies_a_share_that_fits_inside_the_band():
    exception = OneShareException(ceiling=Decimal("10000000"), max_deep_rungs=1)
    denial = one_share_exception_denial(
        row=_buy_row(),
        price_min=Decimal("400000"),
        price_max=Decimal("400000"),
        qty=Decimal("1"),
        band_high=Decimal("400000"),
        exception=exception,
    )
    assert denial == DENIED_SHARE_WITHIN_BAND
