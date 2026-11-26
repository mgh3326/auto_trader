"""Read-only Naver disclosure/session adapters and disclosure normalization."""

from __future__ import annotations

import asyncio
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app.services.research_candles.session_segment import UNKNOWN, session_segment_of

_KST = ZoneInfo("Asia/Seoul")
DISCLOSURE_HOST = "m.stock.naver.com"
SESSIONS_HOST = "stock.naver.com"
DISCLOSURE_URL = f"https://{DISCLOSURE_HOST}/api/stock/{{symbol}}/disclosure"
SESSIONS_URL = f"https://{SESSIONS_HOST}/api/stockSecurity/exchanges/sessions"
ALLOWED_HOSTS = frozenset({DISCLOSURE_HOST, SESSIONS_HOST})
REQUEST_INTERVAL_SECONDS = 0.5


class NaverDisclosureError(RuntimeError):
    """Base error for a Naver request or response contract failure."""


class NaverDisclosureHttpError(NaverDisclosureError):
    """The Naver endpoint returned a non-success status."""


class NaverDisclosureResponseError(NaverDisclosureError):
    """The Naver endpoint returned an unsupported response shape."""


@dataclass(frozen=True)
class Disclosure:
    symbol: str
    disclosure_id: str
    title: str
    datetime_raw: str
    provider_created_at: datetime

    @property
    def event_date(self) -> date:
        return self.provider_created_at.astimezone(_KST).date()


@dataclass(frozen=True)
class SessionWindow:
    exchange: str
    market: str
    label: str
    open_at: datetime
    close_at: datetime


def parse_disclosure_datetime(value: str) -> datetime:
    """Parse Naver's zone-less KST receipt timestamp to an aware instant."""
    if not isinstance(value, str) or not value:
        raise ValueError("disclosure datetime is required")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(UTC)


def parse_disclosure(symbol: str, row: Any) -> Disclosure:
    if type(row) is not dict:
        raise NaverDisclosureResponseError("disclosure row must be an object")
    disclosure_id = row.get("disclosureId")
    title = row.get("title")
    raw_datetime = row.get("datetime")
    if not isinstance(disclosure_id, (str, int)) or not str(disclosure_id):
        raise NaverDisclosureResponseError("disclosureId is required")
    if not isinstance(title, str) or not title.strip():
        raise NaverDisclosureResponseError("title is required")
    if not isinstance(raw_datetime, str):
        raise NaverDisclosureResponseError("datetime is required")
    try:
        provider_created_at = parse_disclosure_datetime(raw_datetime)
    except ValueError as exc:
        raise NaverDisclosureResponseError("datetime is invalid") from exc
    return Disclosure(
        symbol=symbol,
        disclosure_id=str(disclosure_id),
        title=title.strip(),
        datetime_raw=raw_datetime,
        provider_created_at=provider_created_at,
    )


def normalize_title(value: str | None) -> str:
    """Normalize only Unicode compatibility and whitespace for exact title links."""
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def match_dart_receipt(disclosure: Disclosure, dart_rows: Sequence[Any]) -> str | None:
    """Return a unique DART receipt matching symbol, KST date and normalized title."""
    matches = {
        row.source_event_id
        for row in dart_rows
        if getattr(row, "source", None) == "dart"
        and getattr(row, "symbol", None) == disclosure.symbol
        and getattr(row, "event_date", None) == disclosure.event_date
        and normalize_title(getattr(row, "title", None))
        == normalize_title(disclosure.title)
        and isinstance(getattr(row, "source_event_id", None), str)
    }
    return next(iter(matches)) if len(matches) == 1 else None


async def collect_symbol_disclosures(
    *,
    symbol: str,
    fetch_page: Callable[[str, int], Awaitable[list[dict[str, Any]]]],
    insert_event: Callable[[dict[str, Any]], Awaitable[bool]],
    newest_stored_at: datetime | None,
    received_at: Callable[[], datetime],
    dart_rows: Sequence[Any] = (),
    commit: bool,
    page_size: int = 20,
    max_pages: int = 5,
) -> dict[str, int]:
    """Page until the stored-time frontier, preserving provider rows first-write-wins."""
    inserted = pages = 0
    for page_number in range(1, max_pages + 1):
        rows = await fetch_page(symbol, page_number)
        pages += 1
        received = received_at()
        parsed = [parse_disclosure(symbol, row) for row in rows]
        for disclosure, raw in zip(parsed, rows, strict=True):
            if commit:
                receipt = match_dart_receipt(disclosure, dart_rows)
                inserted += await insert_event(
                    disclosure_event(
                        disclosure,
                        first_received_at=received,
                        dart_rcept_no=receipt,
                        raw_payload=raw,
                    )
                )
        if not rows:
            break
        if newest_stored_at is not None and any(
            row.provider_created_at <= newest_stored_at for row in parsed
        ):
            break
        if len(rows) < page_size:
            break
    return {"pages": pages, "inserted": inserted}


def disclosure_event(
    disclosure: Disclosure,
    *,
    first_received_at: datetime,
    dart_rcept_no: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(raw_payload or {})
    payload["datetime"] = disclosure.datetime_raw
    if dart_rcept_no:
        payload["dart_rcept_no"] = dart_rcept_no
    return {
        "category": "disclosure",
        "market": "kr",
        "country": "KR",
        "symbol": disclosure.symbol,
        "company_name": None,
        "title": disclosure.title,
        "event_date": disclosure.event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "Asia/Seoul",
        "time_hint": "unknown",
        "importance": None,
        "status": "released",
        "source": "naver_disclosure",
        "source_event_id": disclosure.disclosure_id,
        "source_url": None,
        "fiscal_year": None,
        "fiscal_quarter": None,
        "provider_created_at": disclosure.provider_created_at,
        "provider_created_raw": disclosure.datetime_raw,
        "first_received_at": first_received_at,
        "fetched_at": first_received_at,
        "raw_payload_json": payload,
    }


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("session boundary must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("session boundary must include a timezone")
    return parsed.astimezone(UTC)


def parse_sessions(payload: Any) -> tuple[datetime, list[SessionWindow]]:
    """Parse Naver's exchange/market session envelope into bounded windows."""
    if type(payload) is not dict or not isinstance(payload.get("serverTime"), str):
        raise NaverDisclosureResponseError("sessions response missing serverTime")
    try:
        server_time = _datetime(payload["serverTime"])
    except ValueError as exc:
        raise NaverDisclosureResponseError("serverTime is invalid") from exc
    windows: list[SessionWindow] = []
    exchanges = payload.get("exchanges")
    if type(exchanges) is not list:
        raise NaverDisclosureResponseError("sessions response missing exchanges")
    try:
        for exchange_item in exchanges:
            exchange = exchange_item["exchange"]
            markets = exchange_item["markets"]
            if not isinstance(exchange, str) or type(markets) is not list:
                raise ValueError("invalid exchange entry")
            for market_item in markets:
                market = market_item["market"]
                sessions = market_item.get("sessions", [])
                if not isinstance(market, str) or type(sessions) is not list:
                    raise ValueError("invalid market entry")
                for session in sessions:
                    label = session.get("label") or session.get("name")
                    start = session.get("openAt")
                    end = session.get("closeAt")
                    if not all(
                        isinstance(item, str) and item for item in (label, start, end)
                    ):
                        continue
                    windows.append(
                        SessionWindow(
                            exchange, market, label, _datetime(start), _datetime(end)
                        )
                    )
    except (KeyError, TypeError, ValueError) as exc:
        raise NaverDisclosureResponseError("sessions entry is invalid") from exc
    return server_time, windows


class NaverPublicClient:
    """Unauthenticated Naver JSON client with allowlisted hosts and global pacing."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=False
        )
        self._owns_client = client is None
        self._last_request: float | None = None
        self._monotonic = monotonic
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_json(self, url: str, *, params: Any) -> Any:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username
            or parsed.password
        ):
            raise NaverDisclosureError("URL host is not allowed")
        if self._last_request is not None:
            delay = REQUEST_INTERVAL_SECONDS - (self._monotonic() - self._last_request)
            if delay > 0:
                await self._sleep(delay)
        self._last_request = self._monotonic()
        try:
            response = await self._client.get(
                url, params=params, follow_redirects=False
            )
        except httpx.TimeoutException as exc:
            raise NaverDisclosureHttpError("Naver request timed out") from exc
        except httpx.HTTPError as exc:
            raise NaverDisclosureHttpError("Naver request failed") from exc
        if response.is_redirect:
            raise NaverDisclosureHttpError("Naver redirects are not followed")
        if response.status_code != 200:
            raise NaverDisclosureHttpError(
                f"Naver returned HTTP {response.status_code}"
            )
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise NaverDisclosureResponseError("Naver response is not JSON")
        try:
            return response.json()
        except ValueError as exc:
            raise NaverDisclosureResponseError(
                "Naver response JSON is invalid"
            ) from exc

    async def disclosures(
        self, symbol: str, *, page: int, page_size: int = 20
    ) -> list[dict[str, Any]]:
        payload = await self._get_json(
            DISCLOSURE_URL.format(symbol=symbol),
            params={"page": page, "pageSize": page_size},
        )
        if type(payload) is not list or any(type(row) is not dict for row in payload):
            raise NaverDisclosureResponseError(
                "disclosures response must be an object array"
            )
        return payload

    async def sessions(self) -> tuple[datetime, list[SessionWindow]]:
        payload = await self._get_json(
            SESSIONS_URL,
            params=[("exchanges", "krx"), ("exchanges", "nxt")],
        )
        return parse_sessions(payload)


def _ours_windows(day: date, exchange: str, market: str) -> list[SessionWindow]:
    venue = "KRX" if exchange.lower() == "krx" else "NTX"
    labels: list[tuple[str, datetime]] = []
    start = datetime.combine(day, datetime.min.time(), tzinfo=_KST)
    for minute in range(24 * 60):
        local = start + timedelta(minutes=minute)
        segment = session_segment_of("kr", venue, local.astimezone(UTC))
        if segment != UNKNOWN:
            labels.append((segment, local))
    result: list[SessionWindow] = []
    for segment in dict.fromkeys(label for label, _ in labels):
        times = [at for label, at in labels if label == segment]
        result.append(
            SessionWindow(
                exchange, market, segment, times[0], times[-1] + timedelta(minutes=1)
            )
        )
    return result


def compare_session_windows(
    day: date, naver_windows: list[SessionWindow]
) -> list[dict[str, str]]:
    """Compare each venue/market's Naver intervals to session_segment_of output."""
    output: list[dict[str, str]] = []
    pairs = [
        (exchange, market)
        for exchange in ("krx", "nxt")
        for market in ("KOSPI", "KOSDAQ")
    ]
    for exchange, market in pairs:
        theirs = [
            w
            for w in naver_windows
            if w.exchange.lower() == exchange and w.market.upper() == market
        ]
        ours = _ours_windows(day, exchange, market)
        for window in theirs:
            exact = next(
                (
                    w
                    for w in ours
                    if w.open_at == window.open_at and w.close_at == window.close_at
                ),
                None,
            )
            output.append(
                {
                    "exchange": exchange,
                    "market": market,
                    "window": window.label,
                    "naver": f"{window.open_at.isoformat()}..{window.close_at.isoformat()}",
                    "ours": "unavailable"
                    if exact is None and not ours
                    else (
                        f"{exact.open_at.isoformat()}..{exact.close_at.isoformat()}"
                        if exact
                        else "none"
                    ),
                    "verdict": "match"
                    if exact
                    else ("naver_only" if not ours else "mismatch"),
                }
            )
        for window in ours:
            if not any(
                w.open_at == window.open_at and w.close_at == window.close_at
                for w in theirs
            ):
                output.append(
                    {
                        "exchange": exchange,
                        "market": market,
                        "window": window.label,
                        "naver": "none",
                        "ours": f"{window.open_at.isoformat()}..{window.close_at.isoformat()}",
                        "verdict": "ours_only",
                    }
                )
    return output


__all__ = [
    "ALLOWED_HOSTS",
    "Disclosure",
    "NaverDisclosureError",
    "NaverDisclosureHttpError",
    "NaverDisclosureResponseError",
    "NaverPublicClient",
    "SessionWindow",
    "collect_symbol_disclosures",
    "compare_session_windows",
    "disclosure_event",
    "match_dart_receipt",
    "normalize_title",
    "parse_disclosure",
    "parse_disclosure_datetime",
    "parse_sessions",
]
