"""ROB-365 hotfix — US index current-quote hardening.

``_fetch_index_us_current`` reads ``yfinance`` ``fast_info`` attributes via
``getattr(info, name, None)``. That only shields against ``AttributeError`` —
but yfinance's ``FastInfo`` computes values lazily and can raise
``TypeError: 'NoneType' object is not subscriptable`` (subscripting a missing
price frame) when a ticker has no fetchable fast data. That exception propagates
out of ``getattr`` and crashes the current-quote path, while the history path
(``yf.download`` → empty DataFrame) keeps working.

These tests pin the hardened contract: the current path must never raise, fall
back to the latest history row when fast_info is unusable, and fail closed to a
degraded result when even history is unavailable.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from app.mcp_server.tooling import fundamentals_sources_indices as idx

pytestmark = pytest.mark.asyncio


class _BrokenFastInfo:
    """fast_info whose attribute access raises like yfinance does when the
    underlying price frame is ``None`` — a ``TypeError``, not ``AttributeError``."""

    def __getattr__(self, name: str):
        raise TypeError("'NoneType' object is not subscriptable")


def _patch_ticker(monkeypatch, fast_info_value, *, raise_on_acquire=False):
    class _Ticker:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @property
        def fast_info(self):
            if raise_on_acquire:
                raise TypeError("'NoneType' object is not subscriptable")
            return fast_info_value

    monkeypatch.setattr("yfinance.Ticker", lambda symbol, session=None: _Ticker())


def _patch_download(monkeypatch, df: pd.DataFrame):
    monkeypatch.setattr("yfinance.download", lambda *args, **kwargs: df)


def _history_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    dates = pd.date_range("2026-05-26", periods=n, freq="D")
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": [c - 5 for c in closes],
            "High": [c + 5 for c in closes],
            "Low": [c - 8 for c in closes],
            "Close": closes,
            "Volume": [1_000_000 + i for i in range(n)],
        }
    ).set_index("Date")


async def test_fast_info_typeerror_falls_back_to_history(monkeypatch):
    _patch_ticker(monkeypatch, _BrokenFastInfo())
    _patch_download(monkeypatch, _history_df([5490.0, 5500.0]))

    res = await idx._fetch_index_us_current("^GSPC", "S&P 500", "SPX")

    assert res["symbol"] == "SPX"
    assert res["current"] == pytest.approx(5500.0)  # latest history close
    assert res["change"] == pytest.approx(10.0)  # vs prior row close 5490
    assert res["change_pct"] == pytest.approx(0.18, abs=0.01)  # 10/5490*100
    assert res["source"] == "yfinance_history_fallback"
    assert "unavailable" not in res


async def test_fast_info_typeerror_no_history_returns_degraded(monkeypatch):
    _patch_ticker(monkeypatch, _BrokenFastInfo())
    _patch_download(monkeypatch, pd.DataFrame())  # empty -> history []

    res = await idx._fetch_index_us_current("^VIX", "CBOE Volatility Index", "VIX")

    assert res["symbol"] == "VIX"
    assert res["current"] is None
    assert res["change"] is None
    assert res["change_pct"] is None
    assert res["unavailable"] is True
    assert "degraded_reason" in res


async def test_fast_info_acquisition_raising_is_handled(monkeypatch):
    # Even ``ticker.fast_info`` property access raising must not crash.
    _patch_ticker(monkeypatch, None, raise_on_acquire=True)
    _patch_download(monkeypatch, _history_df([100.0, 101.0, 102.0]))

    res = await idx._fetch_index_us_current("^DJI", "Dow Jones", "DJI")

    assert res["current"] == pytest.approx(102.0)
    assert res["source"] == "yfinance_history_fallback"
    assert "unavailable" not in res


async def test_normal_fast_info_preserved_no_history_fetch(monkeypatch):
    # Regression: a healthy fast_info path is unchanged and must NOT trigger the
    # history fallback (source stays 'yfinance').
    @dataclasses.dataclass(frozen=True)
    class _FastInfo:
        last_price: float = 5500.0
        regular_market_previous_close: float = 5450.0
        open: float = 5460.0
        day_high: float = 5510.0
        day_low: float = 5430.0
        last_volume: int = 3_000_000

    _patch_ticker(monkeypatch, _FastInfo())

    def _boom(*args, **kwargs):
        raise AssertionError("history fallback must not run when fast_info works")

    monkeypatch.setattr("yfinance.download", _boom)

    res = await idx._fetch_index_us_current("^GSPC", "S&P 500", "SPX")

    assert res["current"] == pytest.approx(5500.0)
    assert res["change"] == pytest.approx(50.0)
    assert res["source"] == "yfinance"
    assert "unavailable" not in res


async def test_single_missing_attr_does_not_block_other_fields(monkeypatch):
    # fast_info present but missing one attribute (AttributeError path) must still
    # work via getattr default — and a present last_price means no fallback.
    @dataclasses.dataclass(frozen=True)
    class _PartialFastInfo:
        last_price: float = 18.5
        regular_market_previous_close: float = 17.2

    _patch_ticker(monkeypatch, _PartialFastInfo())

    def _boom(*args, **kwargs):
        raise AssertionError("history fallback must not run when last_price exists")

    monkeypatch.setattr("yfinance.download", _boom)

    res = await idx._fetch_index_us_current("^VIX", "CBOE Volatility Index", "VIX")

    assert res["current"] == pytest.approx(18.5)
    assert res["source"] == "yfinance"
    assert res["open"] is None  # missing attr -> None, not a crash
