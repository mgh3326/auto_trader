"""ROB-1036 — eligibility-aware read models.

``status == 'closed' AND brier_score IS NOT NULL`` is *not* an eligibility
predicate: it says the row was scored, not that it belongs in the cohort. The
calibration aggregate therefore demands an explicit contract version and an
explicit :class:`EligibilityPredicate`, and every result carries the cohort it
was produced under plus separately-reported included / excluded / unidentifiable
counts — so a caller can never present a cohort as if nothing had been held back
or folded in.

The cohort vocabulary itself lives in :mod:`.cohort` (pure, no aggregate import)
and is re-exported here for callers that already import this module. The
calibration aggregate lives in ``trade_journal.forecast_service``; this module
adds the decided-only convenience wrapper and the generic partition helper that
trade-performance / PnL consumers reuse.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invalid_sample_eligibility.cohort import (
    COMPATIBILITY_CALIBRATION_COHORT,
    COMPATIBILITY_STAGE,
    DECIDED_ONLY_CALIBRATION_COHORT,
    DECIDED_ONLY_STAGE,
    DECIDED_ONLY_TRADE_PERFORMANCE_COHORT,
    EligibilityBucket,
    EligibilityDomain,
    EligibilityPartition,
    EligibilityPredicate,
    partition_by_eligibility,
)
from app.services.invalid_sample_eligibility.contract import CONTRACT_VERSION
from app.services.trade_journal.forecast_service import (
    build_forecast_calibration_aggregate,
)


async def build_decided_only_forecast_calibration_aggregate(
    db: AsyncSession,
    *,
    contract_version: str = CONTRACT_VERSION,
    group_by: str = "created_by",
    created_by: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Calibration over decided inclusions only — the option-① end-state cohort.

    Available now so a caller that wants the fully-decided cohort can ask for it
    today, and so promoting the default is a one-line swap. Today this returns an
    empty cohort until eligibility decisions exist; the reported
    ``unidentifiable`` count says exactly how many rows are waiting on a decision.
    """

    return await build_forecast_calibration_aggregate(
        db,
        contract_version=contract_version,
        predicate=DECIDED_ONLY_CALIBRATION_COHORT,
        group_by=group_by,
        created_by=created_by,
        symbol=symbol,
        instrument_type=instrument_type,
        days=days,
    )


__all__ = [
    "COMPATIBILITY_CALIBRATION_COHORT",
    "COMPATIBILITY_STAGE",
    "DECIDED_ONLY_CALIBRATION_COHORT",
    "DECIDED_ONLY_STAGE",
    "DECIDED_ONLY_TRADE_PERFORMANCE_COHORT",
    "EligibilityBucket",
    "EligibilityDomain",
    "EligibilityPartition",
    "EligibilityPredicate",
    "build_decided_only_forecast_calibration_aggregate",
    "partition_by_eligibility",
]
