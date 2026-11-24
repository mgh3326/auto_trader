"""Read-only view over the KIS futures quote layer (hk 6148).

Gate: ``KIS_FUTURES_QUOTES_ENABLED`` exactly ``"true"`` (default off, separate from
``KIS_FUTURES_READ_ENABLED``), checked in ``open_quotes_view`` before the KIS
client — and so any token lookup or network call — exists.

The view exposes ``price``, ``minute_bars``, ``master`` (the contract resolver needs
the futures master; it is a public download, not an account read) and ``close``.
It forwards to four allowlisted ``KISClient`` methods and nothing else: no account
number, no order TR, no ``/trading/`` path (``tests/services/kis_futures_quotes``
checks the reachable surface statically).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.brokers.kis.futures_quotes import (
    DAY_START_CURSOR,
    FuturesQuotesGateOff,
    FuturesQuotesUnsupported,
    gate_check,
)

#: The only KISClient methods this view may call.
ALLOWED_TARGET_METHODS = frozenset(
    {
        "quote_futures_master",
        "quote_futures_price",
        "quote_futures_minutes",
        "close",
    }
)

__all__ = [
    "ALLOWED_TARGET_METHODS",
    "FuturesQuotesGateOff",
    "FuturesQuotesUnsupported",
    "FuturesQuotesView",
    "open_quotes_view",
]


class FuturesQuotesView:
    __slots__ = ("_target",)

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(name)

    async def master(self) -> list[dict[str, str]]:
        return await self._target.quote_futures_master()

    async def price(self, symbol: str) -> dict[str, Any]:
        return await self._target.quote_futures_price(symbol)

    async def minute_bars(
        self,
        symbol: str,
        *,
        session: str = "day",
        day: date,
        until_hour: str = DAY_START_CURSOR,
    ) -> list[dict[str, Any]]:
        """Raw KST-day minute rows (ascending). ``session="night"`` is unsupported."""
        return await self._target.quote_futures_minutes(
            symbol, session=session, day=day, until_hour=until_hour
        )

    async def close(self) -> None:
        await self._target.close()


def open_quotes_view() -> FuturesQuotesView:
    """Build the view on the live KIS client; refuses before anything else."""
    gate_check()
    from app.services.brokers.kis.client import KISClient

    return FuturesQuotesView(KISClient(is_mock=False))
