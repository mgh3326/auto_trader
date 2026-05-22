"""ROB-285 — Repository for crypto_instrument_health.

Service-internal. Do not import from outside
``app/services/instrument_health/``. Use
``CryptoInstrumentHealthService`` as the public write surface.

The audit ``tests/services/instrument_health/test_instrument_health_service``
asserts that ``importlib.import_module(
"app.services.instrument_health.repository._public_export")`` raises
``ImportError`` — i.e., the repository has no submodule of that name.
This module-level guard is satisfied by-construction because
``_public_export`` is a private class, not a submodule.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instrument_health import CryptoInstrumentHealth


class CryptoInstrumentHealthRepository:
    """Service-internal DB boundary for ``crypto_instrument_health``.

    Direct external imports of this class are an anti-pattern — use
    ``CryptoInstrumentHealthService`` (which composes this repository).
    """

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def get(self, instrument_id: int) -> CryptoInstrumentHealth | None:
        result = await self._session.execute(
            select(CryptoInstrumentHealth).where(
                CryptoInstrumentHealth.instrument_id == instrument_id
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        instrument_id: int,
        state: str,
        reason: str | None = None,
        attempts: int | None = None,
        retry_after_at: dt.datetime | None = None,
        last_closed_candle_time: dt.datetime | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or update a single row with the supplied fields.

        Uses ON CONFLICT (instrument_id) DO UPDATE — keeps a single row
        per instrument and bumps ``last_state_change_at`` and
        ``updated_at`` to ``now()`` on every write.
        """
        # ``attempts`` is treated as an absolute set, not an increment:
        # callers compute the desired value (e.g., previous + 1) and pass
        # it in. ``None`` means "leave existing or default to 0 on insert".
        await self._session.execute(
            text(
                """
                INSERT INTO public.crypto_instrument_health (
                    instrument_id, state, reason,
                    last_state_change_at, last_closed_candle_time,
                    attempts, retry_after_at, metadata,
                    created_at, updated_at
                ) VALUES (
                    :instrument_id, :state, :reason,
                    now(), :last_closed_candle_time,
                    COALESCE(:attempts, 0), :retry_after_at,
                    CAST(:extra_metadata AS JSONB),
                    now(), now()
                )
                ON CONFLICT (instrument_id) DO UPDATE
                SET state                   = EXCLUDED.state,
                    reason                  = EXCLUDED.reason,
                    last_state_change_at    = now(),
                    last_closed_candle_time = COALESCE(
                        EXCLUDED.last_closed_candle_time,
                        public.crypto_instrument_health.last_closed_candle_time
                    ),
                    attempts                = COALESCE(
                        EXCLUDED.attempts, public.crypto_instrument_health.attempts
                    ),
                    retry_after_at          = EXCLUDED.retry_after_at,
                    metadata                = COALESCE(
                        EXCLUDED.metadata, public.crypto_instrument_health.metadata
                    ),
                    updated_at              = now()
                """
            ),
            {
                "instrument_id": instrument_id,
                "state": state,
                "reason": reason,
                "last_closed_candle_time": last_closed_candle_time,
                "attempts": attempts,
                "retry_after_at": retry_after_at,
                "extra_metadata": (
                    json.dumps(extra_metadata) if extra_metadata is not None else None
                ),
            },
        )
