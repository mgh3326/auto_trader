"""§S177 closed cash-funding classifier contract tests."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.order_proposals import cash_funding_exemption
from app.services.order_proposals.cash_funding_exemption import (
    CASH_FUNDING_EXIT_INTENT,
    CASH_FUNDING_REJECT_REASONS,
    TRANCHE_SLACK_UNITS,
    FundingTarget,
    parse_funding_target,
    resolve_cash_funding_exemption,
)


def _target(*, market: str = "equity_us", required: str = "100") -> FundingTarget:
    return FundingTarget(
        market=market,
        required=Decimal(required),
        plan_ref="planned-buy-001",
    )


def _resolve(**overrides):
    values = {
        "exit_intent": CASH_FUNDING_EXIT_INTENT,
        "symbol": "SGOV",
        "account_mode": "kis_live",
        "market": "equity_us",
        "side": "sell",
        "order_type": "limit",
        "funding_target": _target(),
        "quantity": Decimal("2"),
        "current_price": Decimal("100"),
        "measured_shortfall": Decimal("100"),
    }
    values.update(overrides)
    return resolve_cash_funding_exemption(**values)


def test_cash_funding_module_reads_no_settings_env_db_or_policy_loader():
    """Mirror the parking allowlist AST guard for the new pure module."""
    source = Path(inspect.getfile(cash_funding_exemption)).read_text(encoding="utf-8")
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
        "app.services.order_proposals.parking_allowlist",
    }
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
        "Session",
    ):
        assert forbidden not in referenced


@pytest.mark.parametrize(
    ("symbol", "account_mode", "market", "funding_market"),
    [
        ("459580", "kis_live", "equity_kr", "equity_kr"),
        ("357870", "kis_live", "equity_kr", "kr"),
        ("SGOV", "kis_live", "equity_us", "equity_us"),
        ("BIL", "kis_live", "equity_us", "us"),
    ],
)
def test_only_cash_funding_passes_for_each_closed_cash_proxy_symbol(
    symbol: str,
    account_mode: str,
    market: str,
    funding_market: str,
):
    passed = _resolve(
        symbol=symbol,
        account_mode=account_mode,
        market=market,
        funding_target=_target(market=funding_market),
    )
    assert passed.exempt is True
    assert passed.reason == "exempt"

    for other_intent in ("take_profit", "loss_cut", "trim", None):
        blocked = _resolve(
            exit_intent=other_intent,
            symbol=symbol,
            account_mode=account_mode,
            market=market,
            funding_target=_target(market=funding_market),
        )
        assert blocked.exempt is False
        assert blocked.reason == "not_cash_funding"


def test_missing_funding_target_is_closed_reason():
    verdict = _resolve(funding_target=None)
    assert verdict.exempt is False
    assert verdict.reason == "funding_target_missing"


def test_quantity_cap_accepts_exact_boundary_and_rejects_one_more():
    target = _target(required="201")
    # ceil(201 / 100) + 1 = 4
    at_boundary = _resolve(
        funding_target=target,
        quantity=Decimal("4"),
        measured_shortfall=Decimal("201"),
    )
    over_boundary = _resolve(
        funding_target=target,
        quantity=Decimal("5"),
        measured_shortfall=Decimal("201"),
    )

    assert TRANCHE_SLACK_UNITS == Decimal("1")
    assert at_boundary.exempt is True
    assert at_boundary.max_quantity == Decimal("4")
    assert over_boundary.exempt is False
    assert over_boundary.reason == "quantity_exceeds_funding_need"


def test_required_cannot_exceed_measured_shortfall():
    verdict = _resolve(
        funding_target=_target(required="101"),
        measured_shortfall=Decimal("100"),
    )
    assert verdict.exempt is False
    assert verdict.reason == "required_exceeds_measured_shortfall"


def test_non_cash_proxy_symbol_is_closed_before_target_checks():
    verdict = _resolve(symbol="AAPL")
    assert verdict.exempt is False
    assert verdict.reason == "symbol_not_cash_proxy"


def test_cross_currency_funding_is_forbidden():
    verdict = _resolve(funding_target=_target(market="equity_kr"))
    assert verdict.exempt is False
    assert verdict.reason == "funding_currency_mismatch"


def test_parse_funding_target_is_exact_and_preserves_dedicated_validation():
    parsed = parse_funding_target(
        {"market": "equity_us", "required": "100", "plan_ref": "p-1"}
    )
    assert parsed == FundingTarget("equity_us", Decimal("100"), "p-1")
    assert (
        parse_funding_target(
            {"market": "equity_us", "required": 100.0, "plan_ref": "p-1"}
        )
        is None
    )
    assert parse_funding_target({"market": "equity_us", "required": "100"}) is None


def test_reject_vocabulary_is_exactly_closed():
    assert CASH_FUNDING_REJECT_REASONS == {
        "not_cash_funding",
        "side_not_sell",
        "order_type_not_limit",
        "symbol_not_cash_proxy",
        "funding_target_missing",
        "funding_currency_mismatch",
        "funding_required_invalid",
        "funding_plan_ref_missing",
        "shortfall_unmeasured",
        "no_measured_shortfall",
        "required_exceeds_measured_shortfall",
        "current_price_unavailable",
        "quantity_invalid",
        "quantity_exceeds_funding_need",
    }
