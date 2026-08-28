"""§163차 — cash-parking ticker allowlist (SGOV/BIL).

The §163차 exemption removes the per-order cap, which §106차 defined as the
maximum loss boundary of one automation error. These tests hold the
replacement boundary in place: the allowlist is closed and unwidenable at
runtime, membership is exact-element (never substring or case-folded), the
cumulative USD 10,000 cap is enforced from broker-origin exposure, every way
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
    PARKING_ALLOWLIST_ACCOUNT_MARKETS,
    PARKING_ALLOWLIST_SYMBOLS,
    PARKING_CUMULATIVE_CAP_USD,
    ParkingExposure,
    canonical_eligibility_symbol,
    canonical_exposure_symbol,
    is_parking_allowlisted,
)
from app.services.order_proposals.parking_exposure import (
    load_parking_exposure,
    sum_parking_exposure,
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


# --------------------------------------------------------------------------
# 1. the allowlist is closed and cannot be widened at runtime
# --------------------------------------------------------------------------


def test_allowlist_constants_are_exactly_the_authorized_scope():
    assert PARKING_ALLOWLIST_SYMBOLS == frozenset({"SGOV", "BIL"})
    assert PARKING_ALLOWLIST_ACCOUNT_MARKETS == frozenset({("kis_live", "equity_us")})
    assert PARKING_CUMULATIVE_CAP_USD == Decimal("10000")


def test_allowlist_containers_are_immutable_frozensets():
    assert isinstance(PARKING_ALLOWLIST_SYMBOLS, frozenset)
    assert isinstance(PARKING_ALLOWLIST_ACCOUNT_MARKETS, frozenset)
    for container in (PARKING_ALLOWLIST_SYMBOLS, PARKING_ALLOWLIST_ACCOUNT_MARKETS):
        assert not hasattr(container, "add")
        assert not hasattr(container, "update")


def test_allowlist_module_reads_no_settings_env_db_or_policy():
    """A runtime session widens the allowlist only if something feeds it.

    Nothing does: the module's entire import surface is stdlib plus the pure
    symbol helper, and it contains no environment or settings access at all.
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
    # code is not allowed to read them.
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
    """A KR-market SGOV gets no exemption: cap and marketability both apply."""
    decision = _decide(
        group=_group(market="equity_kr", account_mode="kis_live"),
        exposure=_flat(),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"


# --------------------------------------------------------------------------
# 4. the three authorized directions
# --------------------------------------------------------------------------


def test_allowlisted_marketable_buy_over_the_per_order_cap_is_eligible():
    """Both §163차 releases at once: marketable buy + per-order cap exemption."""
    decision = _decide(exposure=_flat())

    assert decision.eligible is True
    assert decision.reason == "eligible"
    assert decision.details["parking_allowlist"] == "SGOV"
    assert decision.details["per_order_cap_basis"] == "exempt_parking"
    assert decision.details["marketability"] == "parking_allowlist_marketable"
    # USD 2,000 is above the USD 1,500 per-order cap and is admitted anyway.
    assert decision.details["notional"] == "2000"
    assert decision.details["parking_exposure_before"] == "0"
    assert decision.details["parking_exposure_after"] == "2000"
    assert decision.details["parking_cap"] == "10000"


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


def test_daily_cap_still_applies_to_a_parking_buy():
    decision = _decide(exposure=_flat(), daily=Decimal("19000"))

    assert decision.eligible is False
    assert decision.reason == "daily_cap_exceeded"
    assert decision.details["daily_notional_after"] == "21000"


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
    """§163차 lifts marketability and the per-order cap, not the profit proof."""
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


def test_parking_sell_that_proves_profit_is_exempt_from_the_per_order_cap():
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
    assert decision.details["per_order_cap_basis"] == "exempt_parking"
    assert decision.details["loss_guard"] == "net_profit_proven"
    # USD 2,000 over the USD 1,500 per-order cap, admitted by the exemption.
    assert decision.details["notional"] == "2000"


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
    )

    assert exposure.available is True
    assert exposure.exposure == Decimal("4321.00")


def test_exposure_side_rejects_non_ascii_lookalike_rows():
    assert canonical_exposure_symbol("ſGOV") is None
    assert canonical_exposure_symbol("ЅGOV") is None
