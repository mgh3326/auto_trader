"""Pre-submit attribution for the NH counterfactual mirror lane."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import NHMockSignalLedger
from app.services.kis_mock_attribution import (
    MIRROR_SIGNAL_SOURCE,
    MIRROR_STRATEGY,
    MissingAttribution,
    mint_correlation_id,
    validate_strategy,
)


@dataclass(frozen=True, slots=True)
class NHMockAttribution:
    correlation_id: str
    strategy: str
    signal_source: str
    counterfactual_of: uuid.UUID


def resolve_attribution(
    *,
    symbol: str,
    side: str,
    price: Any,
    quantity: Any,
    strategy: str | None,
    correlation_id: str | None,
    counterfactual_of: str | uuid.UUID | None,
    mirror_cohort: str | None,
) -> NHMockAttribution:
    """Require an actual mirror cohort and original ledger UUID before send."""

    if mirror_cohort != "mock_counterfactual":
        raise MissingAttribution(("mirror_cohort",))
    if counterfactual_of is None:
        raise MissingAttribution(("counterfactual_of",))
    try:
        original = (
            counterfactual_of
            if isinstance(counterfactual_of, uuid.UUID)
            else uuid.UUID(str(counterfactual_of))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise MissingAttribution(("counterfactual_of",)) from exc
    # Explicit strategy is permitted only when it is real; absent derives the
    # same dedicated mirror-lane owner used by kis_mock.
    resolved_strategy = (
        validate_strategy(strategy) if strategy is not None else MIRROR_STRATEGY
    )
    return NHMockAttribution(
        correlation_id=correlation_id
        or mint_correlation_id(
            symbol=symbol, side=side, price=price, quantity=quantity
        ),
        strategy=resolved_strategy,
        signal_source=MIRROR_SIGNAL_SOURCE,
        counterfactual_of=original,
    )


async def record_signal(
    db: AsyncSession,
    *,
    attribution: NHMockAttribution,
    symbol: str,
    side: str,
    quantity: Any,
    price: Any,
) -> None:
    """Commit attribution before broker send; idempotent only for same signal."""

    await db.execute(
        pg_insert(NHMockSignalLedger)
        .values(
            correlation_id=attribution.correlation_id,
            strategy=attribution.strategy,
            signal_source=attribution.signal_source,
            symbol=symbol,
            side=side,
            intended_quantity=Decimal(str(quantity)),
            intended_price=Decimal(str(price)),
            counterfactual_of=attribution.counterfactual_of,
        )
        .on_conflict_do_nothing(constraint="uq_nh_mock_signal_correlation_id")
    )
    await db.commit()
