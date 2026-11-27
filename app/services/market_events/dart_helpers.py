"""Market-wide DART daily filing list on the official OpenDART API (hk 6408).

``fetch_dart_filings_for_date`` reads ``list.json`` (공시검색) for one receipt
day, page by page, through the same closed-error client that the capital detail
path uses (``dart_capital``). The official list is the source of rows and of the
receipt-number identity. Its ``rcept_dt`` is a date only (``YYYYMMDD``).

The receipt time (HH:MM) still comes from the same day's ``list_date_ex`` read,
used as an enrichment only: ``fetch_dart_receipt_times_for_date`` returns the
scraped times by exact ``rcept_no``, and ``join_receipt_times`` applies them to
official rows. A failed scrape or a missing match leaves the official row
date-only (fail-open).

The corp-code registry (``corpCode.xml``) is still used, but only for rows whose
list entry has no ``stock_code`` (unlisted issuers).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import tempfile
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.symbol import KR_SHORT_CODE
from app.core.timezone import today_kst
from app.services.disclosures.dart import _get_client

logger = logging.getLogger(__name__)

DART_CORP_REGISTRY_TTL_SECONDS = 7 * 24 * 60 * 60
DART_CORP_REGISTRY_CACHE_PATH = Path(
    "/tmp/auto_trader/opendartreader/dart_corp_registry.json"
)
DART_CORP_REGISTRY_LOCK_PATH = DART_CORP_REGISTRY_CACHE_PATH.with_suffix(".lock")

# Columns the receipt-time enrichment needs from ``list_date_ex``.
DART_LIST_DATE_EX_REQUIRED_COLUMNS = frozenset({"rcept_no", "rcept_dt"})
# A scraped receipt value that carries a time of day: a date, then HH:MM.
_TIMED_RECEIPT_RE = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2})[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?$"
)

# OpenDART list.json page size (max 100) and the most pages one receipt day may
# take before the list is refused as incomplete. 50 pages is 5,000 filings.
DART_LIST_PAGE_COUNT = 100
DART_LIST_MAX_PAGES = 50


def normalize_dart_corp_name(value: Any) -> str:
    """Collapse whitespace for exact DART registry company-name matching."""
    if value is None:
        return ""
    text = str(value)
    if text in {"nan", "<NA>", "NaT"}:
        return ""
    return " ".join(text.split())


def _listed_stock_code(value: Any) -> str | None:
    """Return the registry's KRX short code when it is well formed, else None.

    A listed issuer can carry an alphanumeric short code (e.g. 0256C0), so the
    shared ``KR_SHORT_CODE`` rule decides, not a digit-only check (hk 7031).
    """
    code = normalize_dart_corp_name(value)
    return code if KR_SHORT_CODE.fullmatch(code) else None


def dart_row_stock_code(row: dict[str, Any]) -> str:
    """Return the normalized explicit stock code, if the scraped row has one."""
    return (row.get("stock_code") or row.get("corp_cls_stock_code") or "").strip()


@contextmanager
def _corp_registry_lock():
    DART_CORP_REGISTRY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(DART_CORP_REGISTRY_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _registry_from_frame(frame: Any) -> dict[str, list[dict[str, str]]]:
    required = {"corp_code", "corp_name", "stock_code", "modify_date"}
    columns = getattr(frame, "columns", None)
    if columns is None or not required.issubset(columns):
        raise ValueError("DART corp registry response has an invalid schema")

    registry: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in frame.to_dict(orient="records"):
        name = normalize_dart_corp_name(row.get("corp_name"))
        if not name:
            continue
        corp_code = str(row.get("corp_code") or "").strip()
        stock_code = str(row.get("stock_code") or "").strip()
        modify_date = str(row.get("modify_date") or "").strip()
        registry[name].append(
            {
                "corp_code": corp_code,
                "stock_code": stock_code,
                "modify_date": modify_date,
            }
        )
    if not registry:
        raise ValueError("DART corp registry response has no usable rows")
    return dict(registry)


def _read_corp_registry_cache(now: float) -> dict[str, list[dict[str, str]]] | None:
    try:
        if now - DART_CORP_REGISTRY_CACHE_PATH.stat().st_mtime > (
            DART_CORP_REGISTRY_TTL_SECONDS
        ):
            return None
        value = json.loads(DART_CORP_REGISTRY_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not value:
            return None
        if any(
            not isinstance(name, str)
            or not isinstance(entries, list)
            or any(
                not isinstance(entry, dict)
                or any(
                    not isinstance(entry.get(field, ""), str)
                    for field in ("corp_code", "stock_code", "modify_date")
                )
                for entry in entries
            )
            for name, entries in value.items()
        ):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _write_corp_registry_cache(
    registry: dict[str, list[dict[str, str]]],
) -> None:
    DART_CORP_REGISTRY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".dart_corp_registry_",
        suffix=".tmp",
        dir=DART_CORP_REGISTRY_CACHE_PATH.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(registry, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, DART_CORP_REGISTRY_CACHE_PATH)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _load_dart_corp_registry_sync(
    api_key: str,
) -> dict[str, list[dict[str, str]]] | None:
    """Load the official corpCode registry without exposing request failures."""
    try:
        with _corp_registry_lock():
            cached = _read_corp_registry_cache(time.time())
            if cached is not None:
                return cached
            from app.services.disclosures.dart import _opendartreader

            fetcher = _opendartreader("OpenDartReaderCorpCodesFetcher")
            if fetcher is None:
                raise RuntimeError("DART registry client unavailable")
            registry = _registry_from_frame(fetcher(api_key))
            _write_corp_registry_cache(registry)
            return registry
    except Exception:
        # OpenDartReader exceptions can contain the API key in their URL. Never
        # log or propagate the exception or its chain from this boundary.
        logger.warning("DART corp registry unavailable")
        return None


async def fetch_dart_corp_registry() -> dict[str, list[dict[str, str]]] | None:
    """Return the cached official corpCode registry, or None on any failure."""
    api_key = settings.opendart_api_key
    if not api_key:
        return None
    return await asyncio.to_thread(_load_dart_corp_registry_sync, api_key)


def _list_page_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Return ``(rows, total_pages)`` for one validated ``list.json`` page."""
    from app.services.market_events.dart_capital import DartDetailError

    items = payload.get("list")
    if not isinstance(items, list):
        raise DartDetailError(
            "OpenDART filing list has no list field", failure_code="bad_response"
        )
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise DartDetailError(
                "OpenDART filing list row is not an object", failure_code="bad_response"
            )
        rows.append({str(key): value for key, value in item.items()})
    try:
        total_pages = int(payload.get("total_page"))
    except (TypeError, ValueError):
        raise DartDetailError(
            "OpenDART filing list has no page count", failure_code="bad_response"
        ) from None
    if total_pages < 0 or (total_pages == 0 and rows):
        raise DartDetailError(
            "OpenDART filing list page count contradicts its rows",
            failure_code="bad_response",
        )
    return rows, total_pages


async def fetch_dart_filings_for_date(
    target_date: date,
    *,
    before_request: Callable[[], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """Return every DART filing received on one day, from the official list API.

    A day with no filings (status 013) returns ``[]``; the caller decides what an
    empty day means (hk 3845 pending rule). Each paged request goes through the
    closed-error client, so failures arrive with a ``failure_code``. Rows are
    de-duplicated by receipt number: the list can shift while it is paged.

    ``before_request`` is awaited before every list-page request, so a caller can
    budget and pace the HTTP requests themselves (hk 6408 review), not one call
    per day.

    An empty page before the reported last page is a contradiction and fails
    with ``bad_response``; it is never read as the end of the list.

    The official JSON API has no on-disk page cache, so there is no cache switch
    to manage here (the scraper's ``docs_cache`` problem no longer exists).
    """
    from app.services.market_events.dart_capital import (
        DartDetailError,
        request_list_page,
    )

    day = target_date.strftime("%Y%m%d")
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        if before_request is not None:
            await before_request()
        payload = await request_list_page(
            bgn_de=day,
            end_de=day,
            page_no=str(page),
            page_count=str(DART_LIST_PAGE_COUNT),
        )
        if payload is None:
            break
        page_rows, total_pages = _list_page_rows(payload)
        if not page_rows and page < total_pages:
            raise DartDetailError(
                "OpenDART filing list ended before its reported page count",
                failure_code="bad_response",
            )
        rows.extend(page_rows)
        if page >= total_pages:
            break
        if page >= DART_LIST_MAX_PAGES:
            raise DartDetailError(
                "OpenDART filing list exceeds the page limit",
                failure_code="bad_response",
            )
        page += 1

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        receipt = str(row.get("rcept_no") or "").strip()
        if receipt and receipt in seen:
            continue
        seen.add(receipt)
        unique.append(row)
    return unique


async def fetch_dart_receipt_times_for_date(target_date: date) -> dict[str, str]:
    """Receipt time of the day's filings from the list_date_ex scrape (hk 6968).

    Returns ``{rcept_no: "YYYY-MM-DD HH:MM[:SS]"}`` for rows whose scraped receipt
    value carries a time on ``target_date``. A duplicate receipt number is dropped
    (ambiguous). Any failure returns ``{}``: this is enrichment, so the official
    rows still store date-only. The exception class name is logged; the message is
    not, because an OpenDartReader error can carry the request URL and its key.
    A day on or after today is never read from the on-disk page cache (hk 3845).
    """
    client = await _get_client()
    if client is None:
        logger.warning("DART receipt-time enrichment skipped: client unavailable")
        return {}
    iso = target_date.isoformat()
    use_cache = target_date < today_kst()

    def read_sync() -> dict[str, str]:
        frame = client.list_date_ex(iso, cache=use_cache)
        columns = getattr(frame, "columns", None)
        if columns is None or not DART_LIST_DATE_EX_REQUIRED_COLUMNS.issubset(columns):
            raise ValueError("list_date_ex response has an unexpected schema")
        # One receipt number, one usable time. Anything ambiguous is dropped.
        candidates: dict[str, list[str | None]] = {}
        for record in frame.to_dict(orient="records"):
            receipt = str(record.get("rcept_no") or "").strip()
            if not receipt:
                continue
            value = record.get("rcept_dt")
            text = (
                value.isoformat() if hasattr(value, "isoformat") else str(value or "")
            )
            match = _TIMED_RECEIPT_RE.match(text.strip())
            usable = (
                text.strip().replace("T", " ")
                if match and match.group(1) == iso
                else None
            )
            candidates.setdefault(receipt, []).append(usable)
        return {
            receipt: values[0]
            for receipt, values in candidates.items()
            if len(values) == 1 and values[0] is not None
        }

    try:
        return await asyncio.to_thread(read_sync)
    except Exception as exc:
        logger.warning(
            "DART receipt-time enrichment unavailable (%s); rows stay date-only",
            type(exc).__name__,
        )
        return {}


def join_receipt_times(
    rows: list[dict[str, Any]], times: dict[str, str]
) -> tuple[list[dict[str, Any]], int]:
    """Apply scraped receipt times to official rows, by exact receipt number.

    Only official ``rows`` are returned: a scraped receipt with no official row is
    never inserted. A time is applied only when its date equals the row's own
    official receipt date. Returns ``(rows, applied_count)``.
    """
    applied = 0
    joined: list[dict[str, Any]] = []
    for row in rows:
        receipt = str(row.get("rcept_no") or "").strip()
        official_day = str(row.get("rcept_dt") or "").strip()[:8]
        timed = times.get(receipt)
        if timed and timed[:10].replace("-", "") == official_day:
            joined.append({**row, "rcept_dt": timed})
            applied += 1
        else:
            joined.append(row)
    return joined, applied
