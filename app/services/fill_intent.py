"""Fill-alert intent and reference slippage (hk 2834).

Pure: no DB, no broker, no clock. The lookup that fills a ``FillIntent`` lives in
``app.services.execution_ledger.fill_intent_lookup``; the notifier formatters
only render what they are given.

A fill alert says *why* the order existed (strategy, thesis summary, ladder
position) and *how the fill compared to the reference price*. Three intent
states exist so that a missing answer is never shown as an absent order:

* ``matched``       — a proposal rung carries the broker evidence;
* ``no_match``      — the lookup ran and found no rung (a direct order);
* ``lookup_failed`` — the lookup raised, so absence is unknown.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.services.fill_notification import FillOrder, format_fill_money

IntentStatus = Literal["matched", "no_match", "lookup_failed"]
ReferenceKind = Literal["limit", "approval_price"]

MAX_THESIS_CHARS = 60

NO_MATCH_TEXT = "기록 없음(직접 주문)"
LOOKUP_FAILED_TEXT = "조회 실패"


@dataclass(frozen=True)
class FillIntent:
    status: IntentStatus
    strategy: str | None = None
    thesis_summary: str | None = None
    #: 1-based position of the rung among the proposal's rungs, and the total.
    rung_position: int | None = None
    rung_total: int | None = None
    reference_price: Decimal | None = None
    reference_kind: ReferenceKind | None = None


NO_MATCH = FillIntent(status="no_match")
LOOKUP_FAILED = FillIntent(status="lookup_failed")


def summarize_thesis(text: str | None) -> str | None:
    """First non-blank line, whitespace collapsed, bounded for a one-line alert."""
    if not text:
        return None
    first = next((line for line in text.splitlines() if line.strip()), "")
    collapsed = " ".join(first.split())
    if not collapsed:
        return None
    if len(collapsed) > MAX_THESIS_CHARS:
        return collapsed[: MAX_THESIS_CHARS - 1] + "…"
    return collapsed


@dataclass(frozen=True)
class FillSlippage:
    #: ``True`` favourable, ``False`` adverse, ``None`` exactly at the reference.
    favourable: bool | None
    #: Signed ``(fill - reference) / reference * 10000``.
    bp: Decimal
    #: Signed ``(fill - reference) * qty`` in the fill's currency.
    total: Decimal
    fill_price: Decimal
    reference_price: Decimal
    reference_kind: ReferenceKind


def compute_slippage(
    order: FillOrder, intent: FillIntent | None
) -> FillSlippage | None:
    """Slippage against the rung reference, or ``None`` when no reference applies.

    Buy: a fill below the reference is favourable. Sell: a fill above it is.
    """
    if intent is None or intent.status != "matched":
        return None
    reference = intent.reference_price
    if reference is None or intent.reference_kind is None or reference <= 0:
        return None
    if order.side not in ("bid", "ask"):
        return None
    fill = Decimal(str(order.filled_price))
    qty = Decimal(str(order.filled_qty))
    if not fill.is_finite() or not qty.is_finite():
        return None
    diff = fill - reference
    if diff == 0:
        favourable: bool | None = None
    elif order.side == "bid":
        favourable = diff < 0
    else:
        favourable = diff > 0
    return FillSlippage(
        favourable=favourable,
        bp=diff / reference * Decimal(10000),
        total=diff * qty,
        fill_price=fill,
        reference_price=reference,
        reference_kind=intent.reference_kind,
    )


def _identity(text: str) -> str:
    return text


def render_intent_value(
    intent: FillIntent,
    *,
    escape: Callable[[str], str] = _identity,
) -> str:
    """The value that follows ``의도: `` in the alert."""
    if intent.status == "no_match":
        return NO_MATCH_TEXT
    if intent.status == "lookup_failed":
        return LOOKUP_FAILED_TEXT
    parts = [
        escape(intent.strategy) if intent.strategy else "전략 미기재",
        escape(intent.thesis_summary) if intent.thesis_summary else "근거 요약 없음",
    ]
    if intent.rung_position is not None and intent.rung_total is not None:
        parts.append(f"래더 {intent.rung_position}/{intent.rung_total}")
    return " · ".join(parts)


def render_slippage_value(slippage: FillSlippage, *, is_usd: bool) -> str:
    """The value that follows ``슬리피지: `` in the alert."""
    if slippage.favourable is None:
        label = "기준가 일치"
    elif slippage.favourable:
        label = "유리"
    else:
        label = "불리"
    kind = "지정가" if slippage.reference_kind == "limit" else "승인 시점가"
    total = slippage.total
    sign = "+" if total > 0 else ("-" if total < 0 else "")
    money = format_fill_money(float(abs(total)), is_usd=is_usd)
    return (
        f"{label} {float(slippage.bp):+.2f}bp · 총 {sign}{money} "
        f"(체결 {format_fill_money(float(slippage.fill_price), is_usd=is_usd)} / "
        f"{kind} {format_fill_money(float(slippage.reference_price), is_usd=is_usd)})"
    )
