# app/mcp_server/tooling/orders_toss_variants.py
"""Toss Securities live MCP order tools.

Every tool is hard-pinned to ``account_mode="toss_live"``. They:
- Validate ``validate_toss_api_config`` before any side effect.
- Default mutation tools to ``dry_run=True`` and require ``confirm=True`` before any POST.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from app.core.config import settings, validate_toss_api_config
from app.core.timezone import KST, now_kst
from app.mcp_server.tick_size import adjust_tick_size_kr, get_tick_size_kr
from app.mcp_server.tooling.account_modes import (
    ACCOUNT_MODE_TOSS_LIVE,
    normalize_account_mode,
)
from app.mcp_server.tooling.order_validation import (
    LossCutContext,
    _validate_loss_cut_preconditions,
    evaluate_sector_concentration,
    evaluate_sell_price_guards,
)
from app.mcp_server.tooling.portfolio_cash import get_account_costs_setting
from app.mcp_server.tooling.toss_approval import (
    APPROVAL_TTL_SECONDS,
    build_canonical_payload,
    derive_approval_digest,
    derive_client_order_id,
    encode_approval_token,
    verify_approval_token,
)
from app.mcp_server.tooling.toss_live_ledger import (
    record_toss_place_order,
    record_toss_replacement_order,
)
from app.services.account_routing import build_cost_profiles
from app.services.brokers.toss import TossReadClient
from app.services.brokers.toss.dto import TossWarningInfo
from app.services.brokers.toss.errors import TossApiResponseError
from app.services.brokers.toss.market_calendar import get_kr_toss_session_from_toss
from app.services.brokers.toss.warnings_guard import check_warnings_guard
from app.services.exchange_rate_service import get_usd_krw_rate_details
from app.services.kr_symbol_universe_service import get_kr_nxt_tradability
from app.services.nxt_preflight import (
    RETRY_AT_REGULAR,
    ROUTE_VIA_KIS,
    NxtPreflightVerdict,
    NxtTradability,
    evaluate_nxt_preflight,
)
from app.services.toss_sellable_cache import get_shared_sellable_cache

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TOSS_LIVE_ORDER_TOOL_NAMES: set[str] = {
    "toss_preview_order",
    "toss_place_order",
    "toss_modify_order",
    "toss_cancel_order",
    "toss_get_order_history",
    "toss_get_positions",
    "toss_get_orderable_cash",
    "toss_reconcile_orders",
}

_BPS = Decimal("10000")
_PRICE_CONTEXT_UNAVAILABLE = "price_context_unavailable"


@dataclass(frozen=True)
class _OrderProposalContext:
    client_order_id: str
    correlation_id: str | None
    rung: str | int | None


_order_proposal_context: ContextVar[_OrderProposalContext | None] = ContextVar(
    "toss_order_proposal_context", default=None
)


@contextmanager
def _bind_order_proposal_context(
    *,
    client_order_id: str,
    correlation_id: str | None,
    rung: str | int | None,
):
    """Bind trusted proposal identity without exposing it in the MCP schema."""
    token = _order_proposal_context.set(
        _OrderProposalContext(client_order_id, correlation_id, rung)
    )
    try:
        yield
    finally:
        _order_proposal_context.reset(token)


def _config_error() -> dict[str, Any] | None:
    missing = validate_toss_api_config()
    if missing:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": (
                "Toss live account is disabled or missing required configuration: "
                + ", ".join(missing)
            ),
        }
    return None


def _check_mode_arg(
    account_mode: str | None, account_type: str | None
) -> dict[str, Any] | None:
    if account_mode is None and account_type is None:
        return None
    try:
        routing = normalize_account_mode(account_mode, account_type)
    except ValueError as exc:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": str(exc),
        }
    if routing.account_mode != ACCOUNT_MODE_TOSS_LIVE:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": (
                f"Invalid account_mode resolving to {routing.account_mode!r}. "
                f"Toss live tools only support account_mode='{ACCOUNT_MODE_TOSS_LIVE}'."
            ),
        }
    return None


def _entry_guard(
    account_mode: str | None, account_type: str | None
) -> dict[str, Any] | None:
    return _config_error() or _check_mode_arg(account_mode, account_type)


@asynccontextmanager
async def _client_context():
    client = TossReadClient.from_settings()
    try:
        yield client
    finally:
        await client.aclose()


def _warning_payload(
    warnings: Iterable[TossWarningInfo],
) -> list[dict[str, str | None]]:
    return [
        {
            "warning_type": w.warning_type,
            "exchange": w.exchange,
            "start_date": w.start_date,
            "end_date": w.end_date,
        }
        for w in warnings
    ]


def _infer_market(
    symbol: str, market: Literal["kr", "us"] | None
) -> Literal["kr", "us"]:
    if market is not None:
        val = str(market).strip().lower()
        if val not in ("kr", "us"):
            raise ValueError(f"Invalid market: {market!r}. Toss supports 'kr' or 'us'.")
        return cast(Literal["kr", "us"], val)
    clean_sym = str(symbol).strip()
    if re.match(r"^\d{6}$", clean_sym):
        return "kr"
    return "us"


def _decimal_string(value: str | int | float | None, name: str) -> Decimal:
    if value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, float):
        raise TypeError(f"{name} must be str or int, not float")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid decimal value for {name}: {value!r}") from exc


def _stringify_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return f"{normalized:f}"


def _decimal_bps(value: float) -> Decimal:
    return Decimal(str(value)) / _BPS


def _quantize_bps_pct(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _currency_for_market(market: Literal["kr", "us"]) -> str:
    return "KRW" if market == "kr" else "USD"


def _distance_key_for_market(market: Literal["kr", "us"]) -> str:
    return "distance_krw" if market == "kr" else "distance_usd"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _stringify_decimal(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_json_safe(inner) for inner in value]
    if isinstance(value, tuple):
        return [_json_safe(inner) for inner in value]
    return value


def _new_client_order_id() -> str:
    return uuid.uuid4().hex


def _estimate_krw_notional(
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
) -> Decimal | None:
    if market != "kr":
        return None
    if price is not None and quantity is not None:
        return price * quantity
    if order_amount is not None:
        return order_amount
    return None


def _high_value_uncheckable(
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
) -> bool:
    """True when a KR order's KRW notional cannot be estimated locally (e.g. a
    market order has no price), so the local 1억 confirm gate cannot evaluate it.
    The broker still enforces ``confirm-high-value-required`` server-side."""
    return (
        market == "kr"
        and _estimate_krw_notional(market, quantity, price, order_amount) is None
    )


def _high_value_error(
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
    confirm_high_value_order: bool,
    base: dict[str, Any],
) -> dict[str, Any] | None:
    notional = _estimate_krw_notional(market, quantity, price, order_amount)
    if notional is not None and notional >= Decimal("100000000"):
        if not confirm_high_value_order:
            return {
                "success": False,
                **base,
                "error": (
                    f"High-value KR order (notional={notional} KRW >= 100M KRW) "
                    "requires confirm_high_value_order=True."
                ),
            }
    if notional is None and _high_value_uncheckable(
        market, quantity, price, order_amount
    ):
        logger.warning(
            "Toss KR order high-value (1억+) local gate could not be evaluated "
            "(no estimable KRW notional, e.g. market order); relying on broker-side "
            "confirm-high-value-required enforcement. confirm_high_value_order=%s",
            confirm_high_value_order,
        )
    return None


def _snap_kr_limit_price(
    price: Decimal, side: str, market: Literal["kr", "us"], order_type: str
) -> tuple[Decimal, Decimal | None]:
    """KR 지정가만 KRX tick에 스냅. 반환 (적용가, 원가 또는 None[무변경])."""
    if market != "kr" or order_type != "limit" or price <= 0:
        return price, None
    adjusted = Decimal(str(adjust_tick_size_kr(float(price), side)))
    if adjusted == price:
        return price, None
    logger.info(
        "Toss KR limit tick-snapped: side=%s original=%s tick=%s adjusted=%s",
        side,
        price,
        get_tick_size_kr(float(price)),
        adjusted,
    )
    return adjusted, price


def _live_mutation_disabled_error(
    operation: str,
    base: dict[str, Any],
) -> dict[str, Any] | None:
    if bool(getattr(settings, "toss_live_order_mutations_enabled", False)):
        return None
    return {
        "success": False,
        **base,
        "error": (
            f"{operation} requires TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. "
            "Keep live Toss mutations disabled until the accepted-order ledger "
            "and operator live-smoke hold are cleared."
        ),
    }


async def _find_holding(client: TossReadClient, symbol: str) -> Any | None:
    res = await client.holdings(symbol=symbol)
    if res and hasattr(res, "items") and res.items:
        for item in res.items:
            if item.symbol == symbol:
                return item
    return None


async def _latest_price(client: TossReadClient, symbol: str) -> Decimal:
    res = await client.prices([symbol])
    if res:
        for p in res:
            if p.symbol == symbol:
                return p.last_price
    raise ValueError(f"Could not resolve latest price for symbol: {symbol}")


async def _preview_price_context(
    client: TossReadClient, symbol: str
) -> tuple[Decimal | None, str | None, str | None]:
    try:
        prices = await client.prices([symbol])
        for item in prices:
            if item.symbol == symbol:
                return item.last_price, item.currency, None
        return None, None, f"Could not resolve latest price for symbol: {symbol}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"Failed to retrieve current price for {symbol}: {exc}"


def _limit_fill_context(
    *,
    market: Literal["kr", "us"],
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"],
    price: Decimal | None,
    current_price: Decimal | None,
) -> tuple[list[str], dict[str, Any] | None]:
    if (
        order_type != "limit"
        or price is None
        or current_price is None
        or current_price <= Decimal("0")
        or price == current_price
    ):
        return [], None

    direction = "above_market" if price > current_price else "below_market"
    marketable = (side == "buy" and price > current_price) or (
        side == "sell" and price < current_price
    )
    order_warnings: list[str] = []
    if side == "buy" and price > current_price:
        order_warnings.append("buy_limit_above_market")
    elif side == "sell" and price < current_price:
        order_warnings.append("sell_limit_below_market")
    elif side == "sell" and price > current_price:
        order_warnings.append("sell_limit_above_market")

    distance = abs(price - current_price)
    distance_pct = _quantize_bps_pct(distance / current_price * Decimal("100"))
    return order_warnings, {
        _distance_key_for_market(market): _stringify_decimal(distance),
        "distance_pct": _stringify_decimal(distance_pct),
        "currency": _currency_for_market(market),
        "marketable": marketable,
        "direction": direction,
    }


def _preview_notional(
    *,
    quantity: Decimal | None,
    effective_price: Decimal | None,
    order_amount: Decimal | None,
) -> Decimal | None:
    if order_amount is not None:
        return order_amount
    if quantity is not None and effective_price is not None:
        return quantity * effective_price
    return None


async def _preview_cost_context(
    *,
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    effective_price: Decimal | None,
    order_amount: Decimal | None,
) -> dict[str, Any]:
    currency = _currency_for_market(market)
    notional = _preview_notional(
        quantity=quantity,
        effective_price=effective_price,
        order_amount=order_amount,
    )
    if notional is None:
        return {
            "estimated_value": None,
            "estimated_value_currency": currency,
            "fee": None,
            "fee_currency": currency,
            "fx_cost_full_conversion": None,
            "fx_cost_full_conversion_currency": "KRW" if market == "us" else None,
            "estimated_costs": {
                "cost_profile_source": None,
                "cost_profile_review_required": True,
                "message": "notional unavailable: quantity/effective price or order_amount required",
            },
        }

    try:
        account_costs = await get_account_costs_setting()
        cost_profile_message = None
    except Exception as exc:  # noqa: BLE001
        account_costs = None
        cost_profile_message = f"account_costs_unavailable: {exc}"

    profiles = build_cost_profiles(account_costs)
    profile = profiles.market_profile("toss", market)
    fee = notional * _decimal_bps(profile.commission_bps)
    estimated_costs: dict[str, Any] = {
        "notional": _stringify_decimal(notional),
        "notional_currency": currency,
        "fee": _stringify_decimal(fee),
        "fee_currency": currency,
        "commission_bps": profile.commission_bps,
        "fx_spread_bps": profile.fx_spread_bps,
        "cost_profile_source": profiles.source,
        "cost_profile_review_required": profiles.review_required,
    }
    if cost_profile_message is not None:
        estimated_costs["cost_profile_message"] = cost_profile_message

    fx_cost_full_conversion: Decimal | None = None
    fx_cost_full_conversion_currency: str | None = None
    if market == "us":
        fx_cost_full_conversion_currency = "KRW"
        try:
            quote = await get_usd_krw_rate_details()
            usd_krw = Decimal(str(quote.default_rate))
            fx_cost_full_conversion = (
                notional * usd_krw * _decimal_bps(profile.fx_spread_bps)
            )
            estimated_costs.update(
                {
                    "fx_cost_full_conversion": _stringify_decimal(
                        fx_cost_full_conversion
                    ),
                    "fx_cost_full_conversion_currency": "KRW",
                    "fx_rate_usd_krw": _stringify_decimal(usd_krw),
                    "fx_rate_source": quote.source,
                    "fx_assumption": "full_notional_krw_conversion",
                }
            )
        except Exception as exc:  # noqa: BLE001
            estimated_costs.update(
                {
                    "fx_cost_full_conversion": None,
                    "fx_cost_full_conversion_currency": "KRW",
                    "fx_cost_message": f"fx_rate_unavailable: {exc}",
                    "fx_assumption": "full_notional_krw_conversion",
                }
            )
    else:
        fx_cost_full_conversion = Decimal("0")
        fx_cost_full_conversion_currency = "KRW"
        estimated_costs.update(
            {
                "fx_cost_full_conversion": "0",
                "fx_cost_full_conversion_currency": "KRW",
                "fx_assumption": "not_applicable_kr_order",
            }
        )

    return {
        "estimated_value": _stringify_decimal(notional),
        "estimated_value_currency": currency,
        "fee": _stringify_decimal(fee),
        "fee_currency": currency,
        "fx_cost_full_conversion": _stringify_decimal(fx_cost_full_conversion)
        if fx_cost_full_conversion is not None
        else None,
        "fx_cost_full_conversion_currency": fx_cost_full_conversion_currency,
        "estimated_costs": estimated_costs,
    }


async def _sell_loss_guard(
    client: TossReadClient,
    symbol: str,
    order_type: Literal["limit", "market"],
    price: Decimal | None,
    base: dict[str, Any],
    *,
    loss_cut_ctx: LossCutContext | None = None,
    current_price: Decimal | None = None,
) -> dict[str, Any] | None:
    try:
        holding = await _find_holding(client, symbol)
    except Exception as exc:
        return {
            "success": False,
            **base,
            "error": f"Failed to retrieve holdings for sell loss guard: {exc}",
        }

    if not holding:
        return {
            "success": False,
            **base,
            "error": f"No holding found for symbol {symbol} to validate sell cost basis (fail closed).",
        }

    avg = holding.average_purchase_price
    if avg <= Decimal("0"):
        return {
            "success": False,
            **base,
            "error": f"Invalid holding average purchase price for symbol {symbol}: {avg} (fail closed).",
        }

    floor = avg * Decimal("1.01")

    if loss_cut_ctx is not None:
        if price is None:
            return {
                "success": False,
                **base,
                "error": "loss_cut requires a limit sell price.",
            }
        curr_price = current_price
        if curr_price is None:
            try:
                curr_price = await _latest_price(client, symbol)
            except Exception as exc:
                return {
                    "success": False,
                    **base,
                    "error": (
                        "Failed to retrieve current price for loss_cut slip-band "
                        f"validation (fail closed): {exc}"
                    ),
                }
        error = evaluate_sell_price_guards(
            price=float(price),
            current_price=float(curr_price),
            avg_price=float(avg),
            defensive_trim_ctx=None,
            scalping_exit_ctx=None,
            loss_cut_ctx=loss_cut_ctx,
        )
        if error is not None:
            return {"success": False, **base, "error": error}
        return None

    if order_type == "limit":
        if price is None:
            return {
                "success": False,
                **base,
                "error": "Limit sell order requires price for cost basis validation.",
            }
        if price < floor:
            return {
                "success": False,
                **base,
                "error": f"Limit sell price {price} is below average purchase price floor ({floor}) (sell floor is avg_purchase_price * 1.01).",
            }
    else:  # market
        try:
            curr_price = await _latest_price(client, symbol)
        except Exception as exc:
            return {
                "success": False,
                **base,
                "error": f"Failed to retrieve current price for market sell cost basis validation (fail closed): {exc}",
            }
        if curr_price < floor:
            return {
                "success": False,
                **base,
                "error": f"Market sell proxy price {curr_price} is below average purchase price floor ({floor}) (sell floor is avg_purchase_price * 1.01).",
            }

    return None


async def _opposite_pending_error(
    client: TossReadClient,
    symbol: str,
    side: Literal["buy", "sell"],
    base: dict[str, Any],
) -> dict[str, Any] | None:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        while True:
            page = await client.list_orders(status="OPEN", symbol=symbol, cursor=cursor)
            opp_side = "SELL" if side == "buy" else "BUY"
            for order in page.orders:
                if order.symbol == symbol and order.side.upper() == opp_side:
                    return {
                        "success": False,
                        **base,
                        "error": f"An opposite pending order exists for symbol {symbol} ({opp_side}).",
                    }
            if not page.has_next:
                return None
            next_cursor = page.next_cursor
            if not next_cursor or next_cursor in seen_cursors:
                return {
                    "success": False,
                    **base,
                    "error": (
                        "Failed to check all pending orders: Toss pagination cursor "
                        "was missing or repeated (fail closed)."
                    ),
                }
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    except Exception as exc:
        return {
            "success": False,
            **base,
            "error": f"Failed to check pending orders: {exc}",
        }


_MARKET_NOT_SUPPORTED_CODE = "market-not-supported-for-stock"


def _toss_error_response(exc: Exception, base: dict[str, Any]) -> dict[str, Any]:
    if isinstance(exc, TossApiResponseError):
        payload = {
            "success": False,
            **base,
            "error": str(exc),
            "status_code": exc.status_code,
            "code": exc.envelope.code,
            "request_id": exc.envelope.request_id,
            "message": exc.envelope.message,
            "data": exc.envelope.data,
        }
        if exc.envelope.code == _MARKET_NOT_SUPPORTED_CODE:
            payload["error_code"] = "nxt_session_not_tradable"
            payload["alternatives"] = [RETRY_AT_REGULAR, ROUTE_VIA_KIS]
            payload["hint"] = (
                "Symbol is not tradable in the current NXT session. Retry during "
                "the KRX regular session, or route via KIS SOR."
            )
        return payload
    return {
        "success": False,
        **base,
        "error": f"{type(exc).__name__}: {exc}",
    }


async def _invalidate_sellable_after_sell_mutation(symbol: str) -> None:
    """Best-effort cache correction after Toss accepted a sell mutation."""
    try:
        await get_shared_sellable_cache().invalidate(symbol)
    except Exception as exc:  # noqa: BLE001 — never mask a live broker result
        logger.warning(
            "Toss sellable-cache invalidation failed symbol=%s: %s", symbol, exc
        )


_SAFE_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _order_id_error(order_id: str, base: dict[str, Any]) -> dict[str, Any] | None:
    candidate = (order_id or "").strip()
    if not candidate or not _SAFE_ORDER_ID_RE.fullmatch(candidate):
        return {
            "success": False,
            **base,
            "error": f"Unsafe order id rejected: {order_id!r}",
        }
    return None


_SAFE_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")


def _client_order_id_error(
    client_order_id: str | None, base: dict[str, Any]
) -> dict[str, Any] | None:
    if client_order_id is None:
        return None
    candidate = client_order_id.strip()
    if (
        not candidate
        or candidate != client_order_id
        or not _SAFE_CLIENT_ORDER_ID_RE.fullmatch(candidate)
    ):
        return {
            "success": False,
            **base,
            "error": f"Unsafe client order id rejected: {client_order_id!r}",
        }
    return None


async def _nxt_preflight_context(
    symbol: str,
    market: Literal["kr", "us"],
    *,
    now: datetime | None = None,
) -> tuple[NxtPreflightVerdict, NxtTradability] | None:
    """KR-only session-aware NXT preflight. None when market != 'kr' or mode off.

    Fail-open: get_kr_toss_session_from_toss returns None when the Toss calendar
    is unavailable -> evaluate_nxt_preflight yields an advisory (non-blocking)
    verdict.
    """
    if market != "kr":
        return None
    mode = getattr(settings, "toss_nxt_preflight_mode", "warn")
    if mode == "off":
        return None
    moment = now or now_kst()
    # Fail-open: a DB/calendar hiccup (or a missing kr_symbol_universe table) must
    # never break an order preview/place. Any error -> no advisory preflight.
    try:
        session = await get_kr_toss_session_from_toss(moment)
        tradability = (await get_kr_nxt_tradability([symbol])).get(
            symbol
        ) or NxtTradability(nxt_eligible=False, nxt_trading_suspended=None, asof=None)
    except Exception as exc:  # noqa: BLE001 - advisory preflight must never block an order
        logger.warning(
            "NXT preflight context unavailable for %s, skipping (fail-open): %s",
            symbol,
            exc,
        )
        return None
    verdict = evaluate_nxt_preflight(session, tradability)
    return verdict, tradability


async def toss_preview_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    account_mode: str | None = None,
    account_type: str | None = None,
    rung: str | int | None = None,
    exit_intent: str | None = None,
    exit_reason: str | None = None,
    retrospective_id: int | None = None,
    approval_issue_id: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    mkt = _infer_market(symbol, market)
    if exit_intent is not None and exit_intent != "loss_cut":
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": f"unknown exit_intent {exit_intent!r} (only 'loss_cut')",
        }
    loss_cut_ctx, loss_cut_errors = await _validate_loss_cut_preconditions(
        exit_intent=exit_intent,
        retrospective_id=retrospective_id,
        exit_reason=exit_reason,
        approval_issue_id=approval_issue_id,
        side=side,
        order_type=order_type,
        is_mock=False,
        symbol=symbol,
    )
    if loss_cut_errors:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": "loss_cut_preconditions_failed",
            "violations": loss_cut_errors,
        }
    quantity_dec = (
        _decimal_string(quantity, "quantity") if quantity is not None else None
    )
    price_dec = _decimal_string(price, "price") if price is not None else None
    order_amount_dec = (
        _decimal_string(order_amount, "order_amount")
        if order_amount is not None
        else None
    )

    tick_meta: dict[str, Any] = {}
    if price_dec is not None:
        price_dec, original_for_meta = _snap_kr_limit_price(
            price_dec, side, mkt, order_type
        )
        if original_for_meta is not None:
            tick_meta = {
                "tick_adjusted": True,
                "original_price": _stringify_decimal(original_for_meta),
                "adjusted_price": _stringify_decimal(price_dec),
            }

    quantity_str = _stringify_decimal(quantity_dec)
    price_str = _stringify_decimal(price_dec)
    order_amount_str = _stringify_decimal(order_amount_dec)

    canonical = build_canonical_payload(
        market=mkt,
        symbol=symbol,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        quantity=quantity_str,
        price=price_str,
        order_amount=order_amount_str,
    )
    now = now_kst()
    proposal_context = _order_proposal_context.get()
    client_order_id = (
        proposal_context.client_order_id
        if proposal_context is not None
        else derive_client_order_id(canonical, market=mkt, now=now, rung=rung)
    )
    approval_hash = encode_approval_token(canonical, now=now)
    approval_expires_at = (
        (now + timedelta(seconds=APPROVAL_TTL_SECONDS)).astimezone(KST).isoformat()
    )

    payload: dict[str, Any] = {
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
    }
    if quantity_str is not None:
        payload["quantity"] = quantity_str
    if price_str is not None:
        payload["price"] = price_str
    if order_amount_str is not None:
        payload["orderAmount"] = order_amount_str

    warnings_list = []
    warnings_check_msg = None
    order_warnings: list[str] = []
    current_price_dec: Decimal | None = None
    current_price_currency: str | None = None
    price_context_message: str | None = None
    try:
        async with _client_context() as client:
            (
                current_price_dec,
                current_price_currency,
                price_context_message,
            ) = await _preview_price_context(client, symbol)
            guard_res = await check_warnings_guard(
                client, symbol, market=mkt, side=side
            )
            warnings_list = [
                {
                    "warning_type": w.warning_type,
                    "exchange": w.exchange,
                    "start_date": w.start_date,
                    "end_date": w.end_date,
                }
                for w in guard_res.warnings
            ]
            if guard_res.error_message:
                warnings_check_msg = guard_res.error_message
    except Exception as exc:
        logger.error("Failed to check warnings in preview: %s", exc, exc_info=True)
        warnings_check_msg = f"Failed to check warnings: {exc}"

    if price_context_message is not None:
        order_warnings.append(_PRICE_CONTEXT_UNAVAILABLE)

    if loss_cut_ctx is not None:
        if current_price_dec is None:
            return {
                "success": False,
                "source": "toss",
                "account_mode": ACCOUNT_MODE_TOSS_LIVE,
                "preview": True,
                "error": (
                    "Failed to retrieve current price for loss_cut slip-band "
                    "validation (fail closed)."
                ),
            }
        async with _client_context() as client:
            loss_cut_guard = await _sell_loss_guard(
                client,
                symbol,
                order_type,
                price_dec,
                {
                    "source": "toss",
                    "account_mode": ACCOUNT_MODE_TOSS_LIVE,
                    "preview": True,
                },
                loss_cut_ctx=loss_cut_ctx,
                current_price=current_price_dec,
            )
        if loss_cut_guard is not None:
            return loss_cut_guard

    fill_warnings, fill_distance = _limit_fill_context(
        market=mkt,
        side=side,
        order_type=order_type,
        price=price_dec,
        current_price=current_price_dec,
    )
    order_warnings.extend(fill_warnings)

    nxt_preflight_payload: dict[str, Any] | None = None
    preflight = await _nxt_preflight_context(symbol, mkt)
    if preflight is not None:
        verdict, _ = preflight
        nxt_preflight_payload = verdict.to_dict()
        if verdict.block:
            order_warnings.append("nxt_session_not_tradable")

    effective_price = price_dec if price_dec is not None else current_price_dec
    cost_context = await _preview_cost_context(
        market=mkt,
        quantity=quantity_dec,
        effective_price=effective_price,
        order_amount=order_amount_dec,
    )

    sector_conc = None
    if side == "buy":
        estimated_val_str = cost_context.get("estimated_value")
        estimated_val = (
            float(estimated_val_str) if estimated_val_str is not None else None
        )
        order_currency = cost_context.get("estimated_value_currency") or (
            "KRW" if mkt == "kr" else "USD"
        )
        sector_conc = await evaluate_sector_concentration(
            symbol=symbol,
            market=mkt,
            order_estimated_value=estimated_val,
            order_currency=order_currency,
            # ROB-646 Finding 1: whole live portfolio (Toss is live-only)
            account_ctx={"is_mock": False},
        )
        if sector_conc and sector_conc.get("warning"):
            order_warnings.append(sector_conc["warning"])

    response = {
        "success": True,
        "preview": True,
        "market": mkt,
        **tick_meta,
        "current_price": _stringify_decimal(current_price_dec),
        "current_price_currency": current_price_currency,
        "order_warnings": order_warnings,
        "payload_preview": payload,
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "warnings": warnings_list,
        "warnings_check_message": warnings_check_msg,
        "approval_hash": approval_hash,
        "approval_expires_at": approval_expires_at,
        "sector_concentration": sector_conc,
        "nxt_preflight": nxt_preflight_payload,
        **cost_context,
    }
    if price_context_message is not None:
        response["price_context_message"] = price_context_message
    if fill_distance is not None:
        response["fill_distance"] = fill_distance
    if loss_cut_ctx is not None:
        response["exit_intent"] = "loss_cut"
        response["retrospective_id"] = loss_cut_ctx.retrospective_id
        response["loss_cut_slip_band"] = float(current_price_dec) * (
            1.0 - loss_cut_ctx.max_slip
        )
    return response


async def _toss_place_order_impl(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    reason: str | None = None,
    exit_intent: str | None = None,
    exit_reason: str | None = None,
    retrospective_id: int | None = None,
    approval_issue_id: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: str | int | None = None,
    stop_loss: str | int | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    report_item_uuid: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
    approval_hash: str | None = None,
    rung: str | int | None = None,
    client_order_id_override: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    proposal_context = _order_proposal_context.get()
    mkt = _infer_market(symbol, market)
    if exit_intent is not None and exit_intent != "loss_cut":
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": f"unknown exit_intent {exit_intent!r} (only 'loss_cut')",
        }
    loss_cut_ctx, loss_cut_errors = await _validate_loss_cut_preconditions(
        exit_intent=exit_intent,
        retrospective_id=retrospective_id,
        exit_reason=exit_reason,
        approval_issue_id=approval_issue_id,
        side=side,
        order_type=order_type,
        is_mock=False,
        symbol=symbol,
    )
    if loss_cut_errors:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "dry_run": dry_run,
            "mutation_sent": False,
            "error": "loss_cut_preconditions_failed",
            "violations": loss_cut_errors,
        }
    quantity_dec = (
        _decimal_string(quantity, "quantity") if quantity is not None else None
    )
    price_dec = _decimal_string(price, "price") if price is not None else None
    order_amount_dec = (
        _decimal_string(order_amount, "order_amount")
        if order_amount is not None
        else None
    )
    target_price_dec = (
        _decimal_string(target_price, "target_price")
        if target_price is not None
        else None
    )
    stop_loss_dec = (
        _decimal_string(stop_loss, "stop_loss") if stop_loss is not None else None
    )

    tick_meta: dict[str, Any] = {}
    if price_dec is not None:
        price_dec, original_for_meta = _snap_kr_limit_price(
            price_dec, side, mkt, order_type
        )
        if original_for_meta is not None:
            tick_meta = {
                "tick_adjusted": True,
                "original_price": _stringify_decimal(original_for_meta),
                "adjusted_price": _stringify_decimal(price_dec),
            }

    quantity_str = _stringify_decimal(quantity_dec)
    price_str = _stringify_decimal(price_dec)
    order_amount_str = _stringify_decimal(order_amount_dec)

    canonical = build_canonical_payload(
        market=mkt,
        symbol=symbol,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        quantity=quantity_str,
        price=price_str,
        order_amount=order_amount_str,
    )
    now = now_kst()
    client_order_id = client_order_id_override or derive_client_order_id(
        canonical, market=mkt, now=now, rung=rung
    )
    ledger_approval_hash = derive_approval_digest(canonical)

    payload: dict[str, Any] = {
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
    }
    if quantity_str is not None:
        payload["quantity"] = quantity_str
    if price_str is not None:
        payload["price"] = price_str
    if order_amount_str is not None:
        payload["orderAmount"] = order_amount_str
    if confirm_high_value_order:
        payload["confirmHighValueOrder"] = True

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "dry_run": dry_run,
        "mutation_sent": False,
        **tick_meta,
        # ROB-545 Major — carry the clientOrderId on every response (incl. error
        # paths) so a failed/timed-out order can be retried with the *same*
        # idempotency key instead of minting a new one.
        "client_order_id": payload["clientOrderId"],
    }

    if (
        id_guard := _client_order_id_error(client_order_id_override, base_response)
    ) is not None:
        return id_guard
    mode = getattr(settings, "toss_approval_hash_mode", "optional")
    if loss_cut_ctx is not None and not dry_run:
        if approval_hash is None:
            return {
                "success": False,
                **base_response,
                "error": (
                    "loss_cut live send requires approval_hash "
                    "(re-run toss_preview_order and pass the returned token)"
                ),
                "error_code": "loss_cut_approval_hash_required",
            }
        result = verify_approval_token(approval_hash, canonical, now=now)
        if not result.ok:
            err = {
                "success": False,
                **base_response,
                "error": result.message,
                "error_code": result.error_code,
            }
            if result.diff is not None:
                err["diff"] = result.diff
            return err
    elif mode != "off":
        if approval_hash is not None:
            result = verify_approval_token(approval_hash, canonical, now=now)
            if not result.ok:
                err = {
                    "success": False,
                    **base_response,
                    "error": result.message,
                    "error_code": result.error_code,
                }
                if result.diff is not None:
                    err["diff"] = result.diff
                return err
        elif mode == "required":
            return {
                "success": False,
                **base_response,
                "error": (
                    "toss_place_order requires approval_hash "
                    "(TOSS_APPROVAL_HASH_MODE=required). Re-preview and pass "
                    "approval_hash from toss_preview_order."
                ),
                "error_code": "approval_hash_required",
            }
        elif mode == "warn":
            logger.warning(
                "toss_place_order called without approval_hash "
                "(mode=warn) symbol=%s side=%s",
                symbol,
                side,
            )

    if dry_run:
        sector_conc = None
        order_warnings = []
        if side == "buy":
            effective_price = price_dec
            if effective_price is None:
                try:
                    async with _client_context() as client:
                        current_price_dec, _, _ = await _preview_price_context(
                            client, symbol
                        )
                        effective_price = current_price_dec
                except Exception:
                    pass
            cost_context = await _preview_cost_context(
                market=mkt,
                quantity=quantity_dec,
                effective_price=effective_price,
                order_amount=order_amount_dec,
            )
            estimated_val_str = cost_context.get("estimated_value")
            estimated_val = (
                float(estimated_val_str) if estimated_val_str is not None else None
            )
            order_currency = cost_context.get("estimated_value_currency") or (
                "KRW" if mkt == "kr" else "USD"
            )
            sector_conc = await evaluate_sector_concentration(
                symbol=symbol,
                market=mkt,
                order_estimated_value=estimated_val,
                order_currency=order_currency,
                # ROB-646 Finding 1: whole live portfolio (Toss is live-only)
                account_ctx={"is_mock": False},
            )
            if sector_conc and sector_conc.get("warning"):
                order_warnings.append(sector_conc["warning"])

        dry_run_res = {
            "success": True,
            **base_response,
            "payload_preview": payload,
            "sector_concentration": sector_conc,
        }
        if loss_cut_ctx is not None:
            dry_run_res["exit_intent"] = "loss_cut"
            dry_run_res["retrospective_id"] = loss_cut_ctx.retrospective_id
        if order_warnings:
            dry_run_res["order_warnings"] = order_warnings
        return dry_run_res

    if not confirm:
        return {
            "success": False,
            **base_response,
            "error": "toss_place_order requires confirm=True when dry_run=False.",
        }

    # Guard: high-value KR order
    if (
        guard := _high_value_error(
            mkt,
            quantity_dec,
            price_dec,
            order_amount_dec,
            confirm_high_value_order,
            base_response,
        )
    ) is not None:
        return guard

    if (
        mutation_gate := _live_mutation_disabled_error(
            "toss_place_order", base_response
        )
    ) is not None:
        return mutation_gate

    async def execute_order(client: TossReadClient):
        # Guard: Warnings check
        guard_res = await check_warnings_guard(client, symbol, market=mkt, side=side)
        guard_warnings = _warning_payload(guard_res.warnings)
        if not guard_res.ok:
            return {
                "success": False,
                **base_response,
                "error": guard_res.error_message,
                "warnings": guard_warnings,
            }

        # Guard: opposite pending order check
        if (
            opp_guard := await _opposite_pending_error(
                client, symbol, side, base_response
            )
        ) is not None:
            return opp_guard

        # Guard: sell loss check
        if side == "sell":
            if (
                sell_guard := await _sell_loss_guard(
                    client,
                    symbol,
                    order_type,
                    price_dec,
                    base_response,
                    loss_cut_ctx=loss_cut_ctx,
                )
            ) is not None:
                return sell_guard

        # Guard: NXT session preflight. Required mode fail-closes before POST;
        # warn/optional log but proceed (fail-open on unknown session).
        preflight = await _nxt_preflight_context(symbol, mkt)
        if preflight is not None:
            verdict, _ = preflight
            if verdict.block:
                mode = getattr(settings, "toss_nxt_preflight_mode", "warn")
                if mode == "required":
                    return {
                        "success": False,
                        **base_response,
                        "error": (
                            f"NXT session {verdict.session!r} does not support "
                            f"{symbol} ({verdict.reason}); order not sent."
                        ),
                        "error_code": "nxt_session_not_tradable",
                        "session": verdict.session,
                        "alternatives": list(verdict.alternatives),
                    }
                logger.warning(
                    "NXT preflight advisory (mode=%s): symbol=%s session=%s "
                    "reason=%s — proceeding with live send",
                    mode,
                    symbol,
                    verdict.session,
                    verdict.reason,
                )

        res = None
        try:
            res = await client.place_order(payload)
            if side == "sell":
                await _invalidate_sellable_after_sell_mutation(symbol)
            raw_response = {
                "orderId": res.order_id,
                "clientOrderId": res.client_order_id,
                "payload": _json_safe(payload),
            }
            ledger = await record_toss_place_order(
                market=mkt,
                symbol=symbol,
                side=side,
                order_type=order_type,
                time_in_force=time_in_force,
                quantity=quantity_dec,
                price=price_dec,
                order_amount=order_amount_dec,
                currency=("KRW" if mkt == "kr" else "USD"),
                client_order_id=res.client_order_id or str(payload["clientOrderId"]),
                broker_order_id=res.order_id,
                raw_response=raw_response,
                reason=reason,
                exit_intent=exit_intent,
                exit_reason=exit_reason,
                retrospective_id=retrospective_id,
                approval_issue_id=approval_issue_id,
                thesis=thesis,
                strategy=strategy,
                target_price=target_price_dec,
                stop_loss=stop_loss_dec,
                min_hold_days=min_hold_days,
                notes=notes,
                indicators_snapshot=indicators_snapshot,
                report_item_uuid=report_item_uuid,
                approval_hash=ledger_approval_hash,
                correlation_id_override=(
                    proposal_context.correlation_id
                    if proposal_context is not None
                    else None
                ),
                rung=(proposal_context.rung if proposal_context is not None else rung),
            )
            return {
                "success": True,
                **base_response,
                "mutation_sent": True,
                "order_id": res.order_id,
                "client_order_id": res.client_order_id,
                **ledger,
                "approval_hash_digest": ledger_approval_hash,
                "warnings": guard_warnings,
                "warnings_check_message": guard_res.error_message,
                "message": (
                    "Toss live order accepted and recorded accepted-only; "
                    "run toss_reconcile_orders to book confirmed fills."
                ),
            }
        except Exception as exc:
            err = _toss_error_response(exc, {**base_response, "mutation_sent": True})
            # ROB-545 Major/B2 — if the POST already reached Toss (res set) the
            # broker order id must survive a ledger-write failure or idempotency
            # anomaly, so reconcile/cancel can still find the live order.
            if res is not None:
                err["order_id"] = res.order_id
                if res.client_order_id is not None:
                    err["client_order_id"] = res.client_order_id
            return err

    async with _client_context() as client:
        return await execute_order(client)


async def toss_place_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    reason: str | None = None,
    exit_intent: str | None = None,
    exit_reason: str | None = None,
    retrospective_id: int | None = None,
    approval_issue_id: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: str | int | None = None,
    stop_loss: str | int | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    report_item_uuid: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
    approval_hash: str | None = None,
    rung: str | int | None = None,
) -> dict[str, Any]:
    proposal_context = _order_proposal_context.get()
    return await _toss_place_order_impl(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        order_amount=order_amount,
        market=market,
        time_in_force=time_in_force,
        dry_run=dry_run,
        confirm=confirm,
        confirm_high_value_order=confirm_high_value_order,
        reason=reason,
        exit_intent=exit_intent,
        exit_reason=exit_reason,
        retrospective_id=retrospective_id,
        approval_issue_id=approval_issue_id,
        thesis=thesis,
        strategy=strategy,
        target_price=target_price,
        stop_loss=stop_loss,
        min_hold_days=min_hold_days,
        notes=notes,
        indicators_snapshot=indicators_snapshot,
        report_item_uuid=report_item_uuid,
        account_mode=account_mode,
        account_type=account_type,
        approval_hash=approval_hash,
        rung=rung,
        client_order_id_override=(
            proposal_context.client_order_id if proposal_context is not None else None
        ),
    )


async def toss_modify_order(
    order_id: str,
    new_price: str | int | None = None,
    new_quantity: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "dry_run": dry_run,
        "mutation_sent": False,
    }

    if (id_guard := _order_id_error(order_id, base_response)) is not None:
        return id_guard

    if not dry_run and not confirm:
        return {
            "success": False,
            **base_response,
            "error": "toss_modify_order requires confirm=True when dry_run=False.",
        }

    async def execute_modify(client: TossReadClient):
        try:
            orig_order = await client.get_order(order_id)
        except Exception as exc:
            return _toss_error_response(exc, base_response)

        symbol = orig_order.symbol
        side = orig_order.side.lower()
        orig_order_type = orig_order.order_type.lower()

        mkt = _infer_market(symbol, market)
        new_price_dec = (
            _decimal_string(new_price, "new_price") if new_price is not None else None
        )
        new_quantity_dec = (
            _decimal_string(new_quantity, "new_quantity")
            if new_quantity is not None
            else None
        )

        if mkt == "kr":
            if new_price_dec is None or new_quantity_dec is None:
                return {
                    "success": False,
                    **base_response,
                    "error": "Toss KR order modify requires both new_price and new_quantity.",
                }
        else:  # us
            if new_quantity_dec is not None:
                return {
                    "success": False,
                    **base_response,
                    "error": "Toss US order modify rejects new_quantity; only price modification is supported.",
                }
            if new_price_dec is None:
                return {
                    "success": False,
                    **base_response,
                    "error": "Toss US order modify requires new_price.",
                }

        # ROB-561 — a KR limit reprice must snap to the KRX tick grid just like
        # a fresh place, otherwise the modify hits the same tick-size rejection.
        # Snap BEFORE the sell-loss guard so the guard validates the real price.
        tick_meta: dict[str, Any] = {}
        if new_price_dec is not None:
            new_price_dec, original_for_meta = _snap_kr_limit_price(
                new_price_dec, side, mkt, orig_order_type
            )
            if original_for_meta is not None:
                tick_meta = {
                    "tick_adjusted": True,
                    "original_price": _stringify_decimal(original_for_meta),
                    "adjusted_price": _stringify_decimal(new_price_dec),
                }

        payload: dict[str, Any] = {
            "orderType": orig_order_type.upper(),
        }
        if new_price_dec is not None:
            payload["price"] = _stringify_decimal(new_price_dec)
        if new_quantity_dec is not None:
            payload["quantity"] = _stringify_decimal(new_quantity_dec)
        if confirm_high_value_order:
            payload["confirmHighValueOrder"] = True

        if side == "sell" and orig_order_type == "limit":
            if (
                sell_guard := await _sell_loss_guard(
                    client, symbol, "limit", new_price_dec, base_response
                )
            ) is not None:
                return sell_guard

        if (
            high_value_guard := _high_value_error(
                mkt,
                new_quantity_dec,
                new_price_dec,
                None,
                confirm_high_value_order,
                base_response,
            )
        ) is not None:
            return high_value_guard

        if not dry_run:
            mutation_gate = _live_mutation_disabled_error(
                "toss_modify_order", base_response
            )
            if mutation_gate is not None:
                return mutation_gate

        if dry_run:
            return {
                "success": True,
                **base_response,
                **tick_meta,
                "original_order_id": order_id,
                "payload_preview": payload,
            }

        res = None
        try:
            res = await client.modify_order(order_id, payload)
            if side == "sell":
                await _invalidate_sellable_after_sell_mutation(symbol)
            ledger = await record_toss_replacement_order(
                operation_kind="modify",
                market=mkt,
                symbol=symbol,
                side=side,
                order_type=orig_order_type,
                time_in_force=orig_order.time_in_force,
                quantity=new_quantity_dec or orig_order.quantity,
                price=new_price_dec or orig_order.price,
                order_amount=orig_order.order_amount,
                currency=orig_order.currency,
                original_order_id=order_id,
                replacement_order_id=res.order_id,
                raw_response={
                    "operation": "modify",
                    "originalOrderId": order_id,
                    "replacementOrderId": res.order_id,
                    "payload": _json_safe(payload),
                },
            )
            return {
                "success": True,
                **base_response,
                **tick_meta,
                "mutation_sent": True,
                "original_order_id": order_id,
                "replacement_order_id": res.order_id,
                "operation_semantics": "Toss modify returns a newly issued orderId; it is not the original order id.",
                **ledger,
            }
        except Exception as exc:
            err = _toss_error_response(exc, {**base_response, "mutation_sent": True})
            # ROB-545 Major — keep the order ids on the error path so the live
            # order (and any issued replacement) can be reconciled/cancelled.
            err.setdefault("original_order_id", order_id)
            if res is not None:
                err["replacement_order_id"] = res.order_id
                err.setdefault("order_id", res.order_id)
            return err

    async with _client_context() as client:
        return await execute_modify(client)


async def toss_cancel_order(
    order_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "dry_run": dry_run,
        "mutation_sent": False,
    }

    if (id_guard := _order_id_error(order_id, base_response)) is not None:
        return id_guard

    if dry_run:
        return {
            "success": True,
            **base_response,
            "original_order_id": order_id,
        }

    if not confirm:
        return {
            "success": False,
            **base_response,
            "error": "toss_cancel_order requires confirm=True when dry_run=False.",
        }

    if (
        mutation_gate := _live_mutation_disabled_error(
            "toss_cancel_order", base_response
        )
    ) is not None:
        return mutation_gate

    res = None
    try:
        async with _client_context() as client:
            try:
                orig_order = await client.get_order(order_id)
            except Exception as exc:
                return _toss_error_response(
                    exc, {**base_response, "original_order_id": order_id}
                )
            mkt = _infer_market(orig_order.symbol, None)
            res = await client.cancel_order(order_id)
            if str(orig_order.side).lower() == "sell":
                await _invalidate_sellable_after_sell_mutation(orig_order.symbol)
            ledger = await record_toss_replacement_order(
                operation_kind="cancel",
                market=mkt,
                symbol=orig_order.symbol,
                side=str(orig_order.side).lower(),
                order_type=str(orig_order.order_type).lower(),
                time_in_force=orig_order.time_in_force,
                quantity=orig_order.quantity,
                price=orig_order.price,
                order_amount=orig_order.order_amount,
                currency=orig_order.currency,
                original_order_id=order_id,
                replacement_order_id=res.order_id,
                raw_response={
                    "operation": "cancel",
                    "originalOrderId": order_id,
                    "replacementOrderId": res.order_id,
                },
            )
            return {
                "success": True,
                **base_response,
                "mutation_sent": True,
                "original_order_id": order_id,
                "replacement_order_id": res.order_id,
                **ledger,
                "operation_semantics": "Toss cancel returns a newly issued orderId; it is not the original order id.",
            }
    except Exception as exc:
        err = _toss_error_response(exc, {**base_response, "mutation_sent": True})
        # ROB-545 Major — keep the order ids on the error path so the live order
        # (and any issued cancel replacement) can be reconciled/cancelled.
        err.setdefault("original_order_id", order_id)
        if res is not None:
            err["replacement_order_id"] = res.order_id
            err.setdefault("order_id", res.order_id)
        return err


async def toss_get_order_history(
    status: Literal["open", "closed"] = "closed",
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
    }

    toss_status = "CLOSED" if status == "closed" else "OPEN"

    try:
        async with _client_context() as client:
            page = await client.list_orders(
                status=toss_status,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
                cursor=cursor,
                limit=limit,
            )
            orders_list = []
            for o in page.orders:
                orders_list.append(
                    {
                        "order_id": o.order_id,
                        "symbol": o.symbol,
                        "side": o.side,
                        "order_type": o.order_type,
                        "time_in_force": o.time_in_force,
                        "status": o.status,
                        "price": _stringify_decimal(o.price)
                        if o.price is not None
                        else None,
                        "quantity": _stringify_decimal(o.quantity)
                        if o.quantity is not None
                        else None,
                        "order_amount": _stringify_decimal(o.order_amount)
                        if o.order_amount is not None
                        else None,
                        "currency": o.currency,
                        "ordered_at": _json_safe(o.ordered_at),
                        "canceled_at": _json_safe(o.canceled_at),
                        "execution": _json_safe(o.execution),
                    }
                )
            return {
                "success": True,
                **base_response,
                "orders": orders_list,
                "next_cursor": page.next_cursor,
                "has_next": page.has_next,
            }
    except Exception as exc:
        return _toss_error_response(exc, base_response)


async def toss_get_positions(
    symbol: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
    }

    try:
        async with _client_context() as client:
            res = await client.holdings(symbol=symbol)
            items_list = []
            for item in res.items:
                items_list.append(
                    {
                        "symbol": item.symbol,
                        "name": item.name,
                        "market_country": item.market_country,
                        "currency": item.currency,
                        "quantity": _stringify_decimal(item.quantity)
                        if item.quantity is not None
                        else None,
                        "last_price": _stringify_decimal(item.last_price)
                        if item.last_price is not None
                        else None,
                        "average_purchase_price": _stringify_decimal(
                            item.average_purchase_price
                        )
                        if item.average_purchase_price is not None
                        else None,
                        "market_value": _json_safe(item.market_value),
                        "profit_loss": _json_safe(item.profit_loss),
                        "daily_profit_loss": _json_safe(item.daily_profit_loss),
                        "cost": _json_safe(item.cost),
                    }
                )
            return {
                "success": True,
                **base_response,
                "items": items_list,
                "overview": _json_safe(res.raw_overview),
            }
    except Exception as exc:
        return _toss_error_response(exc, base_response)


async def toss_get_orderable_cash(
    currency: Literal["KRW", "USD"] = "KRW",
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
    }

    try:
        async with _client_context() as client:
            res = await client.buying_power(currency=currency)
            return {
                "success": True,
                **base_response,
                "cash_buying_power": _stringify_decimal(res.cash_buying_power),
                "currency": res.currency,
            }
    except Exception as exc:
        return _toss_error_response(exc, base_response)


async def toss_reconcile_orders(
    symbol: str | None = None,
    order_id: str | None = None,
    market: Literal["kr", "us"] | None = None,
    dry_run: bool = True,
    limit: int = 100,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard
    from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl

    return await toss_reconcile_orders_impl(
        symbol=symbol,
        order_id=order_id,
        market=market,
        dry_run=dry_run,
        limit=limit,
    )


def register_toss_live_order_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="toss_preview_order",
        description=(
            "Preview a Toss Securities live KR/US order without sending. "
            "Hard-pinned to account_mode='toss_live'; matching account_mode is "
            "optional and any other value is rejected. market='kr'|'us' is "
            "accepted or inferred from symbol. This is read-only but still "
            "requires TOSS_API_ENABLED and Toss credentials. The response "
            "includes current_price, fill_distance/order_warnings for limit "
            "marketability, and estimated Toss fee/FX full-conversion costs "
            "from account_costs. It also mints the approval binding for "
            "toss_place_order: approval_hash (a self-contained token over the "
            "tick-normalized order, 5-minute TTL), approval_expires_at, and a "
            "content-based idempotency_key. Pass the optional rung (ladder level) "
            "to keep sibling ladder orders on the same day distinct. Hand the "
            "returned approval_hash (and matching rung) back to toss_place_order. "
            "For exit_intent='loss_cut', also pass exit_reason, a <=72h matching "
            "retrospective_id, and a dedicated per-order Paperclip "
            "approval_issue_id whose API status is exactly 'done'. The preview "
            "revalidates caller authorization and applies the configured current-"
            "price slip band while exempting only that valid request from the "
            "average-cost floor."
        ),
    )(toss_preview_order)
    mcp.tool(
        name="toss_place_order",
        description=(
            "Place a Toss Securities live KR/US order. Hard-pinned to "
            "account_mode='toss_live'; market='kr'|'us' is accepted or inferred. "
            "dry_run=True by default and sends no mutation. Real submission "
            "requires dry_run=False, confirm=True, and "
            "TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. 100M KRW+ computable KR "
            "orders require operator-supplied confirm_high_value_order=True; it "
            "is never inferred. Before POST the tool blocks opposite pending "
            "orders across all paginated OPEN pages and applies the live sell "
            "loss guard (sell price/current market proxy must be >= "
            "avg_purchase_price*1.01). Supports optional metadata (note, "
            "reason, strategy, signal) for ledger recording. Approval-hash "
            "binding (TOSS_APPROVAL_HASH_MODE, default optional): pass the "
            "approval_hash minted by toss_preview_order (with the same rung) so "
            "the tool re-derives the canonical order and fail-closes on mismatch "
            "or expiry. off = ignored; optional = verified only when supplied; "
            "warn = same as optional but logs a hash-less live send; required = "
            "a valid, unexpired approval_hash is mandatory. Order-proposal "
            "preview/submit identity and correlation are bound internally and "
            "cannot be supplied by MCP callers. A valid loss_cut repeats the "
            "Paperclip/caller/retrospective checks immediately before send and "
            "requires the supplied preview approval_hash even when "
            "TOSS_APPROVAL_HASH_MODE=off; unrelated completed issues must not be "
            "reused."
        ),
    )(toss_place_order)
    mcp.tool(
        name="toss_modify_order",
        description=(
            "Modify a pending Toss Securities live order. Hard-pinned to "
            "account_mode='toss_live'. dry_run=True by default; real submission "
            "requires dry_run=False, confirm=True, and "
            "TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. KR modify requires both "
            "new_price and new_quantity. US modify requires new_price and "
            "rejects new_quantity. Sell reprices are blocked below "
            "avg_purchase_price*1.01. Toss returns a newly issued replacement "
            "orderId for successful modify."
        ),
    )(toss_modify_order)
    mcp.tool(
        name="toss_cancel_order",
        description=(
            "Cancel a pending Toss Securities live order. Hard-pinned to "
            "account_mode='toss_live'. dry_run=True by default; real submission "
            "requires dry_run=False, confirm=True, and "
            "TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. Toss returns a newly issued "
            "replacement orderId for successful cancel."
        ),
    )(toss_cancel_order)
    mcp.tool(
        name="toss_get_order_history",
        description=(
            "Retrieve Toss Securities live order history for account_mode='toss_live'. "
            "Supports status='open'|'closed'; closed history supports cursor/limit "
            "pagination."
        ),
    )(toss_get_order_history)
    mcp.tool(
        name="toss_get_positions",
        description=(
            "Retrieve Toss Securities live KR/US positions for "
            "account_mode='toss_live'. Read-only and default-disabled by Toss "
            "API config."
        ),
    )(toss_get_positions)
    mcp.tool(
        name="toss_get_orderable_cash",
        description=(
            "Retrieve Toss Securities live orderable cash/buying power for "
            "account_mode='toss_live' in currency='KRW'|'USD'. Read-only and "
            "default-disabled by Toss API config."
        ),
    )(toss_get_orderable_cash)
    mcp.tool(
        name="toss_reconcile_orders",
        description=(
            "Reconcile Toss Securities live KR/US orders from the local "
            "review.toss_live_order_ledger against single-order broker evidence "
            "from GET /orders/{orderId}. Books fill/journal/realized_pnl only "
            "from confirmed execution evidence and is delta-idempotent. "
            "It also projects partial/fill/cancel evidence onto matching order-"
            "proposal rungs and repairs terminal-ledger projection drift on later "
            "non-dry runs. Toss loss-cut support requires either an enabled fill "
            "poller cadence or a targeted non-dry reconcile after execution. "
            "ROB-568: Surfaces US FX PnL split (security_pnl_krw, fx_pnl_krw) "
            "for overseas equity fills with fx_rate_source/fx_pnl_accuracy labels. "
            "dry_run=True by default."
        ),
    )(toss_reconcile_orders)
