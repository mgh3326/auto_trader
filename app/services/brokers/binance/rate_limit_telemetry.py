"""ROB-285 — Binance rate-limit header parser + telemetry emission.

Parses ``X-MBX-USED-WEIGHT-1M`` and ``X-MBX-ORDER-COUNT-1M`` from REST
responses and emits structured ``logger.info`` + Sentry tag when usage
crosses 50% of the declared limit. Soft-throttle and hard-stop logic
live in the REST client (Task 7); this module only observes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger("app.services.brokers.binance.rate_limit")

# Below this fraction of the declared limit, we don't emit a Sentry tag —
# this avoids noise during normal operation. Logged at INFO regardless.
_SENTRY_TAG_THRESHOLD: float = 0.5


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    used_weight_1m: int | None
    order_count_1m: int | None


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_rate_limit_headers(headers: Mapping[str, str]) -> RateLimitSnapshot:
    """Extract Binance rate-limit counters from REST response headers.

    Header lookup is case-insensitive via dict-style access on
    ``httpx.Headers``; for plain dicts the caller must normalize.
    """
    norm = {k.lower(): v for k, v in headers.items()}
    return RateLimitSnapshot(
        used_weight_1m=_int_or_none(norm.get("x-mbx-used-weight-1m")),
        order_count_1m=_int_or_none(norm.get("x-mbx-order-count-1m")),
    )


def _set_sentry_tag(key: str, value: str) -> None:
    """Indirected so tests can monkeypatch without depending on sentry_sdk.

    Telemetry must NEVER break the adapter. Sentry can be:
    - not installed (ImportError),
    - installed but not initialized (no DSN — set_tag is a safe no-op),
    - installed and misconfigured (set_tag may raise in pathological cases),
    - installed and healthy (set_tag works).

    All four cases are handled with a blanket Exception catch — fail-open
    is correct here because rate-limit telemetry is observability, not
    operational state. If telemetry breaks the adapter, the project
    loses both observability AND the adapter.
    """
    try:
        import sentry_sdk

        sentry_sdk.set_tag(key, value)
    except Exception:  # noqa: BLE001 — intentional fail-open
        # Swallow ImportError, RuntimeError, AttributeError, anything.
        # Adapter functionality cannot depend on Sentry health.
        return


def emit_rate_limit_snapshot(
    snap: RateLimitSnapshot,
    *,
    declared_weight_limit: int = 1200,
) -> None:
    """Log + (conditionally) tag a single rate-limit snapshot.

    ``declared_weight_limit`` defaults to Binance spot REST 1m weight cap
    (1200 as of 2025); pass the exchangeInfo-reported value when known.
    """
    used = snap.used_weight_1m or 0
    pct = (used / declared_weight_limit) if declared_weight_limit > 0 else 0.0
    logger.info(
        "binance.rate_limit "
        f"used_weight_1m={used} "
        f"order_count_1m={snap.order_count_1m or 0} "
        f"declared_weight={declared_weight_limit} "
        f"pct={pct:.2%}"
    )
    if pct >= _SENTRY_TAG_THRESHOLD:
        _set_sentry_tag("binance.rate_limit_weight_pct", f"{int(pct * 100)}")
