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
