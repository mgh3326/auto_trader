"""ROB-696 — fail-open price fallback chain for /invest (KIS → Toss → snapshot)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.services.brokers.toss.dto import TossPrice

logger = logging.getLogger(__name__)

PriceMap = dict[str, float | None]
Fetcher = Callable[[list[str]], Awaitable[PriceMap]]

KIS_FIRST_ORDER: tuple[str, ...] = ("kis", "toss", "snapshot")
TOSS_FIRST_ORDER: tuple[str, ...] = ("toss", "kis", "snapshot")
_KNOWN_LAYERS = frozenset(KIS_FIRST_ORDER)


class PriceFallbackResolver:
    """Pure orchestration: run injected fetchers KIS → Toss → snapshot, merge
    only non-None values, shrink the missing-set each layer, None for the rest.
    Every layer is wrapped fail-open (exception → {} for that layer)."""

    def __init__(
        self,
        *,
        kis_fetch: Fetcher,
        toss_fetch: Fetcher | None,
        snapshot_fetch: Fetcher,
        market: str,
        order: tuple[str, ...] = KIS_FIRST_ORDER,
    ) -> None:
        if len(order) != len(_KNOWN_LAYERS) or set(order) != _KNOWN_LAYERS:
            # Fail-loud: a typo must not silently drop a fallback layer.
            raise ValueError(
                f"order must be a permutation of {sorted(_KNOWN_LAYERS)}, got {order!r}"
            )
        self._kis_fetch = kis_fetch
        self._toss_fetch = toss_fetch
        self._snapshot_fetch = snapshot_fetch
        self._market = market
        self._order = order

    async def resolve(self, symbols: list[str]) -> PriceMap:
        if not symbols:
            return {}
        results: PriceMap = dict.fromkeys(symbols, None)
        layers: dict[str, Fetcher | None] = {
            "kis": self._kis_fetch,
            "toss": self._toss_fetch,
            "snapshot": self._snapshot_fetch,
        }
        missing = symbols  # first layer runs on the full list
        for name in self._order:
            fetch = layers[name]
            if fetch is None:  # e.g. Toss disabled -> skip this layer
                continue
            await self._apply_layer(name, fetch, missing, results)
            missing = self._missing(symbols, results)
            if not missing:
                return results
        return results

    async def _apply_layer(
        self, name: str, fetch: Fetcher, symbols: list[str], results: PriceMap
    ) -> None:
        try:
            fetched = await fetch(symbols)
        except Exception as exc:  # noqa: BLE001 — fail-open per layer
            logger.warning(
                "invest price fallback: %s layer failed for market=%s (%d symbols): %s",
                name,
                self._market,
                len(symbols),
                exc,
            )
            return
        resolved = 0
        for sym in symbols:
            price = fetched.get(sym)
            if price is not None and results.get(sym) is None:
                results[sym] = price
                resolved += 1
        logger.info(
            "invest price fallback: %s resolved %d/%d for market=%s",
            name,
            resolved,
            len(symbols),
            self._market,
        )

    @staticmethod
    def _missing(symbols: list[str], results: PriceMap) -> list[str]:
        return [s for s in symbols if results.get(s) is None]


_TOSS_PRICE_BATCH = 200


class TossPriceClient(Protocol):
    async def prices(self, symbols: list[str] | tuple[str, ...]) -> list[TossPrice]: ...


def _chunk(symbols: list[str], size: int = _TOSS_PRICE_BATCH) -> list[list[str]]:
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


async def fetch_toss_batch_prices(
    client: TossPriceClient, symbols: list[str]
) -> dict[str, float | None]:
    """ONE batched Toss /api/v1/prices call per ≤200 chunk; fail-open to {}."""
    if not symbols:
        return {}
    # Map uppercased-echo -> requested symbol so we return the caller's keys.
    by_upper = {s.upper(): s for s in symbols}
    out: dict[str, float | None] = {}
    try:
        for batch in _chunk([s.upper() for s in symbols]):
            for price in await client.prices(batch):
                requested = by_upper.get(str(price.symbol).upper())
                if requested is not None:
                    out[requested] = float(price.last_price)
    except Exception as exc:  # noqa: BLE001 — fail-open, resolver falls through
        logger.warning(
            "invest price fallback: toss batch prices failed (%d symbols): %s",
            len(symbols),
            exc,
        )
        return {}
    return out
