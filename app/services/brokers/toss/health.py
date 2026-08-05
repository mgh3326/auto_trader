"""Best-effort shared visibility for Toss API failures.

The production Toss callers keep their existing response semantics.  They only
publish a small, secret-free marker after an upstream error so a concurrent,
optional bulk reader can stop itself rather than adding load while the shared
Toss surface is unhealthy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.brokers.toss.auth import _get_redis_client

_ERROR_EVENT_KEY = "toss:health:last_error"
_ERROR_SEQUENCE_KEY = "toss:health:error_sequence"
_ERROR_TTL_SECONDS = 60 * 60

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TossApiErrorSignal:
    """A redacted marker for a Toss response or transport failure."""

    sequence: int
    observed_at: str
    status_code: int | None
    error_type: str
    error_code: str | None


def _encode(signal: TossApiErrorSignal) -> str:
    return json.dumps(
        {
            "sequence": signal.sequence,
            "observed_at": signal.observed_at,
            "status_code": signal.status_code,
            "error_type": signal.error_type,
            "error_code": signal.error_code,
        },
        separators=(",", ":"),
    )


def _decode(raw: str | bytes | None) -> TossApiErrorSignal | None:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload: Any = json.loads(raw)
        return TossApiErrorSignal(
            sequence=int(payload["sequence"]),
            observed_at=str(payload["observed_at"]),
            status_code=(
                int(payload["status_code"])
                if payload.get("status_code") is not None
                else None
            ),
            error_type=str(payload["error_type"]),
            error_code=(
                str(payload["error_code"])
                if payload.get("error_code") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def publish_toss_api_error(
    *,
    status_code: int | None,
    error_type: str,
    error_code: str | None = None,
) -> None:
    """Publish a redacted error marker without changing the caller outcome."""
    try:
        redis_client = await _get_redis_client()
        sequence = int(await redis_client.incr(_ERROR_SEQUENCE_KEY))
        signal = TossApiErrorSignal(
            sequence=sequence,
            observed_at=datetime.now(UTC).isoformat(),
            status_code=status_code,
            error_type=error_type,
            error_code=error_code,
        )
        await redis_client.set(_ERROR_EVENT_KEY, _encode(signal), ex=_ERROR_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        # Health publishing must never hide or replace the original API error.
        logger.debug("best-effort Toss health marker publish failed", exc_info=True)


async def read_toss_api_error_signal() -> TossApiErrorSignal | None:
    """Read the latest shared error marker for a self-stopping bulk reader."""
    redis_client = await _get_redis_client()
    return _decode(await redis_client.get(_ERROR_EVENT_KEY))
