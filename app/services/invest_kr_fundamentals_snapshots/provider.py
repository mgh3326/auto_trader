from __future__ import annotations

import logging
from typing import Any

from app.mcp_server.tooling.screening.common import _get_tvscreener_attr
from app.services.invest_kr_fundamentals_snapshots.builder import (
    KrFundamentalsProviderRow,
    provider_row_from_mapping,
)
from app.services.tvscreener_service import (
    TvScreenerService,
    _import_tvscreener,
)

logger = logging.getLogger(__name__)

#: ROB-429 A1 — full-universe (``limit=None``) fetch bound. tvscreener's
#: ``query_stock_screener(limit=falsy)`` skips ``set_range`` and returns only its
#: small default range (~150), so the full path MUST pass a large explicit bound.
#: This cap sits well above the ~3,909 active KR universe / ~4,250 tvscreener rows.
_FULL_UNIVERSE_FETCH_CAP = 10_000

# (model_key, [StockField name fallbacks]) — version-safe field resolution.
# Probe-validated against tvscreener KOREA market (ROB-428, 2026-06-04).
_KR_STOCK_FIELD_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("symbol", ("ACTIVE_SYMBOL", "SYMBOL")),
    ("name", ("NAME", "DESCRIPTION")),
    ("price", ("PRICE", "CLOSE")),
    ("change_rate", ("CHANGE_PERCENT",)),
    ("volume", ("VOLUME",)),
    ("market_cap", ("MARKET_CAPITALIZATION", "MARKET_CAP_BASIC")),
    ("per", ("PRICE_TO_EARNINGS_RATIO_TTM", "PRICE_TO_EARNINGS_TTM")),
    ("pbr", ("PRICE_TO_BOOK_FQ", "PRICE_TO_BOOK_MRQ", "PRICE_BOOK_CURRENT")),
    (
        "dividend_yield",
        (
            "DIVIDEND_YIELD_FORWARD",
            "DIVIDENDS_YIELD_CURRENT",
            "DIVIDEND_YIELD_RECENT",
            "DIVIDEND_YIELD_CURRENT",
        ),
    ),
    ("roe_ttm", ("RETURN_ON_EQUITY_TTM", "RETURN_ON_EQUITY_FY")),
    (
        "payout_ratio_ttm",
        (
            "DIVIDEND_PAYOUT_RATIO_TTM",
            "DIVIDEND_PAYOUT_RATIO_PERCENT_TTM",
            "DIVIDEND_PAYOUT_RATIO_FY",
        ),
    ),
    ("gross_margin_ttm", ("GROSS_MARGIN_TTM", "GROSS_MARGIN_PERCENT_TTM")),
    ("revenue_yoy", ("REVENUE_ANNUAL_YOY_GROWTH",)),
    ("eps_yoy", ("EPS_DILUTED_ANNUAL_YOY_GROWTH",)),
    ("eps_qoq", ("EPS_DILUTED_QUARTERLY_QOQ_GROWTH",)),
    ("net_income_yoy", ("NET_INCOME_ANNUAL_YOY_GROWTH",)),
    ("net_income_cagr_5y", ("NET_INCOME_CAGR_5Y",)),
    ("continuous_dividend_payout", ("CONTINUOUS_DIVIDEND_PAYOUT",)),
    ("continuous_dividend_growth", ("CONTINUOUS_DIVIDEND_GROWTH",)),
    ("week_high_52", ("WEEK_HIGH_52",)),
    ("rsi14", ("RELATIVE_STRENGTH_INDEX_14",)),
    ("sector", ("SECTOR",)),
    ("industry", ("INDUSTRY",)),
)


def _resolve_kr_fields(stock_field: Any) -> list[Any]:
    """Resolve probe-validated KR StockFields; skip any unresolved field."""
    resolved: list[Any] = []
    for model_key, candidates in _KR_STOCK_FIELD_SPECS:
        field = _get_tvscreener_attr(stock_field, *candidates)
        if field is None:
            logger.warning(
                "[KR-Fundamentals] StockField for %s unresolved (tried %s); "
                "column omitted from select",
                model_key,
                candidates,
            )
            continue
        resolved.append(field)
    return resolved


class TvScreenerKrFundamentalsProvider:
    """Pure market-data provider for KR fundamentals screener snapshots.

    Delegates to the tvscreener library's StockScreener (scanner API) for the
    KOREA market, normalises the DataFrame to snake_case keys, and converts
    each row into a snapshot DTO. It does not persist rows or mutate any
    broker/order/watch state. ``kr.tradingview.com`` is never crawled — only
    the library scanner API is used.
    """

    def __init__(self, *, timeout: float | None = None) -> None:
        self._timeout = timeout

    async def fetch_rows(
        self, *, limit: int | None = None
    ) -> list[KrFundamentalsProviderRow]:
        # ROB-429 A1: full mode (limit is None / non-positive) fetches the FULL KR
        # universe via a large explicit bound; a positive limit is a bounded
        # diagnostic. (`limit or 200` previously collapsed full mode to 200 rows.)
        is_full = limit is None or limit <= 0
        query_limit = _FULL_UNIVERSE_FETCH_CAP if is_full else limit
        tvscreener = _import_tvscreener()
        stock_field = tvscreener.StockField
        market = tvscreener.Market

        columns = _resolve_kr_fields(stock_field)
        if not columns:
            logger.warning(
                "[KR-Fundamentals] No KR StockFields resolved; returning no rows"
            )
            return []

        service = (
            TvScreenerService(timeout=self._timeout)
            if self._timeout is not None
            else TvScreenerService()
        )
        df = await service.query_stock_screener(
            columns=columns,
            markets=[market.KOREA],
            limit=query_limit,
        )

        rows: list[KrFundamentalsProviderRow] = []
        if df is None or df.empty:
            return rows
        for _, raw in df.iterrows():
            mapping = {key: raw[key] for key in df.columns}
            row = provider_row_from_mapping(mapping)
            if row is not None:
                rows.append(row)
        # Full mode returns every provider row unsliced; a bounded diagnostic
        # caps to the requested positive limit.
        return rows if is_full else rows[:limit]
