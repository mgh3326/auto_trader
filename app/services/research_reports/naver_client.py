"""Read-only client for Naver's public stock research JSON API."""

from __future__ import annotations

import asyncio
import html
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

BASE_URL = "https://stock.naver.com/api/stockSecurity/researches/v2"
USER_AGENT = "auto-trader-naver-research/1.0 (+https://github.com/mgh3326/auto_trader)"
CATEGORIES = ("market", "company", "industry", "invest", "economy")
MIN_REQUEST_INTERVAL_SECONDS = 0.5
_TAG_RE = re.compile(r"<[^>]*>")


class NaverResearchError(RuntimeError):
    """A safe, bounded Naver research transport or response error."""


def strip_html(value: str) -> str:
    """Return readable plain text without retaining markup."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split())


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise NaverResearchError(f"invalid_{label}")
    return value


def _text(value: Any, label: str, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise NaverResearchError(f"invalid_{label}")
    return value


def _nid(value: Any) -> str:
    if type(value) is int:
        value = str(value)
    if type(value) is not str or not re.fullmatch(r"[0-9]+", value):
        raise NaverResearchError("invalid_nid")
    return value


def _parse_item(raw: Any) -> dict[str, Any]:
    row = _mapping(raw, "list_item")
    item = dict(row)
    item["nid"] = _nid(row.get("nid"))
    for key in (
        "title",
        "content",
        "brokerName",
        "brokerCode",
        "writeDate",
        "itemCode",
        "itemName",
        "goalPrice",
        "opinionText",
        "opinionType",
    ):
        _text(row.get(key), key)
    return item


def parse_list_response(value: Any, *, requested_index: int) -> dict[str, Any]:
    body = _mapping(value, "list_response")
    has_next = body.get("hasNext")
    items = body.get("items")
    index = body.get("index")
    size = body.get("size")
    total_count = body.get("totalCount")
    if type(has_next) is not bool or type(items) is not list:
        raise NaverResearchError("invalid_list_response")
    if str(index) != str(requested_index):
        raise NaverResearchError("list_index_mismatch")
    for value in (size, total_count):
        if type(value) is int:
            if value < 0:
                raise NaverResearchError("invalid_list_response")
        elif type(value) is str and re.fullmatch(r"[0-9]+", value):
            continue
        else:
            raise NaverResearchError("invalid_list_response")
    parsed = [_parse_item(raw) for raw in items]
    return {"hasNext": has_next, "items": parsed, "index": requested_index}


def parse_detail_response(value: Any, *, expected_nid: str) -> dict[str, Any]:
    body = _mapping(value, "detail_response")
    content = _mapping(body.get("researchContent"), "detail_response")
    if _nid(content.get("nid")) != expected_nid:
        raise NaverResearchError("detail_nid_mismatch")
    for key in ("attachUrl", "attachName", "goalPrice", "opinionText", "opinionType"):
        _text(content.get(key), key)
    return dict(content)


def parse_by_items_response(
    value: Any, *, item_codes: tuple[str, ...]
) -> dict[str, list[dict[str, Any]]]:
    body = _mapping(value, "by_items_response")
    if set(body) - set(item_codes):
        raise NaverResearchError("unexpected_item_code")
    result: dict[str, list[dict[str, Any]]] = {}
    for code, rows in body.items():
        if type(rows) is not list:
            raise NaverResearchError("invalid_by_items_rows")
        parsed = []
        for row in rows:
            parsed.append(_parse_item(row))
        result[code] = parsed
    return result


class NaverResearchClient:
    """Polite unauthenticated HTTP client; callers may inject an httpx client."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_seconds: float = 15.0,
        min_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_interval_seconds < MIN_REQUEST_INTERVAL_SECONDS:
            raise ValueError("min_interval_seconds_too_short")
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._interval = min_interval_seconds
        self._sleep = sleep
        self._clock = monotonic
        self._last_request_at: float | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        if self._last_request_at is not None:
            delay = self._interval - (self._clock() - self._last_request_at)
            if delay > 0:
                await self._sleep(delay)
        self._last_request_at = self._clock()
        try:
            response = await self._http.get(
                f"{BASE_URL}/{path}",
                params=params,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise NaverResearchError(f"transport_{type(exc).__name__}") from None
        if response.is_redirect:
            raise NaverResearchError("redirect_refused")
        if response.status_code != 200:
            raise NaverResearchError(f"http_{response.status_code}")
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise NaverResearchError("invalid_content_type")
        try:
            return response.json()
        except ValueError:
            raise NaverResearchError("invalid_json") from None

    async def list_reports(
        self, category: str, *, size: int = 20, index: int = 0
    ) -> dict[str, Any]:
        if category not in CATEGORIES:
            raise ValueError("unsupported_category")
        if (
            type(size) is not int
            or not 1 <= size <= 100
            or type(index) is not int
            or index < 0
        ):
            raise ValueError("invalid_pagination")
        body = await self._get_json(category, {"size": str(size), "index": str(index)})
        return parse_list_response(body, requested_index=index)

    async def detail(self, category: str, nid: str) -> dict[str, Any]:
        if category not in CATEGORIES or not re.fullmatch(r"[0-9]+", nid):
            raise ValueError("invalid_detail_reference")
        body = await self._get_json(f"{category}/{nid}")
        return parse_detail_response(body, expected_nid=nid)

    async def by_items(
        self, item_codes: list[str], *, size: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        codes = tuple(dict.fromkeys(item_codes))
        if not codes or any(not re.fullmatch(r"[0-9]{6}", code) for code in codes):
            raise ValueError("invalid_item_codes")
        if type(size) is not int or not 1 <= size <= 100:
            raise ValueError("invalid_size")
        params = {"itemCodes": ",".join(codes), "size": str(size)}
        body = await self._get_json("company/by-items", params)
        return parse_by_items_response(body, item_codes=codes)
