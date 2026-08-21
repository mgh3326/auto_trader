"""Calendar-only held-key reader over the W2 portfolio snapshot.

This service never constructs KIS/Upbit/Toss/paper home readers, never calls
quote/FX/sellable endpoints, and never returns a successful manual-only subset
on a cold or corrupt snapshot.
"""

from __future__ import annotations

import logging

from app.services.invest_home_service import PortfolioSnapshotUnavailableError

logger = logging.getLogger(__name__)


class CalendarHeldKeyService:
    """Read W2 snapshot ``held_pairs`` (and the cold-path manual DB keys)."""

    def __init__(self, *, snapshot_cache, manual_reader) -> None:
        self._snapshot_cache = snapshot_cache
        self._manual = manual_reader

    @staticmethod
    def _snapshot_cache_usable(snapshot_cache) -> bool:
        return snapshot_cache is not None and bool(
            getattr(snapshot_cache, "usable", True)
        )

    async def get_held_pairs(
        self,
        *,
        user_id: int,
        include_paper: bool = False,
        paper_sources: frozenset[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Read only held-symbol keys for calendar relation ranking.

        The held-key projection must come from the shared snapshot or the
        direct manual DB key reader. A cold/invalid/unavailable live snapshot
        raises a typed error instead of running full broker readers or
        returning a misleading manual-only subset.
        """
        from app.services.portfolio_snapshot import (
            HELD_KEY_MARKETS,
            held_key_symbol,
            held_pairs_from_portfolio_snapshot,
            portfolio_snapshot_scope,
        )

        cache_usable = self._snapshot_cache_usable(self._snapshot_cache)
        scope = portfolio_snapshot_scope(
            user_id=user_id,
            include_paper=include_paper,
            paper_sources=paper_sources,
        )
        payload = await self._snapshot_cache.get(scope) if cache_usable else None
        if payload is not None:
            try:
                return held_pairs_from_portfolio_snapshot(payload)
            except Exception:
                await self._snapshot_cache.delete(
                    scope,
                    expected_payload=payload,
                )

        manual_reader = self._manual
        manual_pairs: list[tuple[str, str]] = []
        if hasattr(manual_reader, "fetch_held_pairs"):
            try:
                raw_pairs = await manual_reader.fetch_held_pairs(user_id=user_id)
            except Exception as exc:
                logger.warning(
                    "Manual held-key projection failed (%s)", type(exc).__name__
                )
                raise PortfolioSnapshotUnavailableError(
                    "held_key_projection_unavailable"
                ) from None
            manual_pairs = [
                (str(market).lower(), held_key_symbol(market, symbol))
                for market, symbol in raw_pairs
                if str(market).lower() in HELD_KEY_MARKETS
                and held_key_symbol(market, symbol)
            ]

        reason = (
            "snapshot_cache_unusable"
            if not cache_usable
            else "held_key_projection_missing_or_invalid"
        )
        raise PortfolioSnapshotUnavailableError(
            reason,
            manual_pairs=sorted(set(manual_pairs)),
        )
