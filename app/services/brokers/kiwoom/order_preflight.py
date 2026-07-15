from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.mcp_server.tick_size import get_tick_size_kr
from app.services.brokers.kiwoom.normalization import (
    KiwoomMockEvidenceError,
    normalize_orderable_cash,
    normalize_positions,
    validate_mock_response_provenance,
)

PREFLIGHT_OK = "preflight_ok"
PREFLIGHT_SELLABLE_EXCEEDED = "preflight_sellable_exceeded"
PREFLIGHT_CASH_INSUFFICIENT = "preflight_cash_insufficient"
PREFLIGHT_QUOTE_STALE = "preflight_quote_stale"
PREFLIGHT_QUOTE_MISSING = "preflight_quote_missing"
PREFLIGHT_TICK_INVALID = "preflight_tick_invalid"
PREFLIGHT_EVIDENCE_INVALID = "preflight_evidence_invalid"
PREFLIGHT_PROVENANCE_CONFLICT = "preflight_provenance_conflict"
PREFLIGHT_POSITION_READ_FAILED = "preflight_position_read_failed"
PREFLIGHT_CASH_READ_FAILED = "preflight_cash_read_failed"

MAX_PRICE_DISTANCE_PCT = 30.0


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str | None = None


@dataclass
class PreflightResult:
    ok: bool
    error_code: str | None = None
    error_detail: str | None = None
    checks: list[PreflightCheck] = field(default_factory=list)
    estimated_evidence: dict[str, Any] = field(default_factory=dict)

    def to_response_extras(self) -> dict[str, Any]:
        return {
            "preflight_checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks
            ],
            "estimated_evidence": self.estimated_evidence,
        }


def _fail(
    error_code: str,
    error_detail: str,
    checks: list[PreflightCheck],
    estimated: dict[str, Any] | None = None,
) -> PreflightResult:
    return PreflightResult(
        ok=False,
        error_code=error_code,
        error_detail=error_detail,
        checks=checks,
        estimated_evidence=estimated or {},
    )


async def run_order_preflight(
    *,
    account_client: Any,
    symbol: str,
    side: str,
    quantity: int,
    price: int,
    quote_price: int | None = None,
    quote_freshness: str = "unavailable",
) -> PreflightResult:
    checks: list[PreflightCheck] = []
    estimated: dict[str, Any] = {"type": "estimated"}

    if quote_price is None or quote_price <= 0:
        return _fail(
            PREFLIGHT_QUOTE_MISSING,
            f"Quote price is missing or invalid for {symbol}",
            [PreflightCheck("quote_freshness", False, "quote_price missing")],
        )
    if quote_freshness != "fresh":
        return _fail(
            PREFLIGHT_QUOTE_STALE,
            f"Quote is {quote_freshness}, not fresh for {symbol}",
            [PreflightCheck("quote_freshness", False, f"freshness={quote_freshness}")],
        )
    checks.append(PreflightCheck("quote_freshness", True))

    tick = get_tick_size_kr(float(price))
    if tick > 0 and price % tick != 0:
        return _fail(
            PREFLIGHT_TICK_INVALID,
            f"Price {price} is not a valid KRX tick multiple (tick={tick})",
            checks
            + [PreflightCheck("tick_valid", False, f"price%tick={price % tick}")],
        )
    checks.append(PreflightCheck("tick_valid", True, f"tick={tick}"))

    distance_pct = abs(price - quote_price) / quote_price * 100
    estimated["price_distance_pct"] = round(distance_pct, 2)
    estimated["quote_price"] = quote_price

    order_amount = price * quantity
    estimated["order_amount"] = order_amount

    if side == "sell":
        result = await _check_sellable(
            account_client, symbol, quantity, checks, estimated
        )
        if result is not None:
            return result
    elif side == "buy":
        result = await _check_buy_cash(
            account_client, symbol, price, order_amount, checks, estimated
        )
        if result is not None:
            return result

    return PreflightResult(
        ok=True, error_code=PREFLIGHT_OK, checks=checks, estimated_evidence=estimated
    )


async def _check_sellable(
    account_client: Any,
    symbol: str,
    quantity: int,
    checks: list[PreflightCheck],
    estimated: dict[str, Any],
) -> PreflightResult | None:
    try:
        balance_response = await account_client.get_balance()
        validate_mock_response_provenance(balance_response)
        positions = normalize_positions(balance_response)
    except KiwoomMockEvidenceError as exc:
        code = (
            PREFLIGHT_PROVENANCE_CONFLICT
            if exc.code == "kiwoom_mock_provenance_conflict"
            else PREFLIGHT_EVIDENCE_INVALID
        )
        return _fail(code, f"Position evidence invalid: {exc}", checks)
    except Exception as exc:
        return _fail(
            PREFLIGHT_POSITION_READ_FAILED,
            f"Position read failed: {type(exc).__name__}",
            checks,
        )

    sellable = 0
    avg_price = 0
    for pos in positions:
        if pos["symbol"] == symbol:
            sellable = pos["quantity"]
            avg_price = pos["average_price"]
            break

    estimated["sellable_quantity"] = sellable
    estimated["average_price"] = avg_price

    if quantity > sellable:
        return _fail(
            PREFLIGHT_SELLABLE_EXCEEDED,
            f"Requested {quantity} exceeds sellable {sellable} for {symbol}",
            checks
            + [
                PreflightCheck(
                    "sellable", False, f"requested={quantity}, sellable={sellable}"
                )
            ],
            estimated=estimated,
        )
    checks.append(PreflightCheck("sellable", True, f"sellable={sellable}"))

    if avg_price > 0 and sellable > 0:
        pnl = (estimated.get("quote_price", 0) - avg_price) * quantity
        pnl_pct = (
            round((estimated.get("quote_price", 0) - avg_price) / avg_price * 100, 2)
            if avg_price > 0
            else 0.0
        )
        estimated["estimated_pnl"] = pnl
        estimated["estimated_pnl_pct"] = pnl_pct
        estimated["loss_sell"] = pnl < 0

    return None


async def _check_buy_cash(
    account_client: Any,
    symbol: str,
    price: int,
    order_amount: int,
    checks: list[PreflightCheck],
    estimated: dict[str, Any],
) -> PreflightResult | None:
    try:
        cash_response = await account_client.get_orderable_amount(
            symbol=symbol, side="buy", price=price
        )
        validate_mock_response_provenance(cash_response)
        orderable_cash = normalize_orderable_cash(cash_response)
    except KiwoomMockEvidenceError as exc:
        code = (
            PREFLIGHT_PROVENANCE_CONFLICT
            if exc.code == "kiwoom_mock_provenance_conflict"
            else PREFLIGHT_EVIDENCE_INVALID
        )
        return _fail(code, f"Cash evidence invalid: {exc}", checks)
    except Exception as exc:
        return _fail(
            PREFLIGHT_CASH_READ_FAILED,
            f"Cash read failed: {type(exc).__name__}",
            checks,
        )

    estimated["orderable_cash"] = orderable_cash

    if order_amount > orderable_cash:
        return _fail(
            PREFLIGHT_CASH_INSUFFICIENT,
            f"Order amount {order_amount} exceeds orderable cash {orderable_cash}",
            checks
            + [
                PreflightCheck(
                    "cash", False, f"order={order_amount}, cash={orderable_cash}"
                )
            ],
            estimated=estimated,
        )
    checks.append(PreflightCheck("cash", True, f"cash={orderable_cash}"))

    return None
