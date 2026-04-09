"""Naver Finance crawling service for Korean equities.

This module provides async functions to fetch:
- Stock news and disclosures
- Company profile information
- Financial statements
- Foreign/institutional investor trends
- Securities firm investment opinions
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.number_utils import parse_korean_number as _parse_korean_number
from app.services.analyst_normalizer import (
    build_consensus,
    normalize_rating_label,
    rating_to_bucket,
)

# Base URLs for Naver Finance
NAVER_FINANCE_BASE = "https://finance.naver.com"
NAVER_FINANCE_ITEM = f"{NAVER_FINANCE_BASE}/item"

# Request headers to mimic browser
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://finance.naver.com/",
}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _parse_naver_date(date_str: str | None) -> str | None:
    """Parse Naver Finance date formats to ISO format.

    Args:
        date_str: Date string in various formats:
            - "2024.01.15" (full 4-digit year)
            - "24.01.15" (2-digit year)
            - "01.15" (month.day only)

    Returns:
        ISO format date string (e.g., "2024-01-15") or None
    """
    if not date_str:
        return None

    date_str = date_str.strip()
    if not date_str:
        return None

    # Full date format with 4-digit year: 2024.01.15 or 2024-01-15 or 2024/01/15
    match = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    # Short year format: YY.MM.DD (e.g., "26.01.30" → "2026-01-30")
    match = re.match(r"(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", date_str)
    if match:
        yy, month, day = match.groups()
        # Convert 2-digit year to 4-digit (00-99 → 2000-2099)
        year = 2000 + int(yy)
        return f"{year}-{int(month):02d}-{int(day):02d}"

    # Month.day only format (assumes current year): 01.15 or 01-15
    match = re.match(r"(\d{1,2})[.\-/](\d{1,2})$", date_str)
    if match:
        year = date.today().year
        month, day = match.groups()
        parsed = f"{year}-{int(month):02d}-{int(day):02d}"
        if parsed > date.today().isoformat():
            year -= 1
            parsed = f"{year}-{int(month):02d}-{int(day):02d}"
        return parsed

    return date_str


# ---------------------------------------------------------------------------
# HTTP Fetch
# ---------------------------------------------------------------------------


async def _fetch_html(url: str, params: dict[str, Any] | None = None) -> BeautifulSoup:
    """Fetch HTML and return BeautifulSoup object.

    Handles encoding detection (EUC-KR vs UTF-8) for Naver Finance pages.

    Args:
        url: URL to fetch
        params: Query parameters

    Returns:
        BeautifulSoup object of the parsed HTML
    """
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        return await _fetch_html_with_client(client, url, params=params)


def _decode_html_content(content: bytes) -> str:
    try:
        return content.decode("euc-kr")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


async def _fetch_html_with_client(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
) -> BeautifulSoup:
    response = await client.get(url, params=params, headers=DEFAULT_HEADERS)
    response.raise_for_status()
    return BeautifulSoup(_decode_html_content(response.content), "lxml")


def _extract_current_price_from_main_soup(main_soup: BeautifulSoup) -> int | None:
    price_elem = main_soup.select_one("p.no_today em span.blind")
    if price_elem:
        price = _parse_korean_number(price_elem.get_text(strip=True))
        if price:
            return int(price)

    no_today = main_soup.select_one("p.no_today")
    if no_today:
        price = _parse_korean_number(no_today.get_text(strip=True))
        if price:
            return int(price)

    return None


def _parse_news_soup(soup: BeautifulSoup, limit: int) -> list[dict[str, Any]]:
    news_items: list[dict[str, Any]] = []
    table = soup.select_one("table.type5")
    if not table:
        return news_items

    for row in table.select("tr"):
        title_elem = row.select_one("td.title a")
        if not title_elem:
            continue

        source_elem = row.select_one("td.info")
        date_elem = row.select_one("td.date")
        href = title_elem.get("href") or ""
        href_str = href if isinstance(href, str) else ""
        full_url = (
            href_str if href_str.startswith("http") else NAVER_FINANCE_BASE + href_str
        )
        news_items.append(
            {
                "title": title_elem.get_text(strip=True),
                "url": full_url,
                "source": source_elem.get_text(strip=True) if source_elem else "",
                "datetime": (
                    _parse_naver_date(date_elem.get_text(strip=True))
                    if date_elem
                    else None
                ),
            }
        )
        if len(news_items) >= limit:
            break

    return news_items


def _parse_report_detail_soup(soup: BeautifulSoup) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target_price": None,
        "rating": None,
    }

    info_div = soup.select_one("div.view_info_1")
    if not info_div:
        return result

    target_elem = info_div.select_one("em.money strong")
    if target_elem:
        result["target_price"] = _parse_korean_number(target_elem.get_text(strip=True))

    rating_elem = info_div.select_one("em.coment")
    if rating_elem:
        result["rating"] = rating_elem.get_text(strip=True)

    return result


def _collect_opinion_report_infos(
    company_list_soup: BeautifulSoup,
    limit: int,
) -> list[dict[str, Any]]:
    table = company_list_soup.select_one("table.type_1")
    if not table:
        return []

    report_infos: list[dict[str, Any]] = []
    seen_nids: set[str] = set()
    rows = table.select("tbody tr, tr")
    for row in rows:
        cells = row.select("td")
        if len(cells) < 5:
            continue

        try:
            title_elem = cells[1].select_one("a")
            if not title_elem:
                continue

            href = title_elem.get("href") or ""
            href_str = href if isinstance(href, str) else ""
            nid_match = re.search(r"nid=(\d+)", href_str)
            if not nid_match:
                continue

            nid = nid_match.group(1)
            if nid in seen_nids:
                continue
            seen_nids.add(nid)

            report_infos.append(
                {
                    "nid": nid,
                    "stock_name": cells[0].get_text(strip=True),
                    "title": title_elem.get_text(strip=True),
                    "firm": cells[2].get_text(strip=True),
                    "date": _parse_naver_date(cells[4].get_text(strip=True)),
                    "url": (
                        href_str
                        if href_str.startswith("http")
                        else NAVER_FINANCE_BASE + "/research/" + href_str
                    ),
                }
            )
            if len(report_infos) >= limit:
                break
        except (IndexError, ValueError):
            continue

    return report_infos


async def _build_investment_opinions_from_company_list_soup(
    code: str,
    company_list_soup: BeautifulSoup,
    limit: int,
    *,
    current_price: int | None,
    detail_fetcher: Callable[[str], Awaitable[dict[str, Any] | None]],
) -> dict[str, Any]:
    opinions: dict[str, Any] = {
        "symbol": code,
        "count": 0,
        "opinions": [],
        "consensus": None,
    }
    report_infos = _collect_opinion_report_infos(company_list_soup, limit)
    if report_infos:
        detail_tasks = [detail_fetcher(info["nid"]) for info in report_infos]
        details = await asyncio.gather(*detail_tasks, return_exceptions=True)

        for info, detail in zip(report_infos, details, strict=True):
            raw_rating = None
            if isinstance(detail, dict):
                raw_rating = detail.get("rating")

            rating_label = normalize_rating_label(raw_rating)
            opinions["opinions"].append(
                {
                    "stock_name": info["stock_name"],
                    "title": info["title"],
                    "firm": info["firm"],
                    "date": info["date"],
                    "url": info["url"],
                    "target_price": detail.get("target_price")
                    if isinstance(detail, dict)
                    else None,
                    "rating": rating_label,
                    "rating_bucket": rating_to_bucket(rating_label),
                }
            )

    opinions["count"] = len(opinions["opinions"])
    opinions["consensus"] = build_consensus(opinions["opinions"], current_price)
    return opinions


def _parse_valuation_from_soups(
    code: str,
    main_soup: BeautifulSoup,
    sise_soup: BeautifulSoup,
) -> dict[str, Any]:
    valuation: dict[str, Any] = {
        "symbol": code,
        "name": None,
        "current_price": None,
        "per": None,
        "pbr": None,
        "roe": None,
        "roe_controlling": None,
        "dividend_yield": None,
        "high_52w": None,
        "low_52w": None,
        "current_position_52w": None,
    }

    name_elem = main_soup.select_one("div.wrap_company h2 a")
    if name_elem:
        valuation["name"] = name_elem.get_text(strip=True)

    valuation["current_price"] = _extract_current_price_from_main_soup(main_soup)

    per_elem = main_soup.select_one("em#_per")
    if per_elem:
        per_val = _parse_korean_number(per_elem.get_text(strip=True))
        if per_val is not None and per_val != 0:
            valuation["per"] = per_val

    pbr_elem = main_soup.select_one("em#_pbr")
    if pbr_elem:
        pbr_val = _parse_korean_number(pbr_elem.get_text(strip=True))
        if pbr_val is not None and pbr_val != 0:
            valuation["pbr"] = pbr_val

    dvr_elem = main_soup.select_one("em#_dvr")
    if dvr_elem:
        dvr_val = _parse_korean_number(dvr_elem.get_text(strip=True))
        if dvr_val is not None:
            valuation["dividend_yield"] = dvr_val / 100

    for row in main_soup.select("tr"):
        th = row.select_one("th")
        if not th:
            continue
        th_text = th.get_text(strip=True)
        if not th_text.startswith("ROE"):
            continue
        tds = row.select("td")
        if not tds:
            continue
        roe_val = _parse_korean_number(tds[0].get_text(strip=True))
        if roe_val is None:
            continue
        if "ROE(%)" in th_text:
            valuation["roe"] = roe_val
        elif "지배" in th_text:
            valuation["roe_controlling"] = roe_val

    for row in sise_soup.select("tr"):
        cells = row.select("th, td")
        for i, cell in enumerate(cells):
            label = cell.get_text(strip=True)
            if "52주 최고" in label or "52주최고" in label:
                if i + 1 < len(cells):
                    high_val = _parse_korean_number(cells[i + 1].get_text(strip=True))
                    if high_val:
                        valuation["high_52w"] = int(high_val)
            elif "52주 최저" in label or "52주최저" in label:
                if i + 1 < len(cells):
                    low_val = _parse_korean_number(cells[i + 1].get_text(strip=True))
                    if low_val:
                        valuation["low_52w"] = int(low_val)

    if (
        valuation["current_price"] is not None
        and valuation["high_52w"] is not None
        and valuation["low_52w"] is not None
    ):
        high = valuation["high_52w"]
        low = valuation["low_52w"]
        current = valuation["current_price"]
        if high > low:
            valuation["current_position_52w"] = round((current - low) / (high - low), 2)

    return valuation


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


async def fetch_news(code: str, limit: int = 20) -> list[dict[str, Any]]:
    """Fetch stock news from Naver Finance.

    URL: finance.naver.com/item/news_news.naver?code={code}
    (Note: news.naver loads via iframe, so we fetch news_news.naver directly)

    Args:
        code: 6-digit Korean stock code (e.g., "005930")
        limit: Maximum number of news items to return

    Returns:
        List of news items with title, source, datetime, url
    """
    url = f"{NAVER_FINANCE_ITEM}/news_news.naver"
    soup = await _fetch_html(url, params={"code": code, "page": "", "clusterId": ""})
    return _parse_news_soup(soup, limit)


# ---------------------------------------------------------------------------
# Company Profile
# ---------------------------------------------------------------------------


async def fetch_company_profile(code: str) -> dict[str, Any]:
    """Fetch company profile from Naver Finance.

    URL: finance.naver.com/item/main.naver?code={code}

    Args:
        code: 6-digit Korean stock code

    Returns:
        Company profile with name, sector, market_cap, exchange, etc.
    """
    url = f"{NAVER_FINANCE_ITEM}/main.naver"
    soup = await _fetch_html(url, params={"code": code})

    profile: dict[str, Any] = {
        "symbol": code,
        "name": None,
        "sector": None,
        "industry": None,
        "market_cap": None,
        "shares_outstanding": None,
        "per": None,
        "pbr": None,
        "eps": None,
        "bps": None,
        "dividend_yield": None,
        "exchange": None,
        "website": None,
    }

    # Company name from <div class="wrap_company">
    name_elem = soup.select_one("div.wrap_company h2 a")
    if name_elem:
        profile["name"] = name_elem.get_text(strip=True)

    # Market/exchange detection from code_info section
    code_info = soup.select_one("div.code")
    if code_info:
        code_text = code_info.get_text(strip=True)
        if "코스피" in code_text:
            profile["exchange"] = "KOSPI"
        elif "코스닥" in code_text:
            profile["exchange"] = "KOSDAQ"

    # Parse summary table with key metrics
    # Look for tables in the aside section
    for table in soup.select("table.no_info, table.tb_type1"):
        for row in table.select("tr"):
            cells = row.select("th, td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True)
                value_elem = cells[1]

                # Handle em element inside td
                em = value_elem.select_one("em")
                value = (
                    em.get_text(strip=True) if em else value_elem.get_text(strip=True)
                )

                if "시가총액" in label:
                    profile["market_cap"] = _parse_korean_number(value)
                elif "상장주식수" in label:
                    profile["shares_outstanding"] = _parse_korean_number(value)
                elif label == "PER":
                    profile["per"] = _parse_korean_number(value)
                elif label == "PBR":
                    profile["pbr"] = _parse_korean_number(value)
                elif label == "EPS":
                    profile["eps"] = _parse_korean_number(value)
                elif label == "BPS":
                    profile["bps"] = _parse_korean_number(value)
                elif "배당수익률" in label:
                    profile["dividend_yield"] = _parse_korean_number(value)

    # Try to get market cap from _market_sum element
    market_sum_elem = soup.select_one("em#_market_sum")
    if market_sum_elem and profile["market_cap"] is None:
        profile["market_cap"] = _parse_korean_number(
            market_sum_elem.get_text(strip=True)
        )

    # Get sector from tab_con1 section
    sector_elem = soup.select_one("div.tab_con1 em a")
    if sector_elem:
        profile["sector"] = sector_elem.get_text(strip=True)

    # Filter out None values
    return {k: v for k, v in profile.items() if v is not None}


# ---------------------------------------------------------------------------
# Financial Statements
# ---------------------------------------------------------------------------


async def fetch_financials(
    code: str,
    statement: str = "income",
    freq: str = "annual",
) -> dict[str, Any]:
    """Fetch financial statements from Naver Finance.

    URL: finance.naver.com/item/main.naver?code={code} (financial summary section)

    Args:
        code: 6-digit Korean stock code
        statement: "income", "balance", or "cashflow"
        freq: "annual" or "quarterly"

    Returns:
        Financial statement data with periods and metrics
    """
    url = f"{NAVER_FINANCE_ITEM}/main.naver"
    soup = await _fetch_html(url, params={"code": code})

    financials: dict[str, Any] = {
        "symbol": code,
        "statement": statement,
        "freq": freq,
        "currency": "KRW",
        "periods": [],
        "metrics": {},
    }

    # The main page has a financial summary table
    # Look for the cop_analysis section which contains financial data
    fin_section = soup.select_one("div.section.cop_analysis")
    if not fin_section:
        return financials

    # Find the table with financial data
    table = fin_section.select_one("table")
    if not table:
        return financials

    # Parse header row to get periods
    header = table.select_one("thead tr, tr:first-child")
    if header:
        period_cells = header.select("th")[1:]  # Skip first column (metric name)
        financials["periods"] = [cell.get_text(strip=True) for cell in period_cells]

    # Parse data rows
    rows = table.select("tbody tr, tr")
    for row in rows:
        cells = row.select("td, th")
        if len(cells) < 2:
            continue

        metric_name = cells[0].get_text(strip=True)
        if not metric_name:
            continue

        # Filter metrics based on statement type
        income_metrics = ["매출액", "영업이익", "당기순이익", "영업이익률", "순이익률"]
        balance_metrics = ["자산총계", "부채총계", "자본총계", "부채비율"]
        cashflow_metrics = ["영업활동", "투자활동", "재무활동"]

        if statement == "income" and not any(m in metric_name for m in income_metrics):
            continue
        if statement == "balance" and not any(
            m in metric_name for m in balance_metrics
        ):
            continue
        if statement == "cashflow" and not any(
            m in metric_name for m in cashflow_metrics
        ):
            continue

        values = []
        for cell in cells[1:]:
            value = _parse_korean_number(cell.get_text(strip=True))
            values.append(value)

        if values:
            financials["metrics"][metric_name] = values

    return financials


# ---------------------------------------------------------------------------
# Investor Trends
# ---------------------------------------------------------------------------


async def fetch_investor_trends(code: str, days: int = 20) -> dict[str, Any]:
    """Fetch foreign/institutional investor trading trends.

    URL: finance.naver.com/item/frgn.naver?code={code}

    Args:
        code: 6-digit Korean stock code
        days: Number of days of data to fetch

    Returns:
        Daily investor flow data (foreign, institutional, individual net trades)
    """
    url = f"{NAVER_FINANCE_ITEM}/frgn.naver"
    soup = await _fetch_html(url, params={"code": code})

    trends: dict[str, Any] = {
        "symbol": code,
        "days": days,
        "data": [],
    }

    # There are multiple table.type2 on the page
    # The one with actual investor data has rows with 7+ cells
    # Columns: 날짜, 종가, 전일비, 등락률, 거래량, 기관, 외국인
    tables = soup.select("table.type2")
    target_table = None

    for table in tables:
        # Find the table that has data rows with 7 cells
        rows = table.select("tr")
        for row in rows:
            cells = row.select("td")
            if len(cells) >= 7:
                # Check if first cell looks like a date
                first_cell = cells[0].get_text(strip=True)
                if first_cell and first_cell[0].isdigit():
                    target_table = table
                    break
        if target_table:
            break

    if not target_table:
        return trends

    rows = target_table.select("tr")
    for row in rows:
        cells = row.select("td")
        # Columns: 날짜(0), 종가(1), 전일비(2), 등락률(3), 거래량(4), 기관(5), 외국인(6)
        if len(cells) < 7:
            continue

        try:
            date_text = cells[0].get_text(strip=True)
            if not date_text or not date_text[0].isdigit():
                continue

            # Parse 전일비 which includes direction text (상승/하락)
            change_text = cells[2].get_text(strip=True)

            data_point = {
                "date": _parse_naver_date(date_text),
                "close": _parse_korean_number(cells[1].get_text(strip=True)),
                "change": _parse_korean_number(change_text),
                "change_pct": _parse_korean_number(cells[3].get_text(strip=True)),
                "volume": _parse_korean_number(cells[4].get_text(strip=True)),
                "institutional_net": _parse_korean_number(
                    cells[5].get_text(strip=True)
                ),
                "foreign_net": _parse_korean_number(cells[6].get_text(strip=True)),
            }

            trends["data"].append(data_point)

            if len(trends["data"]) >= days:
                break
        except (IndexError, ValueError):
            continue

    return trends


# ---------------------------------------------------------------------------
# Investment Opinions
# ---------------------------------------------------------------------------


async def _fetch_report_detail(nid: str) -> dict[str, Any] | None:
    try:
        url = f"{NAVER_FINANCE_BASE}/research/company_read.naver"
        soup = await _fetch_html(url, params={"nid": nid})
        return _parse_report_detail_soup(soup)
    except Exception:
        return None


async def _fetch_report_detail_with_client(
    client: httpx.AsyncClient, nid: str
) -> dict[str, Any] | None:
    try:
        url = f"{NAVER_FINANCE_BASE}/research/company_read.naver"
        soup = await _fetch_html_with_client(client, url, params={"nid": nid})
        return _parse_report_detail_soup(soup)
    except Exception:
        return None


async def _fetch_current_price(code: str) -> int | None:
    """Fetch current stock price from Naver Finance main page.

    Args:
        code: 6-digit Korean stock code

    Returns:
        Current price as integer, or None if not found
    """
    try:
        url = f"{NAVER_FINANCE_ITEM}/main.naver"
        soup = await _fetch_html(url, params={"code": code})
        return _extract_current_price_from_main_soup(soup)
    except Exception:
        return None


async def fetch_investment_opinions(code: str, limit: int = 10) -> dict[str, Any]:
    """Fetch securities firm investment opinions and target prices.

    URL: finance.naver.com/research/company_list.naver
    Individual reports: finance.naver.com/research/company_read.naver?nid={nid}

    Args:
        code: 6-digit Korean stock code
        limit: Maximum number of opinions to return

    Returns:
        Investment opinions with normalized ratings and consensus statistics:
        - symbol: Stock code
        - count: Number of opinions
        - opinions: List of individual opinions with normalized ratings
        - consensus: Aggregated statistics (buy/hold/sell counts, target prices, upside_pct)
    """
    url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
    company_list_soup = await _fetch_html(
        url, params={"searchType": "itemCode", "itemCode": code}
    )
    current_price = await _fetch_current_price(code)
    return await _build_investment_opinions_from_company_list_soup(
        code,
        company_list_soup,
        limit,
        current_price=current_price,
        detail_fetcher=_fetch_report_detail,
    )


async def _fetch_kr_snapshot(
    code: str,
    *,
    news_limit: int = 5,
    opinion_limit: int = 10,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        main_url = f"{NAVER_FINANCE_ITEM}/main.naver"
        sise_url = f"{NAVER_FINANCE_ITEM}/sise.naver"
        news_url = f"{NAVER_FINANCE_ITEM}/news_news.naver"
        company_list_url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
        page_results = await asyncio.gather(
            _fetch_html_with_client(client, main_url, params={"code": code}),
            _fetch_html_with_client(client, sise_url, params={"code": code}),
            _fetch_html_with_client(
                client,
                news_url,
                params={"code": code, "page": "", "clusterId": ""},
            ),
            _fetch_html_with_client(
                client,
                company_list_url,
                params={"searchType": "itemCode", "itemCode": code},
            ),
            return_exceptions=True,
        )
        main_soup = (
            page_results[0] if isinstance(page_results[0], BeautifulSoup) else None
        )
        sise_soup = (
            page_results[1] if isinstance(page_results[1], BeautifulSoup) else None
        )
        news_soup = (
            page_results[2] if isinstance(page_results[2], BeautifulSoup) else None
        )
        company_list_soup = (
            page_results[3] if isinstance(page_results[3], BeautifulSoup) else None
        )

        snapshot: dict[str, Any] = {
            "valuation": None,
            "news": None,
            "opinions": None,
        }

        if main_soup is not None and sise_soup is not None:
            snapshot["valuation"] = _parse_valuation_from_soups(
                code, main_soup, sise_soup
            )

        if news_soup is not None:
            snapshot["news"] = _parse_news_soup(news_soup, news_limit)

        if company_list_soup is not None:
            current_price = (
                _extract_current_price_from_main_soup(main_soup)
                if main_soup is not None
                else None
            )
            snapshot[
                "opinions"
            ] = await _build_investment_opinions_from_company_list_soup(
                code,
                company_list_soup,
                opinion_limit,
                current_price=current_price,
                detail_fetcher=lambda nid: _fetch_report_detail_with_client(
                    client, nid
                ),
            )

        return snapshot


# ---------------------------------------------------------------------------
# Valuation Metrics
# ---------------------------------------------------------------------------


async def fetch_valuation(code: str) -> dict[str, Any]:
    """Fetch valuation metrics from Naver Finance.

    Fetches data from two pages:
    - main.naver: PER, PBR, dividend yield, current price
    - sise.naver: 52-week high/low

    Args:
        code: 6-digit Korean stock code (e.g., "005930")

    Returns:
        Valuation metrics including PER, PBR, ROE, dividend_yield,
        52-week high/low, current price, and current_position_52w
    """
    main_url = f"{NAVER_FINANCE_ITEM}/main.naver"
    sise_url = f"{NAVER_FINANCE_ITEM}/sise.naver"
    main_soup, sise_soup = await asyncio.gather(
        _fetch_html(main_url, params={"code": code}),
        _fetch_html(sise_url, params={"code": code}),
    )
    return _parse_valuation_from_soups(code, main_soup, sise_soup)


# ---------------------------------------------------------------------------
# Sector Peers
# ---------------------------------------------------------------------------

NAVER_MOBILE_API = "https://m.stock.naver.com/api/stock"


def _parse_total_infos(total_infos: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract key metrics from the ``totalInfos`` list returned by Naver mobile API.

    Args:
        total_infos: List of dicts with ``code``, ``key``, ``value`` fields.

    Returns:
        Dict with parsed ``per``, ``pbr``, ``market_cap`` etc.
    """
    result: dict[str, Any] = {}
    code_map = {
        "per": "per",
        "pbr": "pbr",
        "eps": "eps",
        "bps": "bps",
        "marketValue": "market_cap",
        "dividendYieldRatio": "dividend_yield",
    }
    for item in total_infos:
        code = item.get("code", "")
        if code not in code_map:
            continue
        raw = item.get("value", "")
        # Strip trailing unit suffixes (배, 원, %)
        cleaned = re.sub(r"[배원%]", "", raw).strip() if raw else None
        result[code_map[code]] = _parse_korean_number(cleaned)
    return result


async def _fetch_integration(
    code: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Fetch the Naver mobile basic + integration endpoints for a single stock.

    Returns a dict with ``name``, ``per``, ``pbr``, ``market_cap``, ``current_price``,
    ``change_pct``, ``industry_code``, ``peers_raw``.
    """
    r_basic, r_integ = await asyncio.gather(
        client.get(f"{NAVER_MOBILE_API}/{code}/basic"),
        client.get(f"{NAVER_MOBILE_API}/{code}/integration"),
    )
    r_basic.raise_for_status()
    r_integ.raise_for_status()

    basic = r_basic.json()
    integ = r_integ.json()

    metrics = _parse_total_infos(integ.get("totalInfos", []))
    name = basic.get("stockName") or integ.get("stockName")

    # Current price & change from basic endpoint
    close_raw = basic.get("closePrice")
    change_pct_raw = basic.get("fluctuationsRatio")
    current_price = _parse_korean_number(close_raw) if close_raw else None
    change_pct: float | None = None
    if change_pct_raw is not None:
        try:
            change_pct = float(str(change_pct_raw).replace(",", ""))
        except (ValueError, TypeError):
            pass

    return {
        "symbol": code,
        "name": name,
        "per": metrics.get("per"),
        "pbr": metrics.get("pbr"),
        "market_cap": metrics.get("market_cap"),
        "current_price": current_price,
        "change_pct": change_pct,
        "industry_code": integ.get("industryCode"),
        "peers_raw": integ.get("industryCompareInfo", []),
    }


async def fetch_sector_peers(
    code: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Fetch sector peer stocks for a Korean equity via Naver mobile API.

    Steps:
        1. Call ``/api/stock/{code}/integration`` to get industry code, peer list,
           and the target stock's own metrics.
        2. For each peer, call the same endpoint concurrently to retrieve PER / PBR.
        3. Sort peers by market-cap descending and return the top *limit*.

    If the integration API returns fewer peers than *limit*, falls back to
    scraping the sector detail page to discover additional stock codes, then
    fetches their data via the integration API.

    Args:
        code: 6-digit Korean stock code (e.g. ``"298040"``).
        limit: Maximum number of peers to return.

    Returns:
        Dict with ``symbol``, ``name``, ``sector``, ``peers`` list, and
        ``comparison`` metrics.
    """
    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=10,
    ) as client:
        target = await _fetch_integration(code, client)
        sector_name: str | None = None

        # ---- Collect peer codes from integration response ----
        peer_codes: list[str] = []
        for p in target["peers_raw"]:
            pc = p.get("itemCode", "")
            if pc and pc != code:
                peer_codes.append(pc)

        # If we need more peers, scrape the sector detail page
        industry_code = target.get("industry_code")
        if len(peer_codes) < limit and industry_code:
            extra_codes = await _fetch_sector_stock_codes(str(industry_code), client)
            seen = {code, *peer_codes}
            for ec in extra_codes:
                if ec not in seen:
                    peer_codes.append(ec)
                    seen.add(ec)

        # ---- Fetch integration data for each peer concurrently ----
        peer_codes = peer_codes[: limit + 5]  # fetch extras in case some fail

        async def _safe_fetch(pc: str) -> dict[str, Any] | None:
            try:
                return await _fetch_integration(pc, client)
            except Exception:
                return None

        peer_results = await asyncio.gather(*[_safe_fetch(pc) for pc in peer_codes])

        # Resolve sector name from the sector page title
        if industry_code:
            sector_name = await _fetch_sector_name(str(industry_code), client)

    # ---- Build peer list ----
    peers: list[dict[str, Any]] = []
    for pr in peer_results:
        if pr is None:
            continue
        peers.append(
            {
                "symbol": pr["symbol"],
                "name": pr["name"],
                "current_price": pr["current_price"],
                "change_pct": pr["change_pct"],
                "per": pr["per"],
                "pbr": pr["pbr"],
                "market_cap": pr["market_cap"],
            }
        )

    # Sort by market_cap desc (None last)
    peers.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)
    peers = peers[:limit]

    return {
        "symbol": code,
        "name": target["name"],
        "sector": sector_name,
        "industry_code": industry_code,
        "current_price": target["current_price"],
        "change_pct": target["change_pct"],
        "per": target["per"],
        "pbr": target["pbr"],
        "market_cap": target["market_cap"],
        "peers": peers,
    }


async def _fetch_sector_stock_codes(
    sector_code: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """Scrape stock codes from the Naver sector detail page.

    URL: ``finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={sector_code}``
    """
    url = f"{NAVER_FINANCE_BASE}/sise/sise_group_detail.naver"
    try:
        r = await client.get(url, params={"type": "upjong", "no": sector_code})
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "lxml")

        table = soup.select_one("table.type_5")
        if not table:
            return []

        codes: list[str] = []
        for a in table.select("a[href*='code=']"):
            href = a.get("href")
            href_str = href if isinstance(href, str) else ""
            m = re.search(r"code=(\w{6})", href_str)
            if m:
                codes.append(m.group(1))
        return codes
    except Exception:
        return []


async def _fetch_sector_name(
    sector_code: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Fetch sector name from the Naver sector detail page ``<title>`` tag.

    The title has the format ``"전기장비 : Npay 증권"``.
    """
    url = f"{NAVER_FINANCE_BASE}/sise/sise_group_detail.naver"
    try:
        r = await client.get(url, params={"type": "upjong", "no": sector_code})
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "lxml")
        title_elem = soup.select_one("title")
        if title_elem:
            raw = title_elem.get_text(strip=True)
            # "전기장비 : Npay 증권" → "전기장비"
            return raw.split(":")[0].strip() if ":" in raw else raw
        return None
    except Exception:
        return None
