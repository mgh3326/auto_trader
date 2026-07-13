"""ROB-843 — order-history state for the KIS mock scalping final risk gate.

Reads *order-history* facts from ``review.kis_mock_order_ledger`` — daily order
count, realized loss today, and time since the last close/reconcile (cooldown).
It deliberately does **not** infer held positions from order lifecycle rows: an
``accepted``/``pending``/``fill`` order row is an order, not proof of a durable
position (a filled buy that is later sold is flat). The authoritative open
position / position count comes from a fresh KIS mock holdings snapshot in the
risk gate; this module only supplies order-history counters.

Read-only: no writes, no broker/network I/O. Any DB fault propagates so the
gate fail-closes to zero broker mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, func, not_, or_, select

from app.core.timezone import now_kst
from app.models.review import KISMockOrderLedger

_CLOSED_STATE = "reconciled"

# ROB-843 P2: control-only rows that must NEVER be treated as trades by any
# ledger consumer (daily count, retrospective, journal bridge, holdings). The
# durable in-flight/uncertain state now lives in the write-ahead reservation
# (review.order_send_intents), so new code writes no control rows here; this
# predicate defends against any legacy control rows from earlier revisions.
_CONTROL_SYMBOL = "__ledger_tracking__"
_CONTROL_ROLES: frozenset[str] = frozenset({"tracking_degraded", "native_fallback"})
_CONTROL_REASONS: frozenset[str] = frozenset(
    {"ledger_tracking_degraded", "ledger_tracking_fallback"}
)


def is_control_row(row: KISMockOrderLedger) -> bool:
    """True for a non-trade control/reservation/degradation row (ROB-843 P2)."""
    return (
        row.symbol == _CONTROL_SYMBOL
        or (row.scalping_role in _CONTROL_ROLES)
        or (row.reason in _CONTROL_REASONS)
    )


def real_order_filter():
    """SQLAlchemy predicate selecting only genuine order rows (excludes control
    rows). Apply in every KISMockOrderLedger consumer so a control row is never
    counted, retrospected, or journaled as a trade."""
    return and_(
        KISMockOrderLedger.symbol != _CONTROL_SYMBOL,
        or_(
            KISMockOrderLedger.scalping_role.is_(None),
            not_(KISMockOrderLedger.scalping_role.in_(_CONTROL_ROLES)),
        ),
        or_(
            KISMockOrderLedger.reason.is_(None),
            not_(KISMockOrderLedger.reason.in_(_CONTROL_REASONS)),
        ),
    )


@dataclass(frozen=True)
class MockOrderHistory:
    """Order-history counters for the risk gate (NOT position state)."""

    orders_today: int
    realized_loss_today_krw: Decimal
    seconds_since_last_close_for_symbol: float | None


def _start_of_kst_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# Synthetic scalping rows count as durable submission evidence only once they
# represent a real broker outcome (a fill / reconcile / anomaly), never a mere
# audit placeholder.
_SYNTHETIC_EVIDENCE_STATES: frozenset[str] = frozenset(
    {"fill", "reconciled", "anomaly"}
)


async def count_daily_broker_orders(
    db, *, since: datetime, symbol: str | None = None
) -> int:
    """Count actually-submitted broker orders since ``since`` (ROB-843 P1-2).

    A submission is counted once, evidenced by EITHER a native ledger row (real
    broker order id) OR — when the native write was lost — a synthetic
    fill/anomaly/fallback row keyed by ``(correlation_id, side)``. Synthetic
    evidence whose logical order already has a native row (matched by
    ``(correlation_id, side)``) is not double-counted; rows that never reached
    the broker (preview / blocked / pre-submit failure / rejected / id-less
    native, or audit-only synthetic) are excluded.

    The optional ``symbol`` scope is for deterministic per-symbol testing;
    production passes ``None`` (account-wide daily cap).
    """
    native_q = select(
        KISMockOrderLedger.order_no,
        KISMockOrderLedger.correlation_id,
        KISMockOrderLedger.side,
    ).where(
        KISMockOrderLedger.trade_date >= since,
        KISMockOrderLedger.scalping_role.is_(None),
        real_order_filter(),  # never count a control row
    )
    synthetic_q = select(
        KISMockOrderLedger.correlation_id,
        KISMockOrderLedger.side,
    ).where(
        KISMockOrderLedger.trade_date >= since,
        KISMockOrderLedger.scalping_role.is_not(None),
        real_order_filter(),  # excludes tracking_degraded / native_fallback
        KISMockOrderLedger.lifecycle_state.in_(_SYNTHETIC_EVIDENCE_STATES),
    )
    if symbol is not None:
        native_q = native_q.where(KISMockOrderLedger.symbol == symbol)
        synthetic_q = synthetic_q.where(KISMockOrderLedger.symbol == symbol)

    native_ids: set[str] = set()
    native_pairs: set[tuple[str | None, str]] = set()
    for order_no, corr_id, side in (await db.execute(native_q)).all():
        native_pairs.add((corr_id, side))
        if order_no is not None and order_no.strip():
            native_ids.add(order_no.strip())

    synthetic_pairs = {
        (corr_id, side) for corr_id, side in (await db.execute(synthetic_q)).all()
    }
    orphaned = synthetic_pairs - native_pairs
    return len(native_ids) + len(orphaned)


async def load_kis_mock_order_history(
    *, symbol: str, now: datetime | None = None
) -> MockOrderHistory:
    """Read daily order count, realized loss, and cooldown basis for ``symbol``."""
    from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory

    now = now or now_kst()
    since = _start_of_kst_day(now)

    async with _order_session_factory()() as db:
        # ROB-843 P1: durable pre-send fail-close. An UNRESOLVED write-ahead
        # reservation (order_send_intents) means some automated order is
        # in-flight/uncertain — a state that survives restart / a fresh session /
        # a new gate instance — so block every new order until an explicit
        # reconciliation releases it. Never silently undercounts.
        from app.services.order_send_intent_service import (
            KIS_MOCK_SCALPING_SCOPE,
            OrderSendIntentService,
        )

        if await OrderSendIntentService(db).has_reservations(
            account_scope=KIS_MOCK_SCALPING_SCOPE
        ):
            raise RuntimeError("ledger_tracking_unavailable")
        orders_today = await count_daily_broker_orders(db, since=since)
        realized_loss = await db.scalar(
            select(func.coalesce(func.sum(-KISMockOrderLedger.net_pnl), 0)).where(
                KISMockOrderLedger.lifecycle_state == _CLOSED_STATE,
                KISMockOrderLedger.trade_date >= since,
                KISMockOrderLedger.net_pnl < 0,
            )
        )
        # Cooldown anchors on the actual position-closing SELL reconcile time.
        # Only reconciled SELL rows count (BUY/preview/failed/rejected/no-fill
        # are not closes). reconciled_at is the canonical reconcile timestamp;
        # a legacy close row missing it must fail-close, never silently bypass.
        close_ts_rows = (
            await db.execute(
                select(KISMockOrderLedger.reconciled_at).where(
                    KISMockOrderLedger.symbol == symbol,
                    KISMockOrderLedger.side == "sell",
                    KISMockOrderLedger.lifecycle_state == _CLOSED_STATE,
                )
            )
        ).all()

    seconds_since_close: float | None = None
    if close_ts_rows:
        reconciled_ats = [row[0] for row in close_ts_rows]
        if any(ts is None for ts in reconciled_ats):
            raise RuntimeError(
                f"reconciled SELL close for {symbol} is missing reconciled_at; "
                "cannot compute cooldown (fail-close)"
            )
        last_close = max(reconciled_ats)
        seconds_since_close = (now - last_close).total_seconds()

    return MockOrderHistory(
        orders_today=int(orders_today or 0),
        realized_loss_today_krw=Decimal(str(realized_loss or 0)),
        seconds_since_last_close_for_symbol=seconds_since_close,
    )
