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
    match = re.match(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    # Short year format: YY.MM.DD (e.g., "26.01.30" → "2026-01-30")
    match = re.match(r"(\d{2})[./-](\d{1,2})[./-](\d{1,2})", date_str)
    if match:
        yy, month, day = match.groups()
        # Convert 2-digit year to 4-digit (00-99 → 2000-2099)
        year = 2000 + int(yy)
        return f"{year}-{int(month):02d}-{int(day):02d}"

    # Month.day only format (assumes current year): 01.15 or 01-15
    match = re.match(r"(\d{1,2})[./-](\d{1,2})$", date_str)
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
