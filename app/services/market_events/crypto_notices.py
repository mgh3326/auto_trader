"""Manual Upbit and Bithumb announcement collection (hk 6787).

Provider payloads are normalized into ``market_events`` through
``MarketEventsRepository``. Network access is read-only and pinned to the
provider hosts; the collector itself has no scheduler registration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)

UPBIT_HOST = "api-manager.upbit.com"
BITHUMB_HOST = "api.bithumb.com"
UPBIT_URL = f"https://{UPBIT_HOST}/api/v1/announcements"
BITHUMB_NOTICES_URL = f"https://{BITHUMB_HOST}/v1/notices"
BITHUMB_MARKETS_URL = f"https://{BITHUMB_HOST}/v1/market/all"
UPBIT_SOURCE = "upbit_notice"
BITHUMB_SOURCE = "bithumb_notice"
CATEGORY = "crypto_exchange_notice"
_KST = ZoneInfo("Asia/Seoul")
_TICKER = re.compile(r"\(([A-Z0-9][A-Z0-9]{0,14})\)")
_BITHUMB_NOTICE_ID = re.compile(r"/(\d+)(?:[/?#]|$)")


class CryptoNoticeError(RuntimeError):
    """Base class for sanitized fetch and provider contract failures."""


class CryptoNoticeTransportError(CryptoNoticeError):
    pass


class CryptoNoticeHTTPError(CryptoNoticeError):
    pass


class CryptoNoticeContractError(CryptoNoticeError):
    pass


def classify_notice_kind(title: str, categories: Iterable[str]) -> str:
    """Classify only exact known phrases; an unknown notice remains ``other``."""
    text = " ".join([title, *categories])
    rules = (
        (
            "delisting",
            ("거래지원 종료", "상장폐지", "거래 종료", "delisting", "delist"),
        ),
        (
            "warning_release",
            (
                "유의종목 지정 해제",
                "유의종목 해제",
                "거래유의 종목 해제",
                "warning release",
            ),
        ),
        (
            "warning_designation",
            ("유의종목 지정", "거래유의 종목 지정", "warning designation"),
        ),
        (
            "deposit_withdrawal_suspend",
            (
                "입출금 일시 중단",
                "입출금 중단",
                "입금 및 출금 중단",
                "deposit and withdrawal suspension",
            ),
        ),
        (
            "deposit_withdrawal_resume",
            ("입출금 재개", "입금 및 출금 재개", "deposit and withdrawal resume"),
        ),
        (
            "listing",
            ("신규 거래지원", "거래지원 개시", "신규 상장", "listing", "listed"),
        ),
    )
    for kind, phrases in rules:
        if any(phrase in text for phrase in phrases):
            return kind
    return "other"


def extract_ticker(title: str, known_tickers: Iterable[str]) -> str | None:
    """Return a parenthesized uppercase ticker only when exactly listed."""
    matches = set(_TICKER.findall(title))
    if len(matches) != 1:
        return None
    ticker = next(iter(matches))
    return ticker if ticker in set(known_tickers) else None


def _parse_aware_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CryptoNoticeContractError(f"missing {field}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise CryptoNoticeContractError(f"invalid {field}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CryptoNoticeContractError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _parse_bithumb_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise CryptoNoticeContractError(f"missing {field}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise CryptoNoticeContractError(f"invalid {field}") from None
    return parsed.replace(tzinfo=_KST).astimezone(UTC)


def _categories(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise CryptoNoticeContractError("invalid categories")


def _bithumb_id(pc_url: Any) -> str:
    if not isinstance(pc_url, str):
        raise CryptoNoticeContractError("missing pc_url")
    parsed = urlparse(pc_url)
    if parsed.scheme != "https" or parsed.hostname not in {
        BITHUMB_HOST,
        "feed.bithumb.com",
        "www.bithumb.com",
    }:
        raise CryptoNoticeContractError("invalid pc_url host")
    match = _BITHUMB_NOTICE_ID.search(parsed.path)
    if match is None:
        raise CryptoNoticeContractError("missing notice number")
    return match.group(1)


def normalize_upbit_notice(
    row: dict[str, Any], *, known_tickers: Iterable[str], received_at: datetime
) -> dict[str, Any]:
    notice_id = row.get("id")
    if type(notice_id) not in (int, str) or not str(notice_id):
        raise CryptoNoticeContractError("invalid Upbit notice id")
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CryptoNoticeContractError("invalid Upbit title")
    category = row.get("category")
    categories = _categories(category)
    created = _parse_aware_time(row.get("first_listed_at"), field="first_listed_at")
    listed = _parse_aware_time(row.get("listed_at"), field="listed_at")
    kind = classify_notice_kind(title, categories)
    return {
        "category": CATEGORY,
        "market": "crypto",
        "symbol": extract_ticker(title, known_tickers),
        "title": title.strip(),
        "event_date": created.astimezone(_KST).date(),
        "release_time_utc": created,
        "release_time_local": created.astimezone(_KST).replace(tzinfo=None),
        "source_timezone": "Asia/Seoul",
        "time_hint": "unknown",
        "status": "released",
        "source": UPBIT_SOURCE,
        "source_event_id": str(notice_id),
        "source_url": f"https://upbit.com/service_center/notice?id={notice_id}",
        "provider_created_at": created,
        "provider_created_raw": row["first_listed_at"],
        "provider_updated_at": listed if listed != created else None,
        "provider_updated_raw": row.get("listed_at") if listed != created else None,
        "first_received_at": received_at,
        "fetched_at": received_at,
        "raw_payload_json": {**row, "categories": categories, "notice_kind": kind},
    }


def normalize_bithumb_notice(
    row: dict[str, Any], *, known_tickers: Iterable[str], received_at: datetime
) -> dict[str, Any]:
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CryptoNoticeContractError("invalid Bithumb title")
    categories = _categories(row.get("categories"))
    created = _parse_bithumb_time(row.get("published_at"), field="published_at")
    updated_raw = row.get("modified_at")
    updated = (
        _parse_bithumb_time(updated_raw, field="modified_at")
        if updated_raw is not None
        else None
    )
    event_id = _bithumb_id(row.get("pc_url"))
    kind = classify_notice_kind(title, categories)
    return {
        "category": CATEGORY,
        "market": "crypto",
        "symbol": extract_ticker(title, known_tickers),
        "title": title.strip(),
        "event_date": created.astimezone(_KST).date(),
        "release_time_utc": created,
        "release_time_local": created.astimezone(_KST).replace(tzinfo=None),
        "source_timezone": "Asia/Seoul",
        "time_hint": "unknown",
        "status": "released",
        "source": BITHUMB_SOURCE,
        "source_event_id": event_id,
        "source_url": row["pc_url"],
        "provider_created_at": created,
        "provider_created_raw": row["published_at"],
        "provider_updated_at": updated,
        "provider_updated_raw": updated_raw,
        "first_received_at": received_at,
        "fetched_at": received_at,
        "raw_payload_json": {**row, "notice_kind": kind},
    }


async def _request_json(
    url: str,
    *,
    expected_host: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> Any:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        raise CryptoNoticeContractError("provider URL is outside the host allowlist")
    try:
        async with client_factory(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        raise CryptoNoticeTransportError("provider request timed out") from None
    except httpx.HTTPError:
        raise CryptoNoticeTransportError("provider request failed") from None
    if 300 <= response.status_code < 400:
        raise CryptoNoticeHTTPError("provider redirect refused")
    if response.status_code < 200 or response.status_code >= 300:
        raise CryptoNoticeHTTPError(f"provider returned HTTP {response.status_code}")
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        raise CryptoNoticeContractError("provider response is not JSON")
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        raise CryptoNoticeContractError(
            "provider response contains invalid JSON"
        ) from None


class CryptoNoticeClient:
    def __init__(
        self, *, client_factory: Callable[..., Any] = httpx.AsyncClient
    ) -> None:
        self._client_factory = client_factory

    async def fetch_upbit_page(self, page: int) -> tuple[list[dict[str, Any]], int]:
        payload = await _request_json(
            UPBIT_URL,
            expected_host=UPBIT_HOST,
            params={"os": "web", "page": page, "per_page": 20, "category": "notice"},
            headers={"Referer": "https://upbit.com/"},
            client_factory=self._client_factory,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        notices = data.get("notices") if isinstance(data, dict) else None
        pages = data.get("total_pages") if isinstance(data, dict) else None
        if (
            not isinstance(notices, list)
            or any(not isinstance(row, dict) for row in notices)
            or type(pages) is not int
            or pages < 0
        ):
            raise CryptoNoticeContractError("invalid Upbit notices response")
        return notices, pages

    async def fetch_bithumb(self) -> tuple[list[dict[str, Any]], set[str]]:
        markets = await _request_json(
            BITHUMB_MARKETS_URL,
            expected_host=BITHUMB_HOST,
            client_factory=self._client_factory,
        )
        notices = await _request_json(
            BITHUMB_NOTICES_URL,
            expected_host=BITHUMB_HOST,
            client_factory=self._client_factory,
        )
        if not isinstance(markets, list) or not isinstance(notices, list):
            raise CryptoNoticeContractError("invalid Bithumb response shape")
        tickers: set[str] = set()
        for row in markets:
            if not isinstance(row, dict) or not isinstance(row.get("market"), str):
                raise CryptoNoticeContractError("invalid Bithumb market row")
            market = row["market"]
            if "-" in market:
                tickers.add(market.rsplit("-", 1)[1])
        if any(not isinstance(row, dict) for row in notices):
            raise CryptoNoticeContractError("invalid Bithumb notice row")
        return notices, tickers


async def _existing_ids(repository: MarketEventsRepository, *, source: str) -> set[str]:
    return await repository.list_source_event_ids(
        source=source, category=CATEGORY, market="crypto"
    )


async def _write_notice(
    repository: MarketEventsRepository,
    event: dict[str, Any],
    raw: dict[str, Any],
    *,
    observed_at: datetime,
) -> str:
    source = event["source"]
    source_event_id = event["source_event_id"]
    digest = hashlib.sha256(
        json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    latest = await repository.latest_revision(
        source=source,
        category=CATEGORY,
        market="crypto",
        source_event_id=source_event_id,
    )
    if latest is not None and latest.content_hash == digest:
        return "unchanged"
    if not await repository.append_revision(
        source=source,
        category=CATEGORY,
        market="crypto",
        source_event_id=source_event_id,
        content_hash=digest,
        observed_at=observed_at,
        provider_created_at=event["provider_created_at"],
        provider_updated_at=event["provider_updated_at"],
        raw_payload_json=event["raw_payload_json"],
    ):
        raise RuntimeError("notice revision was not recorded")
    await repository.upsert_event_with_values(event, [])
    return "inserted" if latest is None else "revised"


async def collect_crypto_notices(
    db: AsyncSession | None,
    *,
    pages: int = 5,
    commit: bool = False,
    client: CryptoNoticeClient | None = None,
    repo: MarketEventsRepository | None = None,
    received_at: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Fetch both feeds, stopping at known IDs and optionally persisting rows."""
    if type(pages) is not int or not 1 <= pages <= 20:
        raise ValueError("pages must be between 1 and 20")
    if commit and repo is None:
        raise ValueError("commit requires a repository")
    if repo is None and db is not None:
        repo = MarketEventsRepository(db)
    if repo is None:
        raise ValueError("a repository is required to check stored IDs")
    fetcher = client or CryptoNoticeClient()
    now = received_at or datetime.now(UTC)
    known_upbit: set[str] = set()
    if db is not None:
        known_upbit = set(
            (await db.execute(select(UpbitSymbolUniverse.base_currency))).scalars()
        )
    else:
        known_upbit = set(getattr(fetcher, "upbit_tickers", ()))

    output = {
        UPBIT_SOURCE: {
            "fetched": 0,
            "inserted": 0,
            "revised": 0,
            "stopped_at_known_id": 0,
        },
        BITHUMB_SOURCE: {
            "fetched": 0,
            "inserted": 0,
            "revised": 0,
            "stopped_at_known_id": 0,
        },
    }
    for source in (UPBIT_SOURCE, BITHUMB_SOURCE):
        try:
            existing = await _existing_ids(repo, source=source)
            if source == UPBIT_SOURCE:
                tickers = known_upbit
                total_pages = 0
                for page in range(1, pages + 1):
                    rows, total_pages = await fetcher.fetch_upbit_page(page)
                    stop = False
                    for row in rows:
                        event_id = row.get("id")
                        is_known = str(event_id) in existing
                        event = normalize_upbit_notice(
                            row, known_tickers=tickers, received_at=now
                        )
                        output[source]["fetched"] += 1
                        if commit:
                            result = await _write_notice(
                                repo, event, row, observed_at=now
                            )
                            if result != "unchanged":
                                output[source][result] += 1
                        elif not is_known:
                            output[source]["inserted"] += 1
                        if is_known:
                            output[source]["stopped_at_known_id"] = 1
                            stop = True
                            break
                    if stop or page >= total_pages or not rows:
                        break
                else:
                    if total_pages > pages:
                        raise CryptoNoticeContractError("Upbit page cap reached")
            else:
                rows, tickers = await fetcher.fetch_bithumb()
                for row in rows:
                    event_id = _bithumb_id(row.get("pc_url"))
                    if event_id in existing:
                        is_known = True
                    else:
                        is_known = False
                    event = normalize_bithumb_notice(
                        row, known_tickers=tickers, received_at=now
                    )
                    output[source]["fetched"] += 1
                    if commit:
                        result = await _write_notice(repo, event, row, observed_at=now)
                        if result != "unchanged":
                            output[source][result] += 1
                    elif not is_known:
                        output[source]["inserted"] += 1
                    if is_known:
                        output[source]["stopped_at_known_id"] = 1
                        break
            if commit and db is not None:
                await db.commit()
        except Exception:
            if commit and db is not None:
                await db.rollback()
            raise
    return output


__all__ = [
    "BITHUMB_SOURCE",
    "CATEGORY",
    "CryptoNoticeClient",
    "CryptoNoticeContractError",
    "CryptoNoticeError",
    "CryptoNoticeHTTPError",
    "CryptoNoticeTransportError",
    "UPBIT_SOURCE",
    "classify_notice_kind",
    "collect_crypto_notices",
    "extract_ticker",
    "normalize_bithumb_notice",
    "normalize_upbit_notice",
]
