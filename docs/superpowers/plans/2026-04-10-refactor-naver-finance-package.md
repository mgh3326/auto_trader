# Refactor naver_finance.py into Package Structure

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the monolithic `app/services/naver_finance.py` (1,098 lines) into a domain-organized package `app/services/naver_finance/` while preserving all external import paths.

**Architecture:** Create `app/services/naver_finance/` package with 5 domain modules (`news.py`, `company.py`, `investor.py`, `valuation.py`, `parser.py`) plus `__init__.py` that re-exports every public and private name. All consumers (`from app.services import naver_finance` + `naver_finance.fetch_*` / `naver_finance._*`) continue working unchanged.

**Tech Stack:** Python 3.13+, httpx, BeautifulSoup4, Ruff linter, pytest

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `app/services/naver_finance/__init__.py` | Re-exports all public + private names from submodules |
| Create | `app/services/naver_finance/parser.py` | Shared constants, HTTP helpers, date/HTML parsing utilities |
| Create | `app/services/naver_finance/news.py` | `fetch_news`, `_parse_news_soup` |
| Create | `app/services/naver_finance/company.py` | `fetch_company_profile`, `fetch_financials` |
| Create | `app/services/naver_finance/investor.py` | `fetch_investor_trends`, `fetch_investment_opinions`, opinion helpers, `_fetch_kr_snapshot` |
| Create | `app/services/naver_finance/valuation.py` | `fetch_valuation`, `_parse_valuation_from_soups`, sector peers, mobile API helpers |
| Delete | `app/services/naver_finance.py` | Replaced by the package |

### Import Dependency Graph (within package)

```
parser.py  (no intra-package imports)
  ↑
news.py, company.py, investor.py, valuation.py  (all import from parser)
  ↑
investor.py also imports from news.py (for _parse_news_soup in _fetch_kr_snapshot)
investor.py also imports from valuation.py (for _parse_valuation_from_soups in _fetch_kr_snapshot)
```

### Names Each Module Exports

**`parser.py`** — shared infrastructure:
- `NAVER_FINANCE_BASE`, `NAVER_FINANCE_ITEM`, `DEFAULT_HEADERS`
- `_parse_naver_date`
- `_fetch_html`, `_decode_html_content`, `_fetch_html_with_client`
- `_extract_current_price_from_main_soup`

**`news.py`**:
- `fetch_news`
- `_parse_news_soup`

**`company.py`**:
- `fetch_company_profile`
- `fetch_financials`

**`investor.py`**:
- `fetch_investor_trends`
- `fetch_investment_opinions`
- `_fetch_report_detail`, `_fetch_report_detail_with_client`
- `_fetch_current_price`
- `_parse_report_detail_soup`
- `_collect_opinion_report_infos`
- `_build_investment_opinions_from_company_list_soup`
- `_fetch_kr_snapshot`

**`valuation.py`**:
- `fetch_valuation`
- `_parse_valuation_from_soups`
- `fetch_sector_peers`
- `_parse_total_infos`
- `NAVER_MOBILE_API`
- `_fetch_integration`
- `_fetch_sector_stock_codes`
- `_fetch_sector_name`

### External Consumers (MUST NOT change)

All these files use `from app.services import naver_finance` then access attributes like `naver_finance.fetch_news(...)`, `naver_finance._fetch_html(...)`, `naver_finance._fetch_kr_snapshot(...)`, etc.

| File | Accessed attributes |
|------|-------------------|
| `tests/test_naver_finance.py` | `_parse_naver_date`, `_fetch_html`, `_fetch_html_with_client`, `fetch_news`, `fetch_company_profile`, `fetch_investor_trends`, `fetch_investment_opinions`, `_fetch_kr_snapshot`, `fetch_valuation` |
| `tests/test_mcp_fundamentals_tools.py` | (imports module, patches via monkeypatch on `naver_finance` attrs) |
| `tests/_mcp_screen_stocks_support.py` | (imports module) |
| `app/services/market_data/service.py` | `fetch_company_profile` |
| `app/mcp_server/tooling/fundamentals_sources_naver.py` | `fetch_news`, `_fetch_kr_snapshot`, `fetch_company_profile`, `fetch_financials`, `fetch_investor_trends`, `fetch_investment_opinions`, `fetch_valuation`, `fetch_sector_peers` |

---

## Task 1: Create `parser.py` — shared constants and HTTP helpers

**Files:**
- Create: `app/services/naver_finance/parser.py`

- [ ] **Step 1: Create the parser module**

```python
"""Shared constants, HTTP helpers, and parsing utilities for Naver Finance."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.number_utils import parse_korean_number as _parse_korean_number

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
```

- [ ] **Step 2: Verify the file is syntactically valid**

Run: `uv run python -c "import ast; ast.parse(open('app/services/naver_finance/parser.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/services/naver_finance/parser.py
git commit -m "refactor(naver_finance): add parser.py with shared constants and HTTP helpers"
```

---

## Task 2: Create `news.py`

**Files:**
- Create: `app/services/naver_finance/news.py`

- [ ] **Step 1: Create the news module**

```python
"""Naver Finance news fetching."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from app.services.naver_finance.parser import (
    NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM,
    _fetch_html,
    _parse_naver_date,
)


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
```

- [ ] **Step 2: Verify syntax**

Run: `uv run python -c "import ast; ast.parse(open('app/services/naver_finance/news.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/services/naver_finance/news.py
git commit -m "refactor(naver_finance): add news.py with fetch_news and _parse_news_soup"
```

---

## Task 3: Create `company.py`

**Files:**
- Create: `app/services/naver_finance/company.py`

- [ ] **Step 1: Create the company module**

```python
"""Naver Finance company profile and financial statements."""

from __future__ import annotations

from typing import Any

from app.core.number_utils import parse_korean_number as _parse_korean_number
from app.services.naver_finance.parser import (
    NAVER_FINANCE_ITEM,
    _fetch_html,
)


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
```

- [ ] **Step 2: Verify syntax**

Run: `uv run python -c "import ast; ast.parse(open('app/services/naver_finance/company.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/services/naver_finance/company.py
git commit -m "refactor(naver_finance): add company.py with fetch_company_profile and fetch_financials"
```

---

## Task 4: Create `investor.py`

**Files:**
- Create: `app/services/naver_finance/investor.py`

- [ ] **Step 1: Create the investor module**

This is the largest module. It contains investor trends, investment opinions, and `_fetch_kr_snapshot` (which orchestrates news + valuation + opinions in a single httpx session).

```python
"""Naver Finance investor trends, investment opinions, and KR snapshot."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.number_utils import parse_korean_number as _parse_korean_number
from app.services.analyst_normalizer import (
    build_consensus,
    normalize_rating_label,
    rating_to_bucket,
)
from app.services.naver_finance.news import _parse_news_soup
from app.services.naver_finance.parser import (
    DEFAULT_HEADERS,
    NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM,
    _extract_current_price_from_main_soup,
    _fetch_html,
    _fetch_html_with_client,
    _parse_naver_date,
)
from app.services.naver_finance.valuation import _parse_valuation_from_soups


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
```

- [ ] **Step 2: Verify syntax**

Run: `uv run python -c "import ast; ast.parse(open('app/services/naver_finance/investor.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/services/naver_finance/investor.py
git commit -m "refactor(naver_finance): add investor.py with trends, opinions, and snapshot"
```

---

## Task 5: Create `valuation.py`

**Files:**
- Create: `app/services/naver_finance/valuation.py`

- [ ] **Step 1: Create the valuation module**

```python
"""Naver Finance valuation metrics and sector peer analysis."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.number_utils import parse_korean_number as _parse_korean_number
from app.services.naver_finance.parser import (
    DEFAULT_HEADERS,
    NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM,
    _extract_current_price_from_main_soup,
    _fetch_html,
)

NAVER_MOBILE_API = "https://m.stock.naver.com/api/stock"


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
```

- [ ] **Step 2: Verify syntax**

Run: `uv run python -c "import ast; ast.parse(open('app/services/naver_finance/valuation.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/services/naver_finance/valuation.py
git commit -m "refactor(naver_finance): add valuation.py with valuation metrics and sector peers"
```

---

## Task 6: Create `__init__.py` and delete old file

**Files:**
- Create: `app/services/naver_finance/__init__.py`
- Delete: `app/services/naver_finance.py` (the original single file)

- [ ] **Step 1: Create the `__init__.py` re-exporting all names**

This file re-exports every public and private symbol so that existing `from app.services import naver_finance` + `naver_finance.fetch_news(...)` and `naver_finance._fetch_html(...)` patterns continue working.

```python
"""Naver Finance crawling service for Korean equities.

This package provides async functions to fetch:
- Stock news and disclosures
- Company profile information
- Financial statements
- Foreign/institutional investor trends
- Securities firm investment opinions
- Valuation metrics and sector peer analysis
"""

from app.services.naver_finance.company import (
    fetch_company_profile as fetch_company_profile,
    fetch_financials as fetch_financials,
)
from app.services.naver_finance.investor import (
    _build_investment_opinions_from_company_list_soup as _build_investment_opinions_from_company_list_soup,
    _collect_opinion_report_infos as _collect_opinion_report_infos,
    _fetch_current_price as _fetch_current_price,
    _fetch_kr_snapshot as _fetch_kr_snapshot,
    _fetch_report_detail as _fetch_report_detail,
    _fetch_report_detail_with_client as _fetch_report_detail_with_client,
    _parse_report_detail_soup as _parse_report_detail_soup,
    fetch_investment_opinions as fetch_investment_opinions,
    fetch_investor_trends as fetch_investor_trends,
)
from app.services.naver_finance.news import (
    _parse_news_soup as _parse_news_soup,
    fetch_news as fetch_news,
)
from app.services.naver_finance.parser import (
    DEFAULT_HEADERS as DEFAULT_HEADERS,
    NAVER_FINANCE_BASE as NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM as NAVER_FINANCE_ITEM,
    _decode_html_content as _decode_html_content,
    _extract_current_price_from_main_soup as _extract_current_price_from_main_soup,
    _fetch_html as _fetch_html,
    _fetch_html_with_client as _fetch_html_with_client,
    _parse_naver_date as _parse_naver_date,
)
from app.services.naver_finance.valuation import (
    NAVER_MOBILE_API as NAVER_MOBILE_API,
    _fetch_integration as _fetch_integration,
    _fetch_sector_name as _fetch_sector_name,
    _fetch_sector_stock_codes as _fetch_sector_stock_codes,
    _parse_total_infos as _parse_total_infos,
    _parse_valuation_from_soups as _parse_valuation_from_soups,
    fetch_sector_peers as fetch_sector_peers,
    fetch_valuation as fetch_valuation,
)

__all__ = [
    "DEFAULT_HEADERS",
    "NAVER_FINANCE_BASE",
    "NAVER_FINANCE_ITEM",
    "NAVER_MOBILE_API",
    "_build_investment_opinions_from_company_list_soup",
    "_collect_opinion_report_infos",
    "_decode_html_content",
    "_extract_current_price_from_main_soup",
    "_fetch_current_price",
    "_fetch_html",
    "_fetch_html_with_client",
    "_fetch_integration",
    "_fetch_kr_snapshot",
    "_fetch_report_detail",
    "_fetch_report_detail_with_client",
    "_fetch_sector_name",
    "_fetch_sector_stock_codes",
    "_parse_naver_date",
    "_parse_news_soup",
    "_parse_report_detail_soup",
    "_parse_total_infos",
    "_parse_valuation_from_soups",
    "fetch_company_profile",
    "fetch_financials",
    "fetch_investment_opinions",
    "fetch_investor_trends",
    "fetch_news",
    "fetch_sector_peers",
    "fetch_valuation",
]
```

- [ ] **Step 2: Delete the old single-file module**

Run: `rm app/services/naver_finance.py`

**IMPORTANT:** The old file `app/services/naver_finance.py` MUST be deleted. Python resolves `from app.services import naver_finance` to either a file (`naver_finance.py`) or a package (`naver_finance/`). If both exist, the package takes precedence, but the dead file will confuse developers and linters.

- [ ] **Step 3: Verify import compatibility**

Run: `uv run python -c "from app.services import naver_finance; print(dir(naver_finance))" 2>&1 | head -5`
Expected: A list of all re-exported names

Run: `uv run python -c "from app.services.naver_finance import fetch_news, fetch_valuation, _parse_naver_date; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add app/services/naver_finance/__init__.py
git rm app/services/naver_finance.py
git commit -m "refactor(naver_finance): add __init__.py re-exports and remove old single-file module"
```

---

## Task 7: Verify — lint and tests

**Files:** (no new files)

- [ ] **Step 1: Run linter**

Run: `make lint`
Expected: All checks pass (Ruff + ty)

If Ruff reports issues (e.g., unused imports in submodules, line length), fix them in the relevant file and re-run.

- [ ] **Step 2: Run naver_finance unit tests**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: All tests pass

**Key concern:** Tests use `monkeypatch.setattr(naver_finance, "_fetch_html", ...)`. This patches the attribute on the `naver_finance` **package** (i.e., `__init__.py`). Because `__init__.py` re-exports `_fetch_html` from `parser.py`, the patched name on the package does NOT affect the submodule's own reference. However, since every public function (e.g., `fetch_news`) is defined in a submodule that imports `_fetch_html` directly from `parser.py`, the monkeypatch on the package level won't propagate.

**Fix strategy if tests fail:** If `monkeypatch.setattr(naver_finance, "_fetch_html", mock)` doesn't work because the submodule has its own binding, you need to patch the submodule instead. For example:
- `monkeypatch.setattr(naver_finance.news, "_fetch_html", mock)` for `fetch_news` tests
- `monkeypatch.setattr(naver_finance.parser, "_fetch_html", mock)` for `_fetch_html` tests
- `monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock)` for investor tests

But FIRST try running the tests as-is. If they fail, update the test file's monkeypatch targets to patch the submodule where the function is actually called from. The test file imports `from app.services import naver_finance`, so you can do `monkeypatch.setattr(naver_finance.parser, "_fetch_html", mock)`.

Alternatively, you can make the submodules import `_fetch_html` via the package (from `app.services.naver_finance` import `_fetch_html`) but this creates circular imports — so patching the submodule is the cleaner fix.

**Mapping of monkeypatch targets:**

| Test class | Currently patches | Should patch (if test fails) |
|-----------|------------------|------------------------------|
| `TestFetchNews` | `naver_finance._fetch_html` | `naver_finance.news._fetch_html` |
| `TestFetchCompanyProfile` | `naver_finance._fetch_html` | `naver_finance.company._fetch_html` |
| `TestFetchInvestorTrends` | `naver_finance._fetch_html` | `naver_finance.investor._fetch_html` |
| `TestFetchInvestmentOpinions` | `naver_finance._fetch_html` | `naver_finance.investor._fetch_html` |
| `TestFetchKrSnapshot` | `naver_finance._fetch_html_with_client` | `naver_finance.investor._fetch_html_with_client` |
| `TestFetchHtml` | `httpx.AsyncClient` | (unchanged — patches httpx directly) |
| `TestFetchValuation` | `naver_finance._fetch_html` | `naver_finance.valuation._fetch_html` |

- [ ] **Step 3: Run MCP fundamentals tests**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -v`
Expected: All tests pass

- [ ] **Step 4: Run full test suite for confidence**

Run: `make test`
Expected: No regressions

- [ ] **Step 5: Commit any test fixes**

If test patches needed updating:
```bash
git add tests/test_naver_finance.py
git commit -m "test: update monkeypatch targets for naver_finance package split"
```

---

## Completion Criteria

- [x] `app/services/naver_finance.py` deleted
- [x] `app/services/naver_finance/` package with 5 modules + `__init__.py`
- [x] `__init__.py` re-exports all names for backward compatibility
- [x] `make lint` passes
- [x] `uv run pytest tests/test_naver_finance.py -v` passes
- [x] `uv run pytest tests/test_mcp_fundamentals_tools.py -v` passes
