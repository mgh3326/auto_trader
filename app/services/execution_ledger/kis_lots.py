"""Task #963 — KIS live KR lots and own-open-buy evidence from the execution ledger.

Read-only and DB-only: this module never touches a broker and never writes. It
backs the opt-in ``include_ledger_lots`` block of ``get_holdings`` (MCP layer:
``app/mcp_server/tooling/portfolio_ledger_lots.py``).

Two questions, both answered fail-closed:

* **Lots.** FIFO remaining lots over *authoritative* ``execution_ledger`` rows
  (``reconciler`` / ``manual_import``) for one KIS live KR symbol. Provisional
  ``websocket`` rows are never counted; they are listed separately. The cost is
  a ledger FIFO projection, NOT the broker's moving-average ``avg_buy_price``
  (pre-ledger holdings appear as one ``opening_seed`` lot at the broker average
  as of the seed). The projection is trusted only when the ledger is fresh
  (last successful KIS reconcile <= ``FRESH_MAX_MINUTES``) and its net quantity
  equals the broker quantity the caller already holds; anything else is
  ``ledger_state="unknown"`` with reason codes and ``lots=None`` — never ``[]``.
* **Own open buy evidence (S2/S3 of the #678-preserving proof).** Same-KST-day
  non-terminal buy rows in ``review.kis_live_order_ledger`` (S2) and same-day
  buy fills in ``execution_ledger`` whose order the order ledger has not proven
  complete (S3). Either one, or an unreadable/stale ledger, is ``blocking``.
  ``external_orders_verifiable`` is always ``False``: an order placed outside
  auto_trader (KIS app/HTS) and not yet filled is invisible here by
  construction. That residual is a recorded caveat, not a proof of absence.

The #678 harness denial of ``kis_live_get_order_history`` is untouched: nothing
here calls a KIS order read.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import kst_day_window
from app.models.execution_ledger import ExecutionLedger
from app.models.review import KISLiveOrderLedger
from app.services.execution_ledger.repository import ExecutionLedgerRepository

logger = logging.getLogger(__name__)

COST_METHOD = "fifo_remaining_lots_from_ledger"
COST_BASIS_NOTE = (
    "FIFO over execution_ledger fills (reconciler/manual_import only). Pre-ledger "
    "holdings are one opening_seed lot at the broker average price as of the seed. "
    "This is NOT the broker moving-average avg_buy_price."
)
FRESH_MAX_MINUTES = 90
# Order-ledger statuses proven terminal by broker evidence (kis_live_reconcile_orders).
# Every other status — accepted/pending/partial/unknown/anomaly, or a value this
# module has never seen — is treated as possibly open (fail-closed).
TERMINAL_ORDER_STATUSES = frozenset({"filled", "cancelled", "expired", "rejected"})
AUTHORITATIVE_SOURCES = frozenset({"reconciler", "manual_import"})
_PROVISIONAL_SOURCE = "websocket"
# How far back non-terminal buy rows are still reported (as presumed-dead
# prior-day day orders). They never block: a KRX/NXT day order cannot rest past
# its trading day.
_PRIOR_DAY_LOOKBACK = timedelta(days=7)

UNKNOWN_NO_LEDGER_ROWS = "no_ledger_rows"
UNKNOWN_ONLY_PROVISIONAL_ROWS = "only_provisional_rows"
UNKNOWN_LEDGER_STALE = "ledger_stale"
UNKNOWN_NO_RECONCILE_RUN = "no_reconcile_run"
UNKNOWN_HISTORY_GAP = "oversold_history_gap"
UNKNOWN_REFERENCE_MISSING = "reference_quantity_unavailable"
UNKNOWN_QTY_MISMATCH = "quantity_mismatch_with_reference"
UNKNOWN_PROVISIONAL_PENDING = "provisional_rows_pending_reconcile"
UNKNOWN_LOAD_FAILED = "ledger_read_failed"
UNKNOWN_ORDER_LEDGER_READ_FAILED = "order_ledger_read_failed"

BLOCK_OWN_OPEN_BUY = "own_nonterminal_buy_order_today"
BLOCK_SAME_DAY_FILL = "same_day_buy_fill_order_not_proven_complete"
BLOCK_EVIDENCE_UNKNOWN = "open_buy_evidence_unknown"
BLOCK_SAME_DAY_SELL_FILL = "same_day_sell_fill_in_ledger"
BLOCK_SELL_EVIDENCE_UNKNOWN = "same_day_sell_evidence_unknown"

FreshnessState = Literal["fresh", "stale", "missing"]


@dataclass(frozen=True, slots=True)
class LedgerFill:
    """Projection of one ``review.execution_ledger`` row (KIS live KR)."""

    id: int
    source: str
    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    broker_order_id: str


@dataclass(frozen=True, slots=True)
class OrderRow:
    """Projection of one ``review.kis_live_order_ledger`` buy row."""

    id: int
    order_no: str | None
    status: str
    quantity: Decimal | None
    price: Decimal | None
    trade_date: datetime


@dataclass(frozen=True, slots=True)
class PositionRef:
    """A broker-held KIS live KR position the caller already has in hand."""

    symbol: str
    reference_quantity: Decimal | None
    current_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Freshness:
    state: FreshnessState
    last_reconcile_finished_at: datetime | None
    lag_minutes: float | None


def compute_freshness(last_finished_at: datetime | None, now: datetime) -> Freshness:
    """``fresh`` only for a completed KIS reconcile no older than the cap."""
    if last_finished_at is None:
        return Freshness("missing", None, None)
    finished = (
        last_finished_at
        if last_finished_at.tzinfo
        else last_finished_at.replace(tzinfo=UTC)
    )
    lag = (now - finished).total_seconds() / 60
    if lag < 0:
        # A finish time in the future cannot be verified as recent.
        return Freshness("missing", finished, round(lag, 2))
    state: FreshnessState = "fresh" if lag <= FRESH_MAX_MINUTES else "stale"
    return Freshness(state, finished, round(lag, 2))


def _norm_order_id(order_id: str | None) -> str:
    """Leading-zero-normalized order id (mirrors query_service._supersede_key)."""
    raw = str(order_id or "").strip()
    return raw.lstrip("0") or raw


def _fmt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _fmt_price(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001")), "f")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_today_kst(when: datetime, now: datetime) -> bool:
    """True for the current KST day and for anything dated later than today.

    A later-dated row can only come from clock skew between writers; treating it
    as "today" keeps the evidence fail-closed instead of presuming it dead.
    """
    start, _ = kst_day_window(now)
    aware = when if when.tzinfo else when.replace(tzinfo=UTC)
    return aware >= start


def _split_provisional(
    fills: Sequence[LedgerFill],
) -> tuple[list[LedgerFill], list[LedgerFill], int]:
    """Return (authoritative, provisional-not-superseded, superseded_count).

    A websocket row is a duplicate once an authoritative row covers the same
    order (same rule as query_service._supersede_provisional_fills; venue and
    fill_seq are deliberately ignored because the two writers derive them
    independently).
    """
    authoritative = [f for f in fills if f.source in AUTHORITATIVE_SOURCES]
    covered = {(f.side, _norm_order_id(f.broker_order_id)) for f in authoritative}
    provisional: list[LedgerFill] = []
    superseded = 0
    for fill in fills:
        if fill.source != _PROVISIONAL_SOURCE:
            continue
        if (fill.side, _norm_order_id(fill.broker_order_id)) in covered:
            superseded += 1
        else:
            provisional.append(fill)
    return authoritative, provisional, superseded


def _fifo_lots(
    authoritative: Sequence[LedgerFill],
) -> tuple[list[dict[str, Any]], Decimal, Decimal]:
    """FIFO remaining lots plus (net_quantity, oversold_quantity)."""
    queue: deque[list[Any]] = deque()  # [remaining_qty, unit_cost, fill]
    oversold = Decimal("0")
    for fill in sorted(authoritative, key=lambda f: (f.filled_at, f.id)):
        if fill.quantity <= 0:
            continue
        if fill.side == "buy":
            queue.append([fill.quantity, fill.price, fill])
            continue
        if fill.side != "sell":
            continue
        remaining = fill.quantity
        while remaining > 0 and queue:
            head = queue[0]
            matched = min(remaining, head[0])
            head[0] -= matched
            remaining -= matched
            if head[0] <= 0:
                queue.popleft()
        oversold += remaining
    lots: list[dict[str, Any]] = []
    net = Decimal("0")
    for qty, unit_cost, fill in queue:
        net += qty
        lots.append(
            {
                "opened_at": _iso(fill.filled_at),
                "quantity": _fmt(qty),
                "unit_cost": _fmt_price(unit_cost),
                "origin": (
                    "opening_seed" if fill.source == "manual_import" else "fill"
                ),
                "source": fill.source,
                "broker_order_id": fill.broker_order_id,
                "_qty": qty,
                "_cost": unit_cost,
            }
        )
    return lots, net, oversold


def _signed_net(fills: Sequence[LedgerFill]) -> Decimal:
    return sum(
        (f.quantity if f.side == "buy" else -f.quantity for f in fills), Decimal("0")
    )


def _fill_view(fill: LedgerFill) -> dict[str, Any]:
    return {
        "broker_order_id": fill.broker_order_id,
        "side": fill.side,
        "quantity": _fmt(fill.quantity),
        "price": _fmt_price(fill.price),
        "filled_at": _iso(fill.filled_at),
        "source": fill.source,
        "provisional": fill.source == _PROVISIONAL_SOURCE,
    }


def _order_view(order: OrderRow) -> dict[str, Any]:
    return {
        "order_no": order.order_no,
        "status": order.status,
        "quantity": _fmt(order.quantity),
        "price": _fmt_price(order.price),
        "trade_date": _iso(order.trade_date),
    }


def _open_buy_evidence(
    *,
    fills: Sequence[LedgerFill],
    orders: Sequence[OrderRow] | None,
    freshness: Freshness,
    now: datetime,
) -> dict[str, Any]:
    """S2 + S3: own non-terminal buys / same-day buy fills. Unknown => blocking."""
    unknown: list[str] = []
    if freshness.state == "missing":
        unknown.append(UNKNOWN_NO_RECONCILE_RUN)
    elif freshness.state == "stale":
        unknown.append(UNKNOWN_LEDGER_STALE)
    if orders is None:
        unknown.append(UNKNOWN_ORDER_LEDGER_READ_FAILED)

    open_buys: list[OrderRow] = []
    prior_day_dead: list[OrderRow] = []
    resolved_order_ids: set[str] = set()
    for order in orders or ():
        terminal = order.status in TERMINAL_ORDER_STATUSES
        if _from_today_kst(order.trade_date, now):
            if not terminal:
                open_buys.append(order)
            elif order.order_no:
                resolved_order_ids.add(_norm_order_id(order.order_no))
        elif not terminal:
            prior_day_dead.append(order)

    # Authoritative rows plus provisional rows no authoritative row covers:
    # websocket duplicates of a reconciled order are not counted twice. Seeded
    # opening lots (manual_import) are position snapshots, not orders.
    authoritative, provisional, _ = _split_provisional(fills)
    unproven_fills = [
        f
        for f in (*authoritative, *provisional)
        if f.source != "manual_import"
        and f.side == "buy"
        and _from_today_kst(f.filled_at, now)
        and _norm_order_id(f.broker_order_id) not in resolved_order_ids
    ]

    reasons: list[str] = []
    if unknown:
        reasons.append(BLOCK_EVIDENCE_UNKNOWN)
    if open_buys:
        reasons.append(BLOCK_OWN_OPEN_BUY)
    if unproven_fills:
        reasons.append(BLOCK_SAME_DAY_FILL)
    return {
        "state": "unknown" if unknown else "known",
        "unknown_reasons": unknown,
        "blocking": bool(reasons),
        "blocking_reasons": reasons,
        "kis_live_order_ledger_open_buys": [_order_view(o) for o in open_buys],
        "same_day_buy_fills_unproven_complete": [_fill_view(f) for f in unproven_fills],
        "presumed_dead_prior_day_buys": [_order_view(o) for o in prior_day_dead],
        "scope": "orders_known_to_auto_trader_only",
        "external_orders_verifiable": False,
    }


def _same_day_sell_evidence(
    *, fills: Sequence[LedgerFill], freshness: Freshness, now: datetime
) -> dict[str, Any]:
    """Same-KST-day sell fills for the symbol (opposite-side visibility).

    Read-only evidence for the same-day chain / wash check: any same-day sell
    fill in the ledger (authoritative, or a provisional websocket row no
    authoritative row covers) blocks, and an unverifiable ledger blocks too.
    Sells placed outside auto_trader are visible here only once they fill.
    """
    unknown: list[str] = []
    if freshness.state == "missing":
        unknown.append(UNKNOWN_NO_RECONCILE_RUN)
    elif freshness.state == "stale":
        unknown.append(UNKNOWN_LEDGER_STALE)
    authoritative, provisional, _ = _split_provisional(fills)
    sells = [
        f
        for f in (*authoritative, *provisional)
        if f.source != "manual_import"
        and f.side == "sell"
        and _from_today_kst(f.filled_at, now)
    ]
    reasons: list[str] = []
    if unknown:
        reasons.append(BLOCK_SELL_EVIDENCE_UNKNOWN)
    if sells:
        reasons.append(BLOCK_SAME_DAY_SELL_FILL)
    return {
        "state": "unknown" if unknown else "known",
        "unknown_reasons": unknown,
        "blocking": bool(reasons),
        "blocking_reasons": reasons,
        "fills": [_fill_view(f) for f in sells],
        "scope": "orders_known_to_auto_trader_only",
    }


def build_symbol_block(
    *,
    symbol: str,
    reference_quantity: Decimal | None,
    current_price: Decimal | None,
    fills: Sequence[LedgerFill],
    orders: Sequence[OrderRow] | None,
    freshness: Freshness,
    now: datetime,
) -> dict[str, Any]:
    """Pure projection of one symbol. Deterministic given its inputs."""
    authoritative, provisional, superseded = _split_provisional(fills)
    # Only authoritative rows reach lots/net. provisional_net below is a
    # diagnostic sum of un-superseded websocket rows: it can add a reason code
    # and fill diagnostics, but it never enters lots, net or the known decision.
    lots, net, oversold = _fifo_lots(authoritative)
    provisional_net = _signed_net(provisional)

    reasons: list[str] = []
    if freshness.state == "missing":
        reasons.append(UNKNOWN_NO_RECONCILE_RUN)
    elif freshness.state == "stale":
        reasons.append(UNKNOWN_LEDGER_STALE)
    if not authoritative:
        reasons.append(
            UNKNOWN_ONLY_PROVISIONAL_ROWS if provisional else UNKNOWN_NO_LEDGER_ROWS
        )
    if oversold > 0:
        reasons.append(UNKNOWN_HISTORY_GAP)

    reconciles: bool | None
    if reference_quantity is None or reference_quantity <= 0:
        reasons.append(UNKNOWN_REFERENCE_MISSING)
        reconciles = None
    else:
        reconciles = net == reference_quantity
        if not reconciles:
            # The broker-quantity cross-check is applied to every symbol, so a
            # fill that exists only as a websocket row (net < broker quantity)
            # ends unknown here, never as a lower lot count.
            reasons.append(UNKNOWN_QTY_MISMATCH)
            if provisional and net + provisional_net == reference_quantity:
                reasons.append(UNKNOWN_PROVISIONAL_PENDING)

    known = not reasons
    weighted_cost: Decimal | None = None
    weighted_pnl_pct: Decimal | None = None
    out_lots: list[dict[str, Any]] | None = None
    if known:
        total_qty = sum((lot["_qty"] for lot in lots), Decimal("0"))
        weighted_cost = (
            sum((lot["_qty"] * lot["_cost"] for lot in lots), Decimal("0")) / total_qty
        )
        out_lots = []
        for lot in lots:
            pnl = None
            if current_price is not None and current_price > 0 and lot["_cost"] > 0:
                pnl = (current_price - lot["_cost"]) / lot["_cost"] * Decimal("100")
            public = {k: v for k, v in lot.items() if not k.startswith("_")}
            public["unrealized_pnl_pct"] = (
                format(pnl.quantize(Decimal("0.01")), "f") if pnl is not None else None
            )
            out_lots.append(public)
        if current_price is not None and current_price > 0 and weighted_cost > 0:
            weighted_pnl_pct = (
                (current_price - weighted_cost) / weighted_cost * Decimal("100")
            )

    return {
        "source": "execution_ledger",
        "symbol": symbol,
        "ledger_state": "known" if known else "unknown",
        "unknown_reasons": reasons,
        "cost_method": COST_METHOD,
        "cost_basis_note": COST_BASIS_NOTE,
        "lots": out_lots,
        "net_quantity": _fmt(net) if known else None,
        "weighted_avg_cost": _fmt_price(weighted_cost),
        "weighted_unrealized_pnl_pct": (
            format(weighted_pnl_pct.quantize(Decimal("0.01")), "f")
            if weighted_pnl_pct is not None
            else None
        ),
        "as_of": _iso(freshness.last_reconcile_finished_at),
        "freshness": {
            "state": freshness.state,
            "last_kis_reconcile_finished_at": _iso(
                freshness.last_reconcile_finished_at
            ),
            "lag_minutes": freshness.lag_minutes,
            "fresh_max_minutes": FRESH_MAX_MINUTES,
        },
        "reference_quantity": _fmt(reference_quantity),
        "quantity_reconciles": reconciles,
        "diagnostics": {
            "authoritative_row_count": len(authoritative),
            "ledger_net_quantity": _fmt(net),
            "oversold_quantity": _fmt(oversold),
            "provisional_row_count": len(provisional),
            "provisional_net_quantity": _fmt(provisional_net),
            "superseded_websocket_duplicates": superseded,
        },
        "provisional_rows_excluded": [_fill_view(f) for f in provisional],
        "open_buy_evidence": _open_buy_evidence(
            fills=fills, orders=orders, freshness=freshness, now=now
        ),
        "same_day_sell_evidence": _same_day_sell_evidence(
            fills=fills, freshness=freshness, now=now
        ),
    }


def unknown_block(symbol: str, reason: str) -> dict[str, Any]:
    """Block for a symbol whose ledger read failed outright (fail-closed)."""
    return {
        "source": "execution_ledger",
        "symbol": symbol,
        "ledger_state": "unknown",
        "unknown_reasons": [reason],
        "cost_method": COST_METHOD,
        "cost_basis_note": COST_BASIS_NOTE,
        "lots": None,
        "net_quantity": None,
        "weighted_avg_cost": None,
        "weighted_unrealized_pnl_pct": None,
        "as_of": None,
        "freshness": {
            "state": "missing",
            "last_kis_reconcile_finished_at": None,
            "lag_minutes": None,
            "fresh_max_minutes": FRESH_MAX_MINUTES,
        },
        "reference_quantity": None,
        "quantity_reconciles": None,
        "diagnostics": None,
        "provisional_rows_excluded": [],
        "open_buy_evidence": {
            "state": "unknown",
            "unknown_reasons": [reason],
            "blocking": True,
            "blocking_reasons": [BLOCK_EVIDENCE_UNKNOWN],
            "kis_live_order_ledger_open_buys": [],
            "same_day_buy_fills_unproven_complete": [],
            "presumed_dead_prior_day_buys": [],
            "scope": "orders_known_to_auto_trader_only",
            "external_orders_verifiable": False,
        },
        "same_day_sell_evidence": {
            "state": "unknown",
            "unknown_reasons": [reason],
            "blocking": True,
            "blocking_reasons": [BLOCK_SELL_EVIDENCE_UNKNOWN],
            "fills": [],
            "scope": "orders_known_to_auto_trader_only",
        },
    }


def to_decimal(value: Any) -> Decimal | None:
    """Best-effort Decimal (float via str); ``None`` for empty/invalid/non-finite."""
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


async def load_kis_live_kr_lot_blocks(
    db: AsyncSession,
    refs: Sequence[PositionRef],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Read the ledger once for ``refs`` and project every symbol.

    Raises on a failed fills/reconcile-run read (the caller degrades every
    position to ``ledger_state="unknown"``). A failed order-ledger read only
    degrades ``open_buy_evidence`` to unknown for all symbols.
    """
    moment = now or datetime.now(UTC)
    symbols = sorted({ref.symbol for ref in refs})
    if not symbols:
        return {}

    repo = ExecutionLedgerRepository(db)
    latest = await repo.latest_run_per_broker()
    run = latest.get("kis")
    freshness = compute_freshness(run.finished_at if run else None, moment)

    fill_rows = (
        (
            await db.execute(
                select(ExecutionLedger)
                .where(ExecutionLedger.broker == "kis")
                .where(ExecutionLedger.account_mode == "live")
                .where(ExecutionLedger.instrument_type == "equity_kr")
                .where(ExecutionLedger.currency == "KRW")
                .where(ExecutionLedger.symbol.in_(symbols))
                .order_by(ExecutionLedger.filled_at.asc(), ExecutionLedger.id.asc())
            )
        )
        .scalars()
        .all()
    )
    fills_by_symbol: dict[str, list[LedgerFill]] = {s: [] for s in symbols}
    for row in fill_rows:
        fills_by_symbol[row.symbol].append(
            LedgerFill(
                id=int(row.id),
                source=row.source,
                side=row.side,
                quantity=Decimal(row.filled_qty),
                price=Decimal(row.filled_price),
                filled_at=row.filled_at,
                broker_order_id=row.broker_order_id,
            )
        )

    orders_by_symbol: dict[str, list[OrderRow]] | None
    try:
        window_start, _ = kst_day_window(moment)
        order_rows = (
            (
                await db.execute(
                    select(KISLiveOrderLedger)
                    .where(KISLiveOrderLedger.broker == "kis")
                    .where(KISLiveOrderLedger.account_mode == "kis_live")
                    .where(KISLiveOrderLedger.side == "buy")
                    .where(KISLiveOrderLedger.symbol.in_(symbols))
                    .where(
                        KISLiveOrderLedger.trade_date
                        >= window_start - _PRIOR_DAY_LOOKBACK
                    )
                    .order_by(KISLiveOrderLedger.id.asc())
                )
            )
            .scalars()
            .all()
        )
        orders_by_symbol = {s: [] for s in symbols}
        for row in order_rows:
            orders_by_symbol[row.symbol].append(
                OrderRow(
                    id=int(row.id),
                    order_no=row.order_no,
                    status=row.status,
                    quantity=to_decimal(row.quantity),
                    price=to_decimal(row.price),
                    trade_date=row.trade_date,
                )
            )
    except Exception:  # noqa: BLE001 — read-only evidence degrades, never raises
        logger.warning("kis_live_order_ledger read failed", exc_info=True)
        await db.rollback()
        orders_by_symbol = None

    return {
        ref.symbol: build_symbol_block(
            symbol=ref.symbol,
            reference_quantity=ref.reference_quantity,
            current_price=ref.current_price,
            fills=fills_by_symbol[ref.symbol],
            orders=None if orders_by_symbol is None else orders_by_symbol[ref.symbol],
            freshness=freshness,
            now=moment,
        )
        for ref in refs
    }
