"""Naver Finance valuation metrics and sector peer analysis."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.number_utils import parse_korean_number as _parse_korean_number
from app.services.naver_finance import peer_cache
from app.services.naver_finance.parser import (
    DEFAULT_HEADERS,
    NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM,
    _decode_html_content,
    _extract_current_price_from_main_soup,
    _fetch_html,
)

NAVER_MOBILE_API = "https://m.stock.naver.com/api/stock"

logger = logging.getLogger(__name__)


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


def _parse_basic_info(main_soup: BeautifulSoup) -> dict[str, Any]:
    """Extract company name and current price from the main page soup."""
    info: dict[str, Any] = {"name": None, "current_price": None}
    name_elem = main_soup.select_one("div.wrap_company h2 a")
    if name_elem:
        info["name"] = name_elem.get_text(strip=True)
    info["current_price"] = _extract_current_price_from_main_soup(main_soup)
    return info


def _parse_financial_metrics(main_soup: BeautifulSoup) -> dict[str, Any]:
    """Extract PER, PBR, ROE, and dividend yield from the main page soup."""
    metrics: dict[str, Any] = {
        "per": None,
        "pbr": None,
        "roe": None,
        "roe_controlling": None,
        "dividend_yield": None,
    }

    per_elem = main_soup.select_one("em#_per")
    if per_elem:
        per_val = _parse_korean_number(per_elem.get_text(strip=True))
        if per_val is not None and per_val != 0:
            metrics["per"] = per_val

    pbr_elem = main_soup.select_one("em#_pbr")
    if pbr_elem:
        pbr_val = _parse_korean_number(pbr_elem.get_text(strip=True))
        if pbr_val is not None and pbr_val != 0:
            metrics["pbr"] = pbr_val

    dvr_elem = main_soup.select_one("em#_dvr")
    if dvr_elem:
        dvr_val = _parse_korean_number(dvr_elem.get_text(strip=True))
        if dvr_val is not None:
            metrics["dividend_yield"] = dvr_val / 100

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
            metrics["roe"] = roe_val
        elif "지배" in th_text:
            metrics["roe_controlling"] = roe_val

    return metrics


def _parse_industry_info(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract exchange type and sector from the main page soup.

    ROB-512: 한글 업종은 동종업종비교 헤더의 upjong 링크에서 추출한다 —
    링크 href의 ``no=`` 쿼리값이 Naver 업종번호(안정 식별자)다. 과거 셀렉터
    ``div.tab_con1 em a``는 현행 페이지에서 매칭되지 않아(2026-06-11 라이브
    확인, 전 종목 None) legacy fallback으로만 유지한다.
    """
    info: dict[str, Any] = {"exchange": None, "sector": None, "sector_no": None}

    code_info = soup.select_one("div.code")
    if code_info:
        code_text = code_info.get_text(strip=True)
        if "코스피" in code_text:
            info["exchange"] = "KOSPI"
        elif "코스닥" in code_text:
            info["exchange"] = "KOSDAQ"

    sector_elem = soup.select_one('a[href*="type=upjong"]')
    if sector_elem is not None:
        info["sector"] = sector_elem.get_text(strip=True) or None
        match = re.search(r"[?&]no=(\d+)", sector_elem.get("href") or "")
        if match:
            info["sector_no"] = match.group(1)
        return info

    # legacy fallback (구 페이지 구조 / 기존 fixture 호환)
    legacy_elem = soup.select_one("div.tab_con1 em a")
    if legacy_elem is not None:
        info["sector"] = legacy_elem.get_text(strip=True) or None
    return info


def _parse_peer_comparison(
    peer_results: list[dict[str, Any] | None],
    limit: int,
) -> list[dict[str, Any]]:
    """Build a sorted peer list from raw integration fetch results."""
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
    peers.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)
    return peers[:limit]


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
    valuation = _parse_valuation_from_soups(code, main_soup, sise_soup)

    # ROB-448: overlay EPS/BPS/market_cap from the Naver mobile integration endpoint
    # (already parsed by _parse_total_infos, just not surfaced on the HTML path).
    # Units are RAW KRW: market_cap = won, eps/bps = won/share (parse_korean_number
    # expands 조/억/만). fail-open — a mobile-API hiccup leaves the 3 keys None and
    # never breaks the existing HTML-scraped valuation.
    valuation.setdefault("eps", None)
    valuation.setdefault("bps", None)
    valuation.setdefault("market_cap", None)
    try:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=10) as client:
            integ = await _fetch_integration(code, client)
        for key in ("eps", "bps", "market_cap"):
            if integ.get(key) is not None:
                valuation[key] = integ[key]
    except Exception as exc:  # noqa: BLE001 — additive overlay; degrade to None
        logger.debug(
            "fetch_valuation: integration overlay failed for %s: %s", code, exc
        )

    return valuation


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
    request_timeout: float | None = None,
) -> dict[str, Any]:
    """Fetch the Naver mobile basic + integration endpoints for a single stock.

    Args:
        request_timeout: optional per-request timeout override (ROB-688) — when
            provided, applied to both requests via ``client.get(..., timeout=...)``
            without touching the shared client-level timeout. ``None`` (default)
            keeps the client-level timeout, preserving existing callers'
            behavior (``fetch_valuation``'s overlay and the target fetch inside
            ``fetch_sector_peers``).

    Returns a dict with ``name``, ``per``, ``pbr``, ``market_cap``, ``current_price``,
    ``change_pct``, ``industry_code``, ``peers_raw``.
    """
    get_kwargs: dict[str, Any] = {}
    if request_timeout is not None:
        get_kwargs["timeout"] = request_timeout
    r_basic, r_integ = await asyncio.gather(
        client.get(f"{NAVER_MOBILE_API}/{code}/basic", **get_kwargs),
        client.get(f"{NAVER_MOBILE_API}/{code}/integration", **get_kwargs),
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
        # ROB-448: eps/bps are parsed by _parse_total_infos but were dropped here.
        "eps": metrics.get("eps"),
        "bps": metrics.get("bps"),
        "market_cap": metrics.get("market_cap"),
        "current_price": current_price,
        "change_pct": change_pct,
        "industry_code": integ.get("industryCode"),
        "peers_raw": integ.get("industryCompareInfo", []),
    }


async def _fetch_integration_cached(
    code: str,
    client: httpx.AsyncClient,
    redis_client: Any = None,
    *,
    request_timeout: float | None = None,
) -> dict[str, Any]:
    """Cache-aside over ``_fetch_integration`` (ROB-688).

    Fail-open: any cache miss or Redis outage falls through to the live fetch;
    only non-degraded results (a resolved name) are written back.
    """
    cached = await peer_cache.get_cached_integration(redis_client, code)
    if cached is not None:
        return cached
    result = await _fetch_integration(code, client, request_timeout=request_timeout)
    if result.get("name"):
        await peer_cache.set_cached_integration(redis_client, code, result)
    return result


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
    redis_client = await peer_cache._get_redis_client()
    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=10,
    ) as client:
        target = await _fetch_integration_cached(code, client, redis_client)
        sector_name: str | None = None

        # ---- Collect peer codes from integration response ----
        peer_codes: list[str] = []
        for p in target["peers_raw"]:
            pc = p.get("itemCode", "")
            if pc and pc != code:
                peer_codes.append(pc)

        integration_peer_count = len(peer_codes)

        industry_code = target.get("industry_code")
        sector_soup = None
        if industry_code:
            # Sector page is dual-purpose: sector NAME (always) + extra peers.
            # Fetch it once regardless of whether we need extras (constraint).
            sector_soup = await _fetch_sector_soup(str(industry_code), client)

        if integration_peer_count < limit:
            # Not enough peers from integration — pad with sector-scraped codes,
            # then fetch a few extras in case some fail.
            if sector_soup is not None:
                extra_codes = _parse_sector_stock_codes(sector_soup)
                seen = {code, *peer_codes}
                for ec in extra_codes:
                    if ec not in seen:
                        peer_codes.append(ec)
                        seen.add(ec)
            peer_codes = peer_codes[: limit + 5]
        else:
            # Integration already has enough peers — no over-fetch padding.
            peer_codes = peer_codes[:limit]

        # ---- Fetch integration data for each peer concurrently (bounded) ----
        semaphore = asyncio.Semaphore(max(1, settings.naver_peer_fetch_concurrency))

        async def _safe_fetch(pc: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    return await _fetch_integration_cached(
                        pc,
                        client,
                        redis_client,
                        request_timeout=settings.naver_peer_fetch_timeout_seconds,
                    )
                except Exception:
                    return None

        peer_results = await asyncio.gather(*[_safe_fetch(pc) for pc in peer_codes])

        # Resolve sector name from the sector page title
        if sector_soup is not None:
            sector_name = _parse_sector_name(sector_soup)

    peers = _parse_peer_comparison(peer_results, limit)

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


async def _fetch_sector_soup(
    sector_code: str,
    client: httpx.AsyncClient,
) -> BeautifulSoup | None:
    url = f"{NAVER_FINANCE_BASE}/sise/sise_group_detail.naver"
    try:
        r = await client.get(url, params={"type": "upjong", "no": sector_code})
        return BeautifulSoup(_decode_html_content(r.content), "lxml")
    except Exception:
        return None


def _parse_sector_stock_codes(soup: BeautifulSoup) -> list[str]:
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


def _parse_sector_name(soup: BeautifulSoup) -> str | None:
    title_elem = soup.select_one("title")
    if title_elem:
        raw = title_elem.get_text(strip=True)
        # "전기장비 : Npay 증권" -> "전기장비"
        return raw.split(":")[0].strip() if ":" in raw else raw
    return None


async def _fetch_sector_stock_codes(
    sector_code: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """Scrape stock codes from the Naver sector detail page.

    URL: ``finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={sector_code}``
    """
    soup = await _fetch_sector_soup(sector_code, client)
    return _parse_sector_stock_codes(soup) if soup is not None else []


async def _fetch_sector_name(
    sector_code: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Fetch sector name from the Naver sector detail page ``<title>`` tag.

    The title has the format ``"전기장비 : Npay 증권"``.
    """
    soup = await _fetch_sector_soup(sector_code, client)
    return _parse_sector_name(soup) if soup is not None else None
