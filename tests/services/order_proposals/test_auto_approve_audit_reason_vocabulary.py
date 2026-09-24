"""Audit reason-code vocabulary must cover every real auto-approve rejection.

Task #672: a Toss US SGOV parking buy with a NULL ``broker_account_id`` was
correctly demoted to a human card (§173 requires the configured Toss account
identity before any holdings read), but the durable audit row and Telegram
card read ``invalid_reason_code`` because ``parking_exposure_unavailable`` --
like three other classifier codes -- was missing from the audit allowlist, and
the closed sub-reason ``account_identity_unavailable`` was dropped.

These tests only touch the audit projection. The eligibility verdicts are
asserted unchanged so the fix cannot widen what auto-approves.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals import auto_approve, dispatch
from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)
from app.services.order_proposals.auto_approve_audit import (
    _KNOWN_REASON_CODES,
    append_auto_approve_rejection_attempt,
    build_auto_approve_rejection_attempt,
    build_auto_approve_rejection_card_block,
    project_auto_approve_rejections,
)
from app.services.order_proposals.parking_allowlist import (
    PARKING_EXPOSURE_UNAVAILABLE_REASONS,
    ParkingExposure,
)
from app.services.order_proposals.parking_exposure import load_parking_exposure

_NOW = datetime(2026, 9, 24, 14, 20, 38, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _toss_veto_enabled(monkeypatch):
    """Production had the Toss live veto gate on: the stored row's code was not
    ``account_not_veto_capable`` (an allowlisted code that would have shown)."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", True)


# Production shape on 2026-09-24: US limits in expanded mode.
_US_EXPANDED = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("1500"),
    daily_cap=Decimal("20000"),
    policy_version="2026-09-08.1",
    mode="expanded",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("90"),
)


def _classifier_reject_reasons() -> set[str]:
    source = inspect.getsource(auto_approve.evaluate_auto_approve_eligibility)
    tree = ast.parse(textwrap.dedent(source))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "reject"
            and node.args
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            reasons.add(first.value)
        elif isinstance(first, ast.Subscript) and isinstance(first.value, ast.Dict):
            reasons.update(
                value.value
                for value in first.value.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
        elif isinstance(first, ast.IfExp):
            reasons.update(
                value.value
                for value in (first.body, first.orelse)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
        else:  # pragma: no cover - a new spelling must be taught here
            raise AssertionError(f"unrecognised reject() argument: {ast.dump(first)}")
    return reasons


def _dispatch_fallback_reasons() -> set[str]:
    source = inspect.getsource(dispatch)
    tree = ast.parse(source)
    reasons: set[str] = set(dispatch._AUDITABLE_REVALIDATION_FALLBACK_REASONS)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_manual_fallback_decisions"
        ):
            for keyword in node.keywords:
                if keyword.arg == "reason":
                    assert isinstance(keyword.value, ast.Constant)
                    reasons.add(keyword.value.value)
    return reasons


def test_every_classifier_reject_reason_survives_the_audit_projection():
    reasons = _classifier_reject_reasons()
    # Sanity: the extraction sees the parking and cash-funding gates.
    assert {
        "parking_exposure_unavailable",
        "parking_cap_exceeded",
        "cash_funding_boundary_failed",
        "cash_funding_cumulative_cap_exceeded",
    } <= reasons
    assert reasons - _KNOWN_REASON_CODES == set()


def test_every_dispatch_fallback_reason_survives_the_audit_projection():
    reasons = _dispatch_fallback_reasons()
    assert {"toss_auto_submission_frozen", "auto_veto_thesis_missing"} <= reasons
    assert reasons - _KNOWN_REASON_CODES == set()


def test_invalid_reason_code_remains_the_fallback_for_unknown_codes():
    attempt = build_auto_approve_rejection_attempt(
        decisions=[{"rung_index": 0, "eligible": False, "reason": "made_up_code"}],
        now=_NOW,
    )
    assert attempt is not None
    assert attempt["rungs"][0]["reason_code"] == "invalid_reason_code"


# --------------------------------------------------------------------------
# replay of proposal ae231402 (toss_live SGOV buy 43 @ 100.66, NULL account)
# --------------------------------------------------------------------------


def _toss_sgov_group(broker_account_id):
    return SimpleNamespace(
        symbol="SGOV",
        market="equity_us",
        account_mode="toss_live",
        broker_account_id=broker_account_id,
        order_type="limit",
        action="place",
        exit_intent=None,
        thesis="park idle Toss USD in SGOV",
    )


def _rung():
    return SimpleNamespace(
        rung_index=0,
        side="buy",
        limit_price=Decimal("100.66"),
        quantity=Decimal("43"),
        notional=None,
    )


async def _no_pending() -> Decimal:
    return Decimal("0")


def _toss_holdings(amount: str):
    from app.services.brokers.toss.dto import TossHoldingItem, TossHoldings

    async def _read():
        return TossHoldings(
            items=[
                TossHoldingItem(
                    symbol="SGOV",
                    name="SGOV",
                    market_country="US",
                    currency="USD",
                    quantity=Decimal("55"),
                    last_price=Decimal("100.62"),
                    average_purchase_price=Decimal("100.5"),
                    market_value={"amount": Decimal(amount)},
                    profit_loss={},
                    daily_profit_loss={},
                    cost={},
                )
            ]
        )

    return _read


async def _decide(monkeypatch, *, broker_account_id, held="5534.1"):
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_account_seq", 731)
    group = _toss_sgov_group(broker_account_id)
    exposure = await load_parking_exposure(
        account_mode=group.account_mode,
        market=group.market,
        symbol=group.symbol,
        broker_account_id=group.broker_account_id,
        fetch_toss_holdings=_toss_holdings(held),
        durable_notional_fn=_no_pending,
    )
    decision = evaluate_auto_approve_eligibility(
        group=group,
        rung=_rung(),
        preview={"success": True, "current_price": "100.62"},
        limits=_US_EXPANDED,
        daily_notional=Decimal("0"),
        parking_exposure=exposure,
    )
    return exposure, decision


def _stored(decision) -> dict:
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
        now=_NOW,
    )
    # Round-trip through JSON as the JSONB column would.
    return json.loads(json.dumps(source_asof))


@pytest.mark.asyncio
async def test_null_toss_account_is_still_demoted_but_now_audited_truthfully(
    monkeypatch,
):
    exposure, decision = await _decide(monkeypatch, broker_account_id=None)

    # Verdict unchanged: §173 fails closed before any Toss read.
    assert exposure.available is False
    assert exposure.unavailable_reason == "account_identity_unavailable"
    assert decision.eligible is False
    assert decision.reason == "parking_exposure_unavailable"

    source_asof = _stored(decision)
    [attempt] = project_auto_approve_rejections(source_asof)
    [rung] = attempt["rungs"]
    assert rung["reason_code"] == "parking_exposure_unavailable"
    assert rung["inputs"]["parking_exposure_reason"] == ("account_identity_unavailable")
    assert rung["inputs"]["parking_currency"] == "USD"

    card = build_auto_approve_rejection_card_block(source_asof)
    assert card is not None
    assert "invalid_reason_code" not in card
    assert "`parking_exposure_unavailable` / `account_identity_unavailable`" in card


@pytest.mark.asyncio
async def test_matching_toss_account_path_is_unchanged(monkeypatch):
    """The fix is audit-only: the identity-bound path decides exactly as before."""
    _, eligible = await _decide(monkeypatch, broker_account_id="731")
    assert eligible.eligible is True
    assert eligible.details["parking_exposure_after"] == "9862.48"

    _, over_cap = await _decide(monkeypatch, broker_account_id="731", held="5700")
    assert over_cap.eligible is False
    assert over_cap.reason == "parking_cap_exceeded"
    [attempt] = project_auto_approve_rejections(_stored(over_cap))
    [rung] = attempt["rungs"]
    assert rung["reason_code"] == "parking_cap_exceeded"
    assert rung["inputs"]["parking_exposure_before"] == "5700"
    assert rung["inputs"]["parking_exposure_after"] == "10028.38"
    assert rung["inputs"]["parking_cap"] == "10000"


@pytest.mark.parametrize(
    "reason",
    sorted(PARKING_EXPOSURE_UNAVAILABLE_REASONS | {"not_supplied", "invalid_exposure"}),
)
def test_every_parking_exposure_reason_is_retained(reason):
    decision = evaluate_auto_approve_eligibility(
        group=_toss_sgov_group("731"),
        rung=_rung(),
        preview={"success": True, "current_price": "100.62"},
        limits=_US_EXPANDED,
        daily_notional=Decimal("0"),
        parking_exposure=(
            None
            if reason == "not_supplied"
            else ParkingExposure.observed(Decimal("-1"))
            if reason == "invalid_exposure"
            else ParkingExposure.unavailable(reason)
        ),
    )
    assert decision.eligible is False
    assert decision.reason == "parking_exposure_unavailable"
    [attempt] = project_auto_approve_rejections(_stored(decision))
    assert attempt["rungs"][0]["inputs"]["parking_exposure_reason"] == reason


def test_untrusted_sub_reason_text_is_dropped():
    raw = "private-text-must-not-escape"
    source_asof = append_auto_approve_rejection_attempt(
        {},
        decisions=[
            {
                "rung_index": 0,
                "eligible": False,
                "reason": "parking_exposure_unavailable",
                "parking_exposure_reason": raw,
                "cash_funding_reason": raw,
                "cash_funding_cumulative_reason": raw,
                "parking_currency": raw,
                "parking_exposure_before": raw,
            }
        ],
        now=_NOW,
    )
    assert raw not in json.dumps(source_asof)
    card = build_auto_approve_rejection_card_block(source_asof)
    assert card == "*자동 승인 제외*\n- #1: `parking_exposure_unavailable`"


def test_cash_funding_reasons_are_retained():
    source_asof = append_auto_approve_rejection_attempt(
        {},
        decisions=[
            {
                "rung_index": 0,
                "eligible": False,
                "reason": "cash_funding_boundary_failed",
                "cash_funding_reason": "shortfall_unmeasured",
            },
            {
                "rung_index": 1,
                "eligible": False,
                "reason": "cash_funding_cumulative_cap_exceeded",
                "cash_funding_cumulative_reason": "unmeasured",
            },
        ],
        now=_NOW,
    )
    [attempt] = project_auto_approve_rejections(source_asof)
    first, second = attempt["rungs"]
    assert first["reason_code"] == "cash_funding_boundary_failed"
    assert first["inputs"]["cash_funding_reason"] == "shortfall_unmeasured"
    assert second["reason_code"] == "cash_funding_cumulative_cap_exceeded"
    assert second["inputs"]["cash_funding_cumulative_reason"] == "unmeasured"
