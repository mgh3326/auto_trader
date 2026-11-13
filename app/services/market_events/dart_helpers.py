"""Thin wrapper around OpenDartReader.list_date_ex for market-wide DART fetches.

``list_date_ex`` scrapes ``dart.fss.or.kr`` rather than using the official API,
so its response shape is validated before any result can be treated as usable.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.timezone import today_kst
from app.services.disclosures.dart import _get_client

logger = logging.getLogger(__name__)

DART_CORP_REGISTRY_TTL_SECONDS = 7 * 24 * 60 * 60
DART_CORP_REGISTRY_CACHE_PATH = Path(
    "/tmp/auto_trader/opendartreader/dart_corp_registry.json"
)
DART_CORP_REGISTRY_LOCK_PATH = DART_CORP_REGISTRY_CACHE_PATH.with_suffix(".lock")

DART_LIST_DATE_EX_REQUIRED_COLUMNS = frozenset(
    {
        "rcept_dt",
        "corp_cls",
        "corp_name",
        "rcept_no",
        "report_nm",
        "flr_nm",
        "rm",
    }
)


class DartResponseSchemaError(RuntimeError):
    """The DART scraping response no longer matches the expected table shape."""


def normalize_dart_corp_name(value: Any) -> str:
    """Collapse whitespace for exact DART registry company-name matching."""
    if value is None:
        return ""
    text = str(value)
    if text in {"nan", "<NA>", "NaT"}:
        return ""
    return " ".join(text.split())


def _listed_stock_code(value: Any) -> str | None:
    code = normalize_dart_corp_name(value)
    return code if len(code) == 6 and code.isascii() and code.isdigit() else None


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
    return dict(registry)


def _read_corp_registry_cache(now: float) -> dict[str, list[dict[str, str]]] | None:
    try:
        if now - DART_CORP_REGISTRY_CACHE_PATH.stat().st_mtime > (
            DART_CORP_REGISTRY_TTL_SECONDS
        ):
            return None
        value = json.loads(DART_CORP_REGISTRY_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
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


async def fetch_dart_filings_for_date(
    target_date: date,
    *,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return validated DART filings for one day.

    The OpenDartReader client is loaded lazily and reused across calls.

    ``use_cache=True`` (default) preserves OpenDartReader's on-disk
    ``docs_cache`` behavior — the scheduled daily path relies on it.
    Backfill-style callers pass ``use_cache=False`` so every attempt is a
    real network call: a cached error page would otherwise be re-served to
    every retry of the same date.

    A date on or after today (Asia/Seoul) never uses the cache, whatever
    ``use_cache`` says. OpenDartReader writes each scraped page to
    ``docs_cache/<url>`` once and serves it forever, so an early "no filings
    found" page for a day DART has not finished publishing would hide the later
    filings from every subsequent run.
    """
    use_cache = use_cache and target_date < today_kst()
    client = await _get_client()
    if client is None:
        logger.warning("DART client unavailable; skipping fetch for %s", target_date)
        return []

    iso = target_date.isoformat()

    def fetch_sync() -> list[dict[str, Any]]:
        df = client.list_date_ex(iso, cache=use_cache)
        columns = getattr(df, "columns", None)
        if columns is None:
            raise DartResponseSchemaError(
                "DART list_date_ex returned a non-DataFrame response"
            )
        missing = DART_LIST_DATE_EX_REQUIRED_COLUMNS.difference(columns)
        if missing:
            raise DartResponseSchemaError(
                "DART list_date_ex response missing required columns: "
                + ", ".join(sorted(missing))
            )
        if df.empty:
            return []
        records = df.to_dict(orient="records")
        for record in records:
            receipt_at = record.get("rcept_dt")
            isoformat = getattr(receipt_at, "isoformat", None)
            if callable(isoformat):
                record["rcept_dt"] = isoformat()
        return records

    return await asyncio.to_thread(fetch_sync)
