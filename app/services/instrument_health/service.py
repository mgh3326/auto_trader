"""ROB-285 — CryptoInstrumentHealthService (the public write surface).

All writes to ``crypto_instrument_health`` go through this service. Direct
SQL or repository imports from outside this package are forbidden. The
test ``tests/services/instrument_health/test_instrument_health_service``
locks this invariant.

State lifecycle (see ``docs/runbooks/binance-public-market-data.md``):

- ``healthy`` (default) → ``degraded`` (WS unhealthy after ≥3 reconnect
  failures) → ``healthy`` (next successful reconnect).
- ``healthy`` → ``rate_limited`` (REST 429/418 received) → ``healthy``
  (after ``retry_after_at`` passes).
- ``healthy``/``degraded`` → ``manual_backfill_required`` (gap > cap on
  reconnect) → ``healthy`` only after an operator calls
  ``clear_manual_backfill``.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.instrument_health.repository import (
    CryptoInstrumentHealthRepository,
)

logger = logging.getLogger("app.services.instrument_health")


class InstrumentHealthState(str, enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    MANUAL_BACKFILL_REQUIRED = "manual_backfill_required"


def _emit_sentry_event(state: InstrumentHealthState, *, instrument_id: int, reason: str) -> None:
    """Sentry event for the only state that needs operator attention.

    Per Open items lean #5 (Task 8): only ``manual_backfill_required``
    transitions emit Sentry events. ``degraded`` and ``rate_limited`` are
    transient and visible via logs and the table itself. Fail-open —
    Sentry import or call problems must never break the adapter.
    """
    if state is not InstrumentHealthState.MANUAL_BACKFILL_REQUIRED:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"crypto_instrument_health manual_backfill_required "
            f"instrument_id={instrument_id} reason={reason!r}",
            level="warning",
        )
    except Exception:  # noqa: BLE001 — intentional fail-open
        return


class CryptoInstrumentHealthService:
    """Service surface for ``crypto_instrument_health``."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session
        self._repo = CryptoInstrumentHealthRepository(session=session)

    async def get_state(self, instrument_id: int) -> InstrumentHealthState:
        row = await self._repo.get(instrument_id)
        if row is None:
            return InstrumentHealthState.HEALTHY
        return InstrumentHealthState(row.state)

    async def record_degraded(self, instrument_id: int, *, reason: str) -> None:
        await self._repo.upsert(
            instrument_id=instrument_id,
            state=InstrumentHealthState.DEGRADED.value,
            reason=reason,
        )
        logger.warning(
            "crypto_instrument_health state=degraded "
            "instrument_id=%s reason=%r",
            instrument_id,
            reason,
        )

    async def record_rate_limited(
        self,
        instrument_id: int,
        *,
        retry_after_at: dt.datetime,
        reason: str,
    ) -> None:
        await self._repo.upsert(
            instrument_id=instrument_id,
            state=InstrumentHealthState.RATE_LIMITED.value,
            reason=reason,
            retry_after_at=retry_after_at,
        )
        logger.warning(
            "crypto_instrument_health state=rate_limited "
            "instrument_id=%s retry_after_at=%s reason=%r",
            instrument_id,
            retry_after_at.isoformat(),
            reason,
        )

    async def record_manual_backfill_required(
        self,
        instrument_id: int,
        *,
        reason: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._repo.upsert(
            instrument_id=instrument_id,
            state=InstrumentHealthState.MANUAL_BACKFILL_REQUIRED.value,
            reason=reason,
            extra_metadata=extra_metadata,
        )
        logger.error(
            "crypto_instrument_health state=manual_backfill_required "
            "instrument_id=%s reason=%r",
            instrument_id,
            reason,
        )
        _emit_sentry_event(
            InstrumentHealthState.MANUAL_BACKFILL_REQUIRED,
            instrument_id=instrument_id,
            reason=reason,
        )

    async def record_recovered(self, instrument_id: int) -> None:
        """Transition from ``degraded`` or ``rate_limited`` back to ``healthy``.

        Refuses to clear ``manual_backfill_required`` — use
        ``clear_manual_backfill`` for that explicit operator path.
        """
        current = await self.get_state(instrument_id)
        if current is InstrumentHealthState.MANUAL_BACKFILL_REQUIRED:
            raise ValueError(
                f"crypto_instrument_health instrument_id={instrument_id} is in "
                "manual_backfill_required; refusing automatic recovery. "
                "Use clear_manual_backfill(operator=...) instead."
            )
        await self._repo.upsert(
            instrument_id=instrument_id,
            state=InstrumentHealthState.HEALTHY.value,
            reason=None,
            attempts=0,
            retry_after_at=None,
        )
        logger.info(
            "crypto_instrument_health state=healthy (recovered) instrument_id=%s",
            instrument_id,
        )

    async def clear_manual_backfill(
        self,
        instrument_id: int,
        *,
        operator: str,
    ) -> None:
        """Operator-only path to clear a ``manual_backfill_required`` flag.

        The ``operator`` identifier is logged + persisted in
        ``metadata.cleared_by`` so the audit trail is preserved.
        """
        await self._repo.upsert(
            instrument_id=instrument_id,
            state=InstrumentHealthState.HEALTHY.value,
            reason=None,
            attempts=0,
            retry_after_at=None,
            extra_metadata={
                "cleared_by": operator,
                "cleared_at": dt.datetime.now(tz=dt.UTC).isoformat(),
            },
        )
        logger.info(
            "crypto_instrument_health state=healthy "
            "(manual_backfill_required cleared) instrument_id=%s operator=%s",
            instrument_id,
            operator,
        )
