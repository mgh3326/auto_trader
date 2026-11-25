"""hk 6424: the futures master listing the research ingest needs for ``roll_state``.

``roll_state`` (front / next / expiring / expired) depends on which earlier
contract months are still live, and the only source of that list is the KIS
index-futures master. The ingest server reads it through the same public download
the collector uses (``fetch_master_rows``: no credential, no quotes gate). A
successful listing is reused for ``LISTING_TTL``; a failed read is never cached,
so the next batch retries and a broken master cannot pin a stale answer.

Newly listed months do not change ``roll_state`` of an older contract (a new
month is always later), so a listing up to an hour old cannot misstate a bar.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.services.brokers.kis.futures_read import fetch_master_rows
from app.services.kis_futures_quotes.contracts import listed_months_by_product

logger = logging.getLogger(__name__)

LISTING_TTL = timedelta(hours=1)

MasterFetch = Callable[[], Awaitable[list[dict[str, str]]]]


class FuturesListingProvider:
    """Caches one successful ``product -> listed months`` map. ``None`` = unavailable."""

    def __init__(
        self,
        fetch: MasterFetch,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = LISTING_TTL,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl = ttl
        self._cached: tuple[datetime, dict[str, tuple[str, ...]]] | None = None

    async def listed_months_by_product(self) -> dict[str, tuple[str, ...]] | None:
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < self._ttl:
            return self._cached[1]
        try:
            rows = await self._fetch()
        except Exception as exc:  # noqa: BLE001 - reported per item, never raised
            logger.warning("futures master unavailable error=%s", type(exc).__name__)
            return None
        listing = listed_months_by_product(rows)
        if not listing:
            # An empty or unrecognised master is not a listing: refuse, do not cache.
            return None
        self._cached = (now, listing)
        return listing


#: Process-wide provider used by the ingest route.
FUTURES_LISTING = FuturesListingProvider(fetch_master_rows)

__all__ = ["FUTURES_LISTING", "FuturesListingProvider", "LISTING_TTL"]
