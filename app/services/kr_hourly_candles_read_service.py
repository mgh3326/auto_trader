"""Backward-compatibility shim — real implementation in app.services.kr_intraday.

This module historically owned the KR intraday reader implementation.  Tests and
some downstream code still patch dependencies through this import path, so the
public reader functions below mirror patched shim attributes into the split
implementation modules before delegating.
"""

from typing import Any

import app.services.kr_intraday as _impl
from app.core.db import AsyncSessionLocal  # noqa: F401
from app.services.brokers.kis.client import KISClient  # noqa: F401
from app.services.kr_intraday import (  # noqa: F401
    _INTRADAY_FRAME_COLUMNS,
    _INTRADAY_PERIOD_CONFIGS,
    _VENUE_CONFIGS,
    _aggregate_minutes_to_hourly,
    _empty_intraday_frame,
    _fetch_historical_minutes_via_kis,
    _merge_overlay_into_intraday_frame,
    _normalize_intraday_rows,
    _store_minute_candles_background,
)
from app.services.kr_intraday import _kis_api as _kis_api_module
from app.services.kr_intraday import _repository as _repo_module
from app.services.kr_intraday._repository import _log_task_exception  # noqa: F401
from app.services.kr_intraday._types import _MinuteRow  # noqa: F401

_ORIGINAL_ASYNC_SESSION_LOCAL = AsyncSessionLocal
_ORIGINAL_KIS_CLIENT = KISClient
_ORIGINAL_STORE_MINUTE_CANDLES_BACKGROUND = _store_minute_candles_background
_ORIGINAL_FETCH_HISTORICAL_MINUTES_VIA_KIS = _fetch_historical_minutes_via_kis
_last_synced_async_session_local: object | None = None
_last_synced_kis_client: object | None = None
_last_synced_store_minute_candles_background: object | None = None
_last_synced_fetch_historical_minutes_via_kis: object | None = None


def _sync_patched_dependencies() -> None:
    """Preserve dependency patching through the legacy shim import path."""
    global _last_synced_async_session_local
    global _last_synced_kis_client
    global _last_synced_store_minute_candles_background
    global _last_synced_fetch_historical_minutes_via_kis

    if AsyncSessionLocal is not _ORIGINAL_ASYNC_SESSION_LOCAL:
        _repo_module.AsyncSessionLocal = AsyncSessionLocal
        _last_synced_async_session_local = AsyncSessionLocal
    elif _repo_module.AsyncSessionLocal is _last_synced_async_session_local:
        _repo_module.AsyncSessionLocal = _ORIGINAL_ASYNC_SESSION_LOCAL
        _last_synced_async_session_local = None

    if KISClient is not _ORIGINAL_KIS_CLIENT:
        _kis_api_module.KISClient = KISClient
        _last_synced_kis_client = KISClient
    elif _kis_api_module.KISClient is _last_synced_kis_client:
        _kis_api_module.KISClient = _ORIGINAL_KIS_CLIENT
        _last_synced_kis_client = None

    if (
        _store_minute_candles_background
        is not _ORIGINAL_STORE_MINUTE_CANDLES_BACKGROUND
    ):
        _repo_module._store_minute_candles_background = (  # noqa: SLF001
            _store_minute_candles_background
        )
        _last_synced_store_minute_candles_background = _store_minute_candles_background
    elif (
        _repo_module._store_minute_candles_background  # noqa: SLF001
        is _last_synced_store_minute_candles_background
    ):
        _repo_module._store_minute_candles_background = (  # noqa: SLF001
            _ORIGINAL_STORE_MINUTE_CANDLES_BACKGROUND
        )
        _last_synced_store_minute_candles_background = None

    if (
        _fetch_historical_minutes_via_kis
        is not _ORIGINAL_FETCH_HISTORICAL_MINUTES_VIA_KIS
    ):
        _impl._fetch_historical_minutes_via_kis = (  # noqa: SLF001
            _fetch_historical_minutes_via_kis
        )
        _last_synced_fetch_historical_minutes_via_kis = (
            _fetch_historical_minutes_via_kis
        )
    elif (
        _impl._fetch_historical_minutes_via_kis  # noqa: SLF001
        is _last_synced_fetch_historical_minutes_via_kis
    ):
        _impl._fetch_historical_minutes_via_kis = (  # noqa: SLF001
            _ORIGINAL_FETCH_HISTORICAL_MINUTES_VIA_KIS
        )
        _last_synced_fetch_historical_minutes_via_kis = None


async def read_kr_intraday_candles(**kwargs: Any):
    _sync_patched_dependencies()
    return await _impl.read_kr_intraday_candles(**kwargs)


async def read_kr_hourly_candles_1h(**kwargs: Any):
    _sync_patched_dependencies()
    return await _impl.read_kr_hourly_candles_1h(**kwargs)


def _schedule_background_minute_storage(**kwargs: Any) -> None:
    _sync_patched_dependencies()
    _repo_module._schedule_background_minute_storage(**kwargs)  # noqa: SLF001


__all__ = [
    "read_kr_hourly_candles_1h",
    "read_kr_intraday_candles",
    "_store_minute_candles_background",
    "_log_task_exception",
    "AsyncSessionLocal",
    "KISClient",
    "_MinuteRow",
    "_merge_overlay_into_intraday_frame",
    "_schedule_background_minute_storage",
    "_fetch_historical_minutes_via_kis",
]
