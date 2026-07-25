from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

from app.mcp_server.tooling.screening.common import _get_tvscreener_attr
from app.services.tvscreener_service import TvScreenerService, _import_tvscreener

logger = logging.getLogger(__name__)
_FULL_UNIVERSE_FETCH_CAP = 12_000

_US_STOCK_FIELD_SPECS = (
    ("symbol", ("ACTIVE_SYMBOL", "SYMBOL")),
    ("market_cap", ("MARKET_CAPITALIZATION", "MARKET_CAP_BASIC")),
    ("per", ("PRICE_TO_EARNINGS_RATIO_TTM", "PRICE_TO_EARNINGS_TTM")),
    ("pbr", ("PRICE_TO_BOOK_FQ", "PRICE_TO_BOOK_MRQ", "PRICE_BOOK_CURRENT")),
    ("dividend_yield", ("DIVIDENDS_YIELD", "DIVIDEND_YIELD_FORWARD")),
    ("roe", ("RETURN_ON_EQUITY_TTM",)),
    ("high_52w", ("WEEK_HIGH_52",)),
    ("low_52w", ("WEEK_LOW_52",)),
    ("high_52w_date", ("PRICE_52_WEEK_HIGH_DATE",)),
)


def _date_from_epoch_seconds(value: Any) -> dt.date | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(seconds, tz=dt.UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


class TvScreenerUsValuationProvider:
    """Bulk valuation provider for US stocks using TvScreener."""

    def __init__(self, *, timeout: float | None = None) -> None:
        self._timeout = timeout

    async def fetch_rows(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        is_full = limit is None or limit <= 0
        query_limit = _FULL_UNIVERSE_FETCH_CAP if is_full else limit
        tvs = _import_tvscreener()
        market = tvs.Market
        sf = tvs.StockField

        cols = []
        for key, candidates in _US_STOCK_FIELD_SPECS:
            field = _get_tvscreener_attr(sf, *candidates)
            if field:
                cols.append(field)
            else:
                logger.warning(
                    "[US-Valuation] StockField for %s unresolved (tried %s)",
                    key,
                    candidates,
                )

        if not cols:
            logger.warning(
                "[US-Valuation] No US StockFields resolved; returning empty list"
            )
            return []

        service = (
            TvScreenerService(timeout=self._timeout)
            if self._timeout is not None
            else TvScreenerService()
        )
        df = await service.query_stock_screener(
            columns=cols,
            markets=[market.AMERICA],
            limit=query_limit,
        )
        if df is None or df.empty:
            return []

        mapped_rows = []
        for _, raw in df.iterrows():
            row_dict = raw.to_dict()
            symbol = row_dict.get("symbol") or row_dict.get("active_symbol")
            if not symbol:
                continue

            # ROB-590: serialise the 52w-high date as an ISO STRING (not a dt.date)
            # so the mapped row — which is stored verbatim as raw_payload — stays
            # strict-JSON / Postgres-jsonb safe. _to_date in the builder parses it
            # back for the typed column.
            hi_date = _date_from_epoch_seconds(row_dict.get("price_52_week_high_date"))
            mapped_row = {
                "symbol": symbol,
                "price_earnings_ttm": row_dict.get("price_to_earnings_ratio_ttm")
                or row_dict.get("price_to_earnings_ttm"),
                "price_book_ratio": row_dict.get("price_to_book_mrq")
                or row_dict.get("price_to_book_fq")
                or row_dict.get("price_book_current"),
                "return_on_equity": row_dict.get("return_on_equity_ttm")
                or row_dict.get("return_on_equity"),
                "dividends_yield": row_dict.get("dividend_yield")
                or row_dict.get("dividend_yield_forward")
                or row_dict.get("dividends_yield"),
                "market_cap_basic": row_dict.get("market_capitalization")
                or row_dict.get("market_cap_basic"),
                "price_52_week_high": row_dict.get("52_week_high")
                or row_dict.get("week_high_52"),
                "price_52_week_low": row_dict.get("52_week_low")
                or row_dict.get("week_low_52"),
                "price_52_week_high_date": hi_date.isoformat() if hi_date else None,
            }
            mapped_rows.append(mapped_row)

        return mapped_rows
