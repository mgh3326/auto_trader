"""송신 전 귀속 신호 (hk 5946) — `kiwoom_mock_attribution` 과 같은 형태·순서 (하드룰 7 선물판).

1. :func:`resolve_attribution` 는 I/O 없이 ``(correlation_id, strategy, signal_source)`` 를
   만들거나 :class:`MissingAttribution` 을 던진다. 귀속 불가는 값이 아니라 에러이고
   placeholder strategy 는 없다 (정의는 ``kis_mock_attribution`` 하나를 그대로 쓴다).
2. :func:`record_signal` 은 ``order_send_intents`` 예약과 브로커 호출 **이전에** 신호 행을
   커밋한다. 커밋하지 못하면 호출자는 주문을 보내지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import cast, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.kis_futures_mock import KISFuturesMockSignalLedger
from app.services.kis_mock_attribution import (
    DEFAULT_SIGNAL_SOURCE,
    MissingAttribution,
    validate_strategy,
)
from app.services.live_correlation import live_correlation_id

from .constants import ACCOUNT_MODE

CORRELATION_CONSTRAINT = "uq_kis_futures_mock_signal_ledger_correlation_id"

NOT_SENT_OUTCOME = "suppressed"
#: 브로커가 주문을 받지 못했음이 증명되는 경로만 — 이 목록을 넓히면 계약 변경이다.
NOT_SENT_REASONS: tuple[str, ...] = (
    "rules_refused",
    "duplicate_intent",
    "send_intent_unavailable",
    "pre_dispatch_error",
)


@dataclass(frozen=True, slots=True)
class FuturesAttribution:
    correlation_id: str
    strategy: str
    signal_source: str


@dataclass(frozen=True, slots=True)
class RecordedSignal:
    row_id: int
    #: 이 호출이 행을 만들었는가 — 재생은 앞선 시도의 행을 돌려준다.
    created: bool


class SignalStrategyConflict(RuntimeError):
    """같은 correlation id 가 이미 다른 strategy 에 귀속돼 있다."""


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
    price: Decimal,
    quantity: int,
    kst_trade_day: str | None = None,
    rung: int | str | None = None,
) -> str:
    return live_correlation_id(
        account_scope=ACCOUNT_MODE,
        symbol=symbol,
        side=side,
        price=price,
        quantity=Decimal(quantity),
        kst_trade_day=kst_trade_day or now_kst().strftime("%Y-%m-%d"),
        rung="" if rung is None else str(rung),
    )


def resolve_attribution(
    *,
    symbol: str,
    side: str,
    price: Decimal,
    quantity: int,
    strategy: str | None,
    signal_source: str | None = None,
    correlation_id: str | None = None,
    kst_trade_day: str | None = None,
    rung: int | str | None = None,
) -> FuturesAttribution:
    """귀속 3종을 확정하거나 :class:`MissingAttribution` 을 던진다 (순수)."""
    resolved = (
        validate_strategy(strategy, lane=ACCOUNT_MODE) if strategy is not None else None
    )
    if resolved is None:
        raise MissingAttribution(("strategy",))
    return FuturesAttribution(
        correlation_id=_clean(correlation_id)
        or mint_correlation_id(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            kst_trade_day=kst_trade_day,
            rung=rung,
        ),
        strategy=resolved,
        signal_source=_clean(signal_source) or DEFAULT_SIGNAL_SOURCE,
    )


async def record_signal(
    db: AsyncSession,
    *,
    attribution: FuturesAttribution,
    contract_symbol: str,
    broker_symbol: str,
    side: str,
    quantity: int,
    price: Decimal,
    kst_date: str,
    detail: dict[str, Any] | None = None,
) -> RecordedSignal:
    """신호 행을 커밋한다 (correlation id 멱등). 호출자가 이 커밋 뒤에만 진행한다."""
    stmt = (
        pg_insert(KISFuturesMockSignalLedger)
        .values(
            correlation_id=attribution.correlation_id,
            strategy=attribution.strategy,
            signal_source=attribution.signal_source,
            account_mode=ACCOUNT_MODE,
            contract_symbol=contract_symbol,
            broker_symbol=broker_symbol,
            side=side,
            intended_quantity=_to_decimal(quantity),
            intended_price=_to_decimal(price),
            decision="order",
            outcome_state="recorded",
            detail=detail or {},
            kst_date=kst_date,
        )
        .on_conflict_do_nothing(constraint=CORRELATION_CONSTRAINT)
        .returning(KISFuturesMockSignalLedger.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    created = inserted is not None
    if inserted is None:
        existing = (
            await db.execute(
                select(
                    KISFuturesMockSignalLedger.id, KISFuturesMockSignalLedger.strategy
                ).where(
                    KISFuturesMockSignalLedger.correlation_id
                    == attribution.correlation_id
                )
            )
        ).first()
        if existing is not None and existing.strategy != attribution.strategy:
            # id 는 주문을 가리키지 전략을 가리키지 않는다 — 다른 전략의 행을
            # 다시 붙이면 실제 주문이 엉뚱한 소유자에게 귀속된다.
            raise SignalStrategyConflict(
                "futures mock signal row is attributed to a different strategy"
            )
        inserted = existing.id if existing is not None else None
    await db.commit()
    if inserted is None:  # pragma: no cover - 읽을 수 없는 충돌
        raise RuntimeError("futures mock signal row neither inserted nor found")
    return RecordedSignal(row_id=int(inserted), created=created)


async def mark_signal_outcome(
    db: AsyncSession,
    *,
    correlation_id: str,
    outcome_state: str,
    detail_patch: dict[str, Any] | None = None,
) -> int:
    """이미 있던 신호 행의 송신 후 결과를 적는다 (귀속을 만들지 않는다)."""
    values: dict[str, Any] = {"outcome_state": outcome_state}
    if detail_patch:
        values["detail"] = KISFuturesMockSignalLedger.detail.op("||")(
            cast(detail_patch, JSONB)
        )
    result = await db.execute(
        update(KISFuturesMockSignalLedger)
        .where(KISFuturesMockSignalLedger.correlation_id == correlation_id)
        .values(**values)
    )
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def mark_signal_not_sent(
    db: AsyncSession,
    *,
    correlation_id: str,
    reason: str,
    detail_patch: dict[str, Any] | None = None,
) -> int:
    """송신이 증명적으로 없었던 신호를 ``suppressed`` 로 닫는다.

    ``outcome_state='recorded'`` 위의 compare-and-set 이다 — 앞선 시도가 이미
    ``submitted``/``failed`` 로 닫은 행은 되돌리지 않는다 (중복 의도 거부가 이 함수에
    닿을 때 첫 시도가 실제 주문을 냈을 수 있다).
    """
    if reason not in NOT_SENT_REASONS:
        raise ValueError(f"unknown futures mock not-sent reason: {reason!r}")
    values: dict[str, Any] = {
        "outcome_state": NOT_SENT_OUTCOME,
        "suppressed_reason": reason,
    }
    if detail_patch:
        values["detail"] = KISFuturesMockSignalLedger.detail.op("||")(
            cast(detail_patch, JSONB)
        )
    result = await db.execute(
        update(KISFuturesMockSignalLedger)
        .where(
            KISFuturesMockSignalLedger.correlation_id == correlation_id,
            KISFuturesMockSignalLedger.outcome_state == "recorded",
        )
        .values(**values)
    )
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


class FuturesSignalRecorder:
    """작업마다 자기 세션을 여는 퍼사드 — 예외가 브로커 POST 둘레에 열린 트랜잭션을 남기지 않는다."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def record(self, **kwargs: Any) -> RecordedSignal:
        async with self._session_factory() as db:
            return await record_signal(db, **kwargs)

    async def mark_outcome(self, **kwargs: Any) -> int:
        async with self._session_factory() as db:
            return await mark_signal_outcome(db, **kwargs)

    async def mark_not_sent(self, **kwargs: Any) -> int:
        async with self._session_factory() as db:
            return await mark_signal_not_sent(db, **kwargs)


__all__ = (
    "FuturesAttribution",
    "FuturesSignalRecorder",
    "MissingAttribution",
    "NOT_SENT_OUTCOME",
    "NOT_SENT_REASONS",
    "RecordedSignal",
    "SignalStrategyConflict",
    "mark_signal_not_sent",
    "mark_signal_outcome",
    "mint_correlation_id",
    "record_signal",
    "resolve_attribution",
)
