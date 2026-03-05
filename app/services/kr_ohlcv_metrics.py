from __future__ import annotations

from collections import Counter
from typing import Final

_METRIC_FETCH_SUCCESS: Final[str] = "kr_ohlcv_fetch_success"
_METRIC_ROWS_UPSERTED: Final[str] = "kr_ohlcv_rows_upserted"
_METRIC_QUARANTINE_COUNT: Final[str] = "kr_ohlcv_quarantine_count"

_counters: Counter[tuple[str, str]] = Counter()


def record_fetch_success(route: str) -> None:
    _counters[(_METRIC_FETCH_SUCCESS, str(route).strip().upper())] += 1


def record_rows_upserted(exchange: str, rows: int) -> None:
    normalized_rows = max(int(rows), 0)
    if normalized_rows <= 0:
        return
    _counters[(_METRIC_ROWS_UPSERTED, str(exchange).strip().upper())] += normalized_rows


def record_quarantine(count: int = 1) -> None:
    normalized_count = max(int(count), 0)
    if normalized_count <= 0:
        return
    _counters[(_METRIC_QUARANTINE_COUNT, "total")] += normalized_count


def snapshot() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {
        _METRIC_FETCH_SUCCESS: {},
        _METRIC_ROWS_UPSERTED: {},
        _METRIC_QUARANTINE_COUNT: {},
    }
    for (metric, label), value in _counters.items():
        out.setdefault(metric, {})[label] = int(value)
    return out


def reset() -> None:
    _counters.clear()


__all__ = [
    "record_fetch_success",
    "record_quarantine",
    "record_rows_upserted",
    "reset",
    "snapshot",
]
