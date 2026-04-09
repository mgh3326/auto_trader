"""Shared utilities for candles sync services (KR / US).

kr_candles_sync_service, us_candles_sync_service 가 공유하는 함수 모음.
ohlcv_cache_common.py 와 동일한 패턴으로 사용.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast


@dataclass(frozen=True, slots=True)
class SyncTableConfig:
    """Market-specific table metadata for candle sync SQL generation."""

    table_name: str  # e.g. "kr_candles_1m", "us_candles_1m"
    partition_col: str  # e.g. "venue", "exchange"


def normalize_mode(mode: str) -> Literal["incremental", "backfill"]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"incremental", "backfill"}:
        raise ValueError("mode must be 'incremental' or 'backfill'")
    return cast(Literal["incremental", "backfill"], normalized)


def parse_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None
