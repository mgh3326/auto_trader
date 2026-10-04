"""Task #963 — KIS live KR lots and own-open-buy evidence from the execution ledger.

Read-only and DB-only: this module never touches a broker and never writes. It
backs the opt-in ``include_ledger_lots`` block of ``get_holdings`` (MCP layer:
``app/mcp_server/tooling/portfolio_ledger_lots.py``).

Two questions, both answered fail-closed:

* **Lots.** FIFO remaining lots over *authoritative* ``execution_ledger`` rows
  (``reconciler`` / ``manual_import``) for one KIS live KR symbol. Provisional
  ``websocket`` rows are never counted; they are listed separately. When a
  symbol carries an opening seed (a ``manual_import`` ``SEED-*`` row written by
  ``scripts/seed_execution_ledger_opening_lots.py``), the latest seed governs:
  every other authoritative row stamped strictly before that seed's
  ``filled_at`` instant is already inside the seed quantity and is superseded
  (``diagnostics["pre_seed_rows_superseded"]``) instead of counted twice —
  including any older seed generation left behind by a re-seed. The cutover
  comparison is the seed's own: ``filled_at`` (timestamptz, compared as a
  UTC-aware instant); the seeder carved out ``filled_at >= cutover``
  (``ExecutionLedgerRepository.net_quantity_by_match_key_since``), so a row
  stamped exactly at the cutover instant was never inside the seed and still
  counts. The cost is
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
* **Own open sell evidence (task #1087, the sell-side twin).** Same-KST-day
  non-terminal *sell* rows in the order ledger, same-day sell fills whose order
  the order ledger has not proven complete, and ``same_day_buy_evidence`` (the
  opposite-side view for a sell). ``sellable_by_ledger`` is the known lot net
  minus the order quantity of own non-terminal sells today, clamped to
  ``[0, broker quantity]``; it is ``None`` whenever any input is unknown. It is
  a ceiling, not a permission: the gate is ``open_sell_evidence`` and
  ``same_day_buy_evidence`` both non-blocking.

Task #1173 extends the same block to KIS live **US** positions (``equity_us``,
KIS overseas) through ``load_kis_live_us_lot_blocks``. The projection is the
same function with ``market="us"``; only the inputs differ:

* fills are ``execution_ledger`` rows with ``instrument_type='equity_us'`` and
  ``currency='USD'``; an authoritative row whose venue is not exactly one of
  the KIS overseas order exchange codes ``NASD``/``NYSE``/``AMEX`` (``NASDAQ``,
  ``NAS``, ``krx`` ...) is never counted and turns the block unknown
  (``unrecognized_us_venue_rows``) instead of being dropped silently;
* own orders come from ``review.live_order_ledger`` (``broker='kis'``,
  ``account_scope='kis_live'``, ``market='us'``; ROB-407) plus any
  ``review.kis_live_order_ledger`` row with ``instrument_type='equity_us'``;
* "today" is the US trading date, which rolls over at 20:00 America/New_York
  (after-hours close; a KIS daytime-session order placed after it belongs to
  the next US date), not the KST calendar day;
* freshness is the same KIS reconcile run: one run fetches ``kr,us`` and a
  failed US fetch fails the whole run, so a successful run covers both.

Quarantined rows (#1175) never reach any view: every fills read ANDs
``execution_ledger_in_effect()``. Accept-notice phantoms that are not (yet)
quarantined are ``websocket`` rows, and a websocket row never reaches lots,
net quantity, the broker-quantity reconciliation or ``sellable_by_ledger``.
In the same-day evidence views a websocket row can only add blocking, never
remove it, because an accept notice still names an order placed today.

The #678 harness denial of ``kis_live_get_order_history`` is untouched: nothing
here calls a KIS order read.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol, to_kis_symbol, to_yahoo_symbol
from app.core.timezone import kst_day_window
from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.review import KISLiveOrderLedger, LiveOrderLedger
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
# Opening seeds written by scripts/seed_execution_ledger_opening_lots.py carry
# this broker_order_id prefix (opening_lots._seed_order_id). The prefix gate
# keeps a hypothetical non-seed manual_import row from silently becoming a
# cutover it never computed.
_SEED_ORDER_ID_PREFIX = "SEED-"
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
# Task #1087 — sell-side twins.
BLOCK_OWN_OPEN_SELL = "own_nonterminal_sell_order_today"
BLOCK_SAME_DAY_SELL_FILL_UNPROVEN = "same_day_sell_fill_order_not_proven_complete"
BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN = "open_sell_evidence_unknown"
BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER = "same_day_buy_fill_in_ledger"
BLOCK_BUY_EVIDENCE_UNKNOWN = "same_day_buy_evidence_unknown"

SELLABLE_METHOD = "ledger_net_minus_own_open_sell_orders_today"
SELLABLE_UNKNOWN_LEDGER_STATE = "ledger_state_unknown"
SELLABLE_UNKNOWN_OPEN_SELL_EVIDENCE = "open_sell_evidence_unknown"
SELLABLE_UNKNOWN_OPEN_SELL_QUANTITY = "own_open_sell_quantity_unknown"

# Task #1173 — KIS live US. The KIS overseas order history (the reconciler's
# source) and the overseas balance (the opening seed's source) both name the
# US exchange with these exact codes. Any other spelling on an authoritative
# row is a mapping error to surface, never a row to guess about.
US_VENUES = frozenset({"NASD", "NYSE", "AMEX"})
UNKNOWN_UNRECOGNIZED_VENUE = "unrecognized_us_venue_rows"
US_CURRENCY = "USD"
_US_EASTERN = ZoneInfo("America/New_York")
# The US trading date rolls over at the after-hours close. KIS daytime-session
# orders (10:00 KST = 20:00 EST / 21:00 EDT) placed after it trade on the next
# US date, and no KIS US day order outlives the rollover of its own date.
_US_TRADING_DATE_ROLLOVER = time(20, 0)
US_TRADING_DAY_BASIS = "us_trading_date_rolls_over_at_20_00_america_new_york"
US_ORDER_LEDGER_SOURCES = ("review.live_order_ledger", "review.kis_live_order_ledger")

MarketCode = Literal["kr", "us"]
FreshnessState = Literal["fresh", "stale", "missing"]


@dataclass(frozen=True, slots=True)
class LedgerFill:
    """Projection of one ``review.execution_ledger`` row (KIS live KR or US)."""

    id: int
    source: str
    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    broker_order_id: str
    venue: str = ""


@dataclass(frozen=True, slots=True)
class OrderRow:
    """Projection of one own-order ledger buy or sell row.

    KR: ``review.kis_live_order_ledger``. US: ``review.live_order_ledger`` (and
    any ``equity_us`` row of ``review.kis_live_order_ledger``).
    """

    id: int
    order_no: str | None
    status: str
    quantity: Decimal | None
    price: Decimal | None
    trade_date: datetime
    side: str


@dataclass(frozen=True, slots=True)
class PositionRef:
    """A broker-held KIS live KR or US position the caller already has in hand."""

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


def _aware_utc(when: datetime) -> datetime:
    """Aware comparison instant; a naive stamp reads as UTC (timestamptz)."""
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def us_trading_day_window(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the US trading date containing ``now``, as UTC instants.

    The date rolls over at 20:00 America/New_York (DST-aware), so a regular or
    after-hours order belongs to its own ET date and a KIS daytime-session order
    placed in the KST morning belongs to the next one.
    """
    local = _aware_utc(now).astimezone(_US_EASTERN)
    date = local.date()
    if local.time() >= _US_TRADING_DATE_ROLLOVER:
        date += timedelta(days=1)
    start = datetime.combine(
        date - timedelta(days=1), _US_TRADING_DATE_ROLLOVER, tzinfo=_US_EASTERN
    )
    end = datetime.combine(date, _US_TRADING_DATE_ROLLOVER, tzinfo=_US_EASTERN)
    return start.astimezone(UTC), end.astimezone(UTC)


def _day_start(market: MarketCode, now: datetime) -> datetime:
    """Start of "today": the KST day for KR, the US trading date for US."""
    if market == "us":
        return us_trading_day_window(now)[0]
    start, _ = kst_day_window(now)
    return start


def _from_today(when: datetime, day_start: datetime) -> bool:
    """True for the current day and for anything dated later than today.

    A later-dated row can only come from clock skew between writers; treating it
    as "today" keeps the evidence fail-closed instead of presuming it dead.
    """
    return _aware_utc(when) >= day_start


def _is_opening_seed(fill: LedgerFill) -> bool:
    """A ``manual_import`` ``SEED-*`` row: a position snapshot, not a fill.

    Same rule as the seed cutover. A ``manual_import`` row without the prefix
    is an authoritative actual fill and counts as same-day evidence.
    """
    return fill.source == "manual_import" and fill.broker_order_id.startswith(
        _SEED_ORDER_ID_PREFIX
    )


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


def _apply_seed_cutover(
    authoritative: Sequence[LedgerFill],
) -> tuple[list[LedgerFill], list[LedgerFill], datetime | None]:
    """Drop authoritative rows the latest opening seed already covers.

    The seed CLI writes one ``manual_import`` row per match key whose
    ``broker_order_id`` is ``SEED-<yyyymmdd>-...`` and whose ``filled_at`` is
    exactly the ``--cutover`` instant (UTC-midnight-aware;
    ``scripts/seed_execution_ledger_opening_lots.parse_cutover`` and
    ``opening_lots.py`` ``filled_at=cutover``). Its quantity is
    ``current_qty - net(filled_at >= cutover)`` over non-seed rows
    (``ExecutionLedgerRepository.net_quantity_by_match_key_since``), so the
    seed already represents every row strictly before that instant: counting
    them again double-counts the position the seed absorbed. The mirror
    complement is exact: a row stamped exactly at the cutover instant was
    carved out of the seed and still counts.

    A re-seed at a newer cutover inserts a second ``SEED-*`` row (the order id
    embeds the cutover date, so the unique key differs — the old row is not
    updated). The latest seed governs: its ``filled_at`` is the cutover, the
    seed rows at that instant count, and every other authoritative row with
    ``filled_at < cutover`` is superseded. Symbols with no seed row are
    returned unchanged.
    """
    cutovers = [
        _aware_utc(f.filled_at)
        for f in authoritative
        if f.source == "manual_import"
        and f.broker_order_id.startswith(_SEED_ORDER_ID_PREFIX)
    ]
    if not cutovers:
        return list(authoritative), [], None
    cutover = max(cutovers)
    counted = [f for f in authoritative if _aware_utc(f.filled_at) >= cutover]
    superseded = [f for f in authoritative if _aware_utc(f.filled_at) < cutover]
    return counted, superseded, cutover


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
    day_start: datetime,
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
        if order.side != "buy":
            continue
        terminal = order.status in TERMINAL_ORDER_STATUSES
        if _from_today(order.trade_date, day_start):
            if not terminal:
                open_buys.append(order)
            elif order.order_no:
                resolved_order_ids.add(_norm_order_id(order.order_no))
        elif not terminal:
            prior_day_dead.append(order)

    # Authoritative rows plus provisional rows no authoritative row covers:
    # websocket duplicates of a reconciled order are not counted twice. Opening
    # seeds (manual_import SEED-*) are position snapshots, not orders; any other
    # manual_import row is an actual fill (task #1087 round 1).
    authoritative, provisional, _ = _split_provisional(fills)
    unproven_fills = [
        f
        for f in (*authoritative, *provisional)
        if not _is_opening_seed(f)
        and f.side == "buy"
        and _from_today(f.filled_at, day_start)
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
    *, fills: Sequence[LedgerFill], freshness: Freshness, day_start: datetime
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
        if not _is_opening_seed(f)
        and f.side == "sell"
        and _from_today(f.filled_at, day_start)
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


def _open_sell_evidence(
    *,
    fills: Sequence[LedgerFill],
    orders: Sequence[OrderRow] | None,
    freshness: Freshness,
    day_start: datetime,
) -> dict[str, Any]:
    """Task #1087 — own non-terminal sells / same-day sell fills. Unknown => blocking.

    The sell-side twin of ``_open_buy_evidence``: a same-KST-day non-terminal
    own sell row, a same-day sell fill whose order the order ledger has not
    proven terminal (authoritative, or a provisional websocket row no
    authoritative row covers), a stale/missing reconcile or an unreadable order
    ledger each block. ``own_open_sell_order_quantity`` sums the *order*
    quantity (not the unfilled remainder) of today's non-terminal own sells, so
    it can only overstate what is already committed; it is ``None`` when the
    evidence is unknown or any such order has no quantity.
    """
    unknown: list[str] = []
    if freshness.state == "missing":
        unknown.append(UNKNOWN_NO_RECONCILE_RUN)
    elif freshness.state == "stale":
        unknown.append(UNKNOWN_LEDGER_STALE)
    if orders is None:
        unknown.append(UNKNOWN_ORDER_LEDGER_READ_FAILED)

    open_sells: list[OrderRow] = []
    prior_day_dead: list[OrderRow] = []
    resolved_order_ids: set[str] = set()
    for order in orders or ():
        if order.side != "sell":
            continue
        terminal = order.status in TERMINAL_ORDER_STATUSES
        if _from_today(order.trade_date, day_start):
            if not terminal:
                open_sells.append(order)
            elif order.order_no:
                resolved_order_ids.add(_norm_order_id(order.order_no))
        elif not terminal:
            prior_day_dead.append(order)

    authoritative, provisional, _ = _split_provisional(fills)
    unproven_fills = [
        f
        for f in (*authoritative, *provisional)
        if not _is_opening_seed(f)
        and f.side == "sell"
        and _from_today(f.filled_at, day_start)
        and _norm_order_id(f.broker_order_id) not in resolved_order_ids
    ]

    open_quantity: Decimal | None = None
    if not unknown and all(
        o.quantity is not None and o.quantity >= 0 for o in open_sells
    ):
        open_quantity = sum(
            (o.quantity for o in open_sells if o.quantity is not None), Decimal("0")
        )

    reasons: list[str] = []
    if unknown:
        reasons.append(BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN)
    if open_sells:
        reasons.append(BLOCK_OWN_OPEN_SELL)
    if unproven_fills:
        reasons.append(BLOCK_SAME_DAY_SELL_FILL_UNPROVEN)
    return {
        "state": "unknown" if unknown else "known",
        "unknown_reasons": unknown,
        "blocking": bool(reasons),
        "blocking_reasons": reasons,
        "kis_live_order_ledger_open_sells": [_order_view(o) for o in open_sells],
        "own_open_sell_order_quantity": _fmt(open_quantity),
        "same_day_sell_fills_unproven_complete": [
            _fill_view(f) for f in unproven_fills
        ],
        "presumed_dead_prior_day_sells": [_order_view(o) for o in prior_day_dead],
        "scope": "orders_known_to_auto_trader_only",
        "external_orders_verifiable": False,
        "_open_quantity": open_quantity,
    }


def _same_day_buy_evidence(
    *, fills: Sequence[LedgerFill], freshness: Freshness, day_start: datetime
) -> dict[str, Any]:
    """Task #1087 — same-KST-day buy fills (the opposite-side view for a sell).

    The twin of ``_same_day_sell_evidence``: any same-day buy fill in the ledger
    (authoritative, or a provisional websocket row no authoritative row covers)
    blocks, and an unverifiable ledger blocks too. Opening seeds (``SEED-*``)
    are position snapshots, not buys; other ``manual_import`` rows are fills. Buys placed outside auto_trader are visible here only
    once they fill.
    """
    unknown: list[str] = []
    if freshness.state == "missing":
        unknown.append(UNKNOWN_NO_RECONCILE_RUN)
    elif freshness.state == "stale":
        unknown.append(UNKNOWN_LEDGER_STALE)
    authoritative, provisional, _ = _split_provisional(fills)
    buys = [
        f
        for f in (*authoritative, *provisional)
        if not _is_opening_seed(f)
        and f.side == "buy"
        and _from_today(f.filled_at, day_start)
    ]
    reasons: list[str] = []
    if unknown:
        reasons.append(BLOCK_BUY_EVIDENCE_UNKNOWN)
    if buys:
        reasons.append(BLOCK_SAME_DAY_BUY_FILL_IN_LEDGER)
    return {
        "state": "unknown" if unknown else "known",
        "unknown_reasons": unknown,
        "blocking": bool(reasons),
        "blocking_reasons": reasons,
        "fills": [_fill_view(f) for f in buys],
        "scope": "orders_known_to_auto_trader_only",
    }


def _sellable_by_ledger(
    *,
    known: bool,
    net: Decimal,
    reference_quantity: Decimal | None,
    open_sell: dict[str, Any],
) -> tuple[Decimal | None, dict[str, Any]]:
    """Task #1087 — known lot net minus own open sell orders, clamped to [0, broker].

    ``None`` unless the lot projection is known (fresh, reconciling with the
    broker quantity) AND the open-sell evidence is known with a quantity for
    every own open sell. Provisional websocket rows never reach ``net``.
    """
    reasons: list[str] = []
    if not known:
        reasons.append(SELLABLE_UNKNOWN_LEDGER_STATE)
    if open_sell["state"] != "known":
        reasons.append(SELLABLE_UNKNOWN_OPEN_SELL_EVIDENCE)
    elif open_sell["_open_quantity"] is None:
        reasons.append(SELLABLE_UNKNOWN_OPEN_SELL_QUANTITY)
    open_quantity: Decimal | None = open_sell["_open_quantity"]
    value: Decimal | None = None
    clamped = False
    if not reasons and open_quantity is not None and reference_quantity is not None:
        raw = net - open_quantity
        value = min(max(raw, Decimal("0")), reference_quantity, net)
        clamped = value != raw
    return value, {
        "state": "known" if value is not None else "unknown",
        "unknown_reasons": reasons,
        "method": SELLABLE_METHOD,
        "ledger_net_quantity": _fmt(net) if known else None,
        "own_open_sell_order_quantity": _fmt(open_quantity),
        "reference_quantity": _fmt(reference_quantity),
        "clamped": clamped,
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
    market: MarketCode = "kr",
) -> dict[str, Any]:
    """Pure projection of one symbol. Deterministic given its inputs.

    ``market="us"`` (task #1173) changes only the day boundary (US trading
    date), adds the venue check on authoritative rows and appends the US-only
    keys; the KR block is unchanged.
    """
    day_start = _day_start(market, now)
    authoritative, provisional, superseded = _split_provisional(fills)
    # US: an authoritative row on an unrecognized venue is never counted. It
    # stays visible to the evidence views below (where it can only block) and
    # makes the block unknown, so a NASDAQ/NAS/krx mapping error is loud.
    off_venue = (
        [f for f in authoritative if f.venue.strip().upper() not in US_VENUES]
        if market == "us"
        else []
    )
    if off_venue:
        off_ids = {f.id for f in off_venue}
        authoritative = [f for f in authoritative if f.id not in off_ids]
    # Rows an opening seed already absorbed never reach lots/net; they are
    # listed under diagnostics.pre_seed_rows_superseded instead. Seedless
    # symbols pass through unchanged.
    counted, pre_seed_superseded, seed_cutover = _apply_seed_cutover(authoritative)
    # Only authoritative rows reach lots/net. provisional_net below is a
    # diagnostic sum of un-superseded websocket rows: it can add a reason code
    # and fill diagnostics, but it never enters lots, net or the known decision.
    lots, net, oversold = _fifo_lots(counted)
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
    if off_venue:
        reasons.append(UNKNOWN_UNRECOGNIZED_VENUE)

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

    open_sell = _open_sell_evidence(
        fills=fills, orders=orders, freshness=freshness, day_start=day_start
    )
    sellable, sellable_basis = _sellable_by_ledger(
        known=known,
        net=net,
        reference_quantity=reference_quantity,
        open_sell=open_sell,
    )
    open_sell_public = {k: v for k, v in open_sell.items() if not k.startswith("_")}

    block: dict[str, Any] = {
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
            "counted_row_count": len(counted),
            "ledger_net_quantity": _fmt(net),
            "oversold_quantity": _fmt(oversold),
            "provisional_row_count": len(provisional),
            "provisional_net_quantity": _fmt(provisional_net),
            "superseded_websocket_duplicates": superseded,
            "seed_cutover": _iso(seed_cutover),
            "pre_seed_rows_superseded": [_fill_view(f) for f in pre_seed_superseded],
        },
        "provisional_rows_excluded": [_fill_view(f) for f in provisional],
        "open_buy_evidence": _open_buy_evidence(
            fills=fills, orders=orders, freshness=freshness, day_start=day_start
        ),
        "same_day_sell_evidence": _same_day_sell_evidence(
            fills=fills, freshness=freshness, day_start=day_start
        ),
        "open_sell_evidence": open_sell_public,
        "same_day_buy_evidence": _same_day_buy_evidence(
            fills=fills, freshness=freshness, day_start=day_start
        ),
        "sellable_by_ledger": _fmt(sellable),
        "sellable_by_ledger_basis": sellable_basis,
    }
    if market == "us":
        block["diagnostics"]["unrecognized_venue_rows"] = [
            {**_fill_view(f), "venue": f.venue} for f in off_venue
        ]
        block |= _us_block_keys(day_start)
    return block


def _us_block_keys(day_start: datetime | None) -> dict[str, Any]:
    """Keys only a US block carries (task #1173); a KR block never has them."""
    return {
        "market": "us",
        "currency": US_CURRENCY,
        "accepted_venues": sorted(US_VENUES),
        "trading_day_basis": US_TRADING_DAY_BASIS,
        "trading_day_start": _iso(day_start),
        "order_ledger_sources": list(US_ORDER_LEDGER_SOURCES),
    }


def unknown_block(
    symbol: str, reason: str, *, market: MarketCode = "kr"
) -> dict[str, Any]:
    """Block for a symbol whose ledger read failed outright (fail-closed)."""
    block: dict[str, Any] = {
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
        "open_sell_evidence": {
            "state": "unknown",
            "unknown_reasons": [reason],
            "blocking": True,
            "blocking_reasons": [BLOCK_OPEN_SELL_EVIDENCE_UNKNOWN],
            "kis_live_order_ledger_open_sells": [],
            "own_open_sell_order_quantity": None,
            "same_day_sell_fills_unproven_complete": [],
            "presumed_dead_prior_day_sells": [],
            "scope": "orders_known_to_auto_trader_only",
            "external_orders_verifiable": False,
        },
        "same_day_buy_evidence": {
            "state": "unknown",
            "unknown_reasons": [reason],
            "blocking": True,
            "blocking_reasons": [BLOCK_BUY_EVIDENCE_UNKNOWN],
            "fills": [],
            "scope": "orders_known_to_auto_trader_only",
        },
        "sellable_by_ledger": None,
        "sellable_by_ledger_basis": {
            "state": "unknown",
            "unknown_reasons": [
                SELLABLE_UNKNOWN_LEDGER_STATE,
                SELLABLE_UNKNOWN_OPEN_SELL_EVIDENCE,
            ],
            "method": SELLABLE_METHOD,
            "ledger_net_quantity": None,
            "own_open_sell_order_quantity": None,
            "reference_quantity": None,
            "clamped": False,
        },
    }
    if market == "us":
        block |= _us_block_keys(None)
    return block


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
    degrades ``open_buy_evidence`` and ``open_sell_evidence`` (hence
    ``sellable_by_ledger``) to unknown for all symbols.
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
                .where(execution_ledger_in_effect())
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
                    .where(KISLiveOrderLedger.side.in_(("buy", "sell")))
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
                    side=row.side,
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


def _us_symbol_key(symbol: str) -> str:
    """DB dot-format, upper-case: the key ``get_holdings`` gives a US position."""
    return to_db_symbol(symbol.strip()).upper()


def _us_symbol_spellings(keys: Sequence[str]) -> list[str]:
    """Every spelling a US ledger writer may have stored for these DB symbols.

    The reconciler and the opening seed store the KIS ``pdno`` as received
    (``BRK/B``), the websocket tap the upper-cased raw symbol; rows are mapped
    back with ``_us_symbol_key`` so no spelling is counted under another key.
    """
    return sorted(
        {
            spelling
            for key in keys
            for spelling in (key, to_kis_symbol(key), to_yahoo_symbol(key))
        }
    )


async def load_kis_live_us_lot_blocks(
    db: AsyncSession,
    refs: Sequence[PositionRef],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Task #1173 — the ``load_kis_live_kr_lot_blocks`` twin for KIS live US.

    Same contract: raises on a failed fills/reconcile-run read (the caller
    degrades every US position to unknown); a failed own-order read degrades
    only the open-buy/open-sell evidence (hence ``sellable_by_ledger``). Rows
    are ``broker='kis'``, ``account_mode='live'``, ``equity_us``, ``USD``, not
    quarantined. Venue is checked per row by ``build_symbol_block`` (an
    unrecognized venue is unknown, never silently filtered here). Blocks are
    keyed by the caller's ``ref.symbol``.
    """
    moment = now or datetime.now(UTC)
    keys = sorted({_us_symbol_key(ref.symbol) for ref in refs})
    if not keys:
        return {}
    spellings = _us_symbol_spellings(keys)

    repo = ExecutionLedgerRepository(db)
    latest = await repo.latest_run_per_broker()
    # One KIS reconcile run fetches markets "kr,us" and any US fetch error fails
    # the run (error_summary set), so the latest successful KIS run covers US.
    run = latest.get("kis")
    freshness = compute_freshness(run.finished_at if run else None, moment)

    fill_rows = (
        (
            await db.execute(
                select(ExecutionLedger)
                .where(ExecutionLedger.broker == "kis")
                .where(ExecutionLedger.account_mode == "live")
                .where(ExecutionLedger.instrument_type == "equity_us")
                .where(ExecutionLedger.currency == US_CURRENCY)
                .where(ExecutionLedger.symbol.in_(spellings))
                # #1175: a quarantined row (an accept notice recorded as a
                # fill) is not a fill for any view of this block.
                .where(execution_ledger_in_effect())
                .order_by(ExecutionLedger.filled_at.asc(), ExecutionLedger.id.asc())
            )
        )
        .scalars()
        .all()
    )
    fills_by_key: dict[str, list[LedgerFill]] = {k: [] for k in keys}
    for row in fill_rows:
        bucket = fills_by_key.get(_us_symbol_key(row.symbol))
        if bucket is None:
            continue
        bucket.append(
            LedgerFill(
                id=int(row.id),
                source=row.source,
                side=row.side,
                quantity=Decimal(row.filled_qty),
                price=Decimal(row.filled_price),
                filled_at=row.filled_at,
                broker_order_id=row.broker_order_id,
                venue=row.venue,
            )
        )

    orders_by_key: dict[str, list[OrderRow]] | None
    try:
        window_start = us_trading_day_window(moment)[0] - _PRIOR_DAY_LOOKBACK
        live_rows = (
            (
                await db.execute(
                    select(LiveOrderLedger)
                    .where(LiveOrderLedger.broker == "kis")
                    .where(LiveOrderLedger.account_scope == "kis_live")
                    .where(LiveOrderLedger.market == "us")
                    .where(LiveOrderLedger.side.in_(("buy", "sell")))
                    .where(LiveOrderLedger.symbol.in_(spellings))
                    .where(LiveOrderLedger.trade_date >= window_start)
                    .order_by(LiveOrderLedger.id.asc())
                )
            )
            .scalars()
            .all()
        )
        # KR-only by contract (ROB-395), read anyway: an equity_us row here is
        # still an order auto_trader knows about and must be able to block.
        kis_rows = (
            (
                await db.execute(
                    select(KISLiveOrderLedger)
                    .where(KISLiveOrderLedger.broker == "kis")
                    .where(KISLiveOrderLedger.account_mode == "kis_live")
                    .where(KISLiveOrderLedger.instrument_type == "equity_us")
                    .where(KISLiveOrderLedger.side.in_(("buy", "sell")))
                    .where(KISLiveOrderLedger.symbol.in_(spellings))
                    .where(KISLiveOrderLedger.trade_date >= window_start)
                    .order_by(KISLiveOrderLedger.id.asc())
                )
            )
            .scalars()
            .all()
        )
        orders_by_key = {k: [] for k in keys}
        for row in (*live_rows, *kis_rows):
            bucket = orders_by_key.get(_us_symbol_key(row.symbol))
            if bucket is None:
                continue
            bucket.append(
                OrderRow(
                    id=int(row.id),
                    order_no=row.order_no,
                    status=row.status,
                    quantity=to_decimal(row.quantity),
                    price=to_decimal(row.price),
                    trade_date=row.trade_date,
                    side=row.side,
                )
            )
        for bucket in orders_by_key.values():
            bucket.sort(key=lambda o: (_aware_utc(o.trade_date), o.id))
    except Exception:  # noqa: BLE001 — read-only evidence degrades, never raises
        logger.warning("kis live US order ledger read failed", exc_info=True)
        await db.rollback()
        orders_by_key = None

    blocks: dict[str, dict[str, Any]] = {}
    for ref in refs:
        key = _us_symbol_key(ref.symbol)
        blocks[ref.symbol] = build_symbol_block(
            symbol=ref.symbol,
            reference_quantity=ref.reference_quantity,
            current_price=ref.current_price,
            fills=fills_by_key[key],
            orders=None if orders_by_key is None else orders_by_key[key],
            freshness=freshness,
            now=moment,
            market="us",
        )
    return blocks
