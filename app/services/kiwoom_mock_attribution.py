"""hk 4723 (3797 T-P6) — pre-submit attribution for the Kiwoom mock lane.

Same shape and ordering as ``app/services/kis_mock_attribution.py`` (AGENTS.md
hard rule 7):

1. :func:`resolve_attribution` decides — with no I/O — who owns an order. It
   either produces a complete ``(correlation_id, strategy, signal_source)``
   triple or raises :class:`MissingAttribution`. "Unattributed" is an error,
   not a value, and there is no placeholder strategy.
2. :func:`record_signal` makes that decision durable *before* the
   ``order_send_intents`` reservation and before any broker call.

The failure vocabulary (``MissingAttribution`` / ``InvalidStrategy`` /
``attribution_required`` / ``placeholder_strategy``) is imported from the
kis_mock module on purpose: callers see one error surface, and there is no
second definition of a placeholder strategy to drift.

Only KR ``kiwoom_mock`` places use this today; US rows are reserved by the
table CHECK for a later ticket.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import cast, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import KiwoomMockSignalLedger
from app.services.kis_mock_attribution import (
    DEFAULT_SIGNAL_SOURCE,
    MissingAttribution,
    validate_strategy,
)
from app.services.live_correlation import live_correlation_id

ACCOUNT_SCOPE = "kiwoom_mock"
CORRELATION_CONSTRAINT = "uq_kiwoom_mock_signal_ledger_correlation_id"
logger = logging.getLogger(__name__)

_DECISION_ORDER = "order"
_DECISION_NO_ORDER = "no_order"


@dataclass(frozen=True, slots=True)
class KiwoomMockAttribution:
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
    rung: int | str = 0,
) -> str:
    """Deterministic ``live:kiwoom_mock:<sha16>`` id for one KR mock order.

    ``rung`` is part of the identity for the same reason ``build_client_order_id``
    salts the send key with it: a deliberate second identical order on the same
    day is a distinct order and must get its own signal row, not overwrite the
    first one's outcome.
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
    kst_trade_day: str | None = None,
    rung: int | str | None = None,
) -> KiwoomMockAttribution:
    """Resolve the attribution triple, or raise :class:`MissingAttribution`.

    Pure: no DB, no network, no clock beyond the KST day used for the
    deterministic id. A missing or blank strategy is unattributable and raises
    — it is never replaced by a stand-in.
    """
    resolved_strategy = (
        validate_strategy(strategy, lane=ACCOUNT_SCOPE)
        if strategy is not None
        else None
    )
    if resolved_strategy is None:
        raise MissingAttribution(("strategy",))
    return KiwoomMockAttribution(
        correlation_id=_clean(correlation_id)
        or mint_correlation_id(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            kst_trade_day=kst_trade_day,
            rung=0 if rung is None else rung,
        ),
        strategy=resolved_strategy,
        signal_source=_clean(signal_source) or DEFAULT_SIGNAL_SOURCE,
    )


async def record_signal(
    db: AsyncSession,
    *,
    attribution: KiwoomMockAttribution,
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

    Idempotent on ``correlation_id``. The function owns the transaction, so the
    row is on disk before the caller proceeds to the reservation or the broker.
    """
    if decision not in (_DECISION_ORDER, _DECISION_NO_ORDER):
        raise ValueError(f"unknown decision {decision!r}")

    resolved_outcome = outcome_state or (
        "recorded" if decision == _DECISION_ORDER else "suppressed"
    )
    stmt = (
        pg_insert(KiwoomMockSignalLedger)
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
        .on_conflict_do_nothing(constraint=CORRELATION_CONSTRAINT)
        .returning(KiwoomMockSignalLedger.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    if inserted is None:
        inserted = await db.scalar(
            select(KiwoomMockSignalLedger.id).where(
                KiwoomMockSignalLedger.correlation_id == attribution.correlation_id
            )
        )
    await db.commit()
    if inserted is None:  # pragma: no cover — conflict without a readable row
        raise RuntimeError(
            "kiwoom_mock signal row neither inserted nor found: "
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
        values["detail"] = KiwoomMockSignalLedger.detail.op("||")(
            cast(detail_patch, JSONB)
        )
    result = await db.execute(
        update(KiwoomMockSignalLedger)
        .where(KiwoomMockSignalLedger.correlation_id == correlation_id)
        .values(**values)
    )
    await db.commit()
    return int(result.rowcount or 0)


class KiwoomMockSignalRecorder:
    """Session-per-operation facade used by the KR place path."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def record(self, *, attribution: KiwoomMockAttribution, **kwargs: Any) -> int:
        async with self._session_factory() as db:
            return await record_signal(db, attribution=attribution, **kwargs)

    async def mark_outcome(
        self,
        *,
        correlation_id: str,
        outcome_state: str,
        suppressed_reason: str | None = None,
        detail_patch: dict[str, Any] | None = None,
    ) -> int:
        async with self._session_factory() as db:
            return await mark_signal_outcome(
                db,
                correlation_id=correlation_id,
                outcome_state=outcome_state,
                suppressed_reason=suppressed_reason,
                detail_patch=detail_patch,
            )


__all__ = (
    "ACCOUNT_SCOPE",
    "KiwoomMockAttribution",
    "KiwoomMockSignalRecorder",
    "MissingAttribution",
    "mint_correlation_id",
    "mark_signal_outcome",
    "record_signal",
    "resolve_attribution",
)
