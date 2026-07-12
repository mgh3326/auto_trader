"""Order execution orchestrator.

Thin coordinator: validate -> execute -> record.
Business logic lives in order_validation and order_journal.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal
from typing import cast as typing_cast

import httpx

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.core.exceptions import describe_exception
from app.core.timezone import KST, now_kst
from app.mcp_server.caller_identity import get_caller_source
from app.mcp_server.tick_size import adjust_tick_size_kr, get_tick_size_kr
from app.mcp_server.tooling import order_approval
from app.mcp_server.tooling import order_validation as ov
from app.mcp_server.tooling.order_journal import (
    _append_journal_warning,
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _order_session_factory,
    _save_order_fill,
    _validate_buy_journal_requirements,
)
from app.mcp_server.tooling.order_validation import (
    DefensiveTrimContext,
    ScalpingExitContext,
    _check_balance_and_warn,
    _get_balance_for_order,
    _get_current_price_for_order,
    _get_holdings_for_order,
    _preview_order,
    _record_order_history,
    _resolve_buy_quantity,
    _resolve_scalping_exit_context,
    _validate_defensive_trim_preconditions,
    _validate_sell_side,
    evaluate_sector_concentration,
)
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.services.brokers.kis import KISClient
from app.services.crypto_trade_cooldown_service import CryptoTradeCooldownService
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol


def _coerce_report_item_uuid(value: str | None) -> uuid.UUID | None:
    """ROB-473 — parse a report_item_uuid string fail-open.

    Audit metadata only — a malformed value must never block the order, so a
    bad string resolves to None (no linkage) rather than raising.
    """
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


# Phase 2 strategy constants
CRYPTO_STOP_LOSS_PCT = 0.045
_MOCK_CRYPTO_ERROR = "crypto has no mock venue"

# Crypto trade cooldown service singleton
_order_cooldown_service: CryptoTradeCooldownService | None = None


def _create_kis_client(*, is_mock: bool) -> KISClient:
    if is_mock:
        return KISClient(is_mock=True)
    return KISClient()


async def _call_kis(method: Any, *args: Any, is_mock: bool, **kwargs: Any) -> Any:
    kwargs.pop("is_mock", None)
    if is_mock:
        return await method(*args, **kwargs, is_mock=True)
    return await method(*args, **kwargs)


def _get_crypto_trade_cooldown_service() -> CryptoTradeCooldownService:
    """Get or create the crypto trade cooldown service."""
    global _order_cooldown_service
    if _order_cooldown_service is None:
        _order_cooldown_service = CryptoTradeCooldownService()
    return _order_cooldown_service


def _calculate_date_range(days: int) -> tuple[str, str]:
    """Calculate date range for order lookup."""
    today = datetime.datetime.now()
    start_date = (today - datetime.timedelta(days=days)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")
    return start_date, end_date


def _normalize_market_type_to_external(market_type: str) -> str:
    """Convert internal market_type to external contract values."""
    mapping = {
        "equity_kr": "kr",
        "equity_us": "us",
        "crypto": "crypto",
    }
    return mapping.get(market_type, market_type)


# ---------------------------------------------------------------------------
# Order execution (broker dispatch)
# ---------------------------------------------------------------------------


async def _execute_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    market_type: str,
    is_mock: bool = False,
    identifier: str | None = None,
) -> dict[str, Any]:
    if market_type == "crypto":
        if is_mock:
            raise ValueError(_MOCK_CRYPTO_ERROR)
        return await _execute_crypto_order(
            symbol, side, order_type, quantity, price, identifier=identifier
        )
    if market_type == "equity_kr":
        return await _execute_kr_order(
            symbol, side, order_type, quantity, price, is_mock=is_mock
        )
    return await _execute_us_order(symbol, side, quantity, price, is_mock=is_mock)


async def _execute_crypto_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    identifier: str | None = None,
) -> dict[str, Any]:
    if side == "buy":
        if order_type == "market":
            price_str = f"{price:.0f}" if price else "0"
            return await upbit_service.place_market_buy_order(
                symbol, price_str, identifier=identifier
            )
        volume_str = f"{quantity:.8f}"
        adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
        return await upbit_service.place_buy_order(
            symbol, adjusted_price, volume_str, "limit", identifier=identifier
        )

    holdings = await _get_holdings_for_order(symbol, "crypto")
    if not holdings:
        raise ValueError("No holdings found")

    volume = holdings["quantity"] if quantity is None else quantity
    volume_str = f"{volume:.8f}"
    if order_type == "market":
        return await upbit_service.place_market_sell_order(
            symbol, volume_str, identifier=identifier
        )

    adjusted_price = upbit_service.adjust_price_to_upbit_unit(price)
    return await upbit_service.place_sell_order(
        symbol, volume_str, f"{adjusted_price}", identifier=identifier
    )


async def _execute_kr_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    is_mock: bool = False,
) -> dict[str, Any]:
    kis = _create_kis_client(is_mock=is_mock)
    stock_code = symbol
    order_quantity = int(quantity) if quantity else 0
    order_price = int(price) if price else 0

    original_price = order_price if order_price else None
    if order_type == "limit" and order_price > 0:
        tick_size = get_tick_size_kr(float(order_price))
        order_price = adjust_tick_size_kr(float(order_price), side)

        if original_price is not None and order_price != original_price:
            logger.info(
                "KR limit order tick adjusted: symbol=%s side=%s original_price=%s tick_size=%s adjusted_price=%s",
                symbol,
                side,
                original_price,
                tick_size,
                order_price,
            )
        else:
            logger.debug(
                "KR limit order tick valid: symbol=%s side=%s price=%s tick_size=%s tick_adjusted=false",
                symbol,
                side,
                original_price,
                tick_size,
            )

    if side == "buy":
        result = await _call_kis(
            kis.order_korea_stock,
            stock_code=stock_code,
            order_type="buy",
            quantity=order_quantity,
            price=order_price,
            is_mock=is_mock,
        )
    else:
        result = await _call_kis(
            kis.order_korea_stock,
            stock_code=stock_code,
            order_type="sell",
            quantity=order_quantity,
            price=order_price,
            is_mock=is_mock,
        )

    if original_price is not None and order_price != original_price:
        result["original_price"] = original_price
        result["adjusted_price"] = order_price
        result["tick_adjusted"] = True
    return result


async def _execute_us_order(
    symbol: str,
    side: str,
    quantity: float | None,
    price: float | None,
    is_mock: bool = False,
) -> dict[str, Any]:
    kis = _create_kis_client(is_mock=is_mock)
    exchange_code = await get_us_exchange_by_symbol(symbol)

    if side == "buy":
        return await _call_kis(
            kis.buy_overseas_stock,
            symbol=symbol,
            exchange_code=exchange_code,
            quantity=int(quantity) if quantity else 0,
            price=price if price else 0.0,
            is_mock=is_mock,
        )
    return await _call_kis(
        kis.sell_overseas_stock,
        symbol=symbol,
        exchange_code=exchange_code,
        quantity=int(quantity) if quantity else 0,
        price=price if price else 0.0,
        is_mock=is_mock,
    )


# ---------------------------------------------------------------------------
# _place_order_impl sub-steps
# ---------------------------------------------------------------------------


def _validate_inputs(
    symbol: str,
    side: str,
    order_type: str,
    price: float | None,
    amount: float | None,
    quantity: float | None,
) -> tuple[str, str, str]:
    """Validate and normalize basic inputs. Returns (symbol, side, order_type)."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    side_lower = side.lower().strip()
    if side_lower not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")

    order_type_lower = order_type.lower().strip()
    if order_type_lower not in ("limit", "market"):
        raise ValueError("order_type must be 'limit' or 'market'")

    if order_type_lower == "limit" and price is None:
        raise ValueError("price is required for limit orders")

    if amount is not None and quantity is not None:
        raise ValueError(
            "amount and quantity cannot both be specified. "
            "Use amount for notional-based buying or quantity for unit-based buying."
        )

    if amount is not None and side_lower != "buy":
        raise ValueError(
            "amount can only be used for buy orders. Use quantity for sell orders."
        )

    return symbol, side_lower, order_type_lower


async def _fetch_current_price(
    normalized_symbol: str,
    market_type: str,
    order_type: str,
    price: float | None,
) -> float:
    """Fetch current price, falling back to limit price when available."""
    try:
        current_price = await _get_current_price_for_order(
            normalized_symbol, market_type
        )
    except Exception:
        if order_type == "limit" and price is not None:
            current_price = float(price)
        else:
            raise

    if current_price is None:
        if order_type == "limit" and price is not None:
            current_price = float(price)
        else:
            raise ValueError(f"Failed to get current price for {normalized_symbol}")

    return current_price


async def _build_preview(
    *,
    normalized_symbol: str,
    side: str,
    order_type: str,
    order_quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
    defensive_trim_ctx: DefensiveTrimContext | None,
    is_mock: bool = False,
    scalping_exit_ctx: ScalpingExitContext | None = None,
    loss_cut_ctx: ov.LossCutContext | None = None,
) -> dict[str, Any]:
    """Run preview and enrich result with defaults."""
    dry_run_result = await _preview_order(
        symbol=normalized_symbol,
        side=side,
        order_type=order_type,
        quantity=order_quantity,
        price=price,
        current_price=current_price,
        market_type=market_type,
        defensive_trim_ctx=defensive_trim_ctx,
        is_mock=is_mock,
        scalping_exit_ctx=scalping_exit_ctx,
        loss_cut_ctx=loss_cut_ctx,
    )
    if not isinstance(dry_run_result, dict):
        raise ValueError("Order preview returned invalid result")
    if dry_run_result.get("error"):
        raise ValueError(str(dry_run_result["error"]))

    if (
        side == "sell"
        and order_quantity is not None
        and dry_run_result.get("quantity") is None
    ):
        dry_run_result["quantity"] = order_quantity

    dry_run_result.setdefault("symbol", normalized_symbol)
    dry_run_result.setdefault("side", side)
    dry_run_result.setdefault("order_type", order_type)
    if dry_run_result.get("price") is None:
        dry_run_result["price"] = current_price if order_type == "market" else price
    return dry_run_result


async def _handle_buy_journal(
    *,
    normalized_symbol: str,
    market_type: str,
    dry_run_result: dict[str, Any],
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
) -> tuple[bool, int | None, str | None, str | None]:
    """Create trade journal for buy orders.

    Returns:
        (journal_created, journal_id, journal_status, journal_warning)
    """
    try:
        journal_result = await _create_trade_journal_for_buy(
            symbol=normalized_symbol,
            market_type=market_type,
            preview=dry_run_result,
            thesis=typing_cast(str, thesis),
            strategy=typing_cast(str, strategy),
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
        )
        return (
            journal_result["journal_created"],
            journal_result["journal_id"],
            journal_result["journal_status"],
            None,
        )
    except Exception as journal_exc:
        warning = f"trade journal creation failed after order execution: {journal_exc}"
        logger.warning(warning)
        return False, None, None, warning


async def _handle_sell_journal(
    *,
    normalized_symbol: str,
    dry_run_result: dict[str, Any],
    order_quantity: float | None,
    current_price: float,
    exit_reason: str | None,
    reason: str,
    journal_warning: str | None,
    defensive_trim_ctx: DefensiveTrimContext | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Close journals for sell orders.

    Returns:
        (journal_close_result, updated_journal_warning)
    """
    try:
        preview_qty = _to_float(dry_run_result.get("quantity"), default=0.0)
        preview_price = _to_float(dry_run_result.get("price"), default=0.0)
        resolved_sell_qty = (
            preview_qty if preview_qty > 0 else _to_float(order_quantity, default=0.0)
        )
        resolved_sell_price = (
            preview_price
            if preview_price > 0
            else _to_float(current_price, default=0.0)
        )
        journal_close_result = await _close_journals_on_sell(
            symbol=normalized_symbol,
            sell_quantity=resolved_sell_qty,
            sell_price=resolved_sell_price,
            exit_reason=exit_reason or reason,
            defensive_trim_ctx=defensive_trim_ctx,
        )
        return journal_close_result, journal_warning
    except Exception as journal_exc:
        updated_warning = _append_journal_warning(
            journal_warning, f"journal close failed after sell: {journal_exc}"
        )
        logger.warning("Failed to close journals on sell: %s", journal_exc)
        return None, updated_warning


async def _save_and_link_fill(
    *,
    normalized_symbol: str,
    market_type: str,
    side: str,
    execution_result: dict[str, Any],
    dry_run_result: dict[str, Any],
    journal_created: bool,
) -> tuple[bool, str | None, str | None]:
    """Save order fill to DB and link to journal.

    Returns:
        (fill_recorded, journal_status, journal_warning)
    """
    order_id = execution_result.get("uuid") or execution_result.get("odno")
    price_val = _to_float(dry_run_result.get("price"), default=0.0)
    qty_val = _to_float(dry_run_result.get("quantity"), default=0.0)
    amt_val = _to_float(dry_run_result.get("estimated_value"), default=0.0)
    fee_val = _to_float(dry_run_result.get("fee"), default=0.0)
    currency = "KRW" if market_type != "equity_us" else "USD"
    account_name = "upbit" if market_type == "crypto" else "kis"

    try:
        trade_id = await _save_order_fill(
            symbol=normalized_symbol,
            instrument_type=market_type,
            side=side,
            price=price_val,
            quantity=qty_val,
            total_amount=amt_val,
            fee=fee_val,
            currency=currency,
            account=account_name,
            order_id=str(order_id) if order_id else None,
        )

        if trade_id:
            await _link_journal_to_fill(normalized_symbol, trade_id)
            if journal_created:
                return True, "active", None
            return True, None, None
        elif journal_created:
            return (
                False,
                None,
                "trade journal created but fill was not recorded; journal remains draft",
            )
        return False, None, None
    except Exception as db_exc:
        logger.warning("Failed to record fill to DB: %s", db_exc)
        return False, None, None


async def _record_fill_and_journals(
    *,
    side: str,
    normalized_symbol: str,
    market_type: str,
    execution_result: dict[str, Any],
    dry_run_result: dict[str, Any],
    order_quantity: float | None,
    current_price: float,
    avg_price: float,
    reason: str,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    defensive_trim_ctx: DefensiveTrimContext | None,
) -> dict[str, Any]:
    """Save fill to DB, manage journals (create for buy, close for sell)."""
    journal_created = False
    journal_id: int | None = None
    journal_status: str | None = None
    journal_warning: str | None = None

    if side == "buy":
        (
            journal_created,
            journal_id,
            journal_status,
            journal_warning,
        ) = await _handle_buy_journal(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            dry_run_result=dry_run_result,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
        )

    # Record stop-loss cooldown for crypto sells below threshold
    if (
        market_type == "crypto"
        and side == "sell"
        and avg_price > 0
        and current_price <= avg_price * (1 - CRYPTO_STOP_LOSS_PCT)
    ):
        try:
            cooldown_service = _get_crypto_trade_cooldown_service()
            await cooldown_service.record_stop_loss(normalized_symbol)
        except Exception as cooldown_exc:
            logger.warning("Failed to record stop-loss cooldown: %s", cooldown_exc)

    # Save fill to DB
    fill_recorded, fill_journal_status, fill_warning = await _save_and_link_fill(
        normalized_symbol=normalized_symbol,
        market_type=market_type,
        side=side,
        execution_result=execution_result,
        dry_run_result=dry_run_result,
        journal_created=journal_created,
    )
    if fill_journal_status:
        journal_status = fill_journal_status
    if fill_warning:
        journal_warning = _append_journal_warning(journal_warning, fill_warning)

    # Close journals for sell orders
    journal_close_result: dict[str, Any] | None = None
    if side == "sell":
        journal_close_result, journal_warning = await _handle_sell_journal(
            normalized_symbol=normalized_symbol,
            dry_run_result=dry_run_result,
            order_quantity=order_quantity,
            current_price=current_price,
            exit_reason=exit_reason,
            reason=reason,
            journal_warning=journal_warning,
            defensive_trim_ctx=defensive_trim_ctx,
        )

    result: dict[str, Any] = {
        "fill_recorded": fill_recorded,
        "journal_created": journal_created,
        "journal_id": journal_id,
        "journal_status": journal_status,
    }
    if journal_close_result:
        result["journals_closed"] = journal_close_result["journals_closed"]
        result["journals_kept"] = journal_close_result["journals_kept"]
        result["closed_journal_ids"] = journal_close_result["closed_ids"]
    if journal_warning:
        result["journal_warning"] = journal_warning
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _build_dry_run_response(
    dry_run_result: dict[str, Any],
    balance_warning: str | None,
) -> dict[str, Any]:
    """Build the dry-run preview response."""
    result = {
        "success": True,
        "dry_run": True,
        **dry_run_result,
        "message": "Order preview (dry_run=True)",
    }
    if balance_warning:
        result["warning"] = balance_warning
    return result


def _build_dry_run_blocked_response(
    dry_run_result: dict[str, Any],
    balance_error: dict[str, Any],
) -> dict[str, Any]:
    """ROB-625 — dry_run 잔액부족 차단 응답.

    live와 동일하게 success=False로 차단하되, 프리뷰 본문(estimated_value/fee 등)을
    유지해 운영자가 입금액을 산정할 수 있게 한다. ``balance_error`` 가 success/error/
    insufficient_balance(_detail) 차단 플래그를 덮어쓰도록 뒤에 병합한다.
    """
    return {
        **dry_run_result,
        **balance_error,
        "dry_run": True,
    }


def _duplicate_order_intent_message(account_scope: str) -> str:
    if account_scope == "kis_mock":
        return (
            "이 미러 아이템은 이미 KIS mock 미러 전송 intent가 있어 "
            "중복 전송을 차단했습니다 "
            "(duplicate mock mirror intent; duplicate order intent). "
            "kis_mock_order_ledger의 report_item_uuid/mirror_cohort 상태를 "
            "확인하세요."
        )
    return (
        "동일 주문이 오늘 이미 전송되어 중복 전송을 차단했습니다 "
        "(duplicate order intent). 재전송하지 말고 reconcile로 접수 여부를 "
        "확인하세요. 익일 재배치는 허용됩니다."
    )


async def _execute_and_record(
    *,
    normalized_symbol: str,
    side: str,
    order_type: str,
    order_quantity: float | None,
    price: float | None,
    market_type: str,
    current_price: float,
    avg_price: float,
    dry_run_result: dict[str, Any],
    order_amount: float,
    reason: str,
    exit_reason: str | None,
    exit_intent: str | None = None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    defensive_trim_ctx: DefensiveTrimContext | None,
    order_error_fn: Any,
    is_mock: bool = False,
    correlation_id: str | None = None,
    report_item_uuid: uuid.UUID | None = None,
    approval_hash_digest: str | None = None,
    idempotency_key: str | None = None,
    mirror_cohort: str | None = None,
    mirror_source_bucket: str | None = None,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Execute a live order, record history, fills, and journals."""
    # ROB-102: capture pre-order KIS mock holdings as the reconciler baseline.
    # Best-effort — failure leaves the column NULL and the reconciler will
    # surface the row as `baseline_missing → anomaly` for operator review.
    kis_mock_baseline_qty: Decimal | None = None
    if is_mock:
        from app.mcp_server.tooling.kis_mock_ledger import (
            _fetch_kis_mock_baseline_qty,
        )

        kis_mock_baseline_qty = await _fetch_kis_mock_baseline_qty(
            normalized_symbol=normalized_symbol, market_type=market_type
        )

    # ROB-653 P6-B — KIS has no broker idempotency key; reserve a local intent
    # row before the send. A same-key send the same trading day fails closed.
    # Crypto/Upbit is excluded (it uses the broker-side content identifier).
    intent_account_scope: str | None = None
    intent_key: str | None = None
    intent_reserved = False
    if (
        not is_mock
        and idempotency_key is not None
        and market_type
        in (
            "equity_kr",
            "equity_us",
        )
    ):
        intent_account_scope = "kis_live"
        intent_key = idempotency_key
    elif (
        is_mock
        and mirror_cohort == "mock_counterfactual"
        and correlation_id is not None
        and market_type in ("equity_kr", "equity_us")
    ):
        intent_account_scope = "kis_mock"
        intent_key = correlation_id

    if intent_account_scope is not None and intent_key is not None:
        async with _order_session_factory()() as intent_db:
            try:
                await OrderSendIntentService(intent_db).reserve(
                    account_scope=intent_account_scope,
                    idempotency_key=intent_key,
                    symbol=normalized_symbol,
                    side=side,
                )
                intent_reserved = True
            except DuplicateOrderIntent:
                logger.warning(
                    "KIS duplicate order intent blocked: scope=%s symbol=%s side=%s key=%s",
                    intent_account_scope,
                    normalized_symbol,
                    side,
                    intent_key,
                )
                return order_error_fn(
                    _duplicate_order_intent_message(intent_account_scope)
                )

    async def _release_reserved_mock_mirror_intent_after_send_failure(
        exc: BaseException,
    ) -> bool:
        if (
            not intent_reserved
            or intent_account_scope != "kis_mock"
            or intent_key is None
            or mirror_cohort != "mock_counterfactual"
        ):
            return False

        try:
            async with _order_session_factory()() as release_db:
                deleted = await OrderSendIntentService(release_db).release(
                    account_scope=intent_account_scope,
                    idempotency_key=intent_key,
                )
        except Exception as release_exc:  # noqa: BLE001
            logger.warning(
                "KIS mock mirror intent release failed after send failure: "
                "symbol=%s side=%s key=%s send_error=%s release_error=%s",
                normalized_symbol,
                side,
                intent_key,
                describe_exception(exc),
                describe_exception(release_exc),
            )
            return False

        if deleted:
            logger.info(
                "KIS mock mirror intent released after send failure: "
                "symbol=%s side=%s key=%s send_error=%s",
                normalized_symbol,
                side,
                intent_key,
                describe_exception(exc),
            )
            return True
        return False

    # ROB-843 P1-1: final pre-send freshness re-check, invoked immediately
    # before the real broker POST (after baseline/preflight). KIS-mock-scalping
    # only — live callers pass no hook, so live behavior is unchanged. On a
    # freshness violation the POST is skipped entirely (zero broker calls) and a
    # structured pre_send_blocked result is returned.
    if pre_send_hook is not None:
        try:
            await pre_send_hook()
        except Exception as hook_exc:  # noqa: BLE001 — abort the send, never POST
            reason_codes = list(getattr(hook_exc, "reason_codes", ()) or ())
            logger.info(
                "pre-send freshness block symbol=%s side=%s reasons=%s",
                normalized_symbol,
                side,
                reason_codes,
            )
            await _release_reserved_mock_mirror_intent_after_send_failure(hook_exc)
            return {
                "success": False,
                "pre_send_blocked": True,
                "reason_codes": reason_codes,
                "detail": f"{type(hook_exc).__name__}: {hook_exc}"[:200],
                "account_mode": "kis_mock" if is_mock else market_type,
                "dry_run": False,
            }

    try:
        execution_result = await _execute_order(
            symbol=normalized_symbol,
            side=side,
            order_type=order_type,
            quantity=order_quantity,
            price=price,
            market_type=market_type,
            is_mock=is_mock,
            identifier=idempotency_key if market_type == "crypto" else None,
        )
    except httpx.RequestError as send_exc:
        retry_allowed = await _release_reserved_mock_mirror_intent_after_send_failure(
            send_exc
        )
        # ROB-645: the order POST itself timed out / failed with no broker response.
        # Outcome is UNKNOWN for live orders (may have been accepted) — never re-send live; reconcile.
        # ROB-750: mock mirror has no live broker risk, so its scoped intent is released for retry.
        logger.error(
            "execute_order 실패(outcome unknown): stage=execute_order, "
            "market_type=%s, symbol=%s, side=%s, error=%s",
            market_type,
            normalized_symbol,
            side,
            describe_exception(send_exc),
        )
        raise OrderSendOutcomeUnknown(
            send_exc,
            retry_allowed=retry_allowed,
            retry_hint=(
                "KIS mock mirror pre-send intent를 해제했습니다. "
                "동일 미러 아이템은 재시도할 수 있습니다."
            )
            if retry_allowed
            else None,
        ) from send_exc
    except Exception as exec_exc:
        await _release_reserved_mock_mirror_intent_after_send_failure(exec_exc)
        logger.error(
            "execute_order 실패: stage=execute_order, market_type=%s, "
            "symbol=%s, side=%s, error=%s",
            market_type,
            normalized_symbol,
            side,
            exec_exc,
        )
        raise

    await _record_order_history(
        symbol=normalized_symbol,
        side=side,
        order_type=order_type,
        quantity=order_quantity,
        price=price,
        amount=order_amount,
        reason=reason,
        dry_run=False,
        defensive_trim=defensive_trim_ctx is not None,
        approval_issue_id=(
            defensive_trim_ctx.approval_issue_id if defensive_trim_ctx else None
        ),
        requester_agent_id=(
            defensive_trim_ctx.requester_agent_id if defensive_trim_ctx else None
        ),
        caller_source=get_caller_source() if defensive_trim_ctx else None,
    )

    # KIS mock: write to dedicated ledger, skip live journal/fill paths entirely.
    if is_mock:
        from app.mcp_server.tooling.kis_mock_ledger import _record_kis_mock_order

        return await _record_kis_mock_order(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            side=side,
            order_type=order_type,
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            notes=notes,
            holdings_baseline_qty=kis_mock_baseline_qty,
            correlation_id=correlation_id,
            target_price=target_price,
            min_hold_days=min_hold_days,
            report_item_uuid=report_item_uuid,
            mirror_cohort=mirror_cohort,
            mirror_source_bucket=mirror_source_bucket,
        )

    # ROB-395: live KR orders record accepted-only to the live ledger; fills,
    # journals, and realized_pnl are applied later by kis_live_reconcile_orders
    # from order-id-keyed broker evidence. US/crypto live keep the legacy path.
    if not is_mock and market_type == "equity_kr":
        from app.mcp_server.tooling.kis_live_ledger import _record_kis_live_order

        return await _record_kis_live_order(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            side=side,
            order_type=order_type,
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=reason,
            exit_reason=exit_reason,
            exit_intent=exit_intent,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            report_item_uuid=report_item_uuid,
            approval_hash=approval_hash_digest,
            idempotency_key=idempotency_key,
        )

    # ROB-407: US/해외 live 주문도 accepted-only 기록; fill/journal/pnl은
    # live_reconcile_orders가 broker 체결 증거(해외 일별주문)로만 반영.
    if not is_mock and market_type == "equity_us":
        from app.mcp_server.tooling.live_order_ledger import _record_live_order

        exchange = execution_result.get("ovrs_excg_cd") or (
            execution_result.get("output") or {}
        ).get("OVRS_EXCG_CD")
        return await _record_live_order(
            broker="kis",
            account_scope="kis_live",
            market="us",
            normalized_symbol=normalized_symbol,
            exchange=str(exchange) if exchange else None,
            market_symbol=None,
            side=side,
            order_kind=order_type,
            currency="USD",
            order_no=execution_result.get("odno") or execution_result.get("ord_no"),
            order_time=execution_result.get("ord_tmd"),
            rt_cd=str(execution_result.get("rt_cd", "")) or None,
            response_message=execution_result.get("msg")
            or execution_result.get("msg1"),
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=reason,
            exit_reason=exit_reason,
            exit_intent=exit_intent,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            dt_approval_issue_id=(
                defensive_trim_ctx.approval_issue_id if defensive_trim_ctx else None
            ),
            dt_requester_agent_id=(
                defensive_trim_ctx.requester_agent_id if defensive_trim_ctx else None
            ),
            dt_caller_source=get_caller_source() if defensive_trim_ctx else None,
            report_item_uuid=report_item_uuid,
            approval_hash=approval_hash_digest,
            idempotency_key=idempotency_key,
        )

    # ROB-407: crypto live 주문. 지정가 pending은 accepted-only(reconcile 위임),
    # 시장가는 전송 직후 inline evidence 확인으로 체결 반영.
    if not is_mock and market_type == "crypto":
        from app.mcp_server.tooling.live_order_ledger import _record_live_order

        # ROB-407: live crypto fills/journals are evidence-gated now, but the
        # stop-loss re-entry cooldown is a send-time intent guard that used to
        # live in _record_fill_and_journals. Preserve it here so a stop-loss
        # crypto sell still blocks immediate re-entry (decision: record at send).
        if (
            side == "sell"
            and avg_price > 0
            and current_price <= avg_price * (1 - CRYPTO_STOP_LOSS_PCT)
        ):
            try:
                cooldown_service = _get_crypto_trade_cooldown_service()
                await cooldown_service.record_stop_loss(normalized_symbol)
            except Exception as cooldown_exc:
                logger.warning("Failed to record stop-loss cooldown: %s", cooldown_exc)

        is_market = (order_type or "").lower() == "market" or price is None
        market_symbol = execution_result.get("market") or dry_run_result.get("market")
        return await _record_live_order(
            broker="upbit",
            account_scope="upbit_live",
            market="crypto",
            normalized_symbol=normalized_symbol,
            exchange=None,
            market_symbol=str(market_symbol) if market_symbol else None,
            side=side,
            order_kind="market" if is_market else "limit",
            currency="KRW",
            order_no=execution_result.get("uuid"),
            order_time=execution_result.get("created_at"),
            rt_cd="0" if execution_result.get("uuid") else "1",
            response_message=execution_result.get("error") or None,
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=reason,
            exit_reason=exit_reason,
            exit_intent=exit_intent,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            inline_confirm=is_market,
            dt_approval_issue_id=(
                defensive_trim_ctx.approval_issue_id if defensive_trim_ctx else None
            ),
            dt_requester_agent_id=(
                defensive_trim_ctx.requester_agent_id if defensive_trim_ctx else None
            ),
            dt_caller_source=get_caller_source() if defensive_trim_ctx else None,
            report_item_uuid=report_item_uuid,
            approval_hash=approval_hash_digest,
            idempotency_key=idempotency_key,
        )

    # Record phase: fills + journals
    record_result = await _record_fill_and_journals(
        side=side,
        normalized_symbol=normalized_symbol,
        market_type=market_type,
        execution_result=execution_result,
        dry_run_result=dry_run_result,
        order_quantity=order_quantity,
        current_price=current_price,
        avg_price=avg_price,
        reason=reason,
        exit_reason=exit_reason,
        thesis=thesis,
        strategy=strategy,
        target_price=target_price,
        stop_loss=stop_loss,
        min_hold_days=min_hold_days,
        notes=notes,
        indicators_snapshot=indicators_snapshot,
        defensive_trim_ctx=defensive_trim_ctx,
    )

    return {
        "success": True,
        "dry_run": False,
        "preview": dry_run_result,
        "execution": execution_result,
        **record_result,
        "message": "Order placed and fill recorded successfully"
        if record_result["fill_recorded"]
        else "Order placed successfully",
    }


def _build_order_error(
    message: str,
    source: str,
    symbol: str,
    market_type: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "error": message,
        "source": source,
        "symbol": symbol,
        "instrument_type": market_type,
    }


class OrderSendOutcomeUnknown(Exception):
    """ROB-645 — an order POST failed with no definitive broker response
    (timeout/network error), so we cannot tell whether the order was accepted.

    Raised ONLY around the actual send (``_execute_order``); a RequestError during
    a pre-send read (price/balance/preview) means the order was never submitted and
    must NOT be treated as outcome-unknown. Wraps the original transport exception.
    """

    def __init__(
        self,
        original: BaseException,
        *,
        retry_allowed: bool = False,
        retry_hint: str | None = None,
    ) -> None:
        super().__init__(describe_exception(original))
        self.original = original
        self.retry_allowed = retry_allowed
        self.retry_hint = retry_hint


# ROB-645: reconcile tool to consult when an order's send outcome is unknown.
# Only live paths have a reconcile tool (KR → kis_live_reconcile_orders,
# US/crypto → live_reconcile_orders). Mock has none, so we never name a phantom.
_LIVE_RECONCILE_TOOL_BY_MARKET = {
    "equity_kr": "kis_live_reconcile_orders",
    "equity_us": "live_reconcile_orders",
    "crypto": "live_reconcile_orders",
}


def _reconcile_tool_for(*, market_type: str, is_mock: bool) -> str | None:
    """Return the reconcile MCP tool that confirms whether an order was accepted."""
    if is_mock:
        return None
    return _LIVE_RECONCILE_TOOL_BY_MARKET.get(market_type)


def _augment_error_for_unknown_outcome(
    base_error: dict[str, Any],
    exc: BaseException,
    *,
    market_type: str,
    is_mock: bool,
) -> dict[str, Any]:
    """ROB-645 — enrich an order error when the send outcome is UNKNOWN.

    A timed-out / network-failed order POST may have reached the broker even
    though no response came back. We no longer retry such sends (retry =
    double-submit), so surface an explicit, non-blank error that tells the caller
    to reconcile — never to re-send. Anything that is not a send-time
    outcome-unknown failure (a definitive rejection, or a pre-send read timeout)
    is left unchanged: no order was created.
    """
    if not isinstance(exc, OrderSendOutcomeUnknown):
        return base_error

    reconcile_tool = _reconcile_tool_for(market_type=market_type, is_mock=is_mock)
    reason = describe_exception(exc.original)
    if reconcile_tool:
        confirm_hint = f"{reconcile_tool} 도구로 실제 접수 여부를 확인하세요."
    else:
        confirm_hint = "브로커에서 실제 주문 상태를 확인하세요."

    enriched = dict(base_error)
    enriched["outcome_unknown"] = True
    enriched["reconcile_tool"] = reconcile_tool
    if getattr(exc, "retry_allowed", False):
        enriched["retry_allowed"] = True
        retry_hint = getattr(exc, "retry_hint", None) or (
            "전송 intent가 해제되어 재시도할 수 있습니다."
        )
        enriched["error"] = f"주문 전송 실패: {reason}. {retry_hint}"
        return enriched
    enriched["error"] = (
        f"주문 접수 여부 불확실 (전송 실패: {reason}). 재전송하지 말고 {confirm_hint}"
    )
    return enriched


async def _place_order_impl(
    symbol: str,
    side: Literal["buy", "sell"],
    market: str | None = None,
    order_type: Literal["limit", "market"] = "limit",
    quantity: float | None = None,
    price: float | None = None,
    amount: float | None = None,
    dry_run: bool = True,
    reason: str = "",
    exit_reason: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    defensive_trim: bool = False,
    approval_issue_id: str | None = None,
    exit_intent: str | None = None,
    retrospective_id: int | None = None,
    is_mock: bool = False,
    scalping_exit: bool = False,
    scalping_strategy_id: str | None = None,
    scalping_exit_reason: str | None = None,
    correlation_id: str | None = None,
    report_item_uuid: str | None = None,
    approval_hash: str | None = None,
    rung: str | int | None = None,
    mirror_cohort: str | None = None,
    mirror_source_bucket: str | None = None,
    client_order_id: str | None = None,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    symbol, side_lower, order_type_lower = _validate_inputs(
        symbol,
        side,
        order_type,
        price,
        amount,
        quantity,
    )

    market_type, normalized_symbol = _resolve_market_type(symbol, market)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "kis"}
    source = source_map[market_type]

    def _order_error(message: str) -> dict[str, Any]:
        return _build_order_error(message, source, normalized_symbol, market_type)

    if client_order_id is not None and (
        not isinstance(client_order_id, str)
        or not client_order_id.strip()
        or len(client_order_id) > 40
    ):
        return _order_error(
            "client_order_id must be non-blank and at most 40 characters"
        )

    # Validate buy order journal requirements before any external API calls.
    # Skipped for KIS mock: mock orders write to kis_mock_order_ledger and
    # never create a TradeJournal, so thesis/strategy are optional.
    if not is_mock:
        try:
            _validate_buy_journal_requirements(
                side=side_lower,
                dry_run=dry_run,
                thesis=thesis,
                strategy=strategy,
            )
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "source": "upbit" if market_type == "crypto" else "kis",
                "symbol": normalized_symbol,
                "instrument_type": market_type,
            }

    if market_type == "crypto" and is_mock:
        return _order_error(_MOCK_CRYPTO_ERROR)

    if exit_intent is not None and exit_intent != "loss_cut":
        return _order_error(f"unknown exit_intent {exit_intent!r} (only 'loss_cut')")
    if exit_intent == "loss_cut" and defensive_trim:
        return _order_error("loss_cut and defensive_trim are mutually exclusive")

    loss_cut_ctx: ov.LossCutContext | None = None
    if exit_intent == "loss_cut":
        loss_cut_ctx, loss_cut_errors = await ov._validate_loss_cut_preconditions(
            exit_intent=exit_intent,
            retrospective_id=retrospective_id,
            exit_reason=exit_reason,
            approval_issue_id=approval_issue_id,
            side=side_lower,
            order_type=order_type_lower,
            is_mock=is_mock,
            symbol=normalized_symbol,
        )
        if loss_cut_errors:
            return {
                "success": False,
                "error": "loss_cut_preconditions_failed",
                "violations": loss_cut_errors,
                "source": source,
                "symbol": normalized_symbol,
                "instrument_type": market_type,
            }

    try:
        defensive_trim_ctx = await _validate_defensive_trim_preconditions(
            defensive_trim=defensive_trim,
            approval_issue_id=approval_issue_id,
            side=side_lower,
            order_type=order_type_lower,
        )
    except ValueError as e:
        return _order_error(str(e))

    try:
        scalping_exit_ctx = _resolve_scalping_exit_context(
            scalping_exit=scalping_exit,
            strategy_id=scalping_strategy_id,
            reason=scalping_exit_reason,
            side=side_lower,
            order_type=order_type_lower,
            is_mock=is_mock,
        )
    except ValueError as e:
        return _order_error(str(e))

    # Check stop-loss cooldown for crypto buys
    if side_lower == "buy" and market_type == "crypto":
        cooldown_service = _get_crypto_trade_cooldown_service()
        if await cooldown_service.is_in_cooldown(normalized_symbol):
            return _order_error(
                "Symbol is in stop-loss cooldown until re-entry window expires"
            )

    try:
        current_price = await _fetch_current_price(
            normalized_symbol,
            market_type,
            order_type_lower,
            price,
        )

        # Resolve amount -> quantity for buy orders
        order_quantity, price = _resolve_buy_quantity(
            amount=amount,
            quantity=quantity,
            order_type=order_type_lower,
            market_type=market_type,
            price=price,
            current_price=current_price,
        )

        if order_type_lower == "limit" and order_quantity is None:
            raise ValueError("quantity is required for limit orders")

        # Validate sell-side: holdings, locked, price constraints
        avg_price = 0.0
        if side_lower == "sell":
            order_quantity, avg_price, sell_error = await _validate_sell_side(
                symbol=symbol,
                normalized_symbol=normalized_symbol,
                market_type=market_type,
                quantity=quantity,
                order_type=order_type_lower,
                price=price,
                current_price=current_price,
                order_error_fn=_order_error,
                defensive_trim_ctx=defensive_trim_ctx,
                is_mock=is_mock,
                dry_run=dry_run,
                scalping_exit_ctx=scalping_exit_ctx,
                loss_cut_ctx=loss_cut_ctx,
            )
            if sell_error is not None:
                return sell_error

        # Build preview
        try:
            dry_run_result = await _build_preview(
                normalized_symbol=normalized_symbol,
                side=side_lower,
                order_type=order_type_lower,
                order_quantity=order_quantity,
                price=price,
                current_price=current_price,
                market_type=market_type,
                defensive_trim_ctx=defensive_trim_ctx,
                is_mock=is_mock,
                scalping_exit_ctx=scalping_exit_ctx,
                loss_cut_ctx=loss_cut_ctx,
            )
        except ValueError as preview_exc:
            preview_error = str(preview_exc) or preview_exc.__class__.__name__
            return _order_error(f"Order preview failed: {preview_error}")

        order_amount = _to_float(dry_run_result.get("estimated_value"), default=0.0)

        # Balance pre-check for buy orders
        balance_warning: str | None = None
        balance_error: dict[str, Any] | None = None
        if side_lower == "buy":
            balance_warning, balance_error = await _check_balance_and_warn(
                market_type=market_type,
                normalized_symbol=normalized_symbol,
                side=side_lower,
                order_amount=order_amount,
                dry_run=dry_run,
                order_error_fn=_order_error,
                is_mock=is_mock,
            )

        if balance_error is not None:
            # ROB-625 — dry_run도 잔액부족을 차단하되 프리뷰 본문은 유지한다.
            # (dry_run에서 balance_error가 set되는 경우는 잔액부족 분기뿐: mock 미지원/
            #  조회불가 등은 (warning, None)으로 반환되어 balance_warning 경로로 빠진다.)
            if dry_run:
                return _build_dry_run_blocked_response(dry_run_result, balance_error)
            return balance_error

        if side_lower == "buy":
            mkt_mapped = "kr"
            if market_type == "equity_us":
                mkt_mapped = "us"
            elif market_type == "crypto":
                mkt_mapped = "crypto"

            cur_mapped = "KRW" if market_type != "equity_us" else "USD"

            sector_conc = await evaluate_sector_concentration(
                symbol=normalized_symbol,
                market=mkt_mapped,
                order_estimated_value=dry_run_result.get("estimated_value"),
                order_currency=cur_mapped,
                # ROB-646 Finding 1: whole-portfolio scope (no account/market
                # filter) so KIS and Toss buy paths measure the same denominator.
                account_ctx={"is_mock": is_mock},
            )
            dry_run_result["sector_concentration"] = sector_conc
            if sector_conc.get("verdict") == "over" and not balance_warning:
                balance_warning = sector_conc.get("warning")

        # ROB-653 P6-B — bind previewed↔placed content with an approval hash.
        # Canonical uses post-normalization wire values (tick-snap, amount→qty).
        canonical = order_approval.build_order_canonical_payload(
            market_type=market_type,
            symbol=normalized_symbol,
            side=side_lower,
            order_type=order_type_lower,
            quantity=None if order_quantity is None else str(order_quantity),
            price=None if price is None else str(price),
        )
        now = now_kst()
        salt_market = order_approval.salt_market_for(market_type)
        idempotency_key = client_order_id or order_approval.derive_client_order_id(
            canonical,
            market=salt_market,
            now=now,
            rung=rung,
        )

        # Dry-run exit — the preview emits the approval token operators pass back.
        if dry_run:
            preview_resp = _build_dry_run_response(dry_run_result, balance_warning)
            preview_resp["approval_hash"] = order_approval.encode_approval_token(
                canonical, now=now
            )
            preview_resp["approval_expires_at"] = (
                (now + timedelta(seconds=order_approval.APPROVAL_TTL_SECONDS))
                .astimezone(KST)
                .isoformat()
            )
            preview_resp["idempotency_key"] = idempotency_key
            return preview_resp

        # Live send — approval-hash gate (valid hash = confirm).
        mode = getattr(settings, "order_approval_hash_mode", "optional")

        # ROB-800 — loss_cut is fail-closed on the approval hash regardless of
        # ORDER_APPROVAL_HASH_MODE. The operator must preview the exact order
        # (dry_run=True) and pass the returned hash back: a missing hash is
        # rejected, and a present-but-invalid/expired hash is verified and
        # rejected even when mode="off" (which would otherwise skip verification).
        if loss_cut_ctx is not None:
            if approval_hash is None:
                err = _order_error(
                    "loss_cut live send requires approval_hash "
                    "(re-run dry_run=True and pass the returned approval_hash)"
                )
                err["error_code"] = "loss_cut_approval_hash_required"
                return err
            verdict = order_approval.verify_approval_token(
                approval_hash, canonical, now=now
            )
            if not verdict.ok:
                err = _order_error(verdict.message or "approval_hash invalid")
                err["error_code"] = verdict.error_code
                if verdict.diff is not None:
                    err["diff"] = verdict.diff
                return err
        elif mode != "off":
            if approval_hash is not None:
                verdict = order_approval.verify_approval_token(
                    approval_hash, canonical, now=now
                )
                if not verdict.ok:
                    err = _order_error(verdict.message or "approval_hash invalid")
                    err["error_code"] = verdict.error_code
                    if verdict.diff is not None:
                        err["diff"] = verdict.diff
                    return err
            elif mode == "required" and not is_mock:
                # ROB-659: required-mode fail-close is scoped to LIVE orders only.
                # Mock scalping / watch auto-execute / kis_mock callers can't mint
                # an approval_hash, so gating them would break internal automation
                # loops when the operator flips ORDER_APPROVAL_HASH_MODE=required.
                # The live ScreenerService REST path remains the rollout gate — see
                # docs/runbooks/order-approval-hash.md §6 (required-mode cutover).
                err = _order_error(
                    "approval_hash is required (ORDER_APPROVAL_HASH_MODE=required). "
                    "Re-run with dry_run=True and pass the returned approval_hash."
                )
                err["error_code"] = "approval_hash_required"
                return err
            elif mode == "warn":
                logger.warning(
                    "place_order without approval_hash (mode=warn) symbol=%s side=%s",
                    normalized_symbol,
                    side_lower,
                )

        approval_digest = (
            order_approval.derive_approval_digest(canonical)
            if (mode != "off" or loss_cut_ctx is not None)
            else None
        )

        # Real execution
        return await _execute_and_record(
            normalized_symbol=normalized_symbol,
            side=side_lower,
            order_type=order_type_lower,
            order_quantity=order_quantity,
            price=price,
            market_type=market_type,
            current_price=current_price,
            avg_price=avg_price,
            dry_run_result=dry_run_result,
            order_amount=order_amount,
            reason=reason,
            exit_reason=exit_reason,
            exit_intent=exit_intent,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            defensive_trim_ctx=defensive_trim_ctx,
            order_error_fn=_order_error,
            is_mock=is_mock,
            correlation_id=correlation_id,
            report_item_uuid=_coerce_report_item_uuid(report_item_uuid),
            approval_hash_digest=approval_digest,
            idempotency_key=idempotency_key,
            mirror_cohort=mirror_cohort,
            mirror_source_bucket=mirror_source_bucket,
            pre_send_hook=pre_send_hook,
        )
    except Exception as exc:
        logger.exception(
            "place_order execution failed (symbol=%s side=%s market=%s): %s",
            normalized_symbol,
            side_lower,
            market_type,
            exc,
        )
        await _record_order_history(
            symbol=normalized_symbol,
            side=side_lower,
            order_type=order_type_lower,
            quantity=quantity,
            price=price,
            amount=0,
            reason=reason,
            dry_run=True,
            error=describe_exception(exc),
            defensive_trim=defensive_trim_ctx is not None,
            approval_issue_id=(
                defensive_trim_ctx.approval_issue_id if defensive_trim_ctx else None
            ),
            requester_agent_id=(
                defensive_trim_ctx.requester_agent_id if defensive_trim_ctx else None
            ),
            caller_source=get_caller_source() if defensive_trim_ctx else None,
        )
        # ROB-645: a timed-out order send has an unknown outcome — tell the caller
        # to reconcile instead of re-sending (order retries are disabled).
        base_error = _order_error(describe_exception(exc))
        return _augment_error_for_unknown_outcome(
            base_error, exc, market_type=market_type, is_mock=is_mock
        )


__all__ = [
    "_calculate_date_range",
    "_normalize_market_type_to_external",
    "_get_current_price_for_order",
    "_get_holdings_for_order",
    "_get_balance_for_order",
    "_record_order_history",
    "_preview_order",
    "_execute_order",
    "_place_order_impl",
    "_close_journals_on_sell",
    "_create_trade_journal_for_buy",
    "_link_journal_to_fill",
    "_order_session_factory",
    "_save_order_fill",
    "_validate_buy_journal_requirements",
    "_append_journal_warning",
    "_get_crypto_trade_cooldown_service",
    "CRYPTO_STOP_LOSS_PCT",
]
