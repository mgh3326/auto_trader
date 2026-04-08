"""Backward-compatibility shim — real implementation in app.services.kr_intraday."""

from app.services.kr_intraday import (  # noqa: F401
    _aggregate_minutes_to_hourly,
    _empty_intraday_frame,
    _INTRADAY_FRAME_COLUMNS,
    _INTRADAY_PERIOD_CONFIGS,
    _normalize_intraday_rows,
    _store_minute_candles_background,
    _VENUE_CONFIGS,
    read_kr_hourly_candles_1h,
    read_kr_intraday_candles,
)
from app.services.kr_intraday._repository import (  # noqa: F401
    _log_task_exception,
)

__all__ = [
    "read_kr_hourly_candles_1h",
    "read_kr_intraday_candles",
    "_store_minute_candles_background",
    "_log_task_exception",
]
