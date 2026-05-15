from __future__ import annotations

from typing import Any

import httpx


class NaverStockClient:
    """Small, capped async client for Naver revamped stock JSON endpoints.

    Request handlers must not use this client directly; it is intended for manual
    dry-run/collection jobs where writes are explicit and approval-gated.
    """

    BASE_URL = "https://stock.naver.com"
    MAX_LIST_PAGE_SIZE = 100
    MAX_STOCKLIST_PAGE_SIZE = 20

    def __init__(
        self, http_client: httpx.AsyncClient | None = None, *, timeout: float = 10.0
    ) -> None:
        self._client = http_client or httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=timeout,
            headers={"User-Agent": "auto_trader ROB-222 read-model dry-run/1.0"},
        )
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def fetch_domestic_stock_default(
        self,
        *,
        trade_type: str = "KRX",
        market_type: str = "ALL",
        order_type: str = "up",
        start_idx: int = 0,
        page_size: int = 50,
    ) -> Any:
        return await self._get(
            "/api/domestic/market/stock/default",
            {
                "tradeType": trade_type,
                "marketType": market_type,
                "orderType": order_type,
                "startIdx": max(0, start_idx),
                "pageSize": min(page_size, self.MAX_LIST_PAGE_SIZE),
            },
        )

    async def fetch_upjong_theme_ranking(self, *, sort_type: str = "changeRate") -> Any:
        return await self._get(
            "/api/domestic/home/upjongTheme/ranking", {"sortType": sort_type}
        )

    async def fetch_market_upjong_list(
        self, *, sort_type: str = "changeRate", start_idx: int = 0, page_size: int = 100
    ) -> Any:
        return await self._get(
            "/api/domestic/market/upjong/list",
            {
                "sortType": sort_type,
                "startIdx": max(0, start_idx),
                "pageSize": min(page_size, self.MAX_LIST_PAGE_SIZE),
            },
        )

    async def fetch_market_theme_list(
        self, *, sort_type: str = "changeRate", start_idx: int = 0, page_size: int = 100
    ) -> Any:
        return await self._get(
            "/api/domestic/market/theme/list",
            {
                "sortType": sort_type,
                "startIdx": max(0, start_idx),
                "pageSize": min(page_size, self.MAX_LIST_PAGE_SIZE),
            },
        )

    async def fetch_theme_info(self, theme_no: str, *, market_type: str = "ALL") -> Any:
        return await self._get(
            f"/api/domestic/market/theme/{theme_no}/info", {"marketType": market_type}
        )

    async def fetch_theme_stocklist(
        self,
        theme_no: str,
        *,
        market_type: str = "ALL",
        order_type: str = "priceTop",
        start_idx: int = 0,
        page_size: int = 20,
    ) -> Any:
        return await self._get(
            f"/api/domestic/market/theme/{theme_no}/stocklist",
            {
                "marketType": market_type,
                "orderType": order_type,
                "startIdx": max(0, start_idx),
                "pageSize": min(page_size, self.MAX_STOCKLIST_PAGE_SIZE),
            },
        )

    async def fetch_upjong_info(
        self, upjong_code: str, *, market_type: str = "ALL"
    ) -> Any:
        return await self._get(
            f"/api/domestic/market/upjong/{upjong_code}/info",
            {"marketType": market_type},
        )

    async def fetch_upjong_stocklist(
        self,
        upjong_code: str,
        *,
        market_type: str = "ALL",
        order_type: str = "priceTop",
        start_idx: int = 0,
        page_size: int = 20,
    ) -> Any:
        return await self._get(
            f"/api/domestic/market/upjong/{upjong_code}/stocklist",
            {
                "marketType": market_type,
                "orderType": order_type,
                "startIdx": max(0, start_idx),
                "pageSize": min(page_size, self.MAX_STOCKLIST_PAGE_SIZE),
            },
        )
