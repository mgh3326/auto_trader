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


async def _fetch_report_detail(nid: str) -> dict[str, Any] | None:
    """Fetch individual report page to get target price and rating.

    Args:
        nid: Report ID from Naver Finance

    Returns:
        Dictionary with target_price and rating, or None if not found
    """
    url = f"{NAVER_FINANCE_BASE}/research/company_read.naver"

    try:
        soup = await _fetch_html(url, params={"nid": nid})

        result: dict[str, Any] = {
            "target_price": None,
            "rating": None,
        }

        # Find view_info_1 div which contains target price and rating
        # Structure: <div class="view_info_1">
        #     목표가 <em class="money"><strong>183,000</strong></em>
        #     투자의견 <em class="coment">매수</em>
        # </div>
        info_div = soup.select_one("div.view_info_1")
        if info_div:
            # Target price from em.money > strong
            target_elem = info_div.select_one("em.money strong")
            if target_elem:
                result["target_price"] = _parse_korean_number(
                    target_elem.get_text(strip=True)
                )

            # Rating from em.coment
            rating_elem = info_div.select_one("em.coment")
            if rating_elem:
                result["rating"] = rating_elem.get_text(strip=True)

        return result

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

        # Try span.blind inside p.no_today
        price_elem = soup.select_one("p.no_today em span.blind")
        if price_elem:
            price = _parse_korean_number(price_elem.get_text(strip=True))
            if price:
                return int(price)

        # Fallback: try p.no_today directly
        no_today = soup.select_one("p.no_today")
        if no_today:
            price = _parse_korean_number(no_today.get_text(strip=True))
            if price:
                return int(price)

        return None

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
        Investment opinions with target price, rating, firm, date, and statistics:
        - opinions: List of individual opinions with target_price and rating
        - avg_target_price: Average target price from all opinions with target_price
        - max_target_price: Maximum target price
        - min_target_price: Minimum target price
        - current_price: Current stock price
        - upside_potential: (avg_target_price - current_price) / current_price * 100
    """
    # Search for company-specific research reports
    url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
    soup = await _fetch_html(url, params={"searchType": "itemCode", "itemCode": code})

    opinions: dict[str, Any] = {
        "symbol": code,
        "count": 0,
        "opinions": [],
        "current_price": None,
        "avg_target_price": None,
        "max_target_price": None,
        "min_target_price": None,
        "upside_potential": None,
    }

    # Parse research report table - <table class="type_1">
    # Current structure (as of 2025):
    # Cell 0: 종목명 (link)
    # Cell 1: 리포트 제목 (link with nid)
    # Cell 2: 증권사
    # Cell 3: PDF 링크 (empty text)
    # Cell 4: 날짜
    # Cell 5: 조회수
    table = soup.select_one("table.type_1")
    if not table:
        return opinions

    # Collect report info first
    report_infos: list[dict[str, Any]] = []

    rows = table.select("tbody tr, tr")
    for row in rows:
        cells = row.select("td")
        # Need at least 5 cells (종목명, 제목, 증권사, PDF, 날짜)
        if len(cells) < 5:
            continue

        try:
            # Skip if this is a header row or ad row
            title_elem = cells[1].select_one("a")
            if not title_elem:
                continue

            # Extract report ID from href
            href = title_elem.get("href") or ""
            href_str = href if isinstance(href, str) else ""
            nid_match = re.search(r"nid=(\d+)", href_str)
            if not nid_match:
                continue

            nid = nid_match.group(1)

            report_info = {
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

            report_infos.append(report_info)

            if len(report_infos) >= limit:
                break
        except (IndexError, ValueError):
            continue

    # Fetch details for each report (target_price and rating)
    # Use asyncio.gather for concurrent fetching
    if report_infos:
        detail_tasks = [_fetch_report_detail(info["nid"]) for info in report_infos]
        details = await asyncio.gather(*detail_tasks, return_exceptions=True)

        for info, detail in zip(report_infos, details, strict=True):
            opinion = {
                "stock_name": info["stock_name"],
                "title": info["title"],
                "firm": info["firm"],
                "date": info["date"],
                "url": info["url"],
                "target_price": None,
                "rating": None,
            }

            if isinstance(detail, dict):
                opinion["target_price"] = detail.get("target_price")
                opinion["rating"] = detail.get("rating")

            opinions["opinions"].append(opinion)

    opinions["count"] = len(opinions["opinions"])

    # Calculate target price statistics
    target_prices = [
        op["target_price"]
        for op in opinions["opinions"]
        if op.get("target_price") is not None
    ]

    if target_prices:
        opinions["avg_target_price"] = int(sum(target_prices) / len(target_prices))
        opinions["max_target_price"] = max(target_prices)
        opinions["min_target_price"] = min(target_prices)

        # Fetch current price for upside_potential calculation
        current_price = await _fetch_current_price(code)
        if current_price:
            opinions["current_price"] = current_price
            opinions["upside_potential"] = round(
                (opinions["avg_target_price"] - current_price) / current_price * 100, 2
            )

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
        valuation["current_price"] = _parse_korean_number(
            price_elem.get_text(strip=True)
        )

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


async def _fetch_short_data_from_krx(
    code: str, days: int
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Fetch short selling data directly from KRX API.

    KRX provides short selling data via their data marketplace API.
    Naver Finance embeds this data via iframe.

    Args:
        code: 6-digit Korean stock code
        days: Number of days to fetch

    Returns:
        Tuple of (short_data_list, balance_data)
    """
    short_data_list: list[dict[str, Any]] = []
    balance_data: dict[str, Any] | None = None

    # Calculate date range
    end_date = date.today()
    start_date = end_date - timedelta(days=days * 2)  # Extra days for weekends/holidays

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://data.krx.co.kr",
        "Referer": (
            f"https://data.krx.co.kr/comm/srt/srtLoader/index.cmd"
            f"?screenId=MDCSTAT300&isuCd={code}"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"

            # ISIN check digit varies per stock (0-9)
            # Try common check digits first: 3, 2, 0, 1, 4-9
            check_digits = [3, 2, 0, 1, 4, 5, 6, 7, 8, 9]
            result_data: list[dict[str, Any]] = []

            for check_digit in check_digits:
                isin = f"KR7{code}00{check_digit}"

                data = {
                    "bld": "dbms/MDC/STAT/srt/MDCSTAT30001",
                    "locale": "ko_KR",
                    "isuCd": isin,
                    "strtDd": start_date.strftime("%Y%m%d"),
                    "endDd": end_date.strftime("%Y%m%d"),
                    "share": "1",
                    "csvxls_is498": "false",
                }

                resp = await client.post(url, data=data, headers=headers)

                if resp.status_code == 200:
                    result = resp.json()
                    if isinstance(result, dict) and "OutBlock_1" in result:
                        result_data = result["OutBlock_1"]
                        if result_data:
                            break  # Found data, stop trying other check digits

            # Parse the result data
            for item in result_data:
                # Parse date: "2025/02/03" -> "2025-02-03"
                date_str = item.get("TRD_DD", "").replace("/", "-")

                # Parse values (remove commas)
                short_volume = _parse_korean_number(item.get("CVSRTSELL_TRDVOL"))
                short_amount = _parse_korean_number(item.get("CVSRTSELL_TRDVAL"))
                balance_shares = _parse_korean_number(item.get("STR_CONST_VAL1"))
                balance_amount = _parse_korean_number(item.get("STR_CONST_VAL2"))

                short_data_list.append(
                    {
                        "date": date_str,
                        "short_volume": int(short_volume) if short_volume else None,
                        "short_amount": int(short_amount) if short_amount else None,
                        "short_ratio": None,  # Not directly provided
                        "total_volume": None,  # Not in this API
                        "balance_shares": (
                            int(balance_shares) if balance_shares else None
                        ),
                        "balance_amount": (
                            int(balance_amount) if balance_amount else None
                        ),
                    }
                )

            # Get latest balance data (find first entry with balance)
            for entry in short_data_list:
                if entry.get("balance_shares"):
                    balance_data = {
                        "balance_shares": entry["balance_shares"],
                        "balance_amount": entry.get("balance_amount"),
                    }
                    break

    except Exception:
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
                        idx.strftime("%Y-%m-%d")
                        if hasattr(idx, "strftime")
                        else str(idx)
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


async def _fetch_daily_volumes(code: str, days: int) -> dict[str, int]:
    """Fetch daily trading volumes from Naver Finance.

    Args:
        code: 6-digit Korean stock code
        days: Number of days to fetch

    Returns:
        Dictionary mapping date (YYYY-MM-DD) to volume
    """
    volumes: dict[str, int] = {}

    try:
        # Fetch multiple pages if needed (10 rows per page)
        pages_needed = (days // 10) + 2  # Extra pages for weekends/holidays

        for page in range(1, pages_needed + 1):
            url = f"{NAVER_FINANCE_ITEM}/sise_day.naver"
            soup = await _fetch_html(url, params={"code": code, "page": page})

            # Parse table rows
            rows = soup.select("table.type2 tr")
            for row in rows:
                cells = row.select("td")
                if len(cells) < 7:
                    continue

                # Columns: 날짜, 종가, 전일비, 시가, 고가, 저가, 거래량
                date_text = cells[0].get_text(strip=True)
                volume_text = cells[6].get_text(strip=True)

                if not date_text or not date_text[0].isdigit():
                    continue

                parsed_date = _parse_naver_date(date_text)
                volume = _parse_korean_number(volume_text)

                if parsed_date and volume:
                    volumes[parsed_date] = int(volume)

            if len(volumes) >= days:
                break

    except Exception:
        pass

    return volumes


async def fetch_short_interest(code: str, days: int = 20) -> dict[str, Any]:
    """Fetch short selling data for a Korean stock.

    Tries multiple data sources in order:
    1. KRX API (most reliable for short selling data)
    2. pykrx library as fallback

    Also fetches daily volumes from Naver Finance to calculate short_ratio.

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
    # Fetch company name and daily volumes concurrently
    name = None

    async def fetch_name() -> str | None:
        try:
            url = f"{NAVER_FINANCE_ITEM}/main.naver"
            soup = await _fetch_html(url, params={"code": code})
            name_elem = soup.select_one("div.wrap_company h2 a")
            if name_elem:
                return name_elem.get_text(strip=True)
        except Exception:
            pass
        return None

    # Fetch name, volumes, and short data concurrently
    name_task = fetch_name()
    volumes_task = _fetch_daily_volumes(code, days)
    short_task = _fetch_short_data_from_krx(code, days)

    name, daily_volumes, (short_data, balance_data) = await asyncio.gather(
        name_task, volumes_task, short_task
    )

    # If KRX API didn't return data, try pykrx as fallback
    if not short_data:
        short_data, balance_data = await _fetch_short_data_from_pykrx(code, days)

    # Sort by date descending and limit to requested days
    short_data = sorted(short_data, key=lambda x: x["date"] or "", reverse=True)[:days]

    # Enrich short_data with total_volume and calculate short_ratio
    for entry in short_data:
        entry_date = entry.get("date")
        if entry_date and entry_date in daily_volumes:
            entry["total_volume"] = daily_volumes[entry_date]

            # Calculate short_ratio if we have both short_volume and total_volume
            short_vol = entry.get("short_volume")
            total_vol = entry["total_volume"]
            if short_vol is not None and total_vol and total_vol > 0:
                entry["short_ratio"] = round(short_vol / total_vol * 100, 2)

    # Calculate average short ratio
    avg_short_ratio: float | None = None
    valid_ratios = [
        d["short_ratio"] for d in short_data if d.get("short_ratio") is not None
    ]
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
            m = re.search(r"code=(\w{6})", a.get("href", ""))
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
