"""Thin wrapper around OpenDartReader.list_date_ex for market-wide DART fetches.

``list_date_ex`` scrapes ``dart.fss.or.kr`` rather than using the official API,
so its response shape is validated before any result can be treated as usable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from app.services.disclosures.dart import _get_client

logger = logging.getLogger(__name__)

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


async def fetch_dart_filings_for_date(target_date: date) -> list[dict[str, Any]]:
    """Return validated DART filings for one day.

    The OpenDartReader client is loaded lazily and reused across calls.
    """
    client = await _get_client()
    if client is None:
        logger.warning("DART client unavailable; skipping fetch for %s", target_date)
        return []

    iso = target_date.isoformat()

    def fetch_sync() -> list[dict[str, Any]]:
        df = client.list_date_ex(iso)
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
