"""hk 6424: the futures master listing used to recompute roll_state (no network).

A successful listing is reused for the TTL; a failed or empty read is never cached,
so a broken master cannot pin a stale answer. The master rows are shaped like the
real ``fo_idx_code_mts.mst`` parse (``fetch_master_rows``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.kis_futures_quotes.contracts import listed_months_by_product
from app.services.research_candles.futures_listing import FuturesListingProvider

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

T0 = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)


def _mini_row(name: str, short: str) -> dict[str, str]:
    return {
        "product_type": "B",
        "short_code": short,
        "name": name,
        "underlying_name": "KOSPI200",
    }


def _kospi_row(name: str, short: str) -> dict[str, str]:
    return {
        "product_type": "1",
        "short_code": short,
        "name": name,
        "underlying_name": "KOSPI200",
    }


MASTER: list[dict[str, str]] = [
    _mini_row("미니F 202612", "A05612"),
    _mini_row("미니F 202703", "A05703"),
    _kospi_row("F 202612", "A01612"),
    # Unrelated rows must not leak into the listing.
    {
        "product_type": "B",
        "short_code": "A05999",
        "name": "미니F 202999",
        "underlying_name": "KOSPI200",
    },
]


class _Fetch:
    def __init__(self, rows: Any = None, error: Exception | None = None) -> None:
        self.rows = MASTER if rows is None else rows
        self.error = error
        self.calls = 0

    async def __call__(self) -> list[dict[str, str]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_listed_months_are_per_closed_product_and_sorted() -> None:
    listing = listed_months_by_product(MASTER)
    assert listing == {
        "MF200": ("2026-12", "2027-03"),
        "F200": ("2026-12",),
    }


def test_a_product_with_no_matching_row_is_absent() -> None:
    assert listed_months_by_product([]) == {}


async def test_a_successful_listing_is_reused_within_the_ttl() -> None:
    fetch = _Fetch()
    clock = _Clock(T0)
    provider = FuturesListingProvider(fetch, clock=clock, ttl=timedelta(hours=1))

    first = await provider.listed_months_by_product()
    clock.now = T0 + timedelta(minutes=59)
    second = await provider.listed_months_by_product()

    assert first == second == {"MF200": ("2026-12", "2027-03"), "F200": ("2026-12",)}
    assert fetch.calls == 1


async def test_a_listing_is_refetched_after_the_ttl() -> None:
    fetch = _Fetch()
    clock = _Clock(T0)
    provider = FuturesListingProvider(fetch, clock=clock, ttl=timedelta(hours=1))

    await provider.listed_months_by_product()
    clock.now = T0 + timedelta(hours=1)
    await provider.listed_months_by_product()

    assert fetch.calls == 2


async def test_a_failed_read_is_unavailable_and_not_cached() -> None:
    fetch = _Fetch(error=OSError("master download refused"))
    provider = FuturesListingProvider(fetch, clock=_Clock(T0))

    assert await provider.listed_months_by_product() is None
    assert await provider.listed_months_by_product() is None
    # Both calls reached the fetch: nothing was pinned.
    assert fetch.calls == 2


async def test_an_empty_master_is_not_a_listing() -> None:
    fetch = _Fetch(rows=[])
    provider = FuturesListingProvider(fetch, clock=_Clock(T0))

    assert await provider.listed_months_by_product() is None
    assert await provider.listed_months_by_product() is None
    assert fetch.calls == 2


async def test_the_exception_text_is_not_returned_to_the_caller() -> None:
    provider = FuturesListingProvider(
        _Fetch(error=OSError("https://secret.example/?key=abc")),
        clock=_Clock(T0),
    )
    assert await provider.listed_months_by_product() is None
