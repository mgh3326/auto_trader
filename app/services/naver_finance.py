"""Naver Finance crawling service for Korean equities.

This module provides async functions to fetch:
- Stock news and disclosures
- Company profile information
- Financial statements
- Foreign/institutional investor trends
- Securities firm investment opinions
- Short selling data (via KRX pykrx library)
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup

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
        date_str: Date string in various formats (e.g., "2024.01.15", "01.15")

    Returns:
        ISO format date string (e.g., "2024-01-15") or None
    """
    if not date_str:
        return None

    date_str = date_str.strip()
    if not date_str:
        return None

    # Full date format: 2024.01.15 or 2024-01-15 or 2024/01/15
    match = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    # Short date format (assumes current year): 01.15 or 01-15
    match = re.match(r"(\d{1,2})[.\-/](\d{1,2})", date_str)
    if match:
        year = date.today().year
        month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    return date_str


def _parse_korean_number(value_str: str | None) -> int | float | None:
    """Parse Korean number formats.

    Handles formats like:
    - "1,234" → 1234
    - "5.67%" → 0.0567
    - "1조 2,345억" → 1,234,500,000,000
    - "▼1,234" or "-1,234" → -1234

    Args:
        value_str: Number string in Korean format

    Returns:
        Parsed number (int for whole numbers, float for decimals) or None
    """
    if not value_str:
        return None

    # Remove whitespace
    cleaned = value_str.strip()
    if not cleaned:
        return None

    # Handle percentage
    is_percent = "%" in cleaned
    cleaned = cleaned.replace("%", "")

    # Handle negative indicators
    is_negative = (
        cleaned.startswith("-")
        or "▼" in cleaned
        or "하락" in cleaned
        or cleaned.startswith("−")  # Unicode minus
    )
    cleaned = re.sub(r"[▲▼하락상승\-+−]", "", cleaned)

    # Remove commas and spaces
    cleaned = cleaned.replace(",", "").replace(" ", "")

    # Handle Korean units (조, 억, 만)
    # Process from largest to smallest
    total = 0.0
    remaining = cleaned

    # 조 (trillion in Korean, 10^12)
    if "조" in remaining:
        parts = remaining.split("조")
        try:
            jo_value = float(parts[0]) if parts[0] else 0
            total += jo_value * 1_0000_0000_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # 억 (hundred million, 10^8)
    if "억" in remaining:
        parts = remaining.split("억")
        try:
            eok_value = float(parts[0]) if parts[0] else 0
            total += eok_value * 1_0000_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # 만 (ten thousand, 10^4)
    if "만" in remaining:
        parts = remaining.split("만")
        try:
            man_value = float(parts[0]) if parts[0] else 0
            total += man_value * 1_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # Add any remaining number
    if remaining:
        try:
            total += float(remaining)
        except ValueError:
            if total == 0:
                return None

    # If no Korean units were found, try parsing as plain number
    if total == 0 and not any(unit in value_str for unit in ["조", "억", "만"]):
        try:
            total = float(cleaned)
        except ValueError:
            return None

    # Apply percentage
    if is_percent:
        total = total / 100

    # Apply negative
    if is_negative:
        total = -abs(total)

    # Return int if whole number, float otherwise
    if total == int(total) and not is_percent:
        return int(total)
    return total


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
        response = await client.get(url, params=params, headers=DEFAULT_HEADERS)
        response.raise_for_status()

        # Naver Finance uses EUC-KR encoding for some pages
        content = response.content
        try:
            html = content.decode("euc-kr")
        except UnicodeDecodeError:
            html = content.decode("utf-8", errors="replace")

        return BeautifulSoup(html, "lxml")


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
    # The main news.naver page loads news via iframe
    # We directly fetch the iframe content: news_news.naver
    url = f"{NAVER_FINANCE_ITEM}/news_news.naver"
    soup = await _fetch_html(url, params={"code": code, "page": "", "clusterId": ""})

    news_items: list[dict[str, Any]] = []

    # Parse news table - structure: <table class="type5">
    # The news table contains both company news and disclosure news
    table = soup.select_one("table.type5")
    if not table:
        return []

    rows = table.select("tr")
    for row in rows:
        # Skip header rows and empty rows
        title_elem = row.select_one("td.title a")
        if not title_elem:
            continue

        # Extract source (media name)
        source_elem = row.select_one("td.info")

        # Extract date
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


async def fetch_investment_opinions(code: str, limit: int = 10) -> dict[str, Any]:
    """Fetch securities firm investment opinions and target prices.

    URL: finance.naver.com/research/company_list.naver

    Args:
        code: 6-digit Korean stock code
        limit: Maximum number of opinions to return

    Returns:
        Investment opinions with target price, rating, firm, and date
    """
    # Search for company-specific research reports
    url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
    soup = await _fetch_html(url, params={"searchType": "itemCode", "itemCode": code})

    opinions: dict[str, Any] = {
        "symbol": code,
        "count": 0,
        "opinions": [],
    }

    # Parse research report table - <table class="type_1">
    table = soup.select_one("table.type_1")
    if not table:
        return opinions

    rows = table.select("tbody tr, tr")
    for row in rows:
        cells = row.select("td")
        # Expected columns: 종목명, 리포트 제목, 증권사, 의견, 목표가, 등록일
        if len(cells) < 6:
            continue

        try:
            # Skip if this is a header row or ad row
            title_elem = cells[1].select_one("a")
            if not title_elem:
                continue

            opinion = {
                "stock_name": cells[0].get_text(strip=True),
                "title": title_elem.get_text(strip=True),
                "firm": cells[2].get_text(strip=True),
                "rating": cells[3].get_text(strip=True),
                "target_price": _parse_korean_number(cells[4].get_text(strip=True)),
                "date": _parse_naver_date(cells[5].get_text(strip=True)),
            }

            # Get report URL
            href = title_elem.get("href") or ""
            href_str = href if isinstance(href, str) else ""
            if href_str:
                opinion["url"] = (
                    href_str
                    if href_str.startswith("http")
                    else NAVER_FINANCE_BASE + href_str
                )

            opinions["opinions"].append(opinion)

            if len(opinions["opinions"]) >= limit:
                break
        except (IndexError, ValueError):
            continue

    opinions["count"] = len(opinions["opinions"])
    return opinions


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
    import asyncio

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

    # Fetch both pages concurrently
    main_url = f"{NAVER_FINANCE_ITEM}/main.naver"
    sise_url = f"{NAVER_FINANCE_ITEM}/sise.naver"

    main_soup, sise_soup = await asyncio.gather(
        _fetch_html(main_url, params={"code": code}),
        _fetch_html(sise_url, params={"code": code}),
    )

    # === Parse main.naver page ===

    # Company name from <div class="wrap_company">
    name_elem = main_soup.select_one("div.wrap_company h2 a")
    if name_elem:
        valuation["name"] = name_elem.get_text(strip=True)

    # Current price from <p class="no_today">
    # The actual value is in span.blind inside em
    price_elem = main_soup.select_one("p.no_today em span.blind")
    if price_elem:
        valuation["current_price"] = _parse_korean_number(price_elem.get_text(strip=True))

    # Fallback: try getting from no_today directly
    if valuation["current_price"] is None:
        no_today = main_soup.select_one("p.no_today")
        if no_today:
            # Get text and parse
            price_text = no_today.get_text(strip=True)
            valuation["current_price"] = _parse_korean_number(price_text)

    # PER, PBR, dividend yield from ID-based elements (most reliable)
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

    # Dividend yield (배당수익률) - em#_dvr
    dvr_elem = main_soup.select_one("em#_dvr")
    if dvr_elem:
        dvr_text = dvr_elem.get_text(strip=True)
        # dvr is already a percentage value like "1.05"
        dvr_val = _parse_korean_number(dvr_text)
        if dvr_val is not None:
            # Convert to decimal (1.05% -> 0.0105)
            valuation["dividend_yield"] = dvr_val / 100

    # ROE from table (look for ROE label in th elements)
    # ROE(%) -> roe, ROE(지배주주) -> roe_controlling
    for row in main_soup.select("tr"):
        th = row.select_one("th")
        if th:
            th_text = th.get_text(strip=True)
            if th_text.startswith("ROE"):
                tds = row.select("td")
                if tds:
                    # First td is the most recent value
                    roe_val = _parse_korean_number(tds[0].get_text(strip=True))
                    if roe_val is not None:
                        if "ROE(%)" in th_text:
                            valuation["roe"] = roe_val
                        elif "지배" in th_text:  # ROE(지배주주)
                            valuation["roe_controlling"] = roe_val

    # === Parse sise.naver page for 52-week high/low ===

    # Find rows with "52주 최고" and "52주 최저" labels
    for row in sise_soup.select("tr"):
        cells = row.select("th, td")
        for i, cell in enumerate(cells):
            label = cell.get_text(strip=True)

            if "52주 최고" in label or "52주최고" in label:
                # Value is in next cell
                if i + 1 < len(cells):
                    high_val = _parse_korean_number(cells[i + 1].get_text(strip=True))
                    if high_val:
                        valuation["high_52w"] = int(high_val)
            elif "52주 최저" in label or "52주최저" in label:
                if i + 1 < len(cells):
                    low_val = _parse_korean_number(cells[i + 1].get_text(strip=True))
                    if low_val:
                        valuation["low_52w"] = int(low_val)

    # Calculate current_position_52w if we have all required values
    if (
        valuation["current_price"] is not None
        and valuation["high_52w"] is not None
        and valuation["low_52w"] is not None
    ):
        high = valuation["high_52w"]
        low = valuation["low_52w"]
        current = valuation["current_price"]

        if high > low:
            position = (current - low) / (high - low)
            valuation["current_position_52w"] = round(position, 2)

    return valuation


# ---------------------------------------------------------------------------
# Short Selling Data (via KRX pykrx library)
# ---------------------------------------------------------------------------


async def _fetch_short_data_from_naver(
    code: str, days: int
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Fetch short selling data directly from Naver Finance.

    Naver Finance provides short selling data via an iframe at:
    https://finance.naver.com/item/sise_short.naver?code={code}

    Args:
        code: 6-digit Korean stock code
        days: Number of days to fetch

    Returns:
        Tuple of (short_data_list, balance_data)
    """
    short_data_list: list[dict[str, Any]] = []
    balance_data: dict[str, Any] | None = None

    # Naver Finance short selling page
    # This page shows short selling data in a table
    url = f"{NAVER_FINANCE_ITEM}/sise_short.naver"

    try:
        soup = await _fetch_html(url, params={"code": code})

        # Find the data table - it has class "type2" typically
        # The table structure varies but usually has columns:
        # 날짜, 종가, 전일비, 거래량, 공매도량, 공매도금액, 공매도비중
        table = soup.select_one("table.type2")

        if table:
            rows = table.select("tr")
            for row in rows:
                cells = row.select("td")
                # Need at least 7 cells for full data
                if len(cells) >= 7:
                    try:
                        date_text = cells[0].get_text(strip=True)
                        if not date_text or not date_text[0].isdigit():
                            continue

                        # Parse each column
                        close_price = _parse_korean_number(cells[1].get_text(strip=True))
                        total_volume = _parse_korean_number(cells[3].get_text(strip=True))
                        short_volume = _parse_korean_number(cells[4].get_text(strip=True))
                        short_amount = _parse_korean_number(cells[5].get_text(strip=True))

                        # Short ratio might be in different positions
                        short_ratio_text = cells[6].get_text(strip=True)
                        short_ratio = _parse_korean_number(short_ratio_text.replace("%", ""))

                        short_data_list.append(
                            {
                                "date": _parse_naver_date(date_text),
                                "short_volume": int(short_volume) if short_volume else None,
                                "short_amount": int(short_amount) if short_amount else None,
                                "short_ratio": float(short_ratio) if short_ratio else None,
                                "total_volume": int(total_volume) if total_volume else None,
                                "close_price": int(close_price) if close_price else None,
                            }
                        )

                        if len(short_data_list) >= days:
                            break
                    except (IndexError, ValueError):
                        continue

        # Try to get short balance from a summary section if available
        balance_section = soup.select_one("div.short_balance, table.short_balance")
        if balance_section:
            # Extract balance info if available
            balance_text = balance_section.get_text()
            balance_match = re.search(r"공매도\s*잔고[:\s]*(\d[\d,]*)", balance_text)
            if balance_match:
                balance_data = {
                    "balance_shares": _parse_korean_number(balance_match.group(1)),
                }

    except Exception:
        # If Naver fetch fails, try alternative sources
        pass

    return short_data_list, balance_data


async def _fetch_short_data_from_pykrx(
    code: str, days: int
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Fetch short selling data from KRX via pykrx library.

    Args:
        code: 6-digit Korean stock code
        days: Number of days to fetch

    Returns:
        Tuple of (short_data_list, balance_data)
    """
    # Calculate date range
    end_date = date.today()
    # Add extra days to account for weekends/holidays
    start_date = end_date - timedelta(days=days * 2)

    def _fetch_sync() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Synchronous function to fetch short selling data from pykrx."""
        import pandas as pd

        from pykrx import stock as pykrx_stock

        short_data_list: list[dict[str, Any]] = []

        # Fetch short selling status (거래량, 거래대금, 비중)
        try:
            df_short = pykrx_stock.get_shorting_status_by_date(
                fromdate=start_date.strftime("%Y%m%d"),
                todate=end_date.strftime("%Y%m%d"),
                ticker=code,
            )

            if df_short is not None and not df_short.empty:
                for idx, row in df_short.iterrows():
                    date_str = (
                        idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                    )

                    def safe_value(val: Any) -> Any:
                        if pd.isna(val):
                            return None
                        return val

                    short_amount = safe_value(row.get("공매도거래대금"))
                    total_amount = safe_value(row.get("총거래대금"))
                    short_ratio = safe_value(row.get("비중"))

                    short_data_list.append(
                        {
                            "date": date_str,
                            "short_amount": int(short_amount) if short_amount else None,
                            "total_amount": int(total_amount) if total_amount else None,
                            "short_ratio": float(short_ratio) if short_ratio else None,
                            "short_volume": None,
                            "total_volume": None,
                        }
                    )
        except Exception:
            pass

        # Fetch short balance (공매도 잔고)
        balance_data: dict[str, Any] | None = None
        try:
            df_balance = pykrx_stock.get_shorting_balance_by_date(
                fromdate=end_date.strftime("%Y%m%d"),
                todate=end_date.strftime("%Y%m%d"),
                ticker=code,
            )

            if df_balance is not None and not df_balance.empty:
                last_row = df_balance.iloc[-1]
                balance_data = {
                    "balance_shares": int(last_row.get("공매도잔고", 0)) or None,
                    "balance_amount": int(last_row.get("공매도금액", 0)) or None,
                    "balance_ratio": float(last_row.get("비중", 0)) or None,
                }
        except Exception:
            pass

        return short_data_list, balance_data

    return await asyncio.to_thread(_fetch_sync)


async def fetch_short_interest(code: str, days: int = 20) -> dict[str, Any]:
    """Fetch short selling data for a Korean stock.

    Tries multiple data sources in order:
    1. Naver Finance (direct crawling)
    2. KRX via pykrx library

    Args:
        code: 6-digit Korean stock code (e.g., "005930")
        days: Number of days of data to fetch (default: 20)

    Returns:
        Dictionary with short selling data:
        - symbol: Stock code
        - name: Company name (if available)
        - short_data: List of daily short selling data
        - avg_short_ratio: Average short ratio over the period
        - short_balance: Short balance (if available)
    """
    # Fetch company name
    name = None
    try:
        url = f"{NAVER_FINANCE_ITEM}/main.naver"
        soup = await _fetch_html(url, params={"code": code})
        name_elem = soup.select_one("div.wrap_company h2 a")
        if name_elem:
            name = name_elem.get_text(strip=True)
    except Exception:
        pass

    # Try Naver Finance first
    short_data, balance_data = await _fetch_short_data_from_naver(code, days)

    # If Naver didn't return data, try pykrx
    if not short_data:
        short_data, balance_data = await _fetch_short_data_from_pykrx(code, days)

    # Sort by date descending and limit to requested days
    short_data = sorted(short_data, key=lambda x: x["date"] or "", reverse=True)[:days]

    # Calculate average short ratio
    avg_short_ratio: float | None = None
    valid_ratios = [d["short_ratio"] for d in short_data if d.get("short_ratio") is not None]
    if valid_ratios:
        avg_short_ratio = round(sum(valid_ratios) / len(valid_ratios), 2)

    result: dict[str, Any] = {
        "symbol": code,
        "name": name,
        "short_data": short_data,
        "avg_short_ratio": avg_short_ratio,
    }

    if balance_data:
        result["short_balance"] = balance_data

    return result
