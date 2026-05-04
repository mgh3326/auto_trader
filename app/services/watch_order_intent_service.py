"""Watch order intent ledger writer (ROB-103).

This is the only writer to ``review.watch_order_intent_ledger``. It
- calls ``app.services.exchange_rate_service.get_usd_krw_quote`` for US watches,
- defers to ``watch_order_intent_preview_builder.build_preview`` for the
  pure preview/cap/FX evaluation,
- inserts a single ledger row,
- handles the partial-unique-index conflict by reading back the existing
  ``previewed`` row and returning ``dedupe_hit``.

It must never call any broker submit endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import WatchOrderIntentLedger
from app.services.exchange_rate_service import get_usd_krw_quote
from app.services.watch_intent_policy import IntentPolicy
from app.services.watch_order_intent_preview_builder import (
    ACCOUNT_MODE,
    EXECUTION_SOURCE,
    IntentBuildFailure,
    IntentBuildSuccess,
    build_preview,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IntentEmissionResult:
    status: Literal["previewed", "failed", "dedupe_hit"]
    ledger_id: int | None
    correlation_id: str | None
    idempotency_key: str
    market: str
    symbol: str
    side: str
    quantity: Decimal | None
    limit_price: Decimal | None
    blocked_by: str | None
    reason: str | None

    def to_alert_dict(self) -> dict:
        return {
            "market": self.market,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": float(self.quantity) if self.quantity is not None else None,
            "limit_price": float(self.limit_price) if self.limit_price is not None else None,
            "status": self.status,
            "ledger_id": self.ledger_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
        }


class FxProvider(Protocol):
    async def get_quote(self) -> Decimal | None: ...


class _DefaultFxProvider:
    async def get_quote(self) -> Decimal | None:
        try:
            rate = await get_usd_krw_quote()
        except Exception as exc:
            logger.warning("FX quote fetch failed: %s", exc)
            return None
        if rate is None:
            return None
        return Decimal(str(rate))


def _build_idempotency_key(watch: dict, side: str, kst_date: str) -> str:
    return ":".join(
        [
            str(watch["market"]),
            str(watch["target_kind"]),
            str(watch["symbol"]),
            str(watch["condition_type"]),
            str(watch["threshold_key"]),
            "create_order_intent",
            side,
            kst_date,
        ]
    )


class WatchOrderIntentService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        fx_provider: FxProvider | None = None,
    ) -> None:
        self._db = db
        self._fx = fx_provider or _DefaultFxProvider()

    async def emit_intent(
        self,
        *,
        watch: dict,
        policy: IntentPolicy,
        triggered_value: Decimal,
        kst_date: str,
        correlation_id: str,
    ) -> IntentEmissionResult:
        idempotency_key = _build_idempotency_key(watch, policy.side, kst_date)

        fx_quote: Decimal | None = None
        if watch["market"] == "us":
            fx_quote = await self._fx.get_quote()

        result = build_preview(
            policy=policy,
            watch=watch,
            triggered_value=triggered_value,
            fx_quote=fx_quote,
            kst_date=kst_date,
        )

        if isinstance(result, IntentBuildFailure):
            return await self._insert_failed(
                watch=watch,
                policy=policy,
                triggered_value=triggered_value,
                kst_date=kst_date,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                failure=result,
            )

        return await self._insert_or_dedupe_previewed(
            watch=watch,
            policy=policy,
            triggered_value=triggered_value,
            kst_date=kst_date,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            success=result,
        )

    async def _insert_or_dedupe_previewed(
        self,
        *,
        watch: dict,
        policy: IntentPolicy,
        triggered_value: Decimal,
        kst_date: str,
        correlation_id: str,
        idempotency_key: str,
        success: IntentBuildSuccess,
    ) -> IntentEmissionResult:
        line = success.preview_line
        row = WatchOrderIntentLedger(
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=watch["market"],
            target_kind=watch["target_kind"],
            symbol=watch["symbol"],
            condition_type=watch["condition_type"],
            threshold=watch["threshold"],
            threshold_key=watch["threshold_key"],
            action="create_order_intent",
            side=policy.side,
            account_mode=ACCOUNT_MODE,
            execution_source=EXECUTION_SOURCE,
            lifecycle_state="previewed",
            quantity=line.quantity,
            limit_price=line.limit_price,
            notional=line.notional,
            currency=line.currency,
            notional_krw_input=policy.notional_krw,
            max_notional_krw=policy.max_notional_krw,
            notional_krw_evaluated=success.notional_krw_evaluated,
            fx_usd_krw_used=success.fx_usd_krw_used,
            approval_required=True,
            execution_allowed=False,
            blocking_reasons=[],
            blocked_by=None,
            detail={"basket_preview": success.basket.model_dump(mode="json")},
            preview_line=line.model_dump(mode="json"),
            triggered_value=triggered_value,
            kst_date=kst_date,
        )
        self._db.add(row)
        try:
            await self._db.flush()
        except IntegrityError:
            await self._db.rollback()
            existing = (
                await self._db.execute(
                    select(WatchOrderIntentLedger).where(
                        WatchOrderIntentLedger.idempotency_key == idempotency_key,
                        WatchOrderIntentLedger.lifecycle_state == "previewed",
                    )
                )
            ).scalars().one()
            return IntentEmissionResult(
                status="dedupe_hit",
                ledger_id=existing.id,
                correlation_id=existing.correlation_id,
                idempotency_key=idempotency_key,
                market=existing.market,
                symbol=existing.symbol,
                side=existing.side,
                quantity=existing.quantity,
                limit_price=existing.limit_price,
                blocked_by=None,
                reason="already_previewed_today",
            )
        await self._db.commit()
        return IntentEmissionResult(
            status="previewed",
            ledger_id=row.id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=row.market,
            symbol=row.symbol,
            side=row.side,
            quantity=line.quantity,
            limit_price=line.limit_price,
            blocked_by=None,
            reason=None,
        )

    async def _insert_failed(
        self,
        *,
        watch: dict,
        policy: IntentPolicy,
        triggered_value: Decimal,
        kst_date: str,
        correlation_id: str,
        idempotency_key: str,
        failure: IntentBuildFailure,
    ) -> IntentEmissionResult:
        preview_payload = {
            "lifecycle_state": "failed",
            "blocked_by": failure.blocked_by,
            "blocking_reasons": failure.blocking_reasons,
            "quantity": str(failure.quantity) if failure.quantity is not None else None,
            "limit_price": (
                str(failure.limit_price) if failure.limit_price is not None else None
            ),
            "currency": failure.currency,
        }
        row = WatchOrderIntentLedger(
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=watch["market"],
            target_kind=watch["target_kind"],
            symbol=watch["symbol"],
            condition_type=watch["condition_type"],
            threshold=watch["threshold"],
            threshold_key=watch["threshold_key"],
            action="create_order_intent",
            side=policy.side,
            account_mode=ACCOUNT_MODE,
            execution_source=EXECUTION_SOURCE,
            lifecycle_state="failed",
            quantity=failure.quantity,
            limit_price=failure.limit_price,
            notional=None,
            currency=failure.currency,
            notional_krw_input=policy.notional_krw,
            max_notional_krw=policy.max_notional_krw,
            notional_krw_evaluated=failure.notional_krw_evaluated,
            fx_usd_krw_used=failure.fx_usd_krw_used,
            approval_required=True,
            execution_allowed=False,
            blocking_reasons=failure.blocking_reasons,
            blocked_by=failure.blocked_by,
            detail={"failure_input": {"sizing_source": "notional_krw" if policy.quantity is None else "quantity"}},
            preview_line=preview_payload,
            triggered_value=triggered_value,
            kst_date=kst_date,
        )
        self._db.add(row)
        await self._db.commit()
        return IntentEmissionResult(
            status="failed",
            ledger_id=row.id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=row.market,
            symbol=row.symbol,
            side=row.side,
            quantity=failure.quantity,
            limit_price=failure.limit_price,
            blocked_by=failure.blocked_by,
            reason=failure.blocked_by,
        )


__all__ = [
    "FxProvider",
    "IntentEmissionResult",
    "WatchOrderIntentService",
]
