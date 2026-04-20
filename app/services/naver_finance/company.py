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
    income_metrics = ["매출액", "영업이익", "당기순이익", "영업이익률", "순이익률"]
    balance_metrics = ["자산총계", "부채총계", "자본총계", "부채비율"]
    cashflow_metrics = ["영업활동", "투자활동", "재무활동"]

    for row in rows:
        cells = row.select("td, th")
        if len(cells) < 2:
            continue

        metric_name = cells[0].get_text(strip=True)
        if not metric_name:
            continue

        # Filter metrics based on statement type
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
