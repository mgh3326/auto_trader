"""ROB-866 — Toss manual-activity detection sweep (stage 1: detect + alert only).

Toss has no execution websocket, so KIS/Upbit-style fill triage never fires for
Toss: an operator's app-side manual buy/sell is invisible to the system until they
report it or the next session reads holdings (observed 2026-07-13: manual Peptron
loss-cut, Hynix/Samsung manual buys). This module diffs Toss GET /orders against the
persisted ledger + proposal rungs to surface **unbooked** orders, then (in execution
mode) alerts Telegram and hands off to session_context.

Scope boundaries:
- Read-only against the broker: only ``list_orders`` (GET /orders) is ever called.
- No auto-bookkeeping: nothing is written to ``review.toss_live_order_ledger`` here.
  Stage 2 (fill/journal booking) is deliberately out of scope. The only write is the
  idempotency marker in ``review.toss_manual_activity_alerts`` so the same manual
  order is not re-alerted across sweeps.
- The order_proposals module is read-only SELECT here (ROB-861/862 own its writes).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

KnownOrderIdsLookup = Callable[[set[str]], Awaitable[set[str]]]
NotifyCallable = Callable[[str, str | None], Awaitable[Any]]
AppendSessionContext = Callable[[list[dict[str, Any]]], Awaitable[Any]]

# Toss closed-order statuses that represent an actual execution worth booking.
_FILLED_STATUSES = frozenset({"FILLED", "PARTIAL_FILLED"})

_DEFAULT_CLOSED_PAGE_CAP = 20
_SIDE_KR = {"buy": "매수", "sell": "매도"}


@dataclass(frozen=True)
class ManualOrder:
    """A Toss order that is absent from the ledger and proposal rungs."""

    order_id: str
    symbol: str
    side: str
    status: str
    market: str | None
    quantity: Decimal | None
    filled_quantity: Decimal | None
    avg_fill_price: Decimal | None
    ordered_at: str
    is_open: bool

    def summary(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "status": self.status,
            "market": self.market,
            "quantity": _dec_str(self.quantity),
            "filled_quantity": _dec_str(self.filled_quantity),
            "avg_fill_price": _dec_str(self.avg_fill_price),
            "ordered_at": self.ordered_at,
            "is_open": self.is_open,
        }


@dataclass
class ManualActivitySweep:
    filled: list[ManualOrder] = field(default_factory=list)
    open_orders: list[ManualOrder] = field(default_factory=list)
    scanned_open: int = 0
    scanned_closed: int = 0
    window_hours: int = 24
    closed_pages_capped: bool = False


def _dec_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _market_from_order(order: Any) -> str | None:
    currency = str(getattr(order, "currency", "") or "").upper()
    if currency == "KRW":
        return "kr"
    if currency == "USD":
        return "us"
    return None


def _has_fill_evidence(order: Any) -> bool:
    execution = dict(getattr(order, "execution", {}) or {})
    try:
        return float(execution.get("filledQuantity") or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_filled(order: Any) -> bool:
    status = str(getattr(order, "status", "") or "").upper()
    return status in _FILLED_STATUSES or _has_fill_evidence(order)


def _to_manual(order: Any, *, is_open: bool) -> ManualOrder:
    execution = dict(getattr(order, "execution", {}) or {})
    return ManualOrder(
        order_id=str(order.order_id),
        symbol=str(order.symbol),
        side=str(order.side),
        status=str(order.status),
        market=_market_from_order(order),
        quantity=getattr(order, "quantity", None),
        filled_quantity=execution.get("filledQuantity"),
        avg_fill_price=execution.get("averageFilledPrice"),
        ordered_at=str(getattr(order, "ordered_at", "") or ""),
        is_open=is_open,
    )


async def _collect_orders(
    client: Any,
    *,
    from_date: str,
    to_date: str,
    closed_page_cap: int,
) -> tuple[list[Any], list[Any], bool]:
    """Fetch OPEN (1 call) + paginated CLOSED orders for the window.

    Every ``list_orders`` call spends the shared ORDER_HISTORY (5 TPS) budget inside
    the client transport, so pagination is naturally rate-limited.
    """

    open_page = await client.list_orders(status="OPEN")
    open_orders = list(open_page.orders)

    closed_orders: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    capped = False
    while True:
        page = await client.list_orders(
            status="CLOSED",
            from_date=from_date,
            to_date=to_date,
            cursor=cursor,
            limit=100,
        )
        pages += 1
        closed_orders.extend(page.orders)
        if not page.has_next or not page.next_cursor:
            break
        if pages >= closed_page_cap:
            capped = True
            break
        if page.next_cursor in seen_cursors:
            break
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    return open_orders, closed_orders, capped


async def detect_manual_activity(
    *,
    client: Any,
    known_order_ids: KnownOrderIdsLookup,
    now: datetime,
    window_hours: int = 24,
    closed_page_cap: int = _DEFAULT_CLOSED_PAGE_CAP,
) -> ManualActivitySweep:
    """Return Toss orders absent from the ledger + proposal rungs.

    ``known_order_ids`` is given the set of every fetched broker order id and returns
    the subset that is already known (ledger ∪ proposal rung). Everything else is a
    manual order: closed FILLED/PARTIAL_FILLED go to ``filled``; OPEN go to
    ``open_orders``.
    """

    from_date = (now - timedelta(hours=window_hours)).date().isoformat()
    to_date = now.date().isoformat()

    open_orders, closed_orders, capped = await _collect_orders(
        client, from_date=from_date, to_date=to_date, closed_page_cap=closed_page_cap
    )

    all_ids = {str(o.order_id) for o in open_orders} | {
        str(o.order_id) for o in closed_orders
    }
    known = await known_order_ids(all_ids) if all_ids else set()

    manual_open = [
        _to_manual(o, is_open=True) for o in open_orders if str(o.order_id) not in known
    ]
    manual_filled = [
        _to_manual(o, is_open=False)
        for o in closed_orders
        if str(o.order_id) not in known and _is_filled(o)
    ]
    return ManualActivitySweep(
        filled=manual_filled,
        open_orders=manual_open,
        scanned_open=len(open_orders),
        scanned_closed=len(closed_orders),
        window_hours=window_hours,
        closed_pages_capped=capped,
    )


class TossManualActivityAlertStore:
    """Idempotency marker store for ROB-866 (alert-only; NOT a fill ledger)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def existing_alerted_ids(self, order_ids: set[str]) -> set[str]:
        if not order_ids:
            return set()
        from app.models.review import TossManualActivityAlert

        rows = await self._db.execute(
            select(TossManualActivityAlert.broker_order_id).where(
                TossManualActivityAlert.broker_order_id.in_(order_ids)
            )
        )
        return {row[0] for row in rows}

    async def record_alerts(self, orders: list[ManualOrder]) -> int:
        if not orders:
            return 0
        from app.models.review import TossManualActivityAlert

        for order in orders:
            stmt = (
                pg_insert(TossManualActivityAlert)
                .values(
                    broker_order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    status=order.status,
                    market=order.market,
                    is_open=order.is_open,
                )
                .on_conflict_do_nothing(index_elements=["broker_order_id"])
            )
            await self._db.execute(stmt)
        await self._db.commit()
        return len(orders)


async def _proposal_broker_order_ids(db: AsyncSession, order_ids: set[str]) -> set[str]:
    """Read-only SELECT of proposal-rung broker order ids (ROB-861/862 own writes)."""
    if not order_ids:
        return set()
    from app.models.order_proposals import OrderProposalRung

    rows = await db.execute(
        select(OrderProposalRung.broker_order_id).where(
            OrderProposalRung.broker_order_id.in_(order_ids)
        )
    )
    return {row[0] for row in rows if row[0]}


def _market_label(market: str | None) -> str:
    return {"kr": "KR", "us": "US"}.get(market or "", "기타")


def _order_line(order: ManualOrder) -> str:
    side = _SIDE_KR.get(order.side, order.side)
    qty = _dec_str(order.filled_quantity) or _dec_str(order.quantity) or "?"
    price = _dec_str(order.avg_fill_price)
    tail = f" @ {price}" if price else ""
    return f"• {side} {order.symbol} {qty}{tail} [{order.status}]"


def _format_alert(
    market: str | None, fills: list[ManualOrder], opens: list[ManualOrder]
) -> str:
    lines = [f"🔔 토스 수동 거래 감지 ({_market_label(market)})"]
    lines.extend(_order_line(o) for o in fills)
    if opens:
        lines.append("미결(OPEN):")
        lines.extend(_order_line(o) for o in opens)
    lines.append("→ 원장 미기록. 부기 필요.")
    return "\n".join(lines)


async def _default_notify(message: str, market: str | None) -> Any:
    from app.monitoring.trade_notifier import get_trade_notifier

    notifier = get_trade_notifier()
    return await notifier.notify_agent_message(
        message, market_type=market, skip_discord=True
    )


async def _default_append(entries: list[dict[str, Any]]) -> Any:
    from app.mcp_server.tooling.session_context_tools import session_context_append

    return await session_context_append(entries)


async def _sweep_with_deps(
    *,
    client: Any,
    known_order_ids: KnownOrderIdsLookup,
    alert_store: Any,
    notify: NotifyCallable | None,
    append_session_context: AppendSessionContext | None,
    now: datetime,
    window_hours: int,
    dry_run: bool,
    closed_page_cap: int,
) -> dict[str, Any]:
    sweep = await detect_manual_activity(
        client=client,
        known_order_ids=known_order_ids,
        now=now,
        window_hours=window_hours,
        closed_page_cap=closed_page_cap,
    )

    manual_all = sweep.filled + sweep.open_orders
    manual_ids = {o.order_id for o in manual_all}
    already = (
        await alert_store.existing_alerted_ids(manual_ids) if manual_ids else set()
    )
    new_orders = [o for o in manual_all if o.order_id not in already]

    response: dict[str, Any] = {
        "success": True,
        "source": "toss",
        "dry_run": dry_run,
        "mutation_sent": False,
        "window_hours": window_hours,
        "scanned": {
            "open": sweep.scanned_open,
            "closed": sweep.scanned_closed,
            "closed_pages_capped": sweep.closed_pages_capped,
        },
        "manual_filled": [o.summary() for o in sweep.filled],
        "manual_open": [o.summary() for o in sweep.open_orders],
        "new_count": len(new_orders),
        "already_alerted_count": len(manual_all) - len(new_orders),
        "alerted": False,
    }

    if dry_run or not new_orders:
        return response

    notify = notify or _default_notify
    append_session_context = append_session_context or _default_append

    by_market: dict[str | None, list[ManualOrder]] = {}
    for order in new_orders:
        by_market.setdefault(order.market, []).append(order)

    entries: list[dict[str, Any]] = []
    for market, orders in by_market.items():
        fills = [o for o in orders if not o.is_open]
        opens = [o for o in orders if o.is_open]
        message = _format_alert(market, fills, opens)
        await notify(message, market)
        if market in ("kr", "us"):
            entries.append(
                {
                    "market": market,
                    "entry_type": "handoff_note",
                    "title": f"토스 수동 거래 {len(orders)}건 감지 — 부기 필요",
                    "body": message,
                    "created_by": "system",
                    "refs": {"symbols": sorted({o.symbol for o in orders})},
                }
            )
    if entries:
        await append_session_context(entries)

    await alert_store.record_alerts(new_orders)

    response["alerted"] = True
    response["alerted_count"] = len(new_orders)
    return response


async def run_manual_activity_sweep(
    *,
    window_hours: int = 24,
    dry_run: bool = True,
    closed_page_cap: int = _DEFAULT_CLOSED_PAGE_CAP,
    now: datetime | None = None,
    client: Any | None = None,
    known_order_ids: KnownOrderIdsLookup | None = None,
    alert_store: Any | None = None,
    notify: NotifyCallable | None = None,
    append_session_context: AppendSessionContext | None = None,
    settings_obj: Any | None = None,
) -> dict[str, Any]:
    """Detect Toss manual activity and, in execution mode, alert + hand off.

    Deps (client / known_order_ids / alert_store / notify / append) are injectable
    for tests. When ``known_order_ids`` and ``alert_store`` are both omitted, a live
    DB session is opened to wire the ledger + proposal-rung lookup and the marker
    store.
    """

    if now is None:
        now = datetime.now(UTC)

    if client is None:
        from app.core.config import settings as _settings
        from app.services.brokers.toss import TossReadClient

        client = TossReadClient.from_settings(settings_obj=settings_obj or _settings)

    if known_order_ids is not None and alert_store is not None:
        return await _sweep_with_deps(
            client=client,
            known_order_ids=known_order_ids,
            alert_store=alert_store,
            notify=notify,
            append_session_context=append_session_context,
            now=now,
            window_hours=window_hours,
            dry_run=dry_run,
            closed_page_cap=closed_page_cap,
        )

    from app.core.db import AsyncSessionLocal
    from app.services.toss_live_order_ledger_service import (
        TossLiveOrderLedgerService,
    )

    async with AsyncSessionLocal() as db:
        ledger = TossLiveOrderLedgerService(db)
        store = TossManualActivityAlertStore(db)

        async def known_lookup(ids: set[str]) -> set[str]:
            if not ids:
                return set()
            in_ledger = await ledger.existing_broker_order_ids(ids)
            in_props = await _proposal_broker_order_ids(db, ids)
            return in_ledger | in_props

        return await _sweep_with_deps(
            client=client,
            known_order_ids=known_lookup,
            alert_store=store,
            notify=notify,
            append_session_context=append_session_context,
            now=now,
            window_hours=window_hours,
            dry_run=dry_run,
            closed_page_cap=closed_page_cap,
        )
