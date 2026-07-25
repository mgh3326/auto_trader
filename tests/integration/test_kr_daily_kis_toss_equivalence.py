"""ROB-706 pre-trust gate: KIS adj=True vs Toss adjusted=True KR daily closes must
agree within tolerance for liquid symbols. Opt-in (hits real KIS + Toss); requires
KIS creds + TOSS_API_ENABLED=true. Run ONCE before trusting the fallback in prod:

    uv run pytest tests/integration/test_kr_daily_kis_toss_equivalence.py -v -m "integration and live" --run-live
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.config import settings
from app.services.brokers.kis.client import KISClient
from app.services.market_data.toss_ohlcv import fetch_daily_toss_frame

# NOTE: the `live` marker is load-bearing for safety. The `integration` marker
# alone does NOT exclude a test from the default suite — `make test` runs
# `-m "not live"`, which INCLUDES integration tests. Only the `live` marker is
# auto-skipped by the conftest hook (`tests/conftest.py:597-603`) unless
# `--run-live` is passed. Without `live` this test would fire real KIS + Toss
# HTTP whenever `make test` runs with TOSS_API_ENABLED=true.
pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.asyncio]

_SYMBOLS = ["005930", "000660", "035420"]  # Samsung Elec, SK hynix, NAVER
_N = 60
_REL_TOL = 0.005  # 0.5% — adjustment rounding, not raw/adjusted divergence


@pytest.mark.skipif(
    not settings.toss_api_enabled, reason="TOSS_API_ENABLED must be true"
)
@pytest.mark.parametrize("symbol", _SYMBOLS)
async def test_kis_and_toss_daily_closes_agree(symbol):
    kis = KISClient()
    try:
        kis_frame = await kis.inquire_daily_itemchartprice(
            code=symbol, market="J", n=_N, period="D", end_date=None
        )
    finally:
        await kis.close()
    toss_frame = await fetch_daily_toss_frame(symbol=symbol, count=_N)

    def _closes(frame: pd.DataFrame) -> dict[str, float]:
        col = "date" if "date" in frame.columns else "datetime"
        keyed = {
            str(pd.Timestamp(r[col]).date()): float(r["close"])
            for r in frame.to_dict("records")
        }
        return keyed

    kis_closes, toss_closes = _closes(kis_frame), _closes(toss_frame)
    common = sorted(set(kis_closes) & set(toss_closes))
    assert len(common) >= 20, f"too few overlapping sessions: {len(common)}"
    for day in common:
        k, t = kis_closes[day], toss_closes[day]
        assert abs(k - t) <= _REL_TOL * max(abs(k), 1.0), (
            f"{symbol} {day}: KIS adj close {k} vs Toss adj close {t} "
            f"exceed {_REL_TOL:.1%} — adjusted-series MISMATCH (do NOT enable fallback)"
        )
