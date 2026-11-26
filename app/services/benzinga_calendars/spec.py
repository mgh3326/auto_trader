"""Benzinga calendar endpoint registry (hk 6513).

One frozen record per market_events category. The paths, row-key names and
query-parameter names come from the official Benzinga reference pages; each
record cites the page it was read from (checked 2026-11-25):

* earnings — https://docs.benzinga.com/api-reference/calendar-api/get-earnings
  ``GET /api/v2.1/calendar/earnings`` → ``{"earnings": [...]}``
* ratings — https://docs.benzinga.com/api-reference/calendar-api/get-ratings
  ``GET /api/v2.1/calendar/ratings`` → ``{"ratings": [...]}``
* guidance — https://docs.benzinga.com/api-reference/calendar-api/get-guidance
  ``GET /api/v2.1/calendar/guidance`` → ``{"guidance": [...]}``
* dividends — https://docs.benzinga.com/api-reference/calendar-api/get-dividends
  ``GET /api/v2.2/calendar/dividends`` → ``{"dividends": [...]}``
* economics — https://docs.benzinga.com/api-reference/calendar-api/get-economics
  ``GET /api/v2.1/calendar/economics`` → ``{"economics": [...]}``
* conference calls —
  https://docs.benzinga.com/api-reference/calendar-api/get-conference-calls
  ``GET /api/v2.1/calendar/conference-calls`` → ``{"conference": [...]}``
* short interest —
  https://docs.benzinga.com/api-reference/market-data/get-short-interest-data
  ``GET /api/v1/shortinterest?symbols=...`` →
  ``{"shortInterestData": {"<SYMBOL>": {"data": [...]}}}``
* WIIM — https://docs.benzinga.com/api-reference/news-api/wiims/get-wiims
  ``GET /api/v2/news?channels=WIIM`` → a bare JSON list (the news shape).

The pagination names differ between the two families: calendar endpoints take
``page``/``pagesize`` with ``parameters[date_from]`` style filters, short
interest takes ``page``/``pageSize`` with ``symbols``/``from``/``to``, and the
news/WIIM endpoint takes ``page``/``pageSize`` (maximum 100) with
``dateFrom``/``dateTo``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.market_events import taxonomy


@dataclass(frozen=True)
class CalendarEndpoint:
    category: str
    path: str
    row_key: str | None
    docs_url: str
    page_size: int = 1000
    requires_symbols: bool = False
    nested_symbol_map: bool = False
    extra_params: dict[str, str] = field(default_factory=dict)
    paging_style: str = "calendar"
    symbol_param: str = "parameters[tickers]"
    symbol_search_type: str | None = None
    lookback_days: int = 0


CALENDAR_ENDPOINTS: dict[str, CalendarEndpoint] = {
    endpoint.category: endpoint
    for endpoint in (
        CalendarEndpoint(
            category="earnings",
            path="/api/v2.1/calendar/earnings",
            row_key="earnings",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-earnings",
        ),
        CalendarEndpoint(
            category="analyst_rating",
            path="/api/v2.1/calendar/ratings",
            row_key="ratings",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-ratings",
        ),
        CalendarEndpoint(
            category="guidance",
            path="/api/v2.1/calendar/guidance",
            row_key="guidance",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-guidance",
        ),
        CalendarEndpoint(
            category="dividend",
            path="/api/v2.2/calendar/dividends",
            row_key="dividends",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-dividends",
        ),
        CalendarEndpoint(
            category="economic",
            path="/api/v2.1/calendar/economics",
            row_key="economics",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-economics",
        ),
        CalendarEndpoint(
            category="conference",
            path="/api/v2.1/calendar/conference-calls",
            row_key="conference",
            docs_url=(
                "https://docs.benzinga.com/api-reference/calendar-api/"
                "get-conference-calls"
            ),
        ),
        CalendarEndpoint(
            category="short_interest",
            path="/api/v1/shortinterest",
            row_key="shortInterestData",
            docs_url=(
                "https://docs.benzinga.com/api-reference/market-data/"
                "get-short-interest-data"
            ),
            page_size=1000,
            requires_symbols=True,
            nested_symbol_map=True,
        ),
        CalendarEndpoint(
            category="wiim",
            path="/api/v2/news",
            row_key=None,
            docs_url="https://docs.benzinga.com/api-reference/news-api/wiims/get-wiims",
            page_size=100,
            extra_params={"channels": "WIIM", "displayOutput": "headline"},
        ),
        CalendarEndpoint(
            category="split",
            path="/api/v2.1/calendar/splits",
            row_key="splits",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-splits",
        ),
        CalendarEndpoint(
            category="merger_acquisition",
            path="/api/v2.1/calendar/ma",
            row_key="ma",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-ma",
        ),
        CalendarEndpoint(
            category="offering",
            path="/api/v2.1/calendar/offerings",
            row_key="offerings",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-offerings",
        ),
        CalendarEndpoint(
            category="fda",
            path="/api/v2.1/calendar/fda",
            row_key="fda",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-fda",
            symbol_param="parameters[securities]",
        ),
        CalendarEndpoint(
            category="ipo",
            path="/api/v2.1/calendar/ipos",
            row_key="ipos",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-ipos",
        ),
        CalendarEndpoint(
            category="option_activity",
            path="/api/v1/signal/option_activity",
            row_key="option_activity",
            docs_url="https://docs.benzinga.com/api-reference/calendar-api/get-optionactivity",
            page_size=1000,
            paging_style="calendar_page_size",
        ),
        CalendarEndpoint(
            category="insider_trade",
            path="/api/v1/sec/insider_transactions/filings",
            row_key="data",
            docs_url=(
                "https://docs.benzinga.com/api-reference/calendar-api/"
                "insider-transaction/get-insider-transaction-filing"
            ),
            paging_style="v1_calendar",
            symbol_search_type="symbol",
        ),
        CalendarEndpoint(
            category="congress_trade",
            path="/api/v1/gov/usa/congress/trades",
            row_key="data",
            docs_url=(
                "https://docs.benzinga.com/api-reference/calendar-api/"
                "government-trades/get-government-trades"
            ),
            paging_style="v1_updated",
            symbol_search_type="ticker",
            lookback_days=30,
        ),
        CalendarEndpoint(
            category="analyst_insight",
            path="/api/v1/analyst/insights",
            row_key="analyst-insights",
            docs_url=(
                "https://docs.benzinga.com/api-reference/calendar-api/"
                "analyst-insights/get-analyst-insights"
            ),
            page_size=100,
            paging_style="analyst",
        ),
    )
}

CATEGORIES: tuple[str, ...] = tuple(CALENDAR_ENDPOINTS)


def validate_registry() -> None:
    """Every endpoint category must be a market_events taxonomy category."""
    missing = sorted(set(CALENDAR_ENDPOINTS) - taxonomy.CATEGORIES)
    if missing:
        raise ValueError(f"benzinga categories missing from taxonomy: {missing}")


def endpoint_for(category: str) -> CalendarEndpoint:
    try:
        return CALENDAR_ENDPOINTS[category]
    except KeyError:
        raise ValueError(
            f"unknown benzinga calendar category: {category!r}; "
            f"supported: {list(CALENDAR_ENDPOINTS)}"
        ) from None


__all__ = [
    "CALENDAR_ENDPOINTS",
    "CATEGORIES",
    "CalendarEndpoint",
    "endpoint_for",
    "validate_registry",
]
