from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.symbol import to_db_symbol
from app.schemas.us_action_report import (
    KISUSAccountSnapshot,
    KISUSOrderPreviewLadderRung,
    KISUSOrderPreviewRequest,
    KISUSOrderPreviewResult,
    KISUSOrderSubmitDisabledError,
    USHolding,
)
from app.services.orders.ladder_fill_safety import (
    LadderRung,
    evaluate_ladder_fill_safety,
)

_DEFAULT_MAX_QUANTITY = 5.0
_DEFAULT_MAX_NOTIONAL_USD = 1000.0
_DEFAULT_MAX_LIMIT_DEVIATION_PCT = 10.0

_FORBIDDEN_LIVE_ORDER_METHODS = (
    "submit_order",
    "place_order",
    "cancel_order",
    "modify_order",
)
_REQUIRED_BUY_JOURNAL_FIELDS = (
    "thesis",
    "strategy",
    "target_price_usd",
    "stop_loss_usd",
    "min_hold_days",
)


def _normal_symbol(symbol: str) -> str:
    return to_db_symbol(symbol.upper())


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _journal_value(journal: Mapping[str, Any] | Any | None, *keys: str) -> Any:
    if journal is None:
        return None
    for key in keys:
        if isinstance(journal, Mapping):
            value = journal.get(key)
        else:
            value = getattr(journal, key, None)
        if value is not None:
            return value
    return None


def _journal_map(
    journals_by_symbol: Mapping[str, Mapping[str, Any] | Any] | None,
) -> dict[str, Mapping[str, Any] | Any]:
    return {
        _normal_symbol(symbol): journal
        for symbol, journal in (journals_by_symbol or {}).items()
        if symbol
    }


def _tradeable_holding_by_symbol(
    snapshot: KISUSAccountSnapshot,
) -> dict[str, USHolding]:
    holdings: dict[str, USHolding] = {}
    for holding in snapshot.holdings:
        if (
            holding.manual_only
            or not holding.source_of_truth
            or not holding.is_tradeable
        ):
            continue
        holdings[_normal_symbol(holding.symbol)] = holding
    return holdings


def _manual_or_reference_symbols(snapshot: KISUSAccountSnapshot) -> set[str]:
    return {
        _normal_symbol(holding.symbol)
        for holding in snapshot.holdings
        if holding.manual_only
        or not holding.source_of_truth
        or not holding.is_tradeable
    }


def _pending_duplicate_count(
    snapshot: KISUSAccountSnapshot, *, symbol: str, side: str
) -> int:
    count = 0
    for order in snapshot.open_orders:
        if (
            order.side == side
            and order.pending_qty > 0
            and _normal_symbol(order.symbol) == symbol
        ):
            count += 1
    return count


def _holding_reference_price(holding: USHolding | None) -> float | None:
    if holding is None:
        return None
    if holding.last_price_usd is not None:
        return holding.last_price_usd
    if holding.value_usd is not None and holding.quantity:
        return holding.value_usd / holding.quantity
    return None


def _reference_price_with_source(
    *,
    request: KISUSOrderPreviewRequest,
    holding: USHolding | None,
) -> tuple[float | None, str | None]:
    if request.reference_price_usd is not None and request.reference_price_usd > 0:
        return request.reference_price_usd, "referencePriceUsd"
    holding_price = _holding_reference_price(holding)
    if holding_price is None:
        return None, None
    return holding_price, "holdingReferencePrice"


def _fill_anchor_price_with_source(
    *,
    request: KISUSOrderPreviewRequest,
    reference_price: float | None,
    reference_price_source: str | None,
) -> tuple[float | None, str | None]:
    if request.best_bid_usd is not None and request.best_bid_usd > 0:
        return request.best_bid_usd, "bestBidUsd"
    if reference_price is not None and reference_price > 0:
        return reference_price, reference_price_source
    return None, None


def _first_non_blank(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _request_or_journal_float(
    request_value: float | None,
    journal: Mapping[str, Any] | Any | None,
    *journal_keys: str,
) -> float | None:
    if request_value is not None:
        return request_value
    return _float_or_none(_journal_value(journal, *journal_keys))


def _request_or_journal_int(
    request_value: int | None,
    journal: Mapping[str, Any] | Any | None,
    *journal_keys: str,
) -> int | None:
    if request_value is not None:
        return request_value
    return _int_or_none(_journal_value(journal, *journal_keys))


def _buy_journal_missing_fields(
    request: KISUSOrderPreviewRequest,
    journal: Mapping[str, Any] | Any | None,
) -> list[str]:
    thesis = _first_non_blank(request.thesis, _journal_value(journal, "thesis"))
    strategy = _first_non_blank(request.strategy, _journal_value(journal, "strategy"))
    target = _request_or_journal_float(
        request.target_price_usd,
        journal,
        "target_price",
        "targetPrice",
        "target_price_usd",
    )
    stop = _request_or_journal_float(
        request.stop_loss_usd,
        journal,
        "stop_loss",
        "stopLoss",
        "stop_loss_usd",
    )
    min_hold = _request_or_journal_int(
        request.min_hold_days,
        journal,
        "min_hold_days",
        "minHoldDays",
    )

    missing: list[str] = []
    if thesis is None:
        missing.append("thesis")
    if strategy is None:
        missing.append("strategy")
    if target is None or target <= 0:
        missing.append("target_price_usd")
    if stop is None or stop <= 0:
        missing.append("stop_loss_usd")
    if min_hold is None or min_hold <= 0:
        missing.append("min_hold_days")
    return missing


def preview_kis_us_live_order(
    *,
    account_snapshot: KISUSAccountSnapshot,
    request: KISUSOrderPreviewRequest,
    journals_by_symbol: Mapping[str, Mapping[str, Any] | Any] | None = None,
    max_quantity: float = _DEFAULT_MAX_QUANTITY,
    max_notional_usd: float = _DEFAULT_MAX_NOTIONAL_USD,
    max_limit_deviation_pct: float = _DEFAULT_MAX_LIMIT_DEVIATION_PCT,
) -> KISUSOrderPreviewResult:
    """Validate a candidate KIS-live US limit order without broker mutation.

    This pure preview gate consumes already-read account/journal context and
    returns pass/fail reasons. It intentionally imports no broker order modules
    and never submits, cancels, modifies, records watch intents, or writes DB
    state. Submit remains disabled for this sprint even when every check passes.
    """

    symbol = _normal_symbol(request.symbol)
    side = request.side
    blocked: list[str] = []
    warnings: list[str] = []
    details: dict[str, object] = {
        "accountMode": "kis_live",
        "brokerMutation": "disabled",
        "forbiddenLiveOrderMethods": list(_FORBIDDEN_LIVE_ORDER_METHODS),
    }

    if request.quantity <= 0:
        blocked.append("quantity_must_be_positive")
    if request.limit_price_usd <= 0:
        blocked.append("limit_price_must_be_positive")

    notional = max(request.quantity, 0.0) * max(request.limit_price_usd, 0.0)
    details["maxQuantity"] = max_quantity
    details["maxNotionalUsd"] = max_notional_usd
    if request.quantity > max_quantity:
        blocked.append("quantity_exceeds_preview_bound")
    if notional > max_notional_usd:
        blocked.append("notional_exceeds_preview_bound")

    pending_count = _pending_duplicate_count(account_snapshot, symbol=symbol, side=side)
    details["pendingDuplicateCount"] = pending_count
    if pending_count > 0:
        blocked.append("duplicate_pending_order_exists")

    holdings = _tradeable_holding_by_symbol(account_snapshot)
    holding = holdings.get(symbol)
    if side == "sell":
        sellable_qty = holding.sellable_qty if holding is not None else 0.0
        details["sellableQty"] = sellable_qty
        if symbol in _manual_or_reference_symbols(account_snapshot):
            blocked.append("manual_only_quantity_not_sellable")
        if sellable_qty <= 0:
            blocked.append("kis_live_sellable_quantity_missing")
        elif request.quantity > sellable_qty:
            blocked.append("quantity_exceeds_kis_live_sellable")
    else:
        journal = _journal_map(journals_by_symbol).get(symbol)
        missing = _buy_journal_missing_fields(request, journal)
        details["missingBuyJournalFields"] = missing
        if missing:
            blocked.append("buy_journal_required_fields_missing")

    reference_price, reference_price_source = _reference_price_with_source(
        request=request,
        holding=holding,
    )
    details["referencePriceUsd"] = reference_price
    details["referencePriceSource"] = reference_price_source
    if reference_price is None or reference_price <= 0:
        warnings.append("reference_price_missing_for_limit_sanity")
    elif request.limit_price_usd > 0:
        deviation_pct = (
            abs(request.limit_price_usd - reference_price) / reference_price * 100.0
        )
        details["limitPriceDeviationPct"] = round(deviation_pct, 4)
        details["maxLimitDeviationPct"] = max_limit_deviation_pct
        if deviation_pct > max_limit_deviation_pct:
            blocked.append("limit_price_deviation_exceeds_bound")

    if side == "sell":
        fill_anchor_price, fill_anchor_source = _fill_anchor_price_with_source(
            request=request,
            reference_price=reference_price,
            reference_price_source=reference_price_source,
        )
        implied_single_rung = not request.ladder_rungs
        ladder = request.ladder_rungs or [
            KISUSOrderPreviewLadderRung(
                quantity=request.quantity,
                limit_price_usd=request.limit_price_usd,
            )
        ]
        fill_warnings, fill_safety = evaluate_ladder_fill_safety(
            rungs=[
                LadderRung(limit_price=rung.limit_price_usd, quantity=rung.quantity)
                for rung in ladder
            ],
            anchor_price=fill_anchor_price,
            anchor_source=fill_anchor_source,
            atr=request.atr_usd,
        )
        warnings.extend(fill_warnings)
        if fill_safety is not None:
            fill_safety["impliedFromSingleOrder"] = implied_single_rung
            details["fillSafety"] = fill_safety

    return KISUSOrderPreviewResult(
        symbol=symbol,
        side=side,
        order_type="limit",
        quantity=request.quantity,
        limit_price_usd=request.limit_price_usd,
        notional_usd=round(notional, 2),
        status="blocked" if blocked else "pass",
        submit_enabled=False,
        blocked_reasons=blocked,
        warnings=warnings,
        check_details=details,
    )


def submit_kis_us_live_order_from_preview_disabled(*_: Any, **__: Any) -> None:
    """Explicitly disabled submit seam for the preview-only ROB-244 sprint."""

    raise KISUSOrderSubmitDisabledError(
        "KIS live order submit is disabled for this preview-only flow. "
        "Use the separately reviewed live-order execution path only after approval."
    )
