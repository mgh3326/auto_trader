"""Thin wrapper around OpenDartReader.list_date for per-day market-wide DART fetch (ROB-128).

This is the only DART-side new code; for per-symbol filings the existing
`app/services/disclosures/dart.py::list_filings` is reused elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from app.services.disclosures.dart import _get_client

logger = logging.getLogger(__name__)


async def fetch_dart_filings_for_date(target_date: date) -> list[dict[str, Any]]:
    """Return DART filings for one day. Empty list if DART is unavailable.

    The OpenDartReader client is loaded lazily and reused across calls.
    """
    client = await _get_client()
    if client is None:
        logger.warning("DART client unavailable; skipping fetch for %s", target_date)
        return []

    iso = target_date.isoformat()

    def fetch_sync() -> list[dict[str, Any]]:
        df = client.list_date(iso)
        if df is None or df.empty:
            return []
        return df.to_dict(orient="records")

    return await asyncio.to_thread(fetch_sync)
