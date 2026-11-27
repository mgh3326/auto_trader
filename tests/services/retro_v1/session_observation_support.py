"""Fake-session scenario shared by the ``session_observation`` tests (hk 6421).

Session date 2026-11-25 (an XKRX and XNYS trading day); the previous session of
both markets is 2026-11-24. All values are synthetic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from tests.services.retro_v1.support import FakeSession

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 25)
#: 11:00 KST: KR regular open, NXT pre ended, US sessions of the date not begun.
AS_OF = datetime(2026, 11, 25, 11, 0, tzinfo=KST)

KR_BARS = "FROM research.kr_candles_1m AS b"
US_BARS = "FROM research.us_candles_1m AS b"
FUT_BARS = "FROM research.kr_futures_candles_1m AS b"
CONTRACTS = "SELECT DISTINCT symbol"
MOVERS = "FROM public.invest_momentum_event_snapshots"
KR_UNIVERSE = "FROM public.kr_symbol_universe"
US_UNIVERSE = "FROM public.us_symbol_universe"
OFFICIAL = "FROM public.krx_official_daily"
KR_DAILY = "FROM public.kr_candles_1d"
US_DAILY = "FROM public.us_candles_1d"


def kst(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=KST)


def et(hour: int, minute: int, second: int = 0, *, day: int = 25) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=ET)


def agg(
    symbol: str,
    venue: str,
    segment: str,
    last_bar_at: datetime,
    *,
    lag: int = 50,
    bars: int = 30,
    trade_bars: int | None = None,
    source: str = "KIS",
) -> dict[str, Any]:
    """One aggregate row as the bar query returns it."""
    trades = bars if trade_bars is None else trade_bars
    received = last_bar_at + timedelta(minutes=1, seconds=lag)
    return {
        "symbol": symbol,
        "venue": venue,
        "session_segment": segment,
        "bar_count": bars,
        "trade_bar_count": trades,
        "last_bar_at": last_bar_at,
        "last_trade_bar_at": last_bar_at if trades else None,
        "last_received_at": received,
        "last_bar_received_at": received,
        "last_bar_source": source,
        "last_trade_source": source if trades else None,
        "sources": [source],
    }


def close_row(symbol: str, close: str, source: str) -> dict[str, Any]:
    return {"symbol": symbol, "close": Decimal(close), "source": source}


def scenario(**overrides: Any) -> FakeSession:
    """One morning: observed, no_trades, not_collected, both delays, closed."""
    session = FakeSession()

    def bound(key: str) -> Any:
        return session.bound_params[-1][key]

    def movers(_sql: str) -> list[dict[str, str]]:
        names = {"kr": ["005930", "035420"], "us": ["DELL", "NOPE"]}
        return [{"symbol": s} for s in names[bound("market")]]

    def by_symbol(rows: list[dict[str, Any]]):
        return lambda _sql: [r for r in rows if r["symbol"] in bound("symbols")]

    routes: dict[str, Any] = {
        KR_BARS: by_symbol(
            [
                agg("005930", "KRX", "KRX_REGULAR", kst(10, 58), bars=119),
                agg("005930", "NTX", "NXT_PRE", kst(8, 49), bars=50),
                agg(
                    "278470",
                    "NTX",
                    "NXT_PRE",
                    kst(8, 49),
                    bars=2,
                    trade_bars=0,
                ),
                agg("278470", "KRX", "KRX_REGULAR", kst(10, 40)),
                agg("000660", "NTX", "NXT_OVERLAP", kst(10, 44), lag=900),
                # An unlabelled minute is stored but belongs to no session.
                agg("000660", "NTX", "UNKNOWN", kst(8, 55)),
            ]
        ),
        US_BARS: [],
        FUT_BARS: by_symbol(
            [agg("MF200.2612", "KRX", "FUT_DAY", kst(10, 57), bars=132)]
        ),
        CONTRACTS: [{"symbol": "MF200.2611"}, {"symbol": "MF200.2612"}],
        MOVERS: movers,
        KR_UNIVERSE: by_symbol(
            [
                {"symbol": "005930", "fact": True},
                {"symbol": "000660", "fact": True},
                {"symbol": "278470", "fact": True},
                {"symbol": "035420", "fact": False},
            ]
        ),
        US_UNIVERSE: by_symbol(
            [
                {"symbol": "SPY", "fact": "AMEX"},
                {"symbol": "QQQ", "fact": "NASD"},
                {"symbol": "SMH", "fact": "NASD"},
                {"symbol": "DELL", "fact": "NYSE"},
            ]
        ),
        OFFICIAL: by_symbol([close_row("005930", "171300", "krx_openapi")]),
        KR_DAILY: by_symbol(
            [
                close_row("005930", "170900", "kis"),
                close_row("000660", "1053000", "toss"),
                close_row("035420", "200000", "kis"),
                close_row("035420", "201000", "toss"),
            ]
        ),
        US_DAILY: by_symbol([close_row("SPY", "601.2500", "kis")]),
    }
    routes.update(overrides)
    session.routes = routes
    return session
