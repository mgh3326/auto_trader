"""Quick projection and get_support_resistance build the same levels.

Both used to label a level ``volume_poc``: the standalone tool from the 20-bin
volume profile, quick from a volume-weighted close. They now share
``_collect_support_resistance_price_levels``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_quick import (
    QUICK_SUPPORT_RESISTANCE_COUNT,
    _build_support_resistance,
)
from app.mcp_server.tooling.fundamentals._support_resistance import (
    get_support_resistance_impl,
)
from app.mcp_server.tooling.market_data_indicators import _calculate_volume_profile


def _frame(n: int = QUICK_SUPPORT_RESISTANCE_COUNT) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    close = np.round(70_000 + np.cumsum(rng.normal(0, 600, n)), 0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-07-01", periods=n, freq="B").date,
            "open": close,
            "high": close + rng.uniform(100, 900, n).round(0),
            "low": close - rng.uniform(100, 900, n).round(0),
            "close": close,
            "volume": rng.uniform(1e5, 1e6, n).round(0),
        }
    )


@pytest.mark.asyncio
async def test_quick_and_standalone_return_identical_levels():
    frame = _frame()
    current = float(frame["close"].iloc[-1])

    quick_sup, quick_res = _build_support_resistance(frame, current)
    tool = await get_support_resistance_impl("005930", "kr", preloaded_df=frame)

    assert tool["current_price"] == current
    assert tool["supports"] == quick_sup
    assert tool["resistances"] == quick_res


def test_quick_volume_poc_is_the_volume_profile_poc():
    frame = _frame()
    current = float(frame["close"].iloc[-1])
    poc = _calculate_volume_profile(frame, bins=20)["poc"]["price"]

    sup, res = _build_support_resistance(frame, current)
    poc_levels = [
        level for level in sup + res if "volume_poc" in (level.get("sources") or [])
    ]
    assert len(poc_levels) == 1
    # Clustering may merge the POC with a nearby level (2% tolerance).
    assert abs(poc_levels[0]["price"] - poc) / poc <= 0.02


def test_quick_without_usable_volume_drops_only_volume_levels():
    frame = _frame()
    frame["volume"] = 0.0
    current = float(frame["close"].iloc[-1])

    sup, res = _build_support_resistance(frame, current)
    sources = {src for level in sup + res for src in level.get("sources") or []}
    assert sources
    assert not any(src.startswith("volume_") for src in sources)
