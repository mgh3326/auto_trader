"""Read-only validation between fill observations and legacy projections."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.execution_ledger import ExecutionLedger
from app.models.fill_observation import FillObservation
from app.models.review import Trade
from app.services.fill_observation.contracts import (
    FillDualReadStatus,
    FillDualReadValidation,
)
from app.services.fill_observation.errors import InvalidFillEvidence


def _scope_text(value: str, *, field: str, lower: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InvalidFillEvidence(f"{field} must not be blank")
    return normalized.lower() if lower else normalized


def _optional_decimal(value: object | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _classify_dual_read(
    *,
    observation_count: int,
    observation_quantity: Decimal,
    review_trade_quantity: Decimal | None,
    execution_ledger_quantity: Decimal | None,
) -> tuple[FillDualReadStatus, tuple[str, ...]]:
    observation_present = observation_count > 0
    legacy = {
        "review.trades": review_trade_quantity,
        "review.execution_ledger": execution_ledger_quantity,
    }
    legacy_present = any(value is not None for value in legacy.values())
    if observation_present and legacy_present:
        mismatches = tuple(
            source
            for source, quantity in legacy.items()
            if quantity is not None and quantity != observation_quantity
        )
        return (
            FillDualReadStatus.MISMATCH if mismatches else FillDualReadStatus.MATCH,
            mismatches,
        )
    if observation_present:
        return FillDualReadStatus.NEW_ONLY, ()
    if legacy_present:
        return FillDualReadStatus.LEGACY_ONLY, ()
    return FillDualReadStatus.EMPTY, ()


class FillObservationDualReader:
    """Compare quantities without changing either read source."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self._session_factory = session_factory

    async def validate_order(
        self,
        *,
        broker: str,
        account_ref: str,
        account_mode: str,
        venue: str,
        order_id: str,
    ) -> FillDualReadValidation:
        normalized_broker = _scope_text(broker, field="broker", lower=True)
        normalized_account = _scope_text(account_ref, field="account_ref")
        normalized_mode = _scope_text(
            account_mode,
            field="account_mode",
            lower=True,
        )
        normalized_venue = _scope_text(venue, field="venue", lower=True)
        normalized_order = _scope_text(order_id, field="order_id")

        async with self._session_factory() as session:
            observation_result = await session.execute(
                select(
                    func.count(FillObservation.id),
                    func.coalesce(
                        func.sum(FillObservation.fill_delta_quantity),
                        0,
                    ),
                )
                .where(FillObservation.broker == normalized_broker)
                .where(FillObservation.account_ref == normalized_account)
                .where(FillObservation.account_mode == normalized_mode)
                .where(FillObservation.venue == normalized_venue)
                .where(FillObservation.order_id == normalized_order)
            )
            observation_count_raw, observation_quantity_raw = observation_result.one()
            review_trade_quantity_raw = await session.scalar(
                select(func.sum(Trade.quantity))
                .where(Trade.account == normalized_account)
                .where(Trade.order_id == normalized_order)
            )
            execution_ledger_quantity_raw = await session.scalar(
                select(func.sum(ExecutionLedger.filled_qty))
                .where(ExecutionLedger.broker == normalized_broker)
                .where(ExecutionLedger.account_mode == normalized_mode)
                .where(ExecutionLedger.venue == normalized_venue)
                .where(ExecutionLedger.broker_order_id == normalized_order)
            )

        observation_count = int(observation_count_raw or 0)
        observation_quantity = Decimal(str(observation_quantity_raw or 0))
        review_trade_quantity = _optional_decimal(review_trade_quantity_raw)
        execution_ledger_quantity = _optional_decimal(execution_ledger_quantity_raw)
        status, mismatches = _classify_dual_read(
            observation_count=observation_count,
            observation_quantity=observation_quantity,
            review_trade_quantity=review_trade_quantity,
            execution_ledger_quantity=execution_ledger_quantity,
        )
        return FillDualReadValidation(
            broker=normalized_broker,
            account_ref=normalized_account,
            account_mode=normalized_mode,
            venue=normalized_venue,
            order_id=normalized_order,
            observation_count=observation_count,
            observation_quantity=observation_quantity,
            review_trade_quantity=review_trade_quantity,
            execution_ledger_quantity=execution_ledger_quantity,
            status=status,
            mismatched_sources=mismatches,
        )


__all__ = ["FillObservationDualReader"]
