from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from app.core.symbol import to_db_symbol
from app.schemas.us_action_report import (
    KISUSAccountSnapshot,
    ScreenedUSNewBuyCandidate,
    USNewBuyCandidateCard,
    USNewBuyCandidateCards,
)

_DEFAULT_MIN_HOLD_DAYS = 14
_DEFAULT_TARGET_UPSIDE = 1.08
_DEFAULT_STOP_DOWNSIDE = 0.95


def _normal_symbol(symbol: str) -> str:
    return to_db_symbol(symbol.upper())


def _capital_basis(snapshot: KISUSAccountSnapshot) -> float | None:
    values = [
        value
        for value in (snapshot.usd_buying_power, snapshot.usd_cash)
        if value is not None and value > 0
    ]
    if not values:
        return None
    return min(values)


def _research_value(
    research: Mapping[str, Any] | None,
    *keys: str,
) -> Any:
    if not research:
        return None
    for key in keys:
        value = research.get(key)
        if value is not None:
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_default(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _sort_key(candidate: ScreenedUSNewBuyCandidate) -> tuple[float, float, str]:
    score = candidate.score if candidate.score is not None else -math.inf
    change_rate = (
        candidate.change_rate if candidate.change_rate is not None else -math.inf
    )
    return (float(score), float(change_rate), candidate.symbol)


def build_us_new_buy_candidate_cards(
    *,
    account_snapshot: KISUSAccountSnapshot,
    candidates: list[ScreenedUSNewBuyCandidate],
    research_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
    calendar_risks_by_symbol: Mapping[str, list[str]] | None = None,
    news_risks_by_symbol: Mapping[str, list[str]] | None = None,
    concentration_symbols: set[str] | None = None,
    per_candidate_budget_pct: float = 0.20,
    max_candidates: int = 5,
) -> USNewBuyCandidateCards:
    """Build read-only US new-buy candidate cards from KIS-live capital.

    The function is deliberately pure: callers provide already-read account,
    screener, research, calendar, and news context. It never calls broker order
    submit/cancel/modify methods and never writes watch/order-intent state.
    """

    warnings: list[str] = []
    held_symbols = {
        _normal_symbol(holding.symbol) for holding in account_snapshot.holdings
    }
    open_buy_by_symbol: dict[str, int] = {}
    for order in account_snapshot.open_orders:
        if order.side == "buy" and order.pending_qty > 0:
            symbol = _normal_symbol(order.symbol)
            open_buy_by_symbol[symbol] = open_buy_by_symbol.get(symbol, 0) + 1

    sizing_basis = _capital_basis(account_snapshot)
    if sizing_basis is None:
        warnings.append("kis_live_usd_capital_missing")
    elif account_snapshot.usd_buying_power is not None:
        warnings.append("sized_against_kis_live_usd_buying_power")

    concentration_set = {
        _normal_symbol(symbol) for symbol in (concentration_symbols or set())
    }
    filtered_candidates: list[ScreenedUSNewBuyCandidate] = []
    skipped_held = False
    for candidate in candidates:
        symbol = _normal_symbol(candidate.symbol)
        if symbol in held_symbols:
            skipped_held = True
            continue
        filtered_candidates.append(candidate.model_copy(update={"symbol": symbol}))
    if skipped_held:
        warnings.append("기보유 종목 제외")

    filtered_candidates.sort(key=_sort_key, reverse=True)

    cards: list[USNewBuyCandidateCard] = []
    for rank, candidate in enumerate(filtered_candidates[:max_candidates], start=1):
        symbol = _normal_symbol(candidate.symbol)
        price = candidate.price
        budget = (
            (sizing_basis * per_candidate_budget_pct)
            if sizing_basis is not None
            else 0.0
        )
        quantity = int(budget // price) if price is not None and price > 0 else 0
        notional = round(quantity * price, 2) if price is not None else 0.0

        risk_notes: list[str] = []
        if sizing_basis is None:
            risk_notes.append("KIS live USD buying power 확인 불가")
        if price is None or price <= 0:
            risk_notes.append("candidate price 확인 불가")
        if symbol in open_buy_by_symbol:
            risk_notes.append(
                f"open buy order already pending: {open_buy_by_symbol[symbol]}"
            )
        for item in (calendar_risks_by_symbol or {}).get(symbol, []):
            risk_notes.append(f"calendar: {item}")
        for item in (news_risks_by_symbol or {}).get(symbol, []):
            risk_notes.append(f"news: {item}")
        if symbol in concentration_set:
            risk_notes.append("concentration risk: overlaps existing focused exposure")

        research = (research_by_symbol or {}).get(symbol)
        target = _float_or_none(
            _research_value(research, "target_price", "targetPrice", "target_price_usd")
        )
        stop = _float_or_none(
            _research_value(research, "stop_loss", "stopLoss", "stop_loss_usd")
        )
        if price is not None and price > 0:
            if target is None:
                target = round(price * _DEFAULT_TARGET_UPSIDE, 2)
            if stop is None:
                stop = round(price * _DEFAULT_STOP_DOWNSIDE, 2)
        min_hold_days = _int_or_default(
            _research_value(research, "min_hold_days", "minHoldDays"),
            _DEFAULT_MIN_HOLD_DAYS,
        )
        thesis = str(_research_value(research, "thesis") or "").strip()
        if not thesis:
            candidate_name = candidate.name or symbol
            thesis = f"검토 후보: {candidate_name} — 스크리너/리서치 요약 기반 분석 우선순위."

        sizing_note = "KIS live USD buying power 기준"
        if sizing_basis is not None:
            sizing_note = (
                f"KIS live USD buying power 기준 ${sizing_basis:,.2f} 중 "
                f"{per_candidate_budget_pct:.0%} 한도"
            )

        cards.append(
            USNewBuyCandidateCard(
                symbol=symbol,
                name=candidate.name,
                priority_label=f"분석 우선순위 {rank}",
                price_usd=price,
                sizing_basis_usd=sizing_basis,
                quantity_estimate=quantity,
                notional_estimate_usd=notional,
                sizing_note=sizing_note,
                thesis=thesis,
                target_price_usd=target,
                stop_loss_usd=stop,
                min_hold_days=min_hold_days,
                risk_notes=risk_notes,
                data_warnings=list(candidate.data_warnings),
            )
        )

    return USNewBuyCandidateCards(cards, warnings=warnings)
