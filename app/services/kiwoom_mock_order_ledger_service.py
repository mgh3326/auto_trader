"""hk 4712 (3797 T-P2) — Kiwoom mock order ledger service and send guard.

Design: ``docs/design/3797-kiwoom-mock-ledger.md`` §2.2 and §6.1.

This module is the **only** writer of ``review.kiwoom_mock_order_ledger``
(AGENTS.md hard rule 5). It also owns the pre-send reservation in
``review.order_send_intents`` (``account_scope='kiwoom_mock'`` for KR,
``'kiwoom_mock_us'`` for US) because the Kiwoom order body has no client
idempotency field: the reservation is the only double-send defence.

One table holds both mock accounts (hk 4703/4754). The account mode selects the
rest of the row's identity — market, instrument type, currency, send-key prefix
and trading day — from :data:`_ACCOUNT_PROFILES`, so a caller can never write a
KR row with a US currency (the DB CHECK would refuse it anyway).

Scope is ``place`` only. Modify/cancel rows belong to a later ticket.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import Numeric, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.review import KiwoomMockOrderLedger
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.kiwoom_execution_ledger import (
    build_kiwoom_execution_ledger_upsert,
)
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)

logger = logging.getLogger(__name__)

__all__ = (
    "ACCOUNT_MODE_KIWOOM_MOCK",
    "ACCOUNT_MODE_KIWOOM_MOCK_US",
    "CLIENT_ORDER_ID_PREFIX",
    "CLIENT_ORDER_ID_PREFIX_US",
    "DuplicateOrderIntent",
    "FillBooking",
    "KiwoomMockLedgerPayloadConflict",
    "KiwoomMockOrderLedgerService",
    "KiwoomMockPlaceRecorder",
    "KiwoomMockTransitionRefused",
    "LedgerReceipt",
    "journaled_progress",
    "unmatched_sell_since",
    "RESERVATION_ACCOUNT_SCOPE",
    "RESERVATION_ACCOUNT_SCOPE_US",
    "SendReservation",
    "account_profile",
    "build_client_order_id",
    "build_us_client_order_id",
    "kr_trading_day",
    "trading_day_for",
    "us_trading_day",
)

ACCOUNT_MODE_KIWOOM_MOCK: Final = "kiwoom_mock"
ACCOUNT_MODE_KIWOOM_MOCK_US: Final = "kiwoom_mock_us"
#: ``review.order_send_intents.account_scope`` for KR Kiwoom mock places.
RESERVATION_ACCOUNT_SCOPE: Final = "kiwoom_mock"
#: ``review.order_send_intents.account_scope`` for US Kiwoom mock places.
RESERVATION_ACCOUNT_SCOPE_US: Final = "kiwoom_mock_us"
CLIENT_ORDER_ID_PREFIX: Final = "kwoomm"
CLIENT_ORDER_ID_PREFIX_US: Final = "kwoomus"

_US_EASTERN: Final = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """The ledger identity an account mode implies (design §2.1 CHECK triple)."""

    market: str
    instrument_type: str
    currency: str
    reservation_scope: str
    client_order_id_prefix: str


#: ``account_mode`` -> the rest of the row's identity. The DB CHECK binds the
#: four identity columns together, so they are derived from one place instead
#: of being passed in separately by each caller.
_ACCOUNT_PROFILES: Final[dict[str, AccountProfile]] = {
    ACCOUNT_MODE_KIWOOM_MOCK: AccountProfile(
        market="kr",
        instrument_type="equity_kr",
        currency="KRW",
        reservation_scope=RESERVATION_ACCOUNT_SCOPE,
        client_order_id_prefix=CLIENT_ORDER_ID_PREFIX,
    ),
    ACCOUNT_MODE_KIWOOM_MOCK_US: AccountProfile(
        market="us",
        instrument_type="equity_us",
        currency="USD",
        reservation_scope=RESERVATION_ACCOUNT_SCOPE_US,
        client_order_id_prefix=CLIENT_ORDER_ID_PREFIX_US,
    ),
}


def account_profile(account_mode: str) -> AccountProfile:
    """The identity for ``account_mode``; unknown modes are refused, not guessed."""
    try:
        return _ACCOUNT_PROFILES[account_mode]
    except KeyError as exc:
        raise ValueError(
            f"unknown kiwoom mock account_mode {account_mode!r}; expected one of "
            f"{sorted(_ACCOUNT_PROFILES)}"
        ) from exc


_SEND_STATUSES: Final = frozenset({"accepted", "rejected"})
# kis_mock lifecycle vocabulary (design §2.1 ``lifecycle_state``). A rejected
# send is ``failed``; an accepted one is ``accepted``; an uncertain one was
# sent with an unknown outcome, so it stays ``submitted``.
_LIFECYCLE_FOR_STATUS: Final = {
    "accepted": "accepted",
    "rejected": "failed",
    "uncertain": "submitted",
}

# hk 4722 — terminal-only reconcile. The scan covers open + uncertain rows; the
# only target statuses are the three that need no fill booking (design §3.3).
_RECONCILE_SOURCE_STATUSES: Final = frozenset(
    {"accepted", "pending", "partial", "uncertain"}
)
#: Only these source statuses may be closed. ``uncertain`` is deliberately
#: absent: its only lawful resolution is ``promote_uncertain_to_accepted`` (I3).
#: ``partial`` is absent too: a booked partial fill must never become a no-fill
#: terminal row, or T-P4 would lose the fill it still has to book.
_RECONCILE_TERMINAL_SOURCE_STATUSES: Final = frozenset({"accepted", "pending"})
#: A row whose fill is booked may still be closed by a terminal broker verdict
#: that carried the same fill (``close_filled_as_terminal``), but only once the
#: whole fill is booked. ``mark_terminal`` never takes these sources.
_FILLED_SOURCE_STATUSES: Final = frozenset({"partial", "filled"})
_RECONCILE_TERMINAL_TARGETS: Final = frozenset({"expired", "cancelled", "rejected"})
_LIFECYCLE_FOR_TERMINAL: Final = {
    "expired": "expired",
    "cancelled": "cancelled",
    "rejected": "failed",
}
#: Lifecycle labels a fill transition writes (kis_mock vocabulary).
_LIFECYCLE_FOR_FILL: Final = {"partial": "partial", "filled": "filled"}
#: Only these source statuses may book a fill. ``uncertain`` is deliberately
#: absent (I3: its only resolution is promotion to ``accepted``). ``filled`` is
#: present so a replayed run of the same terminal evidence books nothing and
#: still finds the row.
_OPEN_FILL_STATUSES: Final = frozenset({"accepted", "pending", "partial", "filled"})

#: Journal progress keys persisted in ``last_reconcile_detail`` (JSONB). The
#: journal is written per booked delta (hk 4802), and ``journaled_qty`` /
#: ``journaled_avg`` are the monotonic cumulative markers of what already
#: journaled — the same place the delta booking already records progress.
_JOURNALED_QTY_KEY: Final = "journaled_qty"
_JOURNALED_AVG_KEY: Final = "journaled_avg"
#: A sell delta that could not fully match its FIFO lots leaves this reason so
#: the row surfaces for review; cleared once the remainder journals.
_JOURNAL_MANUAL_REVIEW_KEY: Final = "journal_manual_review"
#: When the unmatched-remainder reason was first recorded (ISO). The retried
#: remainder may only close lots that already existed by then — a lot journaled
#: from a LATER buy is not cover for an older sell (mergewake r4578899894).
_UNMATCHED_SELL_SINCE_KEY: Final = "unmatched_sell_since"


def unmatched_sell_since(row: Any) -> datetime | None:
    """The first-unmatched timestamp of the sell remainder, if recorded."""
    detail = getattr(row, "last_reconcile_detail", None) or {}
    raw = detail.get(_UNMATCHED_SELL_SINCE_KEY) if isinstance(detail, dict) else None
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def journaled_progress(row: Any) -> tuple[Decimal, Decimal]:
    """Quantity and price already journaled for the row (0, 0 when unknown)."""
    detail = row.last_reconcile_detail or {}
    raw_qty = detail.get(_JOURNALED_QTY_KEY) if isinstance(detail, dict) else None
    raw_avg = detail.get(_JOURNALED_AVG_KEY) if isinstance(detail, dict) else None
    try:
        quantity = Decimal(str(raw_qty)) if raw_qty is not None else Decimal(0)
    except (ArithmeticError, ValueError):
        quantity = Decimal(0)
    try:
        average = Decimal(str(raw_avg)) if raw_avg is not None else Decimal(0)
    except (ArithmeticError, ValueError):
        average = Decimal(0)
    return quantity, average


class KiwoomMockTransitionRefused(Exception):
    """A reconcile transition was refused: stale snapshot, wrong source state.

    Never a silent no-op — the caller learns that the row moved (or was never
    open) so it does not report a close it did not perform.
    """

    def __init__(self, *, ledger_id: int, status: str | None, reason: str) -> None:
        self.ledger_id = ledger_id
        self.status = status
        self.reason = reason
        super().__init__(
            f"kiwoom mock ledger transition refused for id={ledger_id} "
            f"(status={status!r}): {reason}"
        )


def kr_trading_day(now: datetime) -> str:
    """KR trading day as KST ``YYYYMMDD`` (design §2.1 ``trading_day``)."""
    return now.astimezone(KST).strftime("%Y%m%d")


def us_trading_day(now: datetime) -> str:
    """US trading day as ``YYYYMMDD`` in the ET trading-date window.

    Design §2.2: the US trading day is the ET date with the 20:00 ET rollover
    ROB-1379 uses (``us_trading_day_window``), so an after-hours order belongs
    to its own ET date and a KST-morning order belongs to the next one. The
    rule itself is not restated here — the window's end instant *is* the
    trading date at 20:00 ET.
    """
    from app.services.execution_ledger.kis_lots import us_trading_day_window

    _, window_end = us_trading_day_window(now)
    return window_end.astimezone(_US_EASTERN).strftime("%Y%m%d")


def trading_day_for(account_mode: str, now: datetime) -> str:
    """The ``trading_day`` label ``account_mode`` records (KST day or ET day)."""
    if account_mode == ACCOUNT_MODE_KIWOOM_MOCK_US:
        return us_trading_day(now)
    account_profile(account_mode)  # unknown modes are refused, never defaulted
    return kr_trading_day(now)


def _exact_decimal(value: object) -> str:
    """Exact decimal text for a send-key payload (never a float repr).

    Normalized, so ``100`` and ``100.00`` produce one key: they are the same
    order, and a trailing zero is not a different instruction.
    """
    return format(Decimal(str(value)).normalize(), "f")


def build_us_client_order_id(
    *,
    symbol: str,
    side: str,
    quantity: int,
    price: object | None,
    exchange: str,
    order_type: str,
    trading_day: str,
    rung: str | int | None = None,
) -> str:
    """``kwoomus-<sha16>(canonical|trading day salt|rung)`` for US mock orders.

    Same derivation as :func:`build_client_order_id`; the payload additionally
    names the market and the order type (US admits ``market`` orders, which
    carry no price), and the price is exact decimal text so ``100`` and
    ``100.00`` are one order. The KR canonical dict is deliberately untouched
    so every already-issued ``kwoomm-`` key keeps its value.
    """
    canonical = {
        "accountMode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        "exchange": str(exchange).strip().upper(),
        "market": "us",
        "orderType": str(order_type),
        "price": None if price is None else _exact_decimal(price),
        "quantity": int(quantity),
        "side": str(side).lower(),
        "symbol": str(symbol).strip().upper(),
    }
    disc = "" if rung is None else str(rung)
    blob = f"{_canonical_json(canonical)}|{trading_day}|{disc}".encode()
    return f"{CLIENT_ORDER_ID_PREFIX_US}-{hashlib.sha256(blob).hexdigest()[:16]}"


def _canonical_json(canonical: dict[str, Any]) -> str:
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def build_client_order_id(
    *,
    symbol: str,
    side: str,
    quantity: int,
    price: int,
    exchange: str,
    trading_day: str,
    rung: str | int | None = None,
) -> str:
    """``kwoomm-<sha16>(canonical|trading-day salt|rung)`` (design §2.2).

    Deterministic: the same trading day and the same order dedupe; the next day
    is a new key; a deliberate second identical order the same day is separated
    by ``rung``. Mirrors the Toss ``tossp6-`` derivation.
    """
    canonical = {
        "accountMode": ACCOUNT_MODE_KIWOOM_MOCK,
        "exchange": str(exchange).strip().upper(),
        "orderType": "limit",
        "price": int(price),
        "quantity": int(quantity),
        "side": str(side).lower(),
        "symbol": str(symbol).strip(),
    }
    disc = "" if rung is None else str(rung)
    blob = f"{_canonical_json(canonical)}|{trading_day}|{disc}".encode()
    return f"{CLIENT_ORDER_ID_PREFIX}-{hashlib.sha256(blob).hexdigest()[:16]}"


class KiwoomMockLedgerPayloadConflict(Exception):
    """The same ``client_order_id`` was recorded with a different order payload."""

    def __init__(self, *, client_order_id: str, differing: tuple[str, ...]) -> None:
        self.client_order_id = client_order_id
        self.differing = differing
        super().__init__(
            f"client_order_id {client_order_id!r} is already recorded with a "
            f"different payload (fields: {', '.join(differing)})."
        )


# Fields that define "the same order". Outcome fields (status, broker order id,
# response) are deliberately not compared: a key sends at most once, so the
# first recorded outcome is the broker truth and a replay returns it unchanged.
_PAYLOAD_FIELDS: Final = (
    "account_mode",
    "operation_kind",
    "trading_day",
    "symbol",
    "side",
    "order_type",
    "quantity",
    "price",
)


@dataclass(frozen=True)
class FillBooking:
    """Detached summary of one broker-evidence fill booking (T-P4).

    Carries the receipt of the ``execution_ledger`` delta so the kernel can
    report it without reading the order row again after the session closed.
    """

    ledger_id: int
    client_order_id: str
    broker_order_id: str
    #: ``booked_partial`` / ``booked_filled`` when this run booked a delta,
    #: ``noop_already_booked`` when the broker evidence added nothing new.
    action: str
    delta_qty: Decimal
    cumulative_qty: Decimal
    #: Price of this delta (derived from the cumulative notionals), not VWAP.
    avg_price: Decimal
    status: str
    execution_ledger_row_id: int | None
    fill_seq: int | None
    #: The broker's cumulative average, the row's recorded ``avg_fill_price``.
    cumulative_avg: Decimal = Decimal(0)
    #: Quantity/price already journaled before this booking, from the row detail.
    journaled_qty: Decimal = Decimal(0)
    journaled_avg: Decimal = Decimal(0)
    #: The journal step's inputs, carried so the kernel never re-reads the row.
    side: str | None = None
    symbol: str = ""
    instrument_type: str = ""
    currency: str = ""
    correlation_id: str | None = None
    thesis: str | None = None
    strategy: str | None = None
    reason: str | None = None
    exit_reason: str | None = None
    notes: str | None = None
    indicators_snapshot: dict[str, Any] | None = None
    trade_id: int | None = None
    journal_id: int | None = None
    #: First-unmatched stamp of an outstanding sell remainder; bounds a retried
    #: close to lots that already existed then. ``None`` on a first attempt.
    unmatched_sell_since: datetime | None = None


class KiwoomMockOrderLedgerService:
    """The only writer of ``review.kiwoom_mock_order_ledger`` (hard rule 5)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record_send(
        self,
        *,
        status: str,
        client_order_id: str,
        idempotency_key: str,
        symbol: str,
        side: str,
        quantity: int,
        price: object | None,
        broker_order_id: str | None,
        response_code: str | None,
        response_message: str | None,
        raw_response: dict[str, Any] | None,
        trading_day: str | None = None,
        correlation_id: str | None = None,
        approval_hash: str | None = None,
        strategy: str | None = None,
        reason: str | None = None,
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
        order_type: str = "limit",
    ) -> KiwoomMockOrderLedger:
        """Record a broker answer: ``accepted`` (needs ``broker_order_id``) or
        ``rejected``. Idempotent on ``client_order_id``.

        ``account_mode`` selects market/instrument/currency and the trading-day
        clock; ``price`` is optional because a US market order carries none.
        """
        if status not in _SEND_STATUSES:
            raise ValueError(
                f"record_send status must be one of {sorted(_SEND_STATUSES)}"
            )
        if status == "accepted" and not broker_order_id:
            # I1: accepted => broker_order_id. A response without an order id is
            # not an acknowledgement; the caller must use record_uncertain.
            raise ValueError("an accepted ledger row requires broker_order_id")
        return await self._record(
            status=status,
            client_order_id=client_order_id,
            idempotency_key=idempotency_key,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            broker_order_id=broker_order_id,
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            trading_day=trading_day,
            correlation_id=correlation_id,
            approval_hash=approval_hash,
            strategy=strategy,
            reason=reason,
            account_mode=account_mode,
            order_type=order_type,
        )

    async def record_uncertain(
        self,
        *,
        client_order_id: str,
        idempotency_key: str,
        symbol: str,
        side: str,
        quantity: int,
        price: object | None,
        broker_order_id: str | None = None,
        response_code: str | None = None,
        response_message: str | None = None,
        raw_response: dict[str, Any] | None = None,
        trading_day: str | None = None,
        correlation_id: str | None = None,
        approval_hash: str | None = None,
        strategy: str | None = None,
        reason: str | None = None,
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
        order_type: str = "limit",
    ) -> KiwoomMockOrderLedger:
        """Record a send whose outcome is unknown (I3). Never resent; only a
        later positive broker read may resolve it. Idempotent on
        ``client_order_id``."""
        return await self._record(
            status="uncertain",
            client_order_id=client_order_id,
            idempotency_key=idempotency_key,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            broker_order_id=broker_order_id,
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            trading_day=trading_day,
            correlation_id=correlation_id,
            approval_hash=approval_hash,
            strategy=strategy,
            reason=reason,
            account_mode=account_mode,
            order_type=order_type,
        )

    async def get_status(self, client_order_id: str) -> str | None:
        """Read-only: the recorded status for a key, or ``None`` if no row."""
        existing = await self._find(client_order_id)
        return None if existing is None else existing.status

    # ------------------------------------------------------------------
    # hk 4722 (3797 T-P3) — reconcile reads and terminal transitions.
    #
    # Terminal-only reconcile closes exactly three outcomes (design §3.3):
    # expired / cancelled / rejected, and resolves ``uncertain`` to
    # ``accepted``. Fills are NOT booked here (T-P4); the methods below cannot
    # set ``filled``/``partial`` and cannot touch ``filled_qty``.
    # ------------------------------------------------------------------

    async def list_reconcile_candidates(
        self,
        *,
        limit: int,
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
        include_journal_recovery: bool = False,
    ) -> tuple[list[KiwoomMockOrderLedger], int]:
        """Open + uncertain place rows, oldest first, plus the open total.

        Scoped to one account mode: a KR scan must never classify a US row.

        ``include_journal_recovery`` also returns rows whose booked fill was
        never fully journaled (``journaled_qty`` below ``filled_qty``): the
        fill evidence is committed, so only a later run can complete the
        journal projection — including terminal rows a short sell closed.
        Recovery candidates are ordered AFTER open rows so a permanently
        unmatched remainder can never starve the open scan, and they are
        limited to rows this projection already touched (a journaled or
        manual-review marker): booked history that predates hk 4802 is NOT
        backfilled — it stays manual-review territory (mergewake r4578899898).
        """
        profile = account_profile(account_mode)
        where = [
            KiwoomMockOrderLedger.account_mode == account_mode,
            KiwoomMockOrderLedger.market == profile.market,
            KiwoomMockOrderLedger.operation_kind == "place",
        ]
        source = KiwoomMockOrderLedger.status.in_(_RECONCILE_SOURCE_STATUSES)
        order: list[Any] = [KiwoomMockOrderLedger.id.asc()]
        if include_journal_recovery:
            journaled = func.coalesce(
                cast(
                    KiwoomMockOrderLedger.last_reconcile_detail[
                        _JOURNALED_QTY_KEY
                    ].astext,
                    Numeric(20, 8),
                ),
                0,
            )
            detail = KiwoomMockOrderLedger.last_reconcile_detail
            where.append(
                or_(
                    source,
                    and_(
                        KiwoomMockOrderLedger.filled_qty.isnot(None),
                        KiwoomMockOrderLedger.filled_qty > journaled,
                        or_(
                            detail[_JOURNALED_QTY_KEY].astext.isnot(None),
                            detail[_JOURNAL_MANUAL_REVIEW_KEY].astext.isnot(None),
                        ),
                    ),
                )
            )
            # Open rows first; recovery rows fill only the leftover window.
            order = [source.desc(), KiwoomMockOrderLedger.id.asc()]
        else:
            where.append(source)
        total = (
            await self._db.execute(
                select(func.count()).select_from(KiwoomMockOrderLedger).where(*where)
            )
        ).scalar_one()
        rows = (
            (
                await self._db.execute(
                    select(KiwoomMockOrderLedger)
                    .where(*where)
                    .order_by(*order)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def list_acknowledged_cancels(
        self,
        *,
        original_order_ids: set[str],
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
    ) -> set[str]:
        """Original order ids for which we hold our own acknowledged cancel.

        The first half of the design §3.2 ``cancelled`` requirement. Cancel rows
        are not created in T-P3, so this is normally empty; it stays a real read
        so the pair rule is enforced the day cancel rows exist.
        """
        if not original_order_ids:
            return set()
        rows = (
            await self._db.execute(
                select(KiwoomMockOrderLedger.original_order_id).where(
                    KiwoomMockOrderLedger.account_mode == account_mode,
                    KiwoomMockOrderLedger.operation_kind == "cancel",
                    KiwoomMockOrderLedger.status == "accepted",
                    KiwoomMockOrderLedger.original_order_id.in_(
                        sorted(original_order_ids)
                    ),
                )
            )
        ).scalars()
        return {str(value) for value in rows if value}

    async def mark_terminal(
        self,
        *,
        ledger_id: int,
        status: str,
        reason: str,
        detail: dict[str, Any] | None = None,
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
    ) -> KiwoomMockOrderLedger:
        """Close an open row as ``expired`` / ``cancelled`` / ``rejected``.

        Refuses every other target and every non-open source: a late close is
        harmless, an early or wrong close is not (§3.2).
        """
        if status not in _RECONCILE_TERMINAL_TARGETS:
            raise ValueError(
                f"mark_terminal status must be one of "
                f"{sorted(_RECONCILE_TERMINAL_TARGETS)}; got {status!r}"
            )
        row = await self._lock_row(ledger_id, account_mode=account_mode)
        if row.status not in _RECONCILE_TERMINAL_SOURCE_STATUSES:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="source_status_not_open",
            )
        self._apply_terminal(row, status=status, reason=reason, detail=detail)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def close_filled_as_terminal(
        self,
        *,
        ledger_id: int,
        status: str,
        reason: str,
        filled_quantity: int,
        detail: dict[str, Any] | None = None,
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
    ) -> KiwoomMockOrderLedger:
        """Close a partially filled row whose whole fill is booked.

        ``mark_terminal`` refuses a ``partial``/``filled`` source on purpose: a
        *no-fill* close must never swallow a booked fill. This is the other
        case — a terminal broker verdict that still carried a positive fill.
        It is allowed only once the row's booked cumulative quantity equals the
        verdict's ``filled_quantity``, so the close adds no evidence loss and
        the row finally leaves the reconcile scan instead of being re-booked as
        a no-op forever.

        ``filled_quantity`` is the broker's cumulative fill on this verdict
        (the exact int the classifier reported). A row that booked less (a
        refused or failed delta) is refused, never closed over unbooked shares.
        """
        if status not in _RECONCILE_TERMINAL_TARGETS:
            raise ValueError(
                f"close_filled_as_terminal status must be one of "
                f"{sorted(_RECONCILE_TERMINAL_TARGETS)}; got {status!r}"
            )
        if isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int):
            raise TypeError("filled_quantity must be an exact int")
        row = await self._lock_row(ledger_id, account_mode=account_mode)
        if row.status not in _FILLED_SOURCE_STATUSES:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="source_status_not_filled",
            )
        booked = row.filled_qty or Decimal(0)
        if filled_quantity <= 0 or booked != Decimal(filled_quantity):
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="fill_not_booked",
            )
        # Keep the booked quantity in the terminal detail: it is the audit trail
        # for what the close consumed. The verdict's exact int is stored because
        # the column's ``numeric(20, 8)`` scale would render as "4.00000000".
        terminal_detail = {
            "verdict": status,
            "booked_filled_qty": str(filled_quantity),
            **(detail or {}),
        }
        self._apply_terminal(row, status=status, reason=reason, detail=terminal_detail)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def promote_uncertain_to_accepted(
        self,
        *,
        ledger_id: int,
        broker_order_id: str,
        detail: dict[str, Any] | None = None,
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
    ) -> KiwoomMockOrderLedger:
        """``uncertain → accepted`` on a positive, complete broker listing.

        The only resolution I3 allows. A non-blank broker order id is required
        by the accepted-side CHECK; nothing is resent.
        """
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            raise ValueError("promote_uncertain_to_accepted requires broker_order_id")
        row = await self._lock_row(ledger_id, account_mode=account_mode)
        if row.status != "uncertain":
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="not_uncertain",
            )
        row.status = "accepted"
        row.broker_order_id = broker_order_id.strip()
        row.lifecycle_state = _LIFECYCLE_FOR_STATUS["accepted"]
        row.reconciled_at = datetime.now(UTC)
        row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
        row.last_reconcile_detail = {
            "action": "uncertain_to_accepted",
            "reason": "positive_listing_match",
            **(detail or {}),
        }
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def record_fill(
        self,
        *,
        ledger_id: int,
        filled_quantity: int,
        average_price: int,
        observed_at: datetime | None = None,
        commit: bool = True,
    ) -> FillBooking:
        """Book the broker-evidence fill delta of one open order (I4, I9).

        ``filled_quantity`` is the broker's *cumulative* filled quantity. The
        delta against the quantity already recorded on the row is upserted into
        ``execution_ledger`` — through :class:`ExecutionLedgerRepository`, the
        only writer — in the same transaction that advances the row, so a re-run
        of identical evidence books nothing new and finds the row already
        advanced.

        A regressed or over-order quantity is refused (the broker and the row
        disagree, so neither is safe to book); ``uncertain`` rows are refused
        because promotion is their only lawful resolution (I3).

        ``commit=False`` leaves the transaction open — the row stays locked and
        the detail update uncommitted — so the caller can write the journal
        effects and the journal progress marker in the same transaction.
        """
        if isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int):
            raise TypeError("filled_quantity must be an exact int")
        if isinstance(average_price, bool) or not isinstance(average_price, int):
            raise TypeError("average_price must be an exact int")
        if average_price <= 0:
            raise ValueError("average_price must be positive")

        row = await self._lock_row(ledger_id)
        journaled = journaled_progress(row)
        if row.status not in _OPEN_FILL_STATUSES:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="source_status_not_open",
            )
        if row.broker_order_id is None:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="no_broker_order_id",
            )
        if row.side not in {"buy", "sell"}:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="side_unknown",
            )
        if row.quantity is None or row.quantity <= 0:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="order_quantity_unknown",
            )

        previous = row.filled_qty or Decimal(0)
        cumulative = Decimal(filled_quantity)
        if cumulative < previous:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="fill_quantity_regressed",
            )
        if cumulative > row.quantity:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="fill_exceeds_order_quantity",
            )

        now = observed_at or datetime.now(UTC)
        status = "filled" if cumulative >= row.quantity else "partial"
        delta = cumulative - previous
        previous_average = row.avg_fill_price
        if (
            delta == 0
            and previous_average is not None
            and Decimal(average_price) != previous_average
        ):
            # The broker changed its cumulative average without filling another
            # share. The stored average describes the booked fills and is the
            # basis the next delta's price is derived from, so replacing it
            # here would silently misprice that delta. Refuse and leave the row
            # untouched for a manual look; a later fill still books against the
            # booked basis.
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="average_price_changed_without_fill",
            )
        row_id: int | None = None
        fill_seq: int | None = None
        if delta > 0:
            try:
                fill = build_kiwoom_execution_ledger_upsert(
                    ledger_id=row.id,
                    broker_order_id=str(row.broker_order_id),
                    symbol=row.symbol,
                    side=str(row.side),
                    previous_filled_qty=previous,
                    previous_average_price=previous_average,
                    filled_quantity=filled_quantity,
                    average_price=average_price,
                    observed_at=now,
                    correlation_id=row.correlation_id,
                )
            except ValueError as exc:
                # The cumulative average cannot be split into an incremental
                # price: refuse rather than book a price we cannot derive.
                raise KiwoomMockTransitionRefused(
                    ledger_id=ledger_id,
                    status=row.status,
                    reason=f"incremental_price_unavailable:{exc}",
                ) from exc
            _status, row_id = await ExecutionLedgerRepository(self._db).upsert_fill(
                fill
            )
            fill_seq = fill.fill_seq
            delta_price = fill.filled_price
            action = f"booked_{status}"
        else:
            delta_price = Decimal(0)
            action = "noop_already_booked"

        row.status = status
        row.lifecycle_state = _LIFECYCLE_FOR_FILL[status]
        row.filled_qty = cumulative
        if delta > 0:
            # Never store an average that no booked delta justifies.
            row.avg_fill_price = Decimal(average_price)
        row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
        row.reconciled_at = now
        # Merge, never replace: earlier reconcile detail survives a booking.
        detail = {
            **(row.last_reconcile_detail or {}),
            "action": action,
            "basis": "broker_evidence",
            "verdict": status,
            "previous_filled_qty": str(previous),
            "broker_cumulative_filled_qty": str(cumulative),
            "broker_cumulative_avg_price": str(Decimal(average_price)),
            "execution_ledger_row_id": row_id,
        }
        if delta > 0:
            detail["delta_fill_price"] = str(delta_price)
        row.last_reconcile_detail = detail
        if commit:
            await self._db.commit()
            await self._db.refresh(row)
        return self._booking_from_row(
            row,
            action=action,
            delta_qty=delta,
            avg_price=delta_price if delta > 0 else Decimal(average_price),
            execution_ledger_row_id=row_id,
            fill_seq=fill_seq,
            journaled=journaled,
        )

    async def lock_for_journal(self, *, ledger_id: int) -> FillBooking:
        """Lock the order row and return its current fill/journal state.

        The journal step must decide what is still unjournaled while holding
        this lock: a journaled marker read without the row lock is only a
        snapshot, so two overlapping runs would otherwise journal the same
        quantity twice (a second lot journal, or a second FIFO close). Any
        status is lockable — a terminal row can still owe journaled shares.
        """
        row = await self._lock_row(ledger_id)
        return self._booking_from_row(row, action="journal_lock")

    async def record_journal_progress(
        self,
        *,
        ledger_id: int,
        trade_id: int | None = None,
        journal_id: int | None = None,
        journaled_qty: Decimal | None = None,
        journaled_avg: Decimal | None = None,
        manual_review_reason: str | None = None,
        commit: bool = True,
    ) -> None:
        """Record what a booked fill journaled, monotonically.

        ``journal_id`` repoints the row at the order's current lot (a fully
        closed lot is replaced by the new one); ``trade_id`` is add-only so a
        re-run cannot repoint the row at a second trade. The progress markers
        only advance — never regress — and ``manual_review_reason`` is stored
        while set and cleared once the unjournaled remainder is resolved.
        """
        row = await self._lock_row(ledger_id)
        if trade_id is not None and row.trade_id is None:
            row.trade_id = trade_id
        if journal_id is not None:
            row.journal_id = journal_id
        detail = dict(row.last_reconcile_detail or {})
        current_qty, _current_avg = journaled_progress(row)
        if journaled_qty is not None and journaled_qty > current_qty:
            detail[_JOURNALED_QTY_KEY] = str(journaled_qty)
            if journaled_avg is not None:
                detail[_JOURNALED_AVG_KEY] = str(journaled_avg)
        if manual_review_reason is not None:
            detail[_JOURNAL_MANUAL_REVIEW_KEY] = manual_review_reason
            # The first unmatched sell stamps when its remainder first missed:
            # a retried close may only consume lots that existed by then.
            if (
                manual_review_reason.startswith("unmatched_sell_quantity")
                and _UNMATCHED_SELL_SINCE_KEY not in detail
            ):
                detail[_UNMATCHED_SELL_SINCE_KEY] = datetime.now(UTC).isoformat()
        elif journaled_qty is not None:
            detail.pop(_JOURNAL_MANUAL_REVIEW_KEY, None)
            detail.pop(_UNMATCHED_SELL_SINCE_KEY, None)
        row.last_reconcile_detail = detail
        if commit:
            await self._db.commit()

    @staticmethod
    def _booking_from_row(
        row: KiwoomMockOrderLedger,
        *,
        action: str,
        delta_qty: Decimal = Decimal(0),
        avg_price: Decimal = Decimal(0),
        execution_ledger_row_id: int | None = None,
        fill_seq: int | None = None,
        journaled: tuple[Decimal, Decimal] | None = None,
    ) -> FillBooking:
        journaled_qty, journaled_avg = (
            journaled if journaled is not None else journaled_progress(row)
        )
        return FillBooking(
            ledger_id=row.id,
            client_order_id=row.client_order_id,
            broker_order_id=str(row.broker_order_id or ""),
            action=action,
            delta_qty=delta_qty,
            cumulative_qty=row.filled_qty or Decimal(0),
            avg_price=avg_price,
            status=row.status,
            execution_ledger_row_id=execution_ledger_row_id,
            fill_seq=fill_seq,
            cumulative_avg=row.avg_fill_price or Decimal(0),
            journaled_qty=journaled_qty,
            journaled_avg=journaled_avg,
            side=row.side,
            symbol=row.symbol,
            instrument_type=row.instrument_type,
            currency=row.currency,
            correlation_id=row.correlation_id,
            thesis=row.thesis,
            strategy=row.strategy,
            reason=row.reason,
            exit_reason=row.exit_reason,
            notes=row.notes,
            indicators_snapshot=row.indicators_snapshot,
            trade_id=row.trade_id,
            journal_id=row.journal_id,
            unmatched_sell_since=unmatched_sell_since(row),
        )

    @staticmethod
    def _apply_terminal(
        row: KiwoomMockOrderLedger,
        *,
        status: str,
        reason: str,
        detail: dict[str, Any] | None,
    ) -> None:
        row.status = status
        row.lifecycle_state = _LIFECYCLE_FOR_TERMINAL[status]
        row.reconciled_at = datetime.now(UTC)
        row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
        row.last_reconcile_detail = {
            "action": f"marked_{status}",
            "reason": reason,
            "basis": "positive_broker_evidence",
            **(detail or {}),
        }

    async def _lock_row(
        self, ledger_id: int, *, account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK
    ) -> KiwoomMockOrderLedger:
        profile = account_profile(account_mode)
        row = (
            await self._db.execute(
                select(KiwoomMockOrderLedger)
                .where(
                    KiwoomMockOrderLedger.id == ledger_id,
                    KiwoomMockOrderLedger.account_mode == account_mode,
                    KiwoomMockOrderLedger.market == profile.market,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id, status=None, reason="row_not_found"
            )
        return row

    async def _find(self, client_order_id: str) -> KiwoomMockOrderLedger | None:
        return (
            await self._db.execute(
                select(KiwoomMockOrderLedger).where(
                    KiwoomMockOrderLedger.client_order_id == client_order_id
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    def _replay(
        existing: KiwoomMockOrderLedger, row: KiwoomMockOrderLedger
    ) -> KiwoomMockOrderLedger:
        differing = tuple(
            name
            for name in _PAYLOAD_FIELDS
            if getattr(existing, name) != getattr(row, name)
        )
        if differing:
            raise KiwoomMockLedgerPayloadConflict(
                client_order_id=existing.client_order_id, differing=differing
            )
        return existing

    async def _record(
        self,
        *,
        status: str,
        client_order_id: str,
        idempotency_key: str,
        symbol: str,
        side: str,
        quantity: int,
        price: object | None,
        broker_order_id: str | None,
        response_code: str | None,
        response_message: str | None,
        raw_response: dict[str, Any] | None,
        trading_day: str | None,
        correlation_id: str | None,
        approval_hash: str | None,
        strategy: str | None,
        reason: str | None,
        account_mode: str,
        order_type: str,
    ) -> KiwoomMockOrderLedger:
        profile = account_profile(account_mode)
        now = datetime.now(UTC)
        qty = Decimal(int(quantity))
        # Exact decimal text: a US price arrives as a Decimal and must not take
        # a float round trip. ``None`` is a US market order (no price).
        px = None if price is None else Decimal(_exact_decimal(price))
        row = KiwoomMockOrderLedger(
            trade_date=now,
            trading_day=trading_day or trading_day_for(account_mode, now),
            symbol=symbol,
            raw_symbol=symbol,
            instrument_type=profile.instrument_type,
            market=profile.market,
            side=side,
            order_type=order_type,
            operation_kind="place",
            quantity=qty,
            price=px,
            amount=None if px is None else qty * px,
            currency=profile.currency,
            client_order_id=client_order_id,
            idempotency_key=idempotency_key,
            approval_hash=approval_hash,
            correlation_id=correlation_id,
            broker_order_id=broker_order_id,
            account_mode=account_mode,
            broker="kiwoom",
            status=status,
            lifecycle_state=_LIFECYCLE_FOR_STATUS[status],
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            strategy=strategy,
            reason=reason,
            reconcile_attempts=0,
        )

        existing = await self._find(client_order_id)
        if existing is not None:
            return self._replay(existing, row)

        self._db.add(row)
        try:
            await self._db.flush()
        except IntegrityError:
            # A concurrent writer won the UNIQUE(client_order_id) race: replay
            # its row (or surface a conflict) instead of failing the caller.
            await self._db.rollback()
            existing = await self._find(client_order_id)
            if existing is None:
                raise
            return self._replay(existing, row)
        await self._db.commit()
        await self._db.refresh(row)
        return row


@dataclass(frozen=True)
class LedgerReceipt:
    """Detached summary of a ledger row (safe to read after the session closes)."""

    ledger_id: int
    status: str
    client_order_id: str


@dataclass(frozen=True)
class SendReservation:
    row_id: int
    idempotency_key: str
    side: str


class KiwoomMockPlaceRecorder:
    """Session-per-operation facade used by the place path.

    Every method opens its own short session so a ledger or reservation error
    can never leave a half-open transaction around the broker POST. The account
    mode selects the reservation scope and the ledger identity, so one facade
    serves both mock accounts without a caller-side switch.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK,
    ) -> None:
        self._session_factory = session_factory
        self._account_mode = account_mode
        self._scope = account_profile(account_mode).reservation_scope

    async def reserve(
        self, *, idempotency_key: str, symbol: str, side: str
    ) -> SendReservation:
        """Commit the pre-send reservation. ``DuplicateOrderIntent`` means the
        key was already reserved: refuse before any broker call (I2)."""
        async with self._session_factory() as db:
            row_id = await OrderSendIntentService(db).reserve(
                account_scope=self._scope,
                idempotency_key=idempotency_key,
                symbol=symbol,
                side=side,
            )
        return SendReservation(
            row_id=row_id, idempotency_key=idempotency_key, side=side
        )

    async def release(self, reservation: SendReservation) -> int:
        """Release exactly the observed reservation row (proven not sent)."""
        async with self._session_factory() as db:
            return await OrderSendIntentService(db).release_if_matches(
                account_scope=self._scope,
                row_id=reservation.row_id,
                idempotency_key=reservation.idempotency_key,
                side=reservation.side,
            )

    async def existing_status(self, client_order_id: str) -> str | None:
        """Ledger status already recorded for this key (``None`` = no row)."""
        async with self._session_factory() as db:
            return await KiwoomMockOrderLedgerService(db).get_status(client_order_id)

    async def record_send(self, **kwargs: Any) -> LedgerReceipt:
        async with self._session_factory() as db:
            row = await KiwoomMockOrderLedgerService(db).record_send(
                account_mode=self._account_mode, **kwargs
            )
            return LedgerReceipt(row.id, row.status, row.client_order_id)

    async def record_uncertain(self, **kwargs: Any) -> LedgerReceipt:
        async with self._session_factory() as db:
            row = await KiwoomMockOrderLedgerService(db).record_uncertain(
                account_mode=self._account_mode, **kwargs
            )
            return LedgerReceipt(row.id, row.status, row.client_order_id)
