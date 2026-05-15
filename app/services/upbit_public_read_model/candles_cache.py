"""Thin Upbit public candles wrapper over the existing closed-candle cache."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import pandas as pd

from app.services.upbit_public_read_model.cache_common import classify_error
from app.services.upbit_public_read_model.types import (
    UpbitBlockMeta,
    UpbitCandlesBlock,
    _now_utc,
)

Period = Literal["day", "week", "month"]


class CandlesCache:
    def __init__(
        self,
        *,
        closed_candles_getter: Callable[..., Awaitable[pd.DataFrame | None]]
        | None = None,
    ) -> None:
        self._closed_candles_getter = closed_candles_getter

    async def get(
        self, market: str, *, period: Period, count: int = 30
    ) -> UpbitCandlesBlock:
        if period not in {"day", "week", "month"}:
            raise ValueError("period must be one of: day, week, month")
        normalized_market = str(market or "").strip().upper()
        now = _now_utc()
        try:
            getter = self._closed_candles_getter
            if getter is None:
                from app.services.upbit_ohlcv_cache import get_closed_candles

                getter = get_closed_candles
            frame = await getter(normalized_market, count=count, period=period)
            rows = _frame_to_rows(frame)
            state = "fresh" if rows else "missing"
            return UpbitCandlesBlock(
                meta=UpbitBlockMeta(
                    source="upbit_candles",
                    state=state,
                    label="Upbit candles",
                    fetchedAt=now,
                ),
                market=normalized_market,
                period=period,
                rows=rows,
            )
        except Exception as exc:  # noqa: BLE001
            return UpbitCandlesBlock(
                meta=UpbitBlockMeta(
                    source="upbit_candles",
                    state="unavailable",
                    label="Upbit candles",
                    errorReason=classify_error(exc),
                ),
                market=normalized_market,
                period=period,
                rows=[],
            )


def _frame_to_rows(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in frame.reset_index().iterrows():
        item: dict[str, Any] = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                item[str(key)] = value.isoformat()
            else:
                item[str(key)] = value.item() if hasattr(value, "item") else value
        rows.append(item)
    return rows
