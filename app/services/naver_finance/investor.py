"""Naver Finance investor trends, investment opinions, and KR snapshot."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.services.analyst_normalizer import (
    build_consensus,
    normalize_rating_label,
    rating_to_bucket,
)
from app.services.naver_finance.detail_cache_port import DetailCachePort
from app.services.naver_finance.news import NaverNewsFetchResult, fetch_stock_news
from app.services.naver_finance.parser import (
    DEFAULT_HEADERS,
    NAVER_FINANCE_ITEM,
    _extract_current_price_from_main_soup,
    _fetch_html,
    _fetch_html_with_client,
    _fetch_json,
    _parse_naver_date,
)
from app.services.naver_finance.valuation import _parse_valuation_from_soups

# Naver research list/detail JSON API (task #930). The legacy
# finance.naver.com/research/company_list.naver page now 302-redirects to the
# SPA stock.naver.com/research/company and drops the itemCode filter, so the
# HTML table parse silently produced zero opinions — the same Naver migration
# family as the #900 investor-flow and #904 news moves. Anonymous JSON
# replacements (no auth, desk-verified 200):
#   list   GET m.stock.naver.com/api/research/stock/{code}?page=1&pageSize=N
#          -> [{researchId, itemCode, itemName, title, brokerName, writeDate,
#              readCount, previewContent, category}]  (no opinion/target fields)
#   detail GET m.stock.naver.com/api/research/company/{researchId}
#          -> {researchContent: {opinion (e.g. "StrongBuy"), goalPrice,
#              prevGoalPrice, priceAtWriteDate, itemCode, brokerName,
#              writeDate, attachUrl}, researchSummaries: [...]}
# TRAP: /api/research/company/{stock_code} is interpreted as a researchId —
# /api/research/company/005930 returns research 5930 for a DIFFERENT stock.
# Detail paths are built only from researchId values the list call returned,
# and each response's researchContent.researchId/itemCode is re-verified so a
# path mixup fails loud instead of quietly serving another stock's opinion.
NAVER_RESEARCH_API = "https://m.stock.naver.com/api/research"
NAVER_RESEARCH_PAGE = "https://m.stock.naver.com/research/company"
# Detail-cache keys are namespaced so rows written under the retired
# company_read.naver nid scheme can never be served for a researchId row.
_DETAIL_CACHE_KEY_PREFIX = "api:"


class NaverResearchContractError(RuntimeError):
    """The Naver research API returned a payload outside the probed shape."""


def _detail_cache_key(research_id: int) -> str:
    return f"{_DETAIL_CACHE_KEY_PREFIX}{research_id}"


def _parse_research_int(value: Any) -> int | None:
    """Strict non-negative int coercion for research payload numbers.

    goalPrice/readCount arrive as decimal strings ("560000"); researchId is a
    JSON int. Commas/grouping are NOT accepted — a malformed number is
    upstream corruption and is rejected rather than silently re-interpreted.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    text = str(value).strip()
    if not text or not re.fullmatch(r"[0-9]+", text):
        return None
    return int(text)


def _normalize_research_list_item(
    raw: Any, code: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Map one research-list row to a report info dict, or (None, reason).

    itemCode is checked against the requested symbol: a list row carrying a
    different itemCode means the filter was lost upstream (the exact #930
    silent-zero signature) and the row must not be counted.
    """
    if not isinstance(raw, dict):
        return None, "row is not a JSON object"
    research_id = _parse_research_int(raw.get("researchId"))
    if research_id is None or research_id <= 0:
        return None, "missing or invalid researchId"
    item_code = str(raw.get("itemCode") or "").strip()
    if item_code != code:
        return None, "itemCode mismatch"
    title = str(raw.get("title") or "").strip()
    if not title:
        return None, "missing title"
    return (
        {
            "research_id": research_id,
            "stock_name": str(raw.get("itemName") or "").strip(),
            "title": title,
            "firm": str(raw.get("brokerName") or "").strip(),
            "date": _parse_naver_date(str(raw.get("writeDate") or "").strip()),
            "url": f"{NAVER_RESEARCH_PAGE}/{research_id}",
        },
        None,
    )


def _parse_research_list_payload(
    code: str, payload: Any
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validate a research-list payload -> (report rows, skip-reason counts).

    Fail-loud (task #930): a non-list body, an empty list, or a list whose
    every row is malformed raises NaverResearchContractError instead of
    returning zero opinions. An empty list is the exact signature of the
    retired endpoint and is indistinguishable from "no coverage", so it must
    never masquerade as a valid empty result on a covered symbol.
    """
    if not isinstance(payload, list):
        raise NaverResearchContractError(
            f"research list for {code}: expected a JSON list, "
            f"got {type(payload).__name__}"
        )
    if not payload:
        raise NaverResearchContractError(
            f"research list for {code} returned 0 reports — refusing to "
            "serve a silent zero (either the symbol has no analyst coverage "
            "or the API regressed; the retired company_list endpoint failed "
            "this exact way)"
        )
    items: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    seen_ids: set[int] = set()
    for raw in payload:
        row, reason = _normalize_research_list_item(raw, code)
        if row is None:
            key = reason or "invalid row"
            skipped[key] = skipped.get(key, 0) + 1
            continue
        if row["research_id"] in seen_ids:
            skipped["duplicate researchId"] = (
                skipped.get("duplicate researchId", 0) + 1
            )
            continue
        seen_ids.add(row["research_id"])
        items.append(row)
    if not items:
        raise NaverResearchContractError(
            f"research list for {code}: every row was malformed: {skipped}"
        )
    return items, skipped


def _parse_research_detail_payload(
    code: str, research_id: int, payload: Any
) -> dict[str, Any]:
    """Validate a research detail payload -> {target_price, rating}.

    ``rating`` is the raw Naver opinion label (e.g. "StrongBuy") for the
    caller to normalize; ``target_price`` is goalPrice parsed to int.
    researchContent.researchId and itemCode are re-verified so a detail
    request accidentally built from the stock code (the /company/{id} trap)
    fails instead of serving another stock's report.
    """
    if not isinstance(payload, dict):
        raise NaverResearchContractError(
            f"research detail {research_id}: expected a JSON object, "
            f"got {type(payload).__name__}"
        )
    content = payload.get("researchContent")
    if not isinstance(content, dict):
        raise NaverResearchContractError(
            f"research detail {research_id}: missing researchContent object"
        )
    content_id = _parse_research_int(content.get("researchId"))
    if content_id != research_id:
        raise NaverResearchContractError(
            f"research detail {research_id}: payload researchId is "
            f"{content.get('researchId')!r}"
        )
    item_code = str(content.get("itemCode") or "").strip()
    if item_code != code:
        raise NaverResearchContractError(
            f"research detail {research_id}: payload itemCode {item_code!r} "
            f"does not match requested symbol {code}"
        )
    opinion = content.get("opinion")
    rating = str(opinion).strip() if opinion is not None else ""
    return {
        "target_price": _parse_research_int(content.get("goalPrice")),
        "rating": rating or None,
    }


async def _fetch_research_json(
    url: str, params: dict[str, Any] | None = None
) -> Any:
    """GET a research JSON endpoint with a fresh client — mirrors _fetch_html."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        return await _fetch_research_json_with_client(client, url, params=params)


async def _fetch_research_json_with_client(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET a m.stock.naver.com JSON API; a non-JSON body is a contract error.

    The retired HTML endpoints redirect to SPA pages, so a body that is not
    decodable JSON is surfaced with status/content-type context instead of an
    unlabeled decode failure.
    """
    response = await client.get(url, params=params, headers=DEFAULT_HEADERS)
    response.raise_for_status()
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise NaverResearchContractError(
            f"non-JSON response from {url} "
            f"(status {response.status_code}, "
            f"content-type {response.headers.get('content-type')!r}); "
            "the legacy research pages redirect to an HTML SPA"
        ) from exc


async def _fetch_research_list(
    code: str, limit: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    url = f"{NAVER_RESEARCH_API}/stock/{code}"
    payload = await _fetch_research_json(url, params={"page": 1, "pageSize": limit})
    return _parse_research_list_payload(code, payload)


async def _fetch_research_list_with_client(
    client: httpx.AsyncClient, code: str, limit: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    url = f"{NAVER_RESEARCH_API}/stock/{code}"
    payload = await _fetch_research_json_with_client(
        client, url, params={"page": 1, "pageSize": limit}
    )
    return _parse_research_list_payload(code, payload)


async def _fetch_research_detail(code: str, research_id: int) -> dict[str, Any]:
    url = f"{NAVER_RESEARCH_API}/company/{research_id}"
    payload = await _fetch_research_json(url)
    return _parse_research_detail_payload(code, research_id, payload)


async def _fetch_research_detail_with_client(
    client: httpx.AsyncClient, code: str, research_id: int
) -> dict[str, Any]:
    url = f"{NAVER_RESEARCH_API}/company/{research_id}"
    payload = await _fetch_research_json_with_client(client, url)
    return _parse_research_detail_payload(code, research_id, payload)


async def _build_investment_opinions_from_research_items(
    code: str,
    items: list[dict[str, Any]],
    limit: int,
    *,
    current_price: int | None,
    detail_fetcher: Callable[[int], Awaitable[dict[str, Any]]],
    window_months: int = 12,
    detail_cache: DetailCachePort | None = None,
    skipped: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Assemble the tool payload from normalized research list rows.

    A per-report detail fetch failure does NOT fabricate a Hold vote: the row
    keeps its list metadata with rating=None and rating_bucket="unrated", so
    it is counted in total_count but in none of buy/hold/sell, and the failure
    is recorded in ``warnings``. If EVERY detail fetch fails the function
    raises NaverResearchContractError — an all-unknown consensus would cache
    and serve as a valid-looking zero-signal result (the #930 signature).
    """
    result: dict[str, Any] = {
        "symbol": code,
        "count": 0,
        "opinions": [],
        "consensus": None,
    }
    warnings: list[str] = []
    for reason, n in (skipped or {}).items():
        warnings.append(f"skipped {n} malformed research list row(s): {reason}")

    selected = items[:limit]
    if selected:
        research_ids = [item["research_id"] for item in selected]
        cache_keys = [_detail_cache_key(rid) for rid in research_ids]
        cached: dict[str, Any] = {}
        if detail_cache is not None:
            cached = await detail_cache.get_many(cache_keys)

        miss_positions = [
            i for i, key in enumerate(cache_keys) if key not in cached
        ]
        miss_results = await asyncio.gather(
            *(detail_fetcher(research_ids[i]) for i in miss_positions),
            return_exceptions=True,
        )

        details: list[Any] = [cached.get(key) for key in cache_keys]
        to_write: dict[str, Any] = {}
        for pos, fetched in zip(miss_positions, miss_results, strict=True):
            details[pos] = fetched
            if isinstance(fetched, dict):
                to_write[cache_keys[pos]] = fetched

        if detail_cache is not None and to_write:
            await detail_cache.put_many(to_write)

        detail_failures = 0
        for item, detail in zip(selected, details, strict=True):
            if isinstance(detail, dict):
                rating_label = normalize_rating_label(detail.get("rating"))
                rating_bucket: str = rating_to_bucket(rating_label)
                target_price = detail.get("target_price")
            else:
                detail_failures += 1
                reason = (
                    str(detail)[:200]
                    if isinstance(detail, BaseException)
                    else "no data"
                )
                warnings.append(
                    "research detail fetch failed for researchId "
                    f"{item['research_id']}: {reason}"
                )
                rating_label = None
                rating_bucket = "unrated"
                target_price = None
            result["opinions"].append(
                {
                    "stock_name": item["stock_name"],
                    "title": item["title"],
                    "firm": item["firm"],
                    "date": item["date"],
                    "url": item["url"],
                    "target_price": target_price,
                    "rating": rating_label,
                    "rating_bucket": rating_bucket,
                }
            )
        if detail_failures == len(selected):
            raise NaverResearchContractError(
                f"research detail fetch failed for all {detail_failures} "
                f"report(s) of {code}; refusing to serve an all-unknown "
                "consensus that would look like a valid zero-signal result"
            )

    result["count"] = len(result["opinions"])
    result["consensus"] = build_consensus(
        result["opinions"], current_price, window_months=window_months
    )
    if warnings:
        result["warnings"] = warnings
    return result


def _research_error_payload(code: str, exc: BaseException) -> dict[str, Any]:
    """Opinions section for the KR snapshot when the research fetch failed.

    The bundle keeps an explicit error block instead of a silently-absent or
    empty opinions section so downstream consumers can tell outage from
    no-coverage.
    """
    return {
        "symbol": code,
        "count": 0,
        "opinions": [],
        "consensus": None,
        "error": f"naver research fetch failed: {exc}",
    }


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
    treats a required-field failure as a skipped row. Commas must be in
    canonical thousands grouping: '45,13,767' is upstream corruption and is
    rejected rather than silently re-interpreted.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    text = str(value).strip()
    if not text or text in {"-", "+"}:
        return None
    if not re.fullmatch(r"[+-]?(\d+|\d{1,3}(,\d{3})+)", text):
        return None
    return int(text.replace(",", ""))


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
    if not isinstance(payload, list):
        # A dict/scalar body is an upstream error or maintenance shape, not a
        # legitimately empty day — count it so '0 rows' stays diagnosable.
        return [], {"payload is not a JSON list": 1}
    items = payload
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

    URLs (task #930, both anonymous JSON on m.stock.naver.com):
        list   /api/research/stock/{code}?page=1&pageSize={limit}
        detail /api/research/company/{researchId}  (per list row, by id only)
    The retired finance.naver.com/research/company_list.naver +
    company_read.naver HTML pages 302-redirect to an SPA and drop the symbol
    filter — they produced the silent-zero result this replaced.

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
          (detail-fetch failures keep their row with rating=None and
          rating_bucket="unrated" — they do not fabricate a Hold vote)
        - consensus: Windowed aggregated statistics (buy/hold/sell counts,
          target prices, upside_pct + rows_total/rows_used/rows_excluded_stale/
          rows_undated/newest_opinion_date/window_months)
        - warnings (optional): skipped list rows / partial detail failures

    Raises:
        NaverResearchContractError: empty/malformed list payload, non-JSON
            (redirect/HTML) response, or every detail fetch failing — the
            #930 silent-zero signatures must never look like a valid
            zero-opinion result.
    """
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        items, skipped = await _fetch_research_list_with_client(client, code, limit)
        current_price = await _fetch_current_price(code)
        return await _build_investment_opinions_from_research_items(
            code,
            items,
            limit,
            current_price=current_price,
            detail_fetcher=lambda research_id: _fetch_research_detail_with_client(
                client, code, research_id
            ),
            window_months=window_months,
            detail_cache=detail_cache,
            skipped=skipped,
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
        page_results = await asyncio.gather(
            _fetch_html_with_client(client, main_url, params={"code": code}),
            _fetch_html_with_client(client, sise_url, params={"code": code}),
            # The legacy news_news.naver page 410s; symbol news lives behind the
            # m.stock.naver.com JSON API now (#904).
            fetch_stock_news(code, limit=news_limit),
            # The legacy research/company_list.naver page 302s to an SPA and
            # dropped the itemCode filter (#930); the m.stock research JSON
            # API replaces it — (items, skipped) tuple or an exception.
            _fetch_research_list_with_client(client, code, opinion_limit),
            return_exceptions=True,
        )
        main_soup = (
            page_results[0] if isinstance(page_results[0], BeautifulSoup) else None
        )
        sise_soup = (
            page_results[1] if isinstance(page_results[1], BeautifulSoup) else None
        )
        news_result = (
            page_results[2]
            if isinstance(page_results[2], NaverNewsFetchResult)
            else None
        )
        research_outcome = page_results[3]

        snapshot: dict[str, Any] = {
            "valuation": None,
            "news": None,
            "opinions": None,
        }

        if main_soup is not None and sise_soup is not None:
            snapshot["valuation"] = _parse_valuation_from_soups(
                code, main_soup, sise_soup
            )

        if news_result is not None:
            snapshot["news"] = news_result.items

        # Opinions: a failed list fetch or an all-detail-failure surfaces as an
        # explicit error block in the bundle — never a silently-missing or
        # zeroed-out section (#930).
        if isinstance(research_outcome, tuple):
            research_items, research_skipped = research_outcome
            current_price = (
                _extract_current_price_from_main_soup(main_soup)
                if main_soup is not None
                else None
            )
            try:
                snapshot["opinions"] = (
                    await _build_investment_opinions_from_research_items(
                        code,
                        research_items,
                        opinion_limit,
                        current_price=current_price,
                        detail_fetcher=lambda research_id: (
                            _fetch_research_detail_with_client(
                                client, code, research_id
                            )
                        ),
                        detail_cache=detail_cache,
                        skipped=research_skipped,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — bundle isolation: the
                # opinions section degrades to an explicit error instead of
                # sinking valuation/news with it.
                snapshot["opinions"] = _research_error_payload(code, exc)
        elif isinstance(research_outcome, BaseException):
            snapshot["opinions"] = _research_error_payload(
                code, research_outcome
            )

        return snapshot
