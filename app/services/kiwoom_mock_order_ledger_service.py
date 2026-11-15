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

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.review import KiwoomMockOrderLedger
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
    "KiwoomMockLedgerPayloadConflict",
    "KiwoomMockOrderLedgerService",
    "KiwoomMockPlaceRecorder",
    "KiwoomMockTransitionRefused",
    "LedgerReceipt",
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
_RECONCILE_TERMINAL_TARGETS: Final = frozenset({"expired", "cancelled", "rejected"})
_LIFECYCLE_FOR_TERMINAL: Final = {
    "expired": "expired",
    "cancelled": "cancelled",
    "rejected": "failed",
}


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
        self, *, limit: int, account_mode: str = ACCOUNT_MODE_KIWOOM_MOCK
    ) -> tuple[list[KiwoomMockOrderLedger], int]:
        """Open + uncertain place rows, oldest first, plus the open total.

        Scoped to one account mode: a KR scan must never classify a US row.
        """
        profile = account_profile(account_mode)
        where = (
            KiwoomMockOrderLedger.account_mode == account_mode,
            KiwoomMockOrderLedger.market == profile.market,
            KiwoomMockOrderLedger.operation_kind == "place",
            KiwoomMockOrderLedger.status.in_(_RECONCILE_SOURCE_STATUSES),
        )
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
                    .order_by(KiwoomMockOrderLedger.id.asc())
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
