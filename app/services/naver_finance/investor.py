"""Naver Finance investor trends, investment opinions, and KR snapshot."""

from __future__ import annotations

import asyncio
import datetime as dt
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
from app.services.naver_finance.detail_cache_port import DetailCachePort
from app.services.naver_finance.news import _parse_news_soup
from app.services.naver_finance.parser import (
    NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM,
    _extract_current_price_from_main_soup,
    _fetch_html,
    _fetch_html_with_client,
    _fetch_json,
    _parse_naver_date,
)
from app.services.naver_finance.valuation import _parse_valuation_from_soups


def _parse_report_detail_soup(soup: BeautifulSoup) -> dict[str, Any] | None:
    info_div = soup.select_one("div.view_info_1")
    if not info_div:
        # ROB-814: the parse anchor itself is missing — a page-shape anomaly
        # (anti-bot interstitial, deleted-post notice, Naver selector rot),
        # NOT a report with a legitimately-absent target. Return None so the
        # assembly treats it like a fetch failure: shown as no-detail but
        # NEVER written to the insert-once ROB-811 cache, which would freeze
        # the anomaly permanently (no update path) even after a parser fix.
        return None

    result: dict[str, Any] = {
        "target_price": None,
        "rating": None,
    }

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
    window_months: int = 12,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    opinions: dict[str, Any] = {
        "symbol": code,
        "count": 0,
        "opinions": [],
        "consensus": None,
    }
    report_infos = _collect_opinion_report_infos(company_list_soup, limit)
    if report_infos:
        nids = [info["nid"] for info in report_infos]
        cached: dict[str, Any] = {}
        if detail_cache is not None:
            cached = await detail_cache.get_many(nids)

        miss_indexes = [i for i, nid in enumerate(nids) if nid not in cached]
        miss_results = await asyncio.gather(
            *(detail_fetcher(nids[i]) for i in miss_indexes),
            return_exceptions=True,
        )

        details: list[Any] = [cached.get(nid) for nid in nids]
        to_write: dict[str, Any] = {}
        for i, result in zip(miss_indexes, miss_results, strict=True):
            details[i] = result
            if isinstance(result, dict):
                to_write[nids[i]] = result

        if detail_cache is not None and to_write:
            await detail_cache.put_many(to_write)

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
    opinions["consensus"] = build_consensus(
        opinions["opinions"], current_price, window_months=window_months
    )
    return opinions


def _parse_holding_rate(text: str | None) -> float | None:
    """Foreign holding RATE as a percent in [0, 100] (e.g. '47.73%' → 47.73).

    ROB-448: ``parse_korean_number`` divides by 100 on a trailing '%' (→0.4773), which
    would mis-scale a holding rate. Strip the '%' and parse the bare number instead.
    """
    if not text:
        return None
    cleaned = text.replace("%", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


# Naver mobile trend JSON endpoint. frgn.naver is a client-rendered SPA since
# 2026-09; this endpoint carries the same investor-flow table as JSON rows
# (newest first).
NAVER_TREND_API = "https://m.stock.naver.com/api/stock"

# investor_flow_snapshots column <- trend JSON field mapping (task #900):
#   snapshot_date           <- bizdate ("YYYYMMDD" -> KST calendar date, ISO str)
#   foreign_net             <- foreignerPureBuyQuant   (signed comma qty, shares)
#   institution_net         <- organPureBuyQuant       (signed comma qty, shares)
#   individual_net          <- individualPureBuyQuant  (signed comma qty, shares)
#   close                   <- closePrice              (comma number, KRW)
#   change_rate             <- derived: emitted as change_pct = (close -
#                              prev_close) / prev_close (a fraction; the builder
#                              x100's it into the percent column). prev_close is
#                              the NEXT item's closePrice (payload is newest
#                              first). NULL for the oldest row in the window.
#   volume                  <- accumulatedTradingVolume (comma int, shares)
#   foreign_holding_rate    <- foreignerHoldRatio ("46.64%" -> 46.64, 0..100)
#   foreign_holding_shares  <- NULL: the payload has no share count and shares =
#                              holdRatio x listed shares is NOT derivable (no
#                              listed-share count in the payload).
#   institutional net-buy AMOUNT <- NULL: not derivable. The payload's
#                              organPureBuyQuant is a share QUANTITY, and
#                              quantity x closePrice is NOT the traded amount
#                              (net-buy amount requires per-trade execution
#                              prices). The column stores quantity only.
#   foreign_net_buy_rank / foreign_net_sell_rank / institution_net_*_rank /
#   *_consecutive_*_days / double_buy / double_sell <- derived downstream by
#                              builder._apply_streaks/_apply_ranks and
#                              repository._with_derived_flags.
_TREND_REQUIRED_FIELDS = (
    "bizdate",
    "foreignerPureBuyQuant",
    "organPureBuyQuant",
    "individualPureBuyQuant",
)


def _parse_trend_int(value: Any) -> int | None:
    """Strict parser for trend quantity fields ('+4,513,767' -> 4513767).

    Rejects '-' / '' / missing keys / decimals / non-numeric text — the caller
    treats a required-field failure as a skipped row.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "+"}:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_trend_bizdate(value: Any) -> str | None:
    """'YYYYMMDD' -> 'YYYY-MM-DD' (KST calendar date); None when malformed."""
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        parsed = dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None
    return parsed.isoformat()


def _parse_trend_row(
    item: Any,
    *,
    prev_close: int | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one trend item -> (row, None) or (None, skip-reason)."""
    if not isinstance(item, dict):
        return None, "row is not a JSON object"
    for field in _TREND_REQUIRED_FIELDS:
        if field not in item:
            return None, f"missing {field}"
    date_str = _parse_trend_bizdate(item.get("bizdate"))
    if date_str is None:
        return None, "invalid bizdate"
    foreign_net = _parse_trend_int(item.get("foreignerPureBuyQuant"))
    if foreign_net is None:
        return None, "invalid foreignerPureBuyQuant"
    institutional_net = _parse_trend_int(item.get("organPureBuyQuant"))
    if institutional_net is None:
        return None, "invalid organPureBuyQuant"
    individual_net = _parse_trend_int(item.get("individualPureBuyQuant"))
    if individual_net is None:
        return None, "invalid individualPureBuyQuant"
    close = _parse_trend_int(item.get("closePrice"))
    change = None
    change_pct = None
    if close is not None and prev_close:
        change = int(close - prev_close)
        change_pct = (close - prev_close) / prev_close
    row = {
        "date": date_str,
        "close": close,
        "change": change,
        "change_pct": change_pct,
        "volume": _parse_trend_int(item.get("accumulatedTradingVolume")),
        "institutional_net": institutional_net,
        "foreign_net": foreign_net,
        "individual_net": individual_net,
        # Not in the payload — stays NULL rather than fabricated.
        "foreign_holding_shares": None,
        "foreign_holding_rate": _parse_holding_rate(item.get("foreignerHoldRatio")),
    }
    return row, None


def _parse_trend_payload(
    payload: Any, *, days: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Convert a trend JSON list into row dicts + skip-reason counts."""
    items = payload if isinstance(payload, list) else []
    data: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for index, item in enumerate(items):
        if len(data) >= days:
            break
        # prev_close = next item's closePrice (newest-first ordering); used to
        # derive change/change_pct. Falls back to the raw next element even if
        # that element itself is malformed — the *previous trading day's* close
        # is still the correct base for this row's change.
        prev_close: int | None = None
        if index + 1 < len(items):
            prev_close = _parse_trend_int(
                items[index + 1].get("closePrice")
                if isinstance(items[index + 1], dict)
                else None
            )
        row, reason = _parse_trend_row(item, prev_close=prev_close)
        if row is None:
            key = reason or "unknown"
            skipped[key] = skipped.get(key, 0) + 1
            continue
        data.append(row)
    return data, skipped


async def fetch_investor_trends(code: str, days: int = 20) -> dict[str, Any]:
    """Fetch foreign/institutional investor trading trends.

    URL: m.stock.naver.com/api/stock/{code}/trend?pageSize={days} (JSON list,
    newest first). The old frgn.naver HTML page is a client-rendered SPA and no
    longer carries the table (task #900).

    Args:
        code: 6-digit Korean stock code
        days: Number of days of data to fetch

    Returns:
        {symbol, days, data: [...], skipped: {reason: count}} — the same row
        contract the HTML parser produced, plus `individual_net` straight from
        the payload and `skipped` so malformed rows are counted, not silent.
    """
    url = f"{NAVER_TREND_API}/{code}/trend"
    payload = await _fetch_json(url, params={"pageSize": days})
    data, skipped = _parse_trend_payload(payload, days=days)
    return {
        "symbol": code,
        "days": days,
        "data": data,
        "skipped": skipped,
    }


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


async def fetch_investment_opinions(
    code: str,
    limit: int = 10,
    *,
    window_months: int = 12,
    detail_cache: DetailCachePort | None = None,
) -> dict[str, Any]:
    """Fetch securities firm investment opinions and target prices.

    URL: finance.naver.com/research/company_list.naver
    Individual reports: finance.naver.com/research/company_read.naver?nid={nid}

    Args:
        code: 6-digit Korean stock code
        limit: Maximum number of opinions to return
        window_months: ROB-486 컨센서스 recency 윈도우(개월). 윈도우 밖 date 의
            행은 집계 제외(rows_excluded_stale). 한 건이라도 윈도우 밖이면
            목표가/upside 집계는 null 이다(ROB-1300 — 잔여 행으로 조용히
            갱신하지 않음). undated 행은 fail-open 으로 유지(rows_undated
            카운트, ROB-488)되며, opinions 리스트 자체는 윈도우 밖 행도 포함한다.

    Returns:
        Investment opinions with normalized ratings and consensus statistics:
        - symbol: Stock code
        - count: Number of opinions
        - opinions: List of individual opinions with normalized ratings
        - consensus: Windowed aggregated statistics (buy/hold/sell counts,
          target prices, upside_pct + rows_total/rows_used/rows_excluded_stale/
          rows_undated/newest_opinion_date/window_months)
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
        window_months=window_months,
        detail_cache=detail_cache,
    )


async def _fetch_kr_snapshot(
    code: str,
    *,
    news_limit: int = 5,
    opinion_limit: int = 10,
    detail_cache: DetailCachePort | None = None,
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
                detail_cache=detail_cache,
            )

        return snapshot
