from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.symbol import to_db_symbol
from app.schemas.us_action_report import (
    KISUSAccountSnapshot,
    USHeldPositionActionCard,
    USHeldPositionActionCards,
    USHolding,
    USOpenOrder,
)

_STOP_LOSS_PNL_THRESHOLD = -10.0
_ADD_DRAWDOWN_THRESHOLD = -5.0
_TRIM_PROFIT_THRESHOLD = 15.0


def _normal_symbol(symbol: str) -> str:
    return to_db_symbol(symbol.upper())


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
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


def _journal_status(journal: Mapping[str, Any] | Any | None) -> str:
    if journal is None:
        return "missing"
    account_type = str(
        _journal_value(journal, "account_type", "accountType") or "live"
    ).lower()
    if account_type == "paper":
        return "paper"
    status = str(_journal_value(journal, "status") or "active").lower()
    if status in {"active", "draft"}:
        return status
    return "inactive"


def _journal_map(
    journals_by_symbol: Mapping[str, Mapping[str, Any] | Any] | None,
) -> dict[str, Mapping[str, Any] | Any]:
    return {
        _normal_symbol(symbol): journal
        for symbol, journal in (journals_by_symbol or {}).items()
        if symbol
    }


def _pending_by_symbol(
    orders: list[USOpenOrder],
    *,
    side: str,
) -> dict[str, float]:
    pending: dict[str, float] = {}
    for order in orders:
        if order.side != side or order.pending_qty <= 0:
            continue
        symbol = _normal_symbol(order.symbol)
        pending[symbol] = pending.get(symbol, 0.0) + order.pending_qty
    return pending


def _active_hold_block(
    journal: Mapping[str, Any] | Any | None,
    now: datetime,
) -> tuple[bool, datetime | None]:
    hold_until = _datetime_or_none(_journal_value(journal, "hold_until", "holdUntil"))
    if hold_until is None:
        return False, None
    compare_now = now
    if hold_until.tzinfo is not None and compare_now.tzinfo is None:
        compare_now = compare_now.replace(tzinfo=UTC)
    if hold_until.tzinfo is None and compare_now.tzinfo is not None:
        hold_until = hold_until.replace(tzinfo=compare_now.tzinfo)
    return hold_until > compare_now, hold_until


def _price(holding: USHolding) -> float | None:
    if holding.last_price_usd is not None:
        return holding.last_price_usd
    if holding.value_usd is not None and holding.quantity:
        return holding.value_usd / holding.quantity
    return None


def _pnl_rate(holding: USHolding) -> float | None:
    if holding.pnl_rate is not None:
        return holding.pnl_rate
    price = _price(holding)
    if price is None or not holding.average_cost_usd:
        return None
    return (price - holding.average_cost_usd) / holding.average_cost_usd * 100.0


def _cap_executable(action: str, sellable_qty: float, quantity: float) -> float:
    if action == "sell":
        return max(sellable_qty, 0.0)
    if action == "trim":
        return max(min(sellable_qty, quantity), 0.0)
    return 0.0


def build_us_held_position_action_cards(
    *,
    account_snapshot: KISUSAccountSnapshot,
    journals_by_symbol: Mapping[str, Mapping[str, Any] | Any] | None = None,
    manual_reference_symbols: set[str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> USHeldPositionActionCards:
    """Classify KIS-live US held positions into sell/trim/hold/add/watch cards.

    This is a pure read-model builder. It consumes an already-built KIS-live
    snapshot plus caller-provided journal/reference context; it never submits,
    cancels, modifies, or records broker/watch/order-intent state.
    """

    warnings: list[str] = []
    cards: list[USHeldPositionActionCard] = []
    journals = _journal_map(journals_by_symbol)
    manual_symbols = {
        _normal_symbol(symbol) for symbol in (manual_reference_symbols or set())
    }
    pending_sell = _pending_by_symbol(account_snapshot.open_orders, side="sell")
    pending_buy = _pending_by_symbol(account_snapshot.open_orders, side="buy")
    held_symbols: set[str] = set()
    current_time = now()

    for holding in account_snapshot.holdings:
        symbol = _normal_symbol(holding.symbol)
        held_symbols.add(symbol)
        card_warnings: list[str] = []
        reasons: list[str] = []
        missing: list[str] = []

        if (
            holding.manual_only
            or not holding.source_of_truth
            or not holding.is_tradeable
        ):
            warnings.append(f"{symbol}: non_kis_tradeable_holding_skipped")
            continue
        if holding.quantity <= 0:
            warnings.append(f"{symbol}: zero_quantity_holding_skipped")
            continue

        journal = journals.get(symbol)
        journal_status = _journal_status(journal)
        if journal_status == "missing":
            missing.append("journal_missing")
        elif journal_status in {"inactive", "paper"}:
            missing.append(f"journal_{journal_status}")

        target_price = _float_or_none(
            _journal_value(journal, "target_price", "targetPrice", "target_price_usd")
        )
        stop_loss = _float_or_none(
            _journal_value(journal, "stop_loss", "stopLoss", "stop_loss_usd")
        )
        thesis = _journal_value(journal, "thesis")
        thesis_text = str(thesis).strip() if thesis is not None else None
        if journal_status in {"active", "draft"} and not thesis_text:
            missing.append("journal_thesis_missing")

        hold_blocked, hold_until = _active_hold_block(journal, current_time)
        if hold_blocked:
            reasons.append("min_hold_active")

        price = _price(holding)
        pnl_rate = _pnl_rate(holding)
        if pnl_rate is None:
            missing.append("pnl_rate_missing")
        pending_sell_qty = pending_sell.get(symbol, 0.0)
        pending_buy_qty = pending_buy.get(symbol, 0.0)
        if pending_sell_qty > 0:
            reasons.append("pending_sell_exists")
            warning = f"{symbol}: duplicate pending sell/trim suppressed"
            card_warnings.append(warning)
            warnings.append(warning)
        if pending_buy_qty > 0:
            reasons.append("pending_buy_exists")

        target_hit = (
            target_price is not None and price is not None and price >= target_price
        )
        stop_hit = stop_loss is not None and price is not None and price <= stop_loss
        pnl_stop_hit = pnl_rate is not None and pnl_rate <= _STOP_LOSS_PNL_THRESHOLD
        profit_trim_hit = pnl_rate is not None and pnl_rate >= _TRIM_PROFIT_THRESHOLD

        action = "hold" if journal_status in {"active", "draft"} else "watch"
        suggested_trim_pct: int | None = None

        if stop_hit or pnl_stop_hit:
            reasons.append("stop_loss_hit" if stop_hit else "pnl_stop_loss_hit")
            action = "hold" if hold_blocked else "sell"
        elif target_hit:
            reasons.append("target_hit")
            action = "hold" if hold_blocked else "trim"
            suggested_trim_pct = None if hold_blocked else 50
        elif profit_trim_hit and journal_status in {"active", "draft"}:
            reasons.append("profit_trim_threshold")
            action = "hold" if hold_blocked else "trim"
            suggested_trim_pct = None if hold_blocked else 25
        elif (
            journal_status in {"active", "draft"}
            and pnl_rate is not None
            and pnl_rate <= _ADD_DRAWDOWN_THRESHOLD
            and not hold_blocked
            and not stop_hit
        ):
            reasons.append("add_candidate_drawdown")
            action = "add"

        if holding.sellable_qty <= 0 and action in {"sell", "trim"}:
            reasons.append("no_sellable_quantity")
            card_warnings.append(
                f"{symbol}: no KIS sellable quantity; executable action suppressed"
            )
            action = "hold"
            suggested_trim_pct = None
        if pending_sell_qty > 0 and action in {"sell", "trim", "add"}:
            action = "hold"
            suggested_trim_pct = None
        if pending_buy_qty > 0 and action == "add":
            warning = f"{symbol}: duplicate pending add suppressed"
            card_warnings.append(warning)
            warnings.append(warning)
            action = "hold"

        executable_qty = _cap_executable(action, holding.sellable_qty, holding.quantity)
        if action == "trim" and suggested_trim_pct is not None:
            executable_qty = min(
                executable_qty, holding.quantity * suggested_trim_pct / 100.0
            )

        cards.append(
            USHeldPositionActionCard(
                symbol=symbol,
                display_name=holding.display_name,
                action=action,
                suggested_trim_pct=suggested_trim_pct,
                executable_qty=round(executable_qty, 8),
                quantity=holding.quantity,
                sellable_qty=holding.sellable_qty,
                pending_sell_qty=pending_sell_qty,
                pending_buy_qty=pending_buy_qty,
                pnl_rate=pnl_rate,
                pnl_usd=holding.pnl_usd,
                last_price_usd=price,
                average_cost_usd=holding.average_cost_usd,
                target_price_usd=target_price,
                stop_loss_usd=stop_loss,
                hold_until=hold_until,
                journal_status=journal_status,  # type: ignore[arg-type]
                thesis=thesis_text,
                reason_codes=reasons,
                missing_context_codes=missing,
                warnings=card_warnings,
            )
        )

    for symbol in sorted(set(journals) - held_symbols):
        warnings.append(f"{symbol}: journal_only_not_kis_held")
    for symbol in sorted(manual_symbols - held_symbols):
        warnings.append(f"{symbol}: manual_reference_only_not_kis_tradeable")

    return USHeldPositionActionCards(cards, warnings=warnings)
