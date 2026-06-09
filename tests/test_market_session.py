"""ROB-464: KR market-session data_state classification."""

from __future__ import annotations

import pandas as pd

from app.mcp_server.tooling import market_session


class _StubCalendar:
    """Deterministic XKRX stand-in so tests don't depend on the real calendar."""

    tz = "Asia/Seoul"

    def __init__(self, *, trading_minute: bool, session: bool) -> None:
        self._trading_minute = trading_minute
        self._session = session

    def is_trading_minute(self, _ts) -> bool:
        return self._trading_minute

    def is_session(self, _ts) -> bool:
        return self._session


def _patch_calendar(monkeypatch, *, trading_minute: bool, session: bool) -> None:
    monkeypatch.setattr(
        market_session,
        "_get_kr_calendar",
        lambda: _StubCalendar(trading_minute=trading_minute, session=session),
    )


def test_returns_fresh_during_regular_session(monkeypatch):
    _patch_calendar(monkeypatch, trading_minute=True, session=True)
    now = pd.Timestamp("2026-06-08 10:00", tz="Asia/Seoul")
    assert market_session.kr_market_data_state(now) == market_session.DATA_STATE_FRESH


def test_returns_premarket_unavailable_before_open_on_trading_day(monkeypatch):
    _patch_calendar(monkeypatch, trading_minute=False, session=True)
    now = pd.Timestamp("2026-06-08 08:32", tz="Asia/Seoul")
    assert (
        market_session.kr_market_data_state(now)
        == market_session.DATA_STATE_PREMARKET_UNAVAILABLE
    )


def test_returns_market_closed_after_session_on_trading_day(monkeypatch):
    _patch_calendar(monkeypatch, trading_minute=False, session=True)
    now = pd.Timestamp("2026-06-08 16:00", tz="Asia/Seoul")
    assert (
        market_session.kr_market_data_state(now)
        == market_session.DATA_STATE_MARKET_CLOSED
    )


def test_returns_market_closed_on_non_session_day(monkeypatch):
    # e.g. weekend / holiday — not a trading session at all.
    _patch_calendar(monkeypatch, trading_minute=False, session=False)
    now = pd.Timestamp("2026-06-07 08:32", tz="Asia/Seoul")
    assert (
        market_session.kr_market_data_state(now)
        == market_session.DATA_STATE_MARKET_CLOSED
    )
