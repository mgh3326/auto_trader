"""``review.kis_futures_mock_order_ledger`` 의 유일한 writer (하드룰 5, 설계 §4).

- ``accepted`` 는 체결이 아니다. T3 는 ``accepted``/``rejected``/``uncertain`` 행만 쓰고
  체결·정산 열(``filled_qty``·``realized_pnl_krw`` …)은 T4/T5 가 쓴다.
- ``execution_ledger`` 에는 아무것도 쓰지 않는다.
- ``uncertain`` 은 종결하지 않고 재전송하지 않는다. 같은 ``client_order_id`` 의 두 번째
  쓰기는 첫 행을 돌려주고(내용이 다르면 충돌), 행을 덮어쓰지 않는다.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.kis_futures_mock import KISFuturesMockOrderLedger
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)

from .constants import ACCOUNT_MODE, BROKER, MINI_KOSPI200, RESERVATION_SCOPE
from .history import TerminalEvidence, futures_order_identity, terminal_evidence
from .rules import DailyLossState

CLIENT_ORDER_ID_PREFIX = "kfm"
CANCEL_CLIENT_ORDER_ID_PREFIX = "kfmc"

_SEND_STATUSES = frozenset({"accepted", "rejected"})
#: 체결 증거가 아직 정산되지 않은 당일 주문 상태 — 일일 손실 판정에서 ``unverified``.
_UNSETTLED_STATUSES = (
    "accepted",
    "pending",
    "partial",
    "filled",
    "uncertain",
    "anomaly",
    "requires_manual_review",
)


class FuturesLedgerPayloadConflict(Exception):
    """같은 client_order_id 가 다른 주문 내용으로 다시 왔다."""


class FuturesLedgerTransitionRefused(Exception):
    pass


def kst_trading_day(now: datetime) -> str:
    """주간 세션의 KST 달력일 (``YYYYMMDD``, 설계 §5)."""
    return now.astimezone(KST).strftime("%Y%m%d")


def _exact_decimal(value: object) -> str:
    return format(Decimal(str(value)), "f")


def build_client_order_id(
    *,
    contract_symbol: str,
    side: str,
    price: Decimal,
    quantity: int,
    trading_day: str,
    rung: int | str | None = None,
) -> str:
    """결정적 송신 키. 같은 거래일의 같은 내용·rung 는 같은 키다 (재전송 방어)."""
    canonical = "|".join(
        (
            contract_symbol,
            side,
            _exact_decimal(price),
            str(int(quantity)),
            trading_day,
            "" if rung is None else str(rung),
        )
    )
    return f"{CLIENT_ORDER_ID_PREFIX}-{hashlib.sha256(canonical.encode()).hexdigest()[:20]}"


def build_cancel_client_order_id(*, original_client_order_id: str) -> str:
    """원주문당 취소는 한 번 — 키가 원주문에서 결정된다."""
    digest = hashlib.sha256(original_client_order_id.encode()).hexdigest()[:20]
    return f"{CANCEL_CLIENT_ORDER_ID_PREFIX}-{digest}"


@dataclass(frozen=True)
class LedgerReceipt:
    ledger_id: int
    status: str
    client_order_id: str


@dataclass(frozen=True)
class SendReservation:
    row_id: int
    idempotency_key: str
    side: str


@dataclass(frozen=True)
class OriginalOrder:
    """취소 대상 place 행의 분리된 사본 (세션이 닫힌 뒤에도 읽을 수 있다)."""

    ledger_id: int
    status: str
    client_order_id: str
    broker_order_id: str
    contract_symbol: str
    broker_symbol: str
    product_code: str
    contract_month: str
    side: str
    position_effect: str
    price: Decimal
    amount: Decimal
    trading_day: str


@dataclass(frozen=True)
class OrderFacts:
    """원장 쓰기에 필요한 요청 사실 (``None`` 값 없음)."""

    client_order_id: str
    contract_symbol: str
    broker_symbol: str
    product_code: str
    contract_month: str
    side: str
    position_effect: str
    operation_kind: str
    quantity: int
    price: Decimal
    amount: Decimal
    trading_day: str
    correlation_id: str | None = None
    strategy: str | None = None
    original_order_id: str | None = None


class FuturesMockOrderLedgerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record_send(
        self,
        facts: OrderFacts,
        *,
        status: str,
        broker_order_id: str | None,
        response_code: str | None,
        response_message: str | None,
        raw_response: dict[str, Any] | None,
        reason: str | None = None,
    ) -> KISFuturesMockOrderLedger:
        if status not in _SEND_STATUSES:
            raise ValueError(
                f"record_send status must be one of {sorted(_SEND_STATUSES)}"
            )
        if status == "accepted" and not broker_order_id:
            # accepted ⇒ broker_order_id. 주문번호 없는 ACK 는 확인이 아니다.
            raise ValueError("an accepted ledger row requires broker_order_id")
        return await self._record(
            facts,
            status=status,
            broker_order_id=broker_order_id,
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            reason=reason,
        )

    async def record_uncertain(
        self,
        facts: OrderFacts,
        *,
        broker_order_id: str | None = None,
        response_code: str | None = None,
        response_message: str | None = None,
        raw_response: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> KISFuturesMockOrderLedger:
        return await self._record(
            facts,
            status="uncertain",
            broker_order_id=broker_order_id,
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            reason=reason,
        )

    async def get_status(self, client_order_id: str) -> str | None:
        row = await self._find(client_order_id)
        return None if row is None else row.status

    async def get_by_broker_order(
        self, *, trading_day: str, broker_order_id: str
    ) -> KISFuturesMockOrderLedger | None:
        identity = futures_order_identity(trading_day, broker_order_id)
        if identity is None:
            return None
        rows = (
            await self._db.scalars(
                select(KISFuturesMockOrderLedger).where(
                    KISFuturesMockOrderLedger.account_mode == ACCOUNT_MODE,
                    KISFuturesMockOrderLedger.trading_day == trading_day,
                    KISFuturesMockOrderLedger.operation_kind == "place",
                )
            )
        ).all()
        matches = [
            row
            for row in rows
            if futures_order_identity(row.trading_day, row.broker_order_id) == identity
        ]
        return matches[0] if len(matches) == 1 else None

    async def reconcile_day(
        self, *, trading_day: str, history: list[dict[str, Any]], commit: bool = False
    ) -> list[dict[str, Any]]:
        """Plan or atomically book exact same-day place-order evidence."""
        query = (
            select(KISFuturesMockOrderLedger)
            .where(
                KISFuturesMockOrderLedger.account_mode == ACCOUNT_MODE,
                KISFuturesMockOrderLedger.trading_day == trading_day,
                KISFuturesMockOrderLedger.operation_kind == "place",
            )
            .order_by(KISFuturesMockOrderLedger.id)
        )
        if commit:
            query = query.with_for_update()
        orders = (await self._db.scalars(query)).all()
        history_by_id: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in history:
            identity = futures_order_identity(item.get("ord_dt"), item.get("odno"))
            if identity is not None and identity[0] == trading_day:
                history_by_id[identity].append(item)

        details: dict[int, dict[str, Any]] = {}
        evidence: dict[int, TerminalEvidence] = {}
        fills_by_symbol: dict[str, list[KISFuturesMockOrderLedger]] = defaultdict(list)
        matched_terminals: set[tuple[str, str]] = set()
        cancelled_by_id: dict[tuple[str, str], KISFuturesMockOrderLedger] = {}
        blocked_symbols: set[str] = set()
        for order in orders:
            identity = futures_order_identity(order.trading_day, order.broker_order_id)
            rows = history_by_id.get(identity, []) if identity is not None else []
            verdict = details[order.id] = {
                "ledger_id": order.id,
                "result": "no_evidence",
            }
            if order.status not in (
                "accepted",
                "pending",
                "partial",
                "filled",
                "cancelled",
                "expired",
            ):
                verdict["result"] = "state_preserved"
                continue
            if not rows:
                continue
            verdict["result"] = "ambiguous_evidence"
            if len(rows) != 1:
                continue
            proof = terminal_evidence(
                rows[0],
                broker_symbol=order.broker_symbol,
                side=order.side,
                quantity=order.quantity,
            )
            if proof is None:
                continue
            if (
                order.status in ("filled", "cancelled", "expired")
                and order.status != proof.status
            ):
                verdict["result"] = "conflicting_evidence"
                continue
            if order.reconciled_at is not None:
                verdict["result"] = "already_reconciled"
                if (order.status, order.filled_qty, order.avg_fill_price) != (
                    proof.status,
                    proof.quantity,
                    proof.price,
                ):
                    verdict["result"] = "conflicting_evidence"
                    continue
            evidence[order.id] = proof
            if identity is not None:
                matched_terminals.add(identity)
                if proof.status == "cancelled":
                    cancelled_by_id[identity] = order
            if proof.status == "filled":
                fills_by_symbol[order.broker_symbol].append(order)

        blocked_symbols.update(
            order.broker_symbol
            for order in orders
            if order.id not in evidence
            and order.status not in ("rejected", "cancelled", "expired")
        )

        # A fill outside our exact joins can consume or establish a position.
        # Disable close attribution on that contract if history cannot rule it out.
        for item in history:
            identity = futures_order_identity(item.get("ord_dt"), item.get("odno"))
            if identity is not None:
                if identity[0] != trading_day:
                    continue
                if identity in matched_terminals:
                    continue
                original_identity = futures_order_identity(
                    item.get("ord_dt"), item.get("orgn_odno")
                )
                original = cancelled_by_id.get(original_identity)
                if (
                    original is not None
                    and len(history_by_id[identity]) == 1
                    and identity[1]
                ):
                    cancel = terminal_evidence(
                        item,
                        broker_symbol=original.broker_symbol,
                        side=original.side,
                        quantity=original.quantity,
                    )
                    if cancel is not None and cancel.status == "cancelled":
                        continue
            symbol = item.get("pdno")
            if isinstance(symbol, str) and symbol:
                blocked_symbols.add(symbol)
            else:
                blocked_symbols.update(fills_by_symbol)

        realized_pnl: dict[int, Decimal] = {}
        for symbol, fills in fills_by_symbol.items():
            # Order time is not execution time. Extra fills make the basis
            # ambiguous even if their order times are distinct.
            if symbol in blocked_symbols or len(fills) != 2:
                continue
            opening, closing = sorted(
                fills, key=lambda order: evidence[order.id].order_time
            )
            entry, exit_ = evidence[opening.id], evidence[closing.id]
            if (
                opening.position_effect != "open"
                or closing.position_effect != "close"
                or opening.side == closing.side
                or entry.order_time == exit_.order_time
            ):
                continue
            assert entry.price is not None and exit_.price is not None
            difference = (
                exit_.price - entry.price
                if closing.side == "sell"
                else entry.price - exit_.price
            )
            realized_pnl[opening.id] = Decimal(0)
            realized_pnl[closing.id] = (
                difference * MINI_KOSPI200.multiplier * exit_.quantity
            )

        timestamp = datetime.now(UTC)
        for order in orders:
            proof = evidence.get(order.id)
            if proof is None:
                continue
            pnl = realized_pnl.get(order.id)
            if order.reconciled_at is not None and (
                pnl is None or order.realized_pnl_krw is not None
            ):
                continue
            detail = details[order.id]
            if (
                proof.status == "filled"
                and order.position_effect == "close"
                and pnl is None
            ):
                detail["result"] = "entry_fill_unverified"
                continue
            detail.update(
                result="booked" if commit else "would_book",
                status=proof.status,
                filled_qty=proof.quantity,
                avg_fill_price=proof.price,
                fee=proof.fee,
                realized_pnl_krw=pnl,
            )
            if commit:
                order.status = proof.status
                order.filled_qty = proof.quantity
                order.avg_fill_price = proof.price
                order.fee = proof.fee
                order.realized_pnl_krw = pnl
                if order.reconciled_at is None:
                    order.reconciled_at = timestamp
        if commit and any(detail["result"] == "booked" for detail in details.values()):
            await self._db.commit()
        return list(details.values())

    async def daily_loss_state(self, *, trading_day: str) -> DailyLossState:
        """당일 확정 손실. 정산 증거 없는 당일 place 행이 있으면 ``unverified`` (설계 §8)."""
        base = (
            KISFuturesMockOrderLedger.account_mode == ACCOUNT_MODE,
            KISFuturesMockOrderLedger.trading_day == trading_day,
            KISFuturesMockOrderLedger.operation_kind == "place",
        )
        unsettled = await self._db.scalar(
            select(func.count())
            .select_from(KISFuturesMockOrderLedger)
            .where(
                *base,
                KISFuturesMockOrderLedger.status.in_(_UNSETTLED_STATUSES),
                KISFuturesMockOrderLedger.realized_pnl_krw.is_(None),
            )
        )
        realized_sum = await self._db.scalar(
            select(
                func.coalesce(func.sum(KISFuturesMockOrderLedger.realized_pnl_krw), 0)
            ).where(*base, KISFuturesMockOrderLedger.realized_pnl_krw.is_not(None))
        )
        realized = Decimal(str(realized_sum or 0))
        return DailyLossState(
            realized_loss_krw=max(Decimal(0), -realized),
            unverified=bool(unsettled),
        )

    async def _find(self, client_order_id: str) -> KISFuturesMockOrderLedger | None:
        return (
            await self._db.execute(
                select(KISFuturesMockOrderLedger).where(
                    KISFuturesMockOrderLedger.client_order_id == client_order_id
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    def _replay(
        existing: KISFuturesMockOrderLedger, facts: OrderFacts
    ) -> KISFuturesMockOrderLedger:
        same = (
            existing.contract_symbol == facts.contract_symbol
            and existing.side == facts.side
            and existing.operation_kind == facts.operation_kind
            and Decimal(existing.quantity) == Decimal(facts.quantity)
            and Decimal(existing.price) == facts.price
        )
        if not same:
            raise FuturesLedgerPayloadConflict(facts.client_order_id)
        return existing

    async def _record(
        self,
        facts: OrderFacts,
        *,
        status: str,
        broker_order_id: str | None,
        response_code: str | None,
        response_message: str | None,
        raw_response: dict[str, Any] | None,
        reason: str | None,
    ) -> KISFuturesMockOrderLedger:
        row = KISFuturesMockOrderLedger(
            trade_date=datetime.now(UTC),
            trading_day=facts.trading_day,
            contract_symbol=facts.contract_symbol,
            broker_symbol=facts.broker_symbol,
            product_code=facts.product_code,
            contract_month=facts.contract_month,
            side=facts.side,
            order_type="limit",
            position_effect=facts.position_effect,
            operation_kind=facts.operation_kind,
            quantity=Decimal(facts.quantity),
            price=facts.price,
            amount=facts.amount,
            currency="KRW",
            client_order_id=facts.client_order_id,
            idempotency_key=facts.client_order_id,
            correlation_id=facts.correlation_id,
            broker_order_id=broker_order_id,
            original_order_id=facts.original_order_id,
            account_mode=ACCOUNT_MODE,
            broker=BROKER,
            status=status,
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            strategy=facts.strategy,
            reason=reason,
            reconcile_attempts=0,
        )
        existing = await self._find(facts.client_order_id)
        if existing is not None:
            return self._replay(existing, facts)
        self._db.add(row)
        try:
            await self._db.flush()
        except IntegrityError:
            # 동시 writer 가 UNIQUE(client_order_id) 경쟁에서 이겼다 — 그 행을 재생한다.
            await self._db.rollback()
            existing = await self._find(facts.client_order_id)
            if existing is None:
                raise
            return self._replay(existing, facts)
        await self._db.commit()
        await self._db.refresh(row)
        return row


class FuturesPlaceRecorder:
    """송신 경로용 세션-작업 퍼사드: 예약·원장 오류가 POST 둘레에 반쯤 열린 트랜잭션을 남기지 않는다."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def reserve(
        self, *, idempotency_key: str, symbol: str, side: str
    ) -> SendReservation:
        """송신 전 예약을 커밋한다. ``DuplicateOrderIntent`` = 이미 예약된 키 → 전송 없음."""
        async with self._session_factory() as db:
            row_id = await OrderSendIntentService(db).reserve(
                account_scope=RESERVATION_SCOPE,
                idempotency_key=idempotency_key,
                symbol=symbol,
                side=side,
            )
        return SendReservation(row_id, idempotency_key, side)

    async def release(self, reservation: SendReservation) -> int:
        """관찰한 예약 행 하나만 지운다 (송신하지 않았음이 증명된 경우뿐)."""
        async with self._session_factory() as db:
            return await OrderSendIntentService(db).release_if_matches(
                account_scope=RESERVATION_SCOPE,
                row_id=reservation.row_id,
                idempotency_key=reservation.idempotency_key,
                side=reservation.side,
            )

    async def existing_status(self, client_order_id: str) -> str | None:
        async with self._session_factory() as db:
            return await FuturesMockOrderLedgerService(db).get_status(client_order_id)

    async def daily_loss_state(self, *, trading_day: str) -> DailyLossState:
        async with self._session_factory() as db:
            return await FuturesMockOrderLedgerService(db).daily_loss_state(
                trading_day=trading_day
            )

    async def get_original_order(
        self, *, trading_day: str, broker_order_id: str
    ) -> OriginalOrder | None:
        async with self._session_factory() as db:
            row = await FuturesMockOrderLedgerService(db).get_by_broker_order(
                trading_day=trading_day, broker_order_id=broker_order_id
            )
            if row is None or row.broker_order_id is None:
                return None
            return OriginalOrder(
                ledger_id=row.id,
                status=row.status,
                client_order_id=row.client_order_id,
                broker_order_id=row.broker_order_id,
                contract_symbol=row.contract_symbol,
                broker_symbol=row.broker_symbol,
                product_code=row.product_code,
                contract_month=row.contract_month,
                side=row.side,
                position_effect=row.position_effect,
                price=Decimal(row.price),
                amount=Decimal(row.amount),
                trading_day=row.trading_day,
            )

    async def record_send(self, facts: OrderFacts, **kwargs: Any) -> LedgerReceipt:
        async with self._session_factory() as db:
            row = await FuturesMockOrderLedgerService(db).record_send(facts, **kwargs)
            return LedgerReceipt(row.id, row.status, row.client_order_id)

    async def record_uncertain(self, facts: OrderFacts, **kwargs: Any) -> LedgerReceipt:
        async with self._session_factory() as db:
            row = await FuturesMockOrderLedgerService(db).record_uncertain(
                facts, **kwargs
            )
            return LedgerReceipt(row.id, row.status, row.client_order_id)


__all__ = (
    "DuplicateOrderIntent",
    "FuturesLedgerPayloadConflict",
    "FuturesMockOrderLedgerService",
    "FuturesPlaceRecorder",
    "LedgerReceipt",
    "OrderFacts",
    "OriginalOrder",
    "SendReservation",
    "build_cancel_client_order_id",
    "build_client_order_id",
    "kst_trading_day",
)
