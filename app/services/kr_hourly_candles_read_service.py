"""Backward-compatibility shim — real implementation in app.services.kr_intraday."""

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
    _schedule_background_minute_storage,
    _store_minute_candles_background,
    read_kr_hourly_candles_1h,
    read_kr_intraday_candles,
)
from app.services.kr_intraday._repository import _log_task_exception  # noqa: F401
from app.services.kr_intraday._types import _MinuteRow  # noqa: F401

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
