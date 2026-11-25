"""hk 6503: KIS master write refuses a mostly-unmapped category (pure)."""

from __future__ import annotations

import pytest

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.sync import (
    MostlyUnmappedError,
    refuse_if_mostly_unmapped,
)

pytestmark = pytest.mark.unit


def _obs(i: int, state: str) -> v.FlagObservation:
    return v.FlagObservation(
        symbol=f"A{i:05d}",
        category=v.CATEGORY_TRADING_HALT,
        state=state,
        source=v.SOURCE_KIS_MASTER,
        raw_code="",
    )


def test_mostly_unmapped_category_is_refused() -> None:
    rows = [_obs(i, v.STATE_UNMAPPED) for i in range(20)]
    with pytest.raises(MostlyUnmappedError):
        refuse_if_mostly_unmapped(rows)


def test_mapped_category_passes() -> None:
    rows = [_obs(i, "none") for i in range(19)] + [_obs(99, "halted")]
    refuse_if_mostly_unmapped(rows)


def test_small_sample_is_not_judged() -> None:
    refuse_if_mostly_unmapped([_obs(i, v.STATE_UNMAPPED) for i in range(5)])
