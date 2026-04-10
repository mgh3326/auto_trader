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
