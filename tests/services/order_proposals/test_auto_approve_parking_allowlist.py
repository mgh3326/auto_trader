"""§163차/§170차 — cash-parking ticker allowlist.

§163차 RAISES the per-order cap for two tickers rather than removing it, so
§106차's "maximum loss of one automation error" boundary keeps its shape, and
adds a second cumulative boundary behind it. These tests hold both in place:
the per-order check still runs at the raised value (and at USD 1,500 for
everything else), the allowlist is closed and unwidenable at runtime,
membership is exact-element (never substring or case-folded), the cumulative
USD 10,000 cap is enforced from broker-origin plus durable exposure, every way
of failing to read that exposure rejects, and everything §163차 did NOT
authorize is proven unchanged.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.order_proposals import parking_allowlist
from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)
from app.services.order_proposals.parking_allowlist import (
    PARKING_ALLOWLIST_SCOPES,
    PARKING_CUMULATIVE_CAP_KRW,
    PARKING_CUMULATIVE_CAP_USD,
    PARKING_DAILY_CAP_EXEMPT_KR,
    PARKING_DAILY_CAP_EXEMPT_US,
    PARKING_PER_ORDER_CAP_KRW,
    PARKING_PER_ORDER_CAP_USD,
    ParkingExposure,
    canonical_eligibility_symbol,
    canonical_exposure_symbol,
    is_parking_allowlisted,
    is_parking_daily_cap_exempt,
    parking_scope,
)
from app.services.order_proposals.parking_exposure import (
    load_parking_exposure,
)
from app.services.order_proposals.parking_exposure import (
    sum_parking_exposure as _sum_parking_exposure,
)

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

_US_EXPANDED = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("1500"),
    daily_cap=Decimal("20000"),
    policy_version="test-policy",
    mode="expanded",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("90"),
)
_US_OFF = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("1500"),
    daily_cap=Decimal("20000"),
    policy_version="test-policy",
    mode="off",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("90"),
)
_KR_EXPANDED = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    # This fixture isolates the parking per-order boundary; production's
    # ordinary KR daily breaker remains separately tested and unchanged.
    per_order_cap=Decimal("2000000"),
    daily_cap=Decimal("20000000"),
    policy_version="test-policy",
    mode="expanded",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("47.4"),
)


def _group(**overrides):
    values = {
        "symbol": "SGOV",
        "market": "equity_us",
        "account_mode": "kis_live",
        "broker_account_id": "acct-1",
        "order_type": "limit",
        "action": "place",
        "exit_intent": None,
        "thesis": "park idle USD in SGOV",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "rung_index": 0,
        "side": "buy",
        "limit_price": Decimal("100"),
        "quantity": Decimal("20"),
        "notional": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _decide(*, group=None, rung=None, preview=None, limits=None, daily=None, exposure):
    return evaluate_auto_approve_eligibility(
        group=group if group is not None else _group(),
        rung=rung if rung is not None else _rung(),
        preview=preview
        if preview is not None
        else {"success": True, "current_price": "100"},
        limits=limits if limits is not None else _US_EXPANDED,
        daily_notional=Decimal("0") if daily is None else daily,
        parking_exposure=exposure,
    )


def _flat() -> ParkingExposure:
    return ParkingExposure.observed(Decimal("0"))


def _us_scope():
    scope = parking_scope(symbol="SGOV", account_mode="kis_live", market="equity_us")
    assert scope is not None
    return scope


def sum_parking_exposure(rows):
    """Preserve the §163 US fixture shorthand with an explicit bound scope."""
    return _sum_parking_exposure(rows, scope=_us_scope())


async def _no_pending() -> Decimal:
    """No same-day auto-approved parking buys yet."""
    return Decimal("0")


# --------------------------------------------------------------------------
# 1. the allowlist is closed and cannot be widened at runtime
# --------------------------------------------------------------------------


def test_allowlist_constants_are_exactly_the_authorized_scope():
    assert {
        (scope.symbol, scope.account_mode, scope.market)
        for scope in PARKING_ALLOWLIST_SCOPES
    } == {
        ("SGOV", "kis_live", "equity_us"),
        ("BIL", "kis_live", "equity_us"),
        ("459580", "kis_live", "equity_kr"),
        ("357870", "kis_live", "equity_kr"),
    }
    # Cap pairs are selected only through the same immutable scope record.
    assert PARKING_PER_ORDER_CAP_USD == Decimal("10000")
    assert PARKING_CUMULATIVE_CAP_USD == Decimal("10000")
    assert PARKING_PER_ORDER_CAP_KRW == Decimal("10000000")
    assert PARKING_CUMULATIVE_CAP_KRW == Decimal("15000000")
    assert PARKING_DAILY_CAP_EXEMPT_US is True
    assert PARKING_DAILY_CAP_EXEMPT_KR is True
    assert all(scope.daily_cap_exempt is True for scope in PARKING_ALLOWLIST_SCOPES)


def test_allowlist_containers_are_immutable_frozensets():
    assert isinstance(PARKING_ALLOWLIST_SCOPES, frozenset)
    assert not hasattr(PARKING_ALLOWLIST_SCOPES, "add")
    assert not hasattr(PARKING_ALLOWLIST_SCOPES, "update")


def test_allowlist_module_reads_no_settings_env_db_or_policy():
    """The allowlist is not fed by anything configurable, as written.

    🔴 Scope of this guard, stated honestly: **accidental prevention + static
    detection, not structural impossibility.** It asserts the module's import
    surface and rejects the obvious spellings of settings/env/file/DB access,
    so re-introducing configurability the ordinary way turns red in CI. A
    determined obfuscation (a string-assembled ``__import__``) defeats it, and
    enumerating spellings is a losing game, so this is deliberately not
    hardened further. The real boundary is that this file is operator-PR-only.
    """
    source = Path(inspect.getfile(parking_allowlist)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert imported == {
        "__future__",
        "dataclasses",
        "decimal",
        "typing",
        "app.core.symbol",
    }

    # Identifiers, not prose: the docstring is allowed to say "settings", the
    # code is not allowed to read them. See the docstring above for what this
    # does and does not prove.
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    for forbidden in (
        "settings",
        "getenv",
        "environ",
        "os",
        "open",
        "load_trading_policy",
        "AsyncSessionLocal",
    ):
        assert forbidden not in referenced


def test_parking_exposure_record_is_frozen():
    exposure = ParkingExposure.observed(Decimal("1"))
    with pytest.raises(FrozenInstanceError):
        exposure.exposure = Decimal("999")  # type: ignore[misc]


def test_unavailable_reason_vocabulary_is_closed():
    """An unrecognized reason collapses, never leaks into the audit trail."""
    assert ParkingExposure.unavailable("row_not_a_mapping").unavailable_reason == (
        "row_not_a_mapping"
    )
    assert ParkingExposure.unavailable("../../etc/passwd").unavailable_reason == (
        "fetch_failed"
    )


# --------------------------------------------------------------------------
# 2. normalization / confusable attacks — every one must be rejected
# --------------------------------------------------------------------------


class _LyingStr(str):
    """A str subclass whose normalization methods lie."""

    def upper(self) -> str:  # pragma: no cover - defensive
        return "SGOV"

    def strip(self, *args, **kwargs) -> str:  # pragma: no cover - defensive
        return "SGOV"


@pytest.mark.parametrize(
    "candidate",
    [
        "sgov",  # lowercase
        "Sgov",  # mixed case
        " SGOV ",  # surrounding whitespace
        "SGOV ",  # trailing whitespace
        "\tSGOV",  # leading tab
        "SGOV\n",  # trailing newline
        "SGOV.U",  # different share class
        "SGOVX",  # different instrument (suffix)
        "XSGOV",  # different instrument (prefix)
        "BILS",  # different instrument
        "BILL",  # different instrument
        "BIL.TO",  # foreign listing
        "BIL-TO",  # foreign listing, hyphen separator
        "BIL/TO",  # foreign listing, slash separator
        "SG",  # substring of SGOV
        "BI",  # substring of BIL
        "ＳＧＯＶ",  # fullwidth look-alike
        "ЅGOV",  # U+0405 CYRILLIC CAPITAL DZE look-alike
        "ſGOV",  # U+017F LONG S — .upper() would fold this to "SGOV"
        "ВIL",  # U+0412 CYRILLIC VE look-alike
        "",  # empty
        "   ",  # whitespace only
        None,
        123,
        b"SGOV",
        ["SGOV"],
        _LyingStr("nope"),
    ],
)
def test_confusable_symbols_are_not_allowlisted(candidate):
    assert (
        is_parking_allowlisted(
            symbol=candidate, account_mode="kis_live", market="equity_us"
        )
        is False
    )


def test_long_s_would_have_passed_a_naive_upper_based_matcher():
    """Proves the ASCII gate is load-bearing, not decoration."""
    assert "ſGOV".upper() == "SGOV"  # the attack a naive matcher admits
    assert canonical_eligibility_symbol("ſGOV") is None


def test_exact_canonical_spellings_are_allowlisted():
    for symbol in ("SGOV", "BIL"):
        assert (
            is_parking_allowlisted(
                symbol=symbol, account_mode="kis_live", market="equity_us"
            )
            is True
        )

    for symbol in ("459580", "357870"):
        assert (
            is_parking_allowlisted(
                symbol=symbol, account_mode="kis_live", market="equity_kr"
            )
            is True
        )


# --------------------------------------------------------------------------
# 3. market and account scoping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("account_mode", "market"),
    [
        ("kis_live", "equity_kr"),  # same four characters, KR market
        ("kis_live", "crypto"),
        ("upbit", "crypto"),
        ("toss_live", "equity_us"),  # veto-capable, but exposure is not readable
        ("toss_live", "equity_kr"),
        ("kis_mock", "equity_us"),
        ("db_simulated", "equity_us"),
        (None, "equity_us"),
        ("kis_live", None),
    ],
)
def test_allowlist_is_scoped_to_kis_live_equity_us(account_mode, market):
    assert (
        is_parking_allowlisted(symbol="SGOV", account_mode=account_mode, market=market)
        is False
    )


def test_kr_proposal_named_sgov_keeps_the_ordinary_gates():
    """A KR-market SGOV gets no parking treatment: USD 1,500 cap applies."""
    decision = _decide(
        group=_group(market="equity_kr", account_mode="kis_live"),
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"


def test_symbol_market_pairs_cannot_open_a_cross_product_mutant():
    """Removing the tuple binding makes either assertion RED immediately."""
    assert not is_parking_allowlisted(
        symbol="SGOV", account_mode="kis_live", market="equity_kr"
    )
    assert not is_parking_allowlisted(
        symbol="459580", account_mode="kis_live", market="equity_us"
    )
    assert not is_parking_allowlisted(
        symbol="357870", account_mode="kis_live", market="equity_us"
    )


@pytest.mark.parametrize(
    ("symbol", "account_mode", "market", "mode"),
    [
        ("SGOVX", "kis_live", "equity_us", "expanded"),
        ("SGOV", "toss_live", "equity_us", "expanded"),
        ("SGOV", "kis_live", "equity_kr", "expanded"),
        ("459580", "kis_live", "equity_us", "expanded"),
        ("SGOV", "kis_live", "equity_us", "off"),
    ],
)
def test_daily_cap_exemption_uses_the_exact_parking_scope(
    symbol, account_mode, market, mode
):
    """Loosening the exclusion predicate makes this AssertionError-red."""
    assert not is_parking_daily_cap_exempt(
        symbol=symbol,
        account_mode=account_mode,
        market=market,
        mode=mode,
    )


@pytest.mark.parametrize(
    ("symbol", "market"),
    [
        ("SGOV", "equity_us"),
        ("BIL", "equity_us"),
        ("459580", "equity_kr"),
        ("357870", "equity_kr"),
    ],
)
def test_every_enabled_parking_scope_is_daily_cap_exempt_in_expanded_mode(
    symbol, market
):
    assert is_parking_daily_cap_exempt(
        symbol=symbol,
        account_mode="kis_live",
        market=market,
        mode="expanded",
    )


def test_cap_currency_is_bound_to_the_same_scope_tuple():
    """A KRW/USD selector swap makes these assertions RED (no FX fallback)."""
    us = parking_scope(symbol="SGOV", account_mode="kis_live", market="equity_us")
    kr_scopes = [
        parking_scope(symbol=symbol, account_mode="kis_live", market="equity_kr")
        for symbol in ("459580", "357870")
    ]
    assert us is not None and all(scope is not None for scope in kr_scopes)
    assert (us.currency, us.per_order_cap, us.cumulative_cap) == (
        "USD",
        Decimal("10000"),
        Decimal("10000"),
    )
    for kr in kr_scopes:
        assert kr is not None
        assert (kr.currency, kr.per_order_cap, kr.cumulative_cap) == (
            "KRW",
            Decimal("10000000"),
            Decimal("15000000"),
        )


# --------------------------------------------------------------------------
# 4. the three authorized directions
# --------------------------------------------------------------------------


def test_allowlisted_marketable_buy_over_the_ordinary_cap_is_eligible():
    """Both §163차 changes at once: marketable buy + RAISED per-order cap."""
    decision = _decide(exposure=_flat())

    assert decision.eligible is True
    assert decision.reason == "eligible"
    assert decision.details["parking_allowlist"] == "SGOV"
    assert decision.details["per_order_cap_basis"] == "parking_raised"
    assert decision.details["marketability"] == "parking_allowlist_marketable"
    # USD 2,000 is above the ordinary USD 1,500 cap; the effective cap for a
    # parking rung is the raised USD 10,000, and it is reported as such.
    assert decision.details["notional"] == "2000"
    assert decision.details["per_order_cap"] == "10000"
    assert decision.details["parking_exposure_before"] == "0"
    assert decision.details["parking_exposure_after"] == "2000"
    assert decision.details["parking_cap"] == "10000"


@pytest.mark.parametrize("symbol", ("459580", "357870"))
def test_kr_parking_per_order_cap_boundary_is_exactly_10m_krw(symbol):
    group = _group(
        symbol=symbol,
        market="equity_kr",
        thesis=f"park idle KRW in {symbol}",
    )
    exact = _decide(
        group=group,
        rung=_rung(limit_price=Decimal("10000"), quantity=Decimal("1000")),
        preview={"success": True, "current_price": "10000"},
        limits=_KR_EXPANDED,
        exposure=_flat(),
    )
    over = _decide(
        group=group,
        rung=_rung(limit_price=Decimal("10000.001"), quantity=Decimal("1000")),
        preview={"success": True, "current_price": "10000.001"},
        limits=_KR_EXPANDED,
        exposure=_flat(),
    )

    assert exact.eligible is True
    assert exact.details["per_order_cap"] == "10000000"
    assert exact.details["parking_cap"] == "15000000"
    assert exact.details["parking_currency"] == "KRW"
    assert over.reason == "per_order_cap_exceeded"
    assert over.details["per_order_cap"] == "10000000"


def test_single_parking_order_over_the_raised_cap_is_rejected():
    """🔴 재작업 2 — the per-order cap is RAISED, not removed.

    This is the whole point of choosing a raise over a removal: one
    automation error on a parking ticker stays bounded at USD 10,000 per
    order. Had the check been skipped, this order would have been unbounded.
    """
    decision = _decide(
        # 100.0001 x 100 = 10,000.01 — one cent over the raised cap.
        rung=_rung(limit_price=Decimal("100.0001"), quantity=Decimal("100")),
        preview={"success": True, "current_price": "100.0001"},
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"
    assert decision.details["notional"] == "10000.01"
    assert decision.details["per_order_cap"] == "10000"
    # Still recorded as a parking rung — it was capped, not un-recognized.
    assert decision.details["parking_allowlist"] == "SGOV"
    assert decision.details["per_order_cap_basis"] == "parking_raised"


def test_single_parking_order_exactly_at_the_raised_cap_is_eligible():
    decision = _decide(
        rung=_rung(limit_price=Decimal("100"), quantity=Decimal("100")),
        exposure=_flat(),
    )

    assert decision.eligible is True
    assert decision.details["notional"] == "10000"


def test_parking_sell_over_the_raised_cap_is_also_rejected():
    """The raise applies to both sides; so does the check."""
    decision = _decide(
        rung=_rung(
            side="sell", limit_price=Decimal("100.0001"), quantity=Decimal("100")
        ),
        preview={
            "success": True,
            "current_price": "100.0001",
            "avg_buy_price": "99",
        },
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"
    assert decision.details["per_order_cap"] == "10000"


def test_non_allowlisted_us_ticker_keeps_the_ordinary_1500_cap():
    """🔴 The raise is scoped to the allowlist and nothing else moved."""
    over = _decide(
        group=_group(symbol="SPY"),
        rung=_rung(limit_price=Decimal("90"), quantity=Decimal("17")),  # 1,530
        exposure=_flat(),
    )
    under = _decide(
        group=_group(symbol="SPY"),
        rung=_rung(limit_price=Decimal("90"), quantity=Decimal("16")),  # 1,440
        exposure=_flat(),
    )

    assert over.eligible is False
    assert over.reason == "per_order_cap_exceeded"
    assert over.details["per_order_cap"] == "1500"
    assert "parking_allowlist" not in over.details
    assert under.eligible is True
    assert under.details["per_order_cap"] == "1500"


def test_non_allowlisted_marketable_buy_is_still_rejected():
    decision = _decide(
        group=_group(symbol="SPY"),
        rung=_rung(quantity=Decimal("10")),  # under the per-order cap
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "marketable_not_resting"


def test_non_allowlisted_buy_over_the_per_order_cap_is_still_rejected():
    decision = _decide(group=_group(symbol="SPY"), exposure=_flat())

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"


def test_parking_buy_over_the_cumulative_cap_is_rejected():
    decision = _decide(exposure=ParkingExposure.observed(Decimal("9000")))

    assert decision.eligible is False
    assert decision.reason == "parking_cap_exceeded"
    assert decision.details["parking_exposure_before"] == "9000"
    assert decision.details["parking_exposure_after"] == "11000"
    assert decision.details["parking_cap"] == "10000"


def test_parking_buy_exactly_at_the_cumulative_cap_is_eligible():
    decision = _decide(exposure=ParkingExposure.observed(Decimal("8000")))

    assert decision.eligible is True
    assert decision.details["parking_exposure_after"] == "10000"


def test_parking_buy_one_cent_over_the_cumulative_cap_is_rejected():
    decision = _decide(exposure=ParkingExposure.observed(Decimal("8000.01")))

    assert decision.eligible is False
    assert decision.reason == "parking_cap_exceeded"


def test_parking_cap_meters_the_executable_price_not_the_discounted_limit():
    """A resting parking buy is metered at the market, never at its limit."""
    decision = _decide(
        rung=_rung(limit_price=Decimal("90")),
        preview={"success": True, "current_price": "100"},
        exposure=ParkingExposure.observed(Decimal("8100")),
    )

    # limit x qty would be 1,800 (8,100 + 1,800 = 9,900, under the cap);
    # current x qty is 2,000, which takes it to 10,100 and rejects.
    assert decision.eligible is False
    assert decision.reason == "parking_cap_exceeded"
    assert decision.details["parking_exposure_after"] == "10100"


def test_parking_cap_accumulates_across_the_rungs_of_one_proposal():
    """The cumulative cap is cumulative — the dispatch loop must carry it.

    Without the dispatch-side accumulator, N rungs each under the cap are each
    measured against the same pre-dispatch exposure and clear a total far above
    it. This asserts the classifier hands back the projection the loop needs,
    and that feeding it back rejects at the right rung.
    """
    exposure = _flat()
    reasons = []
    for _ in range(6):  # 6 x USD 2,000 = USD 12,000, past the USD 10,000 cap
        decision = _decide(exposure=exposure)
        reasons.append(decision.reason)
        if not decision.eligible:
            break
        projected = decision.details["parking_exposure_after"]
        exposure = ParkingExposure.observed(Decimal(projected))

    # Five rungs reach exactly USD 10,000; the sixth is refused.
    assert reasons == ["eligible"] * 5 + ["parking_cap_exceeded"]
    assert exposure.exposure == Decimal("10000")


# --------------------------------------------------------------------------
# 5. fail-closed — every way of not knowing the exposure rejects
# --------------------------------------------------------------------------


def test_parking_buy_without_supplied_exposure_is_rejected():
    """The classifier's default argument is the fail-closed value."""
    decision = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(),
        preview={"success": True, "current_price": "100"},
        limits=_US_EXPANDED,
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is False
    assert decision.reason == "parking_exposure_unavailable"
    assert decision.details["parking_exposure_reason"] == "not_supplied"


@pytest.mark.parametrize(
    "reason",
    [
        "fetch_failed",
        "payload_not_a_list",
        "row_not_a_mapping",
        "symbol_unreadable",
        "evaluation_amount_missing",
        "evaluation_amount_invalid",
        "evaluation_amount_negative",
        "not_requested",
    ],
)
def test_every_unavailable_reason_rejects_a_parking_buy(reason):
    decision = _decide(exposure=ParkingExposure.unavailable(reason))

    assert decision.eligible is False
    assert decision.reason == "parking_exposure_unavailable"
    assert decision.details["parking_exposure_reason"] == reason


@pytest.mark.parametrize(
    "exposure",
    [
        ParkingExposure(available=True, exposure=None),
        ParkingExposure(available=True, exposure=Decimal("-1")),
        ParkingExposure(available=True, exposure=Decimal("NaN")),
    ],
)
def test_malformed_available_exposure_still_rejects(exposure):
    """`available=True` is not by itself a clearance."""
    decision = _decide(exposure=exposure)

    assert decision.eligible is False
    assert decision.reason == "parking_exposure_unavailable"
    assert decision.details["parking_exposure_reason"] == "invalid_exposure"


# --------------------------------------------------------------------------
# 6. what §163차 did NOT authorize
# --------------------------------------------------------------------------


@pytest.mark.parametrize("side", ("buy", "sell"))
def test_enabled_parking_order_does_not_trip_or_consume_daily_cap(side):
    rung = _rung(side=side)
    preview = {"success": True, "current_price": "100"}
    if side == "sell":
        preview["avg_buy_price"] = "1"
    decision = _decide(
        rung=rung,
        preview=preview,
        exposure=_flat(),
        daily=Decimal("20000"),
    )

    assert decision.eligible is True
    assert decision.details["daily_cap_exempt"] is True
    assert decision.details["daily_notional_before"] == "20000"
    assert decision.details["daily_notional_after"] == "20000"


def test_off_mode_parking_symbol_keeps_daily_cap():
    decision = _decide(
        limits=_US_OFF,
        rung=_rung(quantity=Decimal("10")),
        exposure=_flat(),
        daily=Decimal("19500"),
    )

    assert decision.eligible is False
    assert decision.reason == "daily_cap_exceeded"
    assert decision.details["daily_notional_after"] == "20500"


def test_approval_required_tag_cannot_enter_daily_cap_exemption():
    decision = _decide(
        group=_group(source_asof={"tag": "policy_deviation"}),
        exposure=_flat(),
        daily=Decimal("20000"),
    )

    assert decision.eligible is False
    assert decision.reason == "approval_required_tag"
    assert "daily_cap_exempt" not in decision.details


def test_off_mode_verdicts_are_untouched_by_the_allowlist():
    """`off` never reaches the parking branch — cap and distance both apply."""
    over_cap = _decide(limits=_US_OFF, exposure=_flat())
    resting_short = _decide(
        limits=_US_OFF,
        rung=_rung(quantity=Decimal("10")),
        exposure=_flat(),
    )

    assert over_cap.eligible is False
    assert over_cap.reason == "per_order_cap_exceeded"
    assert resting_short.eligible is False
    assert resting_short.reason == "distance_below_minimum"
    assert "parking_allowlist" not in over_cap.details
    assert "parking_allowlist" not in resting_short.details


def test_loss_cut_intent_still_blocks_a_parking_rung():
    decision = _decide(group=_group(exit_intent="loss_cut"), exposure=_flat())

    assert decision.eligible is False
    assert decision.reason == "loss_cut_intent"


def test_policy_deviation_tag_still_blocks_a_parking_rung():
    decision = _decide(
        group=_group(rationale="policy_deviation: parking anyway"),
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "approval_required_tag"


def test_missing_thesis_still_blocks_a_parking_rung():
    decision = _decide(group=_group(thesis="   "), exposure=_flat())

    assert decision.eligible is False
    assert decision.reason == "thesis_required_for_veto_card"


def test_market_order_type_still_blocks_a_parking_rung():
    decision = _decide(group=_group(order_type="market"), exposure=_flat())

    assert decision.eligible is False
    assert decision.reason == "order_type_not_limit"


def test_failed_preview_still_blocks_a_parking_rung():
    decision = _decide(
        preview={"success": False, "current_price": "100"}, exposure=_flat()
    )

    assert decision.eligible is False
    assert decision.reason == "preview_guard_failed"


def test_parking_sell_still_needs_the_fee_netted_profit_proof():
    """§163차 releases marketability and RAISES the per-order cap; the profit
    proof is untouched, and the per-order check itself still runs."""
    decision = _decide(
        rung=_rung(side="sell"),
        preview={
            "success": True,
            "current_price": "100",
            "avg_buy_price": "99.5",  # inside the 1% break-even band
        },
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "breakeven_band"


def test_parking_sell_that_proves_profit_uses_the_raised_per_order_cap():
    decision = _decide(
        rung=_rung(side="sell"),
        preview={
            "success": True,
            "current_price": "100",
            "avg_buy_price": "99",  # outside the band, net of round-trip cost
        },
        exposure=_flat(),
    )

    assert decision.eligible is True
    assert decision.details["per_order_cap_basis"] == "parking_raised"
    assert decision.details["loss_guard"] == "net_profit_proven"
    # USD 2,000 is over the ordinary USD 1,500 cap and under the raised one.
    assert decision.details["notional"] == "2000"
    assert decision.details["per_order_cap"] == "10000"


def test_non_parking_sell_over_the_per_order_cap_is_still_rejected():
    decision = _decide(
        group=_group(symbol="SPY"),
        rung=_rung(side="sell"),
        preview={
            "success": True,
            "current_price": "100",
            "avg_buy_price": "99",
        },
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"


# --------------------------------------------------------------------------
# 7. exposure measurement
# --------------------------------------------------------------------------


def test_exposure_sums_only_allowlisted_rows():
    rows = [
        {"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "1200.50"},
        {"ovrs_pdno": "BIL", "ovrs_stck_evlu_amt": "800"},
        {"ovrs_pdno": "SPY", "ovrs_stck_evlu_amt": "50000"},
        {"ovrs_pdno": "SGOVX", "ovrs_stck_evlu_amt": "9999"},
        {"ovrs_pdno": "BILL", "ovrs_stck_evlu_amt": "9999"},
    ]

    exposure = sum_parking_exposure(rows)

    assert exposure.available is True
    assert exposure.exposure == Decimal("2000.50")


def test_exposure_matching_is_lenient_so_it_cannot_understate():
    """The exposure side normalizes case/whitespace: over-inclusion is safe."""
    exposure = sum_parking_exposure(
        [
            {"ovrs_pdno": " sgov ", "ovrs_stck_evlu_amt": "10"},
            {"ovrs_pdno": "bil", "ovrs_stck_evlu_amt": "5"},
        ]
    )

    assert exposure.available is True
    assert exposure.exposure == Decimal("15")


def test_empty_balance_is_zero_exposure():
    exposure = sum_parking_exposure([])

    assert exposure.available is True
    assert exposure.exposure == Decimal("0")


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (None, "payload_not_a_list"),
        ({"ovrs_pdno": "SGOV"}, "payload_not_a_list"),
        ("[]", "payload_not_a_list"),
        (["SGOV"], "row_not_a_mapping"),
        ([{"ovrs_stck_evlu_amt": "10"}], "symbol_unreadable"),
        ([{"ovrs_pdno": 1234, "ovrs_stck_evlu_amt": "10"}], "symbol_unreadable"),
        # Unreadable != "not parking": a row we cannot normalize fails closed
        # rather than being silently skipped and understating exposure.
        ([{"ovrs_pdno": "\u0405GOV", "ovrs_stck_evlu_amt": "10"}], "symbol_unreadable"),
        ([{"ovrs_pdno": "   ", "ovrs_stck_evlu_amt": "10"}], "symbol_unreadable"),
        ([{"ovrs_pdno": "SGOV"}], "evaluation_amount_missing"),
        (
            [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": None}],
            "evaluation_amount_missing",
        ),
        (
            [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "  "}],
            "evaluation_amount_missing",
        ),
        (
            [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "n/a"}],
            "evaluation_amount_invalid",
        ),
        (
            [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "NaN"}],
            "evaluation_amount_invalid",
        ),
        (
            [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": True}],
            "evaluation_amount_invalid",
        ),
        (
            [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "-1"}],
            "evaluation_amount_negative",
        ),
    ],
)
def test_exposure_fails_closed_on_every_malformed_payload(rows, reason):
    exposure = sum_parking_exposure(rows)

    assert exposure.available is False
    assert exposure.exposure is None
    assert exposure.unavailable_reason == reason


@pytest.mark.asyncio
async def test_load_exposure_makes_no_broker_call_for_a_non_parking_group():
    called = False

    async def _fetch():  # pragma: no cover - must never run
        nonlocal called
        called = True
        return []

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SPY",
        fetch_us_holdings=_fetch,
        durable_notional_fn=_no_pending,
    )

    assert called is False
    assert exposure.available is False
    assert exposure.unavailable_reason == "not_requested"


@pytest.mark.asyncio
async def test_load_exposure_fails_closed_when_the_broker_read_raises():
    async def _fetch():
        raise TimeoutError("broker timeout")

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_fetch,
        durable_notional_fn=_no_pending,
    )

    assert exposure.available is False
    assert exposure.unavailable_reason == "fetch_failed"


@pytest.mark.asyncio
async def test_load_exposure_reads_the_broker_balance_for_a_parking_group():
    async def _fetch():
        return [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "4321.00"}]

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_fetch,
        durable_notional_fn=_no_pending,
    )

    assert exposure.available is True
    assert exposure.exposure == Decimal("4321.00")


@pytest.mark.asyncio
async def test_load_exposure_reads_native_krw_domestic_balance_and_durable_half():
    """KR uses the existing KIS domestic ``pdno``/``evlu_amt`` read surface."""

    async def _fetch_kr():
        return [
            {"pdno": "459580", "evlu_amt": "9000000"},
            {"pdno": "357870", "evlu_amt": "5000000"},
        ]

    async def _pending_kr():
        return Decimal("5000000")

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_kr",
        symbol="459580",
        fetch_kr_holdings=_fetch_kr,
        durable_notional_fn=_pending_kr,
    )

    assert exposure.available is True
    assert exposure.exposure == Decimal("19000000")


def test_kr_parking_symbols_share_one_cumulative_cap():
    """Both KR symbols contribute to one 15m KRW cap, never one cap each.

    A symbol-specific reducer mutant returns only 8m here and turns the final
    rejection assertion RED; the actual scope-wide reducer returns 15m.
    """
    scope = parking_scope(symbol="357870", account_mode="kis_live", market="equity_kr")
    assert scope is not None
    exposure = _sum_parking_exposure(
        [
            {"pdno": "459580", "evlu_amt": "7000000", "crcy_cd": "KRW"},
            {"pdno": "357870", "evlu_amt": "8000000", "crcy_cd": "KRW"},
        ],
        scope=scope,
    )
    assert exposure.available is True
    assert exposure.exposure == Decimal("15000000")

    decision = _decide(
        group=_group(symbol="357870", market="equity_kr", thesis="park idle KRW"),
        rung=_rung(limit_price=Decimal("1"), quantity=Decimal("1")),
        preview={"success": True, "current_price": "1"},
        limits=_KR_EXPANDED,
        exposure=exposure,
    )
    assert decision.eligible is False
    assert decision.reason == "parking_cap_exceeded"
    assert decision.details["parking_cap"] == "15000000"


@pytest.mark.asyncio
async def test_cross_product_symbol_never_selects_the_other_market_reader():
    async def _must_not_read():  # pragma: no cover - must never run
        raise AssertionError("cross-product parking scope must not read a balance")

    for symbol, market in (
        ("SGOV", "equity_kr"),
        ("459580", "equity_us"),
        ("357870", "equity_us"),
    ):
        exposure = await load_parking_exposure(
            account_mode="kis_live",
            market=market,
            symbol=symbol,
            fetch_us_holdings=_must_not_read,
            fetch_kr_holdings=_must_not_read,
            durable_notional_fn=_no_pending,
        )
        assert exposure.available is False
        assert exposure.unavailable_reason == "not_requested"


def test_exposure_side_rejects_non_ascii_lookalike_rows():
    assert canonical_exposure_symbol("ſGOV") is None
    assert canonical_exposure_symbol("ЅGOV") is None


# --------------------------------------------------------------------------
# 8. §163차 재작업 1 — the durable half of the measurement
#
# The balance alone is NOT a cumulative boundary: KIS drops zero-quantity rows
# (``KISAccount._filter_nonzero_holdings``), so an accepted-but-unfilled
# parking buy is invisible to it and the next dispatch re-meters from zero.
# --------------------------------------------------------------------------


def test_kis_balance_reader_drops_zero_quantity_rows():
    """The premise of the whole durable half, asserted against real code.

    If this ever stops being true the durable reader is merely redundant, not
    load-bearing — but while it IS true, a balance-only cap is broken.
    """
    from app.services.brokers.kis.account import AccountClient

    rows = [
        {"ovrs_pdno": "SGOV", "ovrs_cblc_qty": "0"},  # submitted, not filled
        {"ovrs_pdno": "BIL", "ovrs_cblc_qty": "5"},
    ]

    kept = AccountClient._filter_nonzero_holdings(
        AccountClient.__new__(AccountClient), rows, is_overseas=True
    )

    assert [row["ovrs_pdno"] for row in kept] == ["BIL"]


@pytest.mark.asyncio
async def test_pending_approved_buys_are_added_to_held_exposure():
    async def _fetch():
        return [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "3000"}]

    async def _pending():
        return Decimal("2500")

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_fetch,
        durable_notional_fn=_pending,
    )

    assert exposure.available is True
    assert exposure.exposure == Decimal("5500")


@pytest.mark.asyncio
async def test_unfilled_second_proposal_is_refused_by_the_durable_half():
    """🔴 The verifier's reproduction, as a standing regression.

    Two separate single-rung USD 10,000 SGOV proposals on the same account.
    The first is auto-approved and submitted but has not filled, so it leaves
    NO balance row. Measured from the balance alone the second one sees zero
    exposure and clears — USD 20,000 of automation against a USD 10,000 cap.
    With the durable half it is refused.
    """

    async def _flat_balance():
        return []  # nothing filled yet -> no rows at all

    # First proposal: nothing approved yet today.
    first = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_flat_balance,
        durable_notional_fn=_no_pending,
    )
    first_decision = _decide(
        rung=_rung(limit_price=Decimal("100"), quantity=Decimal("100")),
        exposure=first,
    )
    assert first_decision.eligible is True
    assert first_decision.details["parking_exposure_after"] == "10000"

    # Second proposal, same account, same day. The first is submitted but
    # unfilled, so the balance is still empty; the durable record is not.
    async def _pending_10k():
        return Decimal("10000")

    second = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_flat_balance,
        durable_notional_fn=_pending_10k,
    )
    second_decision = _decide(
        rung=_rung(limit_price=Decimal("100"), quantity=Decimal("100")),
        exposure=second,
    )

    assert second.exposure == Decimal("10000")
    assert second_decision.eligible is False
    assert second_decision.reason == "parking_cap_exceeded"
    assert second_decision.details["parking_exposure_before"] == "10000"
    assert second_decision.details["parking_exposure_after"] == "20000"


@pytest.mark.asyncio
async def test_durable_reader_is_mandatory_for_a_parking_group():
    """Balance-only is the broken measure — its absence must not degrade to it."""

    async def _fetch():  # pragma: no cover - must never run
        raise AssertionError("balance must not be read without a durable reader")

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_fetch,
    )

    assert exposure.available is False
    assert exposure.unavailable_reason == "durable_reader_missing"


@pytest.mark.asyncio
async def test_durable_read_failure_fails_closed():
    async def _fetch():
        return []

    async def _raises():
        raise TimeoutError("db timeout")

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_fetch,
        durable_notional_fn=_raises,
    )

    assert exposure.available is False
    assert exposure.unavailable_reason == "durable_read_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, "n/a", Decimal("-1"), Decimal("NaN"), object()]
)
async def test_unusable_durable_value_fails_closed(value):
    async def _fetch():
        return []

    async def _pending():
        return value

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_fetch,
        durable_notional_fn=_pending,
    )

    assert exposure.available is False
    assert exposure.unavailable_reason == "durable_notional_invalid"


@pytest.mark.asyncio
async def test_durable_failure_reaches_the_classifier_as_a_rejection():
    """End to end: an unreadable durable half produces a human card."""

    async def _fetch():
        return []

    async def _raises():
        raise RuntimeError("boom")

    exposure = await load_parking_exposure(
        account_mode="kis_live",
        market="equity_us",
        symbol="SGOV",
        fetch_us_holdings=_fetch,
        durable_notional_fn=_raises,
    )
    decision = _decide(exposure=exposure)

    assert decision.eligible is False
    assert decision.reason == "parking_exposure_unavailable"
    assert decision.details["parking_exposure_reason"] == "durable_read_failed"


def test_durable_side_counts_parking_buys_approved_under_ordinary_gates():
    """A small resting SGOV buy never needed the raised cap but is real exposure.

    The durable filter is the lenient exposure predicate, not the strict
    eligibility one, so a `sgov`-spelled proposal that was auto-approved under
    the ordinary caps still counts against the parking cap.
    """
    from app.services.order_proposals.parking_allowlist import (
        is_parking_exposure_symbol,
    )

    assert (
        is_parking_exposure_symbol(
            " sgov ", account_mode="kis_live", market="equity_us"
        )
        is True
    )
    assert (
        is_parking_exposure_symbol("BIL", account_mode="kis_live", market="equity_us")
        is True
    )
    assert (
        is_parking_exposure_symbol("SGOVX", account_mode="kis_live", market="equity_us")
        is False
    )
    assert (
        is_parking_exposure_symbol("SPY", account_mode="kis_live", market="equity_us")
        is False
    )
    # ...while the *eligibility* side stays strict on the same input.
    assert canonical_eligibility_symbol(" sgov ") is None


# --------------------------------------------------------------------------
# 9. SHOULD-1 — currency
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["tr_crcy_cd", "crcy_cd", "currency_code", "curr_cd"])
def test_non_usd_row_fails_closed(field):
    """The caps are USD; summing a KRW amount into them would be a category error."""
    exposure = sum_parking_exposure(
        [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "1", field: "KRW"}]
    )

    assert exposure.available is False
    assert exposure.unavailable_reason == "currency_not_usd"


def test_non_string_declared_currency_fails_closed():
    exposure = sum_parking_exposure(
        [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "1", "crcy_cd": 840}]
    )

    assert exposure.available is False
    assert exposure.unavailable_reason == "currency_not_usd"


@pytest.mark.parametrize("declared", ["USD", " usd ", "Usd"])
def test_declared_usd_row_is_summed(declared):
    exposure = sum_parking_exposure(
        [{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "7", "tr_crcy_cd": declared}]
    )

    assert exposure.available is True
    assert exposure.exposure == Decimal("7")


def test_row_declaring_no_currency_is_summed():
    """The request pins TR_CRCY_CD=USD; an absent optional field is not an anomaly."""
    exposure = sum_parking_exposure([{"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "7"}])

    assert exposure.available is True
    assert exposure.exposure == Decimal("7")


def test_non_usd_currency_on_a_non_parking_row_is_ignored():
    """A KRW row for some other symbol is none of this cap's business."""
    exposure = sum_parking_exposure(
        [
            {"ovrs_pdno": "SPY", "ovrs_stck_evlu_amt": "999", "crcy_cd": "KRW"},
            {"ovrs_pdno": "SGOV", "ovrs_stck_evlu_amt": "5"},
        ]
    )

    assert exposure.available is True
    assert exposure.exposure == Decimal("5")


def test_krw_exposure_reducer_uses_domestic_fields_and_rejects_usd_rows():
    scope = parking_scope(symbol="459580", account_mode="kis_live", market="equity_kr")
    assert scope is not None
    observed = _sum_parking_exposure(
        [{"pdno": "459580", "evlu_amt": "14999999", "crcy_cd": "KRW"}],
        scope=scope,
    )
    mismatch = _sum_parking_exposure(
        [{"pdno": "459580", "evlu_amt": "1", "crcy_cd": "USD"}], scope=scope
    )

    assert observed.available is True
    assert observed.exposure == Decimal("14999999")
    assert mismatch.available is False
    assert mismatch.unavailable_reason == "currency_not_krw"


def test_kr_cumulative_cap_rejects_one_won_over_the_15m_limit():
    decision = _decide(
        group=_group(symbol="459580", market="equity_kr"),
        rung=_rung(limit_price=Decimal("1"), quantity=Decimal("1")),
        preview={"success": True, "current_price": "1"},
        limits=_KR_EXPANDED,
        exposure=ParkingExposure.observed(Decimal("15000000")),
    )

    assert decision.reason == "parking_cap_exceeded"
    assert decision.details["parking_cap"] == "15000000"
