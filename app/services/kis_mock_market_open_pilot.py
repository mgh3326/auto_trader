"""Operator-gated KIS mock market-open pilot helper (ROB-95).

This module is intentionally dependency-injected and side-effect-free unless the
caller supplies the typed ``kis_mock_place_order`` callable. It must not import
broker, MCP, KIS client, DB, cache, network, or scheduler modules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

PilotMode = Literal["readiness", "dry-run", "submit-mock"]
PilotStatus = Literal["ready", "submitted", "blocked"]
PilotSide = Literal["buy", "sell"]
ReportStatus = Literal["accepted_but_fill_unknown", "filled_inferred", "rejected"]

_ALLOWED_TOOL_NAME = "kis_mock_place_order"
_ALLOWED_ACCOUNT_MODE = "kis_mock"
_ALLOWED_ORDER_TYPE = "limit"


@dataclass(frozen=True)
class KisMockMarketOpenPilotRequest:
    """Narrow request contract for the ROB-95 KIS mock market-open pilot."""

    mode: PilotMode
    symbol: str
    side: str
    quantity: int
    price: Decimal
    approval_text: str | None = None
    account_mode: str = _ALLOWED_ACCOUNT_MODE
    order_type: str = _ALLOWED_ORDER_TYPE
    tool_name: str = _ALLOWED_TOOL_NAME


@dataclass(frozen=True)
class KisMockMarketOpenPilotResult:
    """Deterministic result from a pilot readiness/dry-run/submit attempt."""

    status: PilotStatus
    mode: PilotMode
    symbol: str
    side: str
    quantity: int
    price: int | None
    tool_name: str
    account_mode: str
    dry_run: bool | None
    safety_checks: dict[str, bool] = field(default_factory=dict)
    blocking_reasons: list[str] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] | None = None
    expected_approval_text: str | None = None
    report_status: ReportStatus | None = None


def _is_kr_equity_symbol(symbol: str) -> bool:
    return len(symbol) == 6 and symbol.isdigit()


def _is_positive_integer_decimal(value: Decimal) -> bool:
    return value > 0 and value == value.to_integral_value()


def _side_ko(side: str) -> str:
    return "매도" if side == "sell" else "매수"


def _approval_suffix(side: str) -> str:
    return "청산 승인" if side == "sell" else "제출 승인"


def expected_kis_mock_submit_approval_text(
    *, symbol: str, side: str, quantity: int, price: Decimal
) -> str:
    """Return the exact operator approval text required for mock submit."""
    price_int = int(price)
    return (
        f"ROB-95 KIS mock 승인: {symbol} {_side_ko(side)} {quantity}주 "
        f"지정가 {price_int}원 account_mode=kis_mock dry_run=False "
        f"정규장 모의투자 {_approval_suffix(side)}"
    )


def _base_safety_checks(request: KisMockMarketOpenPilotRequest) -> dict[str, bool]:
    return {
        "typed_kis_mock_route_only": request.tool_name == _ALLOWED_TOOL_NAME,
        "no_generic_place_order": request.tool_name != "place_order",
        "no_kis_live_route": not request.tool_name.startswith("kis_live"),
        "kis_mock_account_mode": request.account_mode == _ALLOWED_ACCOUNT_MODE,
        "limit_order_only": request.order_type == _ALLOWED_ORDER_TYPE,
        "kr_equity_symbol": _is_kr_equity_symbol(request.symbol),
        "supported_side": request.side in {"buy", "sell"},
        "positive_integer_quantity": isinstance(request.quantity, int)
        and request.quantity > 0,
        "positive_integer_limit_price": _is_positive_integer_decimal(request.price),
    }


def _blocking_reasons(request: KisMockMarketOpenPilotRequest) -> list[str]:
    reasons: list[str] = []
    if request.tool_name != _ALLOWED_TOOL_NAME:
        reasons.append("invalid_tool_name")
    if request.account_mode != _ALLOWED_ACCOUNT_MODE:
        reasons.append("invalid_account_mode")
    if request.order_type != _ALLOWED_ORDER_TYPE:
        reasons.append("invalid_order_type")
    if not _is_kr_equity_symbol(request.symbol):
        reasons.append("unsupported_kr_equity_symbol")
    if request.side not in {"buy", "sell"}:
        reasons.append("unsupported_side")
    if not isinstance(request.quantity, int) or request.quantity <= 0:
        reasons.append("invalid_quantity")
    if not _is_positive_integer_decimal(request.price):
        reasons.append("invalid_limit_price")
    return reasons


def _blocked_result(
    request: KisMockMarketOpenPilotRequest,
    *,
    reasons: list[str],
    expected_approval_text: str | None = None,
) -> KisMockMarketOpenPilotResult:
    price = int(request.price) if _is_positive_integer_decimal(request.price) else None
    return KisMockMarketOpenPilotResult(
        status="blocked",
        mode=request.mode,
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        price=price,
        tool_name=request.tool_name,
        account_mode=request.account_mode,
        dry_run=None,
        safety_checks=_base_safety_checks(request),
        blocking_reasons=reasons,
        expected_approval_text=expected_approval_text,
    )


def classify_kis_mock_market_open_report(
    *,
    response: Mapping[str, Any],
    holdings_delta_qty: int | Decimal | None = None,
    cash_delta_krw: int | Decimal | None = None,
    order_history_supported: bool = False,
) -> ReportStatus:
    """Classify KIS mock order evidence without confusing acceptance with fill."""
    if response.get("ok") is False or response.get("error"):
        return "rejected"

    has_holding_delta = holdings_delta_qty not in {None, 0, Decimal("0")}
    has_cash_delta = cash_delta_krw not in {None, 0, Decimal("0")}
    if order_history_supported and has_holding_delta and has_cash_delta:
        return "filled_inferred"
    return "accepted_but_fill_unknown"


def run_kis_mock_market_open_pilot(
    request: KisMockMarketOpenPilotRequest,
    *,
    kis_mock_place_order: Callable[..., Mapping[str, Any]],
    is_regular_session: Callable[[], bool],
    readiness_probe: Callable[[], Mapping[str, Any]] | None = None,
) -> KisMockMarketOpenPilotResult:
    """Run a guarded ROB-95 pilot mode through the injected typed mock route."""
    shape_reasons = _blocking_reasons(request)
    if shape_reasons:
        return _blocked_result(request, reasons=shape_reasons)

    safety_checks = _base_safety_checks(request)
    price = int(request.price)

    if request.mode == "readiness":
        readiness = dict(readiness_probe() if readiness_probe else {})
        return KisMockMarketOpenPilotResult(
            status="ready",
            mode=request.mode,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=price,
            tool_name=request.tool_name,
            account_mode=request.account_mode,
            dry_run=None,
            safety_checks=safety_checks,
            readiness=readiness,
        )

    if request.mode == "dry-run":
        response = dict(
            kis_mock_place_order(
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                price=price,
                dry_run=True,
                account_mode=request.account_mode,
                reason="ROB-95 KIS mock market-open pilot dry-run",
            )
        )
        return KisMockMarketOpenPilotResult(
            status="submitted",
            mode=request.mode,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=price,
            tool_name=request.tool_name,
            account_mode=request.account_mode,
            dry_run=True,
            safety_checks=safety_checks,
            response=response,
        )

    if request.mode != "submit-mock":
        return _blocked_result(request, reasons=["unsupported_mode"])

    expected_approval = expected_kis_mock_submit_approval_text(
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        price=request.price,
    )
    if request.approval_text != expected_approval:
        return _blocked_result(
            request,
            reasons=["approval_text_mismatch"],
            expected_approval_text=expected_approval,
        )
    if not is_regular_session():
        return _blocked_result(
            request,
            reasons=["regular_session_closed"],
            expected_approval_text=expected_approval,
        )
    if request.quantity > 1:
        return _blocked_result(
            request,
            reasons=["quantity_exceeds_default_smoke_limit"],
            expected_approval_text=expected_approval,
        )

    response = dict(
        kis_mock_place_order(
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=price,
            dry_run=False,
            account_mode=request.account_mode,
            reason="ROB-95 KIS mock market-open pilot exact-approved mock submit",
        )
    )
    report_status = classify_kis_mock_market_open_report(response=response)
    return KisMockMarketOpenPilotResult(
        status="submitted",
        mode=request.mode,
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        price=price,
        tool_name=request.tool_name,
        account_mode=request.account_mode,
        dry_run=False,
        safety_checks=safety_checks,
        response=response,
        expected_approval_text=expected_approval,
        report_status=report_status,
    )
