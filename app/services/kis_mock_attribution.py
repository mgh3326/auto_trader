"""Pre-submit attribution for the kis_mock lane.

Two jobs, in this order:

1. :func:`resolve_attribution` decides — with no I/O — who owns an order.
   It either produces a complete ``(correlation_id, strategy, signal_source)``
   triple or raises :class:`MissingAttribution`. There is no third outcome and
   no placeholder: "unattributed" is an error, not a value.
2. :func:`record_signal` makes that decision durable *before* the broker send.

The ordering is the whole point. ``review.kis_mock_order_ledger`` previously
minted its ``correlation_id`` inside the response handler, i.e. after the order
POST had already returned, and left ``strategy`` NULL whenever the caller did
not bother to pass one. An order sent by a process that then died — or whose
ledger insert failed — existed at the broker with no attribution anywhere. That
is the ROB-1093 shape: a nullable column does not mean "may be filled in", it
means "passes without being filled in".

Signals that are evaluated and deliberately produce no order are recorded too
(``decision='no_order'``). Measuring a strategy only by the signals that became
orders silently drops the denominator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import cast, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import KISMockSignalLedger
from app.services.live_correlation import live_correlation_id

ACCOUNT_SCOPE = "kis_mock"

# Lane labels derived from unambiguous execution context. These are real
# attribution — each names exactly one code path that owns the order — not
# stand-ins for a missing answer. A context that matches none of them is
# unattributed and must fail closed.
MIRROR_STRATEGY = "mock_counterfactual_mirror"
MIRROR_SIGNAL_SOURCE = "mirror"
DEFAULT_SIGNAL_SOURCE = "manual"

_DECISION_ORDER = "order"
_DECISION_NO_ORDER = "no_order"


class MissingAttribution(Exception):
    """Raised when an order cannot be attributed to a strategy before send.

    Callers must treat this as a hard block: the broker call does not happen.
    """

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            f"kis_mock order is unattributed; refusing to send. missing={list(missing)}"
        )


@dataclass(frozen=True, slots=True)
class KisMockAttribution:
    """A complete attribution triple. Constructed only via resolve_attribution."""

    correlation_id: str
    strategy: str
    signal_source: str


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def mint_correlation_id(
    *,
    symbol: str,
    side: str,
    price: Any,
    quantity: Any,
    kst_trade_day: str | None = None,
    rung: int = 0,
) -> str:
    """Deterministic kis_mock correlation id.

    Identical inputs produce the identical id the post-send path used before
    (same helper, same ``account_scope``), so moving the mint ahead of the send
    does not renumber anything — it only makes the id exist earlier.
    """
    return live_correlation_id(
        account_scope=ACCOUNT_SCOPE,
        symbol=symbol,
        side=side,
        price=_to_decimal(price) or Decimal("0"),
        quantity=_to_decimal(quantity) or Decimal("0"),
        kst_trade_day=kst_trade_day or now_kst().strftime("%Y-%m-%d"),
        rung=rung,
    )


def resolve_attribution(
    *,
    symbol: str,
    side: str,
    price: Any,
    quantity: Any,
    strategy: str | None = None,
    signal_source: str | None = None,
    correlation_id: str | None = None,
    mirror_cohort: str | None = None,
    kst_trade_day: str | None = None,
) -> KisMockAttribution:
    """Resolve the attribution triple, or raise :class:`MissingAttribution`.

    Pure: no DB, no network, no clock beyond the KST trading day used for the
    deterministic id. An explicit ``strategy`` always wins; ``mirror_cohort``
    identifies the counterfactual-mirror lane on its own. Anything else without
    a strategy is unattributable and raises.
    """
    resolved_strategy = _clean(strategy)
    resolved_source = _clean(signal_source)

    if resolved_strategy is None and _clean(mirror_cohort) == "mock_counterfactual":
        resolved_strategy = MIRROR_STRATEGY
        resolved_source = resolved_source or MIRROR_SIGNAL_SOURCE

    missing: list[str] = []
    if resolved_strategy is None:
        missing.append("strategy")
    if missing or resolved_strategy is None:
        raise MissingAttribution(tuple(missing or ("strategy",)))

    return KisMockAttribution(
        correlation_id=_clean(correlation_id)
        or mint_correlation_id(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            kst_trade_day=kst_trade_day,
        ),
        strategy=resolved_strategy,
        signal_source=resolved_source or DEFAULT_SIGNAL_SOURCE,
    )


async def record_signal(
    db: AsyncSession,
    *,
    attribution: KisMockAttribution,
    symbol: str,
    decision: str,
    instrument_type: str | None = None,
    side: str | None = None,
    intended_quantity: Any = None,
    intended_price: Any = None,
    outcome_state: str | None = None,
    suppressed_reason: str | None = None,
    report_item_uuid: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
    kst_date: str | None = None,
) -> int:
    """Persist the signal row and return its id (existing id when replayed).

    Idempotent on ``correlation_id``: a retry of the same decision re-reads the
    durable row instead of inserting a second one. The caller commits nothing
    itself — this function owns the transaction so the row is on disk before
    the caller proceeds to the broker.
    """
    if decision not in (_DECISION_ORDER, _DECISION_NO_ORDER):
        raise ValueError(f"unknown decision {decision!r}")

    resolved_outcome = outcome_state or (
        "recorded" if decision == _DECISION_ORDER else "suppressed"
    )
    stmt = (
        pg_insert(KISMockSignalLedger)
        .values(
            correlation_id=attribution.correlation_id,
            strategy=attribution.strategy,
            signal_source=attribution.signal_source,
            account_mode=ACCOUNT_SCOPE,
            symbol=symbol,
            instrument_type=instrument_type,
            side=side,
            intended_quantity=_to_decimal(intended_quantity),
            intended_price=_to_decimal(intended_price),
            decision=decision,
            outcome_state=resolved_outcome,
            suppressed_reason=suppressed_reason,
            report_item_uuid=report_item_uuid,
            detail=detail or {},
            kst_date=kst_date or now_kst().strftime("%Y-%m-%d"),
        )
        .on_conflict_do_nothing(constraint="uq_kis_mock_signal_ledger_correlation_id")
        .returning(KISMockSignalLedger.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    if inserted is None:
        inserted = await db.scalar(
            select(KISMockSignalLedger.id).where(
                KISMockSignalLedger.correlation_id == attribution.correlation_id
            )
        )
    await db.commit()
    if inserted is None:  # pragma: no cover — conflict without a readable row
        raise RuntimeError(
            "kis_mock signal row neither inserted nor found: "
            f"{attribution.correlation_id}"
        )
    return int(inserted)


async def mark_signal_outcome(
    db: AsyncSession,
    *,
    correlation_id: str,
    outcome_state: str,
    suppressed_reason: str | None = None,
    detail_patch: dict[str, Any] | None = None,
) -> int:
    """Advance a recorded signal to its post-send outcome.

    Never used to *create* attribution — only to say what happened to a row
    that already existed before the send.
    """
    values: dict[str, Any] = {"outcome_state": outcome_state}
    if suppressed_reason is not None:
        values["suppressed_reason"] = suppressed_reason
    if detail_patch:
        values["detail"] = KISMockSignalLedger.detail.op("||")(
            cast(detail_patch, JSONB)
        )
    result = await db.execute(
        update(KISMockSignalLedger)
        .where(KISMockSignalLedger.correlation_id == correlation_id)
        .values(**values)
    )
    await db.commit()
    return int(result.rowcount or 0)
