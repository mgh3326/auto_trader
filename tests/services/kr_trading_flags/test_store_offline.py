"""hk 6503: store input validation (no database)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.store import _dedupe_last, upsert_flag_observations

pytestmark = pytest.mark.unit


class _NoDb:
    async def execute(self, *_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("validation must fail before any database call")


def test_dedupe_keeps_last_reading_per_symbol_and_category() -> None:
    a = v.FlagObservation(
        "005930", v.CATEGORY_TRADING_HALT, "none", v.SOURCE_KIS_MASTER, "N"
    )
    b = v.FlagObservation(
        "005930", v.CATEGORY_TRADING_HALT, "halted", v.SOURCE_NAVER_INTRADAY, "Y"
    )
    assert _dedupe_last([a, b]) == [b]


@pytest.mark.asyncio
async def test_invalid_state_for_category_is_refused_before_io() -> None:
    bad = v.FlagObservation(
        "005930", v.CATEGORY_MANAGEMENT, "halted", v.SOURCE_KIS_MASTER, "Y"
    )
    with pytest.raises(ValueError):
        await upsert_flag_observations(_NoDb(), [bad], observed_at=datetime.now(UTC))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unknown_source_is_refused_before_io() -> None:
    bad = v.FlagObservation("005930", v.CATEGORY_TRADING_HALT, "none", "scraper", "N")
    with pytest.raises(ValueError):
        await upsert_flag_observations(_NoDb(), [bad], observed_at=datetime.now(UTC))  # type: ignore[arg-type]
