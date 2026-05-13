from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.screening.crypto import _screen_crypto_via_tvscreener
from app.services.invest_crypto_screener_snapshots.builder import (
    CryptoProviderRow,
    provider_row_from_mapping,
)


class TvScreenerUpbitCryptoSnapshotProvider:
    """Pure market-data provider for crypto screener snapshots.

    This intentionally does not persist rows or mutate broker/order state.  The
    current implementation delegates to the existing tvscreener crypto contract
    and converts display rows into snapshot DTOs; account-state filters remain
    outside the persisted snapshot table.
    """

    async def fetch_rows(self, *, limit: int | None = None) -> list[CryptoProviderRow]:
        query_limit = limit or 200
        payload: dict[str, Any] = await _screen_crypto_via_tvscreener(
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=query_limit,
            pure_market_snapshot=True,
        )
        rows: list[CryptoProviderRow] = []
        for raw in payload.get("results") or payload.get("stocks") or []:
            if not isinstance(raw, dict):
                continue
            row = provider_row_from_mapping(raw)
            if row is not None:
                rows.append(row)
        return rows[:query_limit]
