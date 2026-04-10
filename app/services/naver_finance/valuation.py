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
