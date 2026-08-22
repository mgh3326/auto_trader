"""Ranked candidate lists, one function per screener source.

Every builder receives ONLY the decision-date slice of a frozen snapshot
table (or, for the reconstructed sources, bars ending at the decision date).
No builder can see a future row: the caller slices by ``snapshot_date == day``
before dispatch, and ``tests/research`` asserts it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research.screener_bakeoff import indicators as ind
from research.screener_bakeoff.spec import (
    CRYPTO_MIN_TRADE_AMOUNT_KRW,
    GATE_POOL_DEPTH,
    KR_MIN_TURNOVER_KRW,
    RANDOM_SEED,
    SR_WINDOW_BARS,
    US_MIN_TURNOVER_USD,
)


@dataclass
class MarketContext:
    market: str
    #: snapshot_date -> frozen invest_screener_snapshots slice
    screener: dict[dt.date, pd.DataFrame] = field(default_factory=dict)
    #: snapshot_date -> tvscreener KR fundamentals / US valuation slice
    fundamentals: dict[dt.date, pd.DataFrame] = field(default_factory=dict)
    #: snapshot_date -> investor flow slice (KR only)
    flow: dict[dt.date, pd.DataFrame] = field(default_factory=dict)
    #: snapshot_date -> crypto snapshot slice
    crypto: dict[dt.date, pd.DataFrame] = field(default_factory=dict)
    prices: object | None = None  # PricePanel
    #: market session calendar used for the 52w-high recency rule
    calendar: np.ndarray | None = None


def _head(df: pd.DataFrame, n: int = GATE_POOL_DEPTH) -> list[str]:
    return df["symbol"].head(n).tolist()


def _liquid(ctx: MarketContext, day: dt.date) -> pd.DataFrame:
    """Liquidity-filtered universe used by the control and the benchmark."""
    if ctx.market == "crypto":
        df = ctx.crypto.get(day)
        if df is None or df.empty:
            return pd.DataFrame(columns=["symbol"])
        return df[df["trade_amount_24h"].fillna(0) >= CRYPTO_MIN_TRADE_AMOUNT_KRW]
    df = ctx.screener.get(day)
    if df is None or df.empty:
        return pd.DataFrame(columns=["symbol"])
    floor = KR_MIN_TURNOVER_KRW if ctx.market == "kr" else US_MIN_TURNOVER_USD
    # SPEC AMENDMENT (recorded in README §0, made before any result was read):
    # invest_screener_snapshots.daily_turnover is NULL for every KR/US partition
    # before 2026-07-21, which would have voided the liquidity universe on two
    # thirds of the grid.  The turnover proxy is the product of two frozen
    # decision-date columns, so it introduces no look-ahead.
    turnover = df["daily_volume"].fillna(0) * df["latest_close"].fillna(0)
    return df[turnover >= floor]


# ---------------------------------------------------------------------------
# invest_screener_snapshots-backed sources (KR + US, identical columns)
# ---------------------------------------------------------------------------


def src_consecutive_gainers(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.screener.get(day)
    if df is None or df.empty:
        return []
    m = df[
        (df["consecutive_up_days"].fillna(0) >= 5)
        & (df["week_change_rate"].fillna(-1) >= 0)
    ]
    return _head(m.sort_values("week_change_rate", ascending=False))


def src_high_volume_surge(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.screener.get(day)
    if df is None or df.empty:
        return []
    return _head(df.sort_values("daily_volume", ascending=False))


def src_top_gainers(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _liquid(ctx, day)
    if df.empty:
        return []
    return _head(df.sort_values("change_rate", ascending=False))


def src_top_losers(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _liquid(ctx, day)
    if df.empty:
        return []
    return _head(df.sort_values("change_rate", ascending=True))


def src_trade_amount(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.screener.get(day)
    if df is None or df.empty:
        return []
    # same frozen-column turnover proxy as _liquid (README §0 amendment A1)
    m = df.assign(_turnover=df["daily_volume"].fillna(0) * df["latest_close"].fillna(0))
    return _head(m.sort_values("_turnover", ascending=False))


# ---------------------------------------------------------------------------
# investor_flow_snapshots-backed sources (KR only)
# ---------------------------------------------------------------------------


def src_investor_flow_momentum(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.flow.get(day)
    if df is None or df.empty:
        return []
    m = df[df["foreign_consecutive_buy_days"].fillna(0) >= 3]
    return _head(m.sort_values("foreign_net", ascending=False))


def src_double_buy(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.flow.get(day)
    if df is None or df.empty:
        return []
    m = df[(df["double_buy"] == True) & (df["change_rate"].fillna(-1) >= 0)]  # noqa: E712
    return _head(m.sort_values("change_rate", ascending=False))


# ---------------------------------------------------------------------------
# tvscreener KR fundamentals-backed sources
# ---------------------------------------------------------------------------


def _kr_fund(ctx: MarketContext, day: dt.date) -> pd.DataFrame:
    df = ctx.fundamentals.get(day)
    return pd.DataFrame() if df is None else df


def src_kr_oversold(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[df["rsi14"].notna() & (df["rsi14"] <= 30)]
    return _head(m.sort_values("rsi14", ascending=True))


def src_kr_cheap_value(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[
        (df["per"] > 0)
        & (df["per"] <= 15)
        & (df["pbr"] > 0)
        & (df["pbr"] <= 1.5)
        & (df["eps_yoy"].fillna(-1) >= 0)
    ]
    return _head(m.sort_values("pbr", ascending=True))


def src_kr_high_yield_value(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[(df["roe_ttm"].fillna(-1e9) >= 15) & (df["per"] > 0) & (df["per"] <= 10)]
    return _head(m.sort_values("roe_ttm", ascending=False))


def _new_high_age_sessions(calendar: np.ndarray, high_date, day: dt.date) -> float:
    if high_date is None or (isinstance(high_date, float) and np.isnan(high_date)):
        return np.inf
    if isinstance(high_date, pd.Timestamp):
        high_date = high_date.date()
    if not isinstance(high_date, dt.date) or high_date > day:
        return np.inf
    lo = int(np.searchsorted(calendar, high_date, side="right"))
    hi = int(np.searchsorted(calendar, day, side="right"))
    return float(max(0, hi - lo))


def src_kr_undervalued_breakout(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty or ctx.calendar is None:
        return []
    m = df[
        (df["per"] > 0) & (df["per"] <= 10) & (df["pbr"] > 0) & (df["pbr"] <= 1)
    ].copy()
    if m.empty:
        return []
    m["age"] = [
        _new_high_age_sessions(ctx.calendar, hd, day) for hd in m["week_high_52_date"]
    ]
    m = m[m["age"] <= 20]
    return _head(m.sort_values("per", ascending=True))


def src_kr_profitable_company(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[
        (df["roe_ttm"].fillna(-1e9) >= 15)
        & (df["gross_margin_ttm"].fillna(-1e9) >= 0.20)
    ]
    return _head(m.sort_values("gross_margin_ttm", ascending=False))


def src_kr_undervalued_growth(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[
        (df["per"] > 0)
        & (df["per"] <= 20)
        & (df["revenue_yoy"].fillna(-1e9) >= 0.10)
        & (df["eps_yoy"].fillna(-1e9) >= 0.20)
    ]
    return _head(m.sort_values("revenue_yoy", ascending=False))


def src_kr_stable_growth(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[(df["roe_ttm"].fillna(-1e9) >= 15) & (df["eps_yoy"].fillna(-1e9) >= 0.10)]
    return _head(m.sort_values("roe_ttm", ascending=False))


def src_kr_growth_expectation_toss(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[(df["eps_yoy"].fillna(-1e9) >= 0.03) & (df["eps_qoq"].fillna(-1e9) >= 0.10)]
    return _head(m.sort_values("eps_yoy", ascending=False))


def src_kr_steady_dividend(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[
        (df["dividend_yield"].fillna(0) >= 3.0)
        & (df["payout_ratio_ttm"].fillna(-1) >= 30)
        & (df["continuous_dividend_payout"].fillna(0) >= 3)
    ]
    return _head(m.sort_values("dividend_yield", ascending=False))


def src_kr_future_dividend_king(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _kr_fund(ctx, day)
    if df.empty:
        return []
    m = df[
        (df["dividend_yield"].fillna(0) >= 1.0)
        & (df["continuous_dividend_growth"].fillna(0) >= 3)
        & (df["payout_ratio_ttm"].fillna(-1) >= 30)
    ]
    return _head(m.sort_values("dividend_yield", ascending=False))


# ---------------------------------------------------------------------------
# market_valuation_snapshots (US tvscreener) sources
# ---------------------------------------------------------------------------


def src_us_cheap_value(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.fundamentals.get(day)
    if df is None or df.empty:
        return []
    m = df[(df["per"] > 0) & (df["per"] <= 15) & (df["pbr"] > 0) & (df["pbr"] <= 1.5)]
    return _head(m.sort_values("pbr", ascending=True))


def src_us_high_yield_value(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.fundamentals.get(day)
    if df is None or df.empty:
        return []
    m = df[(df["roe"].fillna(-1e9) >= 15) & (df["per"] > 0) & (df["per"] <= 10)]
    return _head(m.sort_values("roe", ascending=False))


def src_us_undervalued_breakout(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.fundamentals.get(day)
    if df is None or df.empty or ctx.calendar is None:
        return []
    m = df[
        (df["per"] > 0) & (df["per"] <= 10) & (df["pbr"] > 0) & (df["pbr"] <= 1)
    ].copy()
    if m.empty:
        return []
    m["age"] = [
        _new_high_age_sessions(ctx.calendar, hd, day) for hd in m["high_52w_date"]
    ]
    m = m[m["age"] <= 20]
    return _head(m.sort_values("per", ascending=True))


def src_us_steady_dividend(ctx: MarketContext, day: dt.date) -> list[str]:
    df = ctx.fundamentals.get(day)
    if df is None or df.empty:
        return []
    m = df[df["dividend_yield"].fillna(0) >= 0.03]
    return _head(m.sort_values("dividend_yield", ascending=False))


# ---------------------------------------------------------------------------
# Reconstructed "현행 주력" RSI source
# ---------------------------------------------------------------------------


def src_tv_rsi45(ctx: MarketContext, day: dt.date, rsi_lookup: dict) -> list[str]:
    universe = _liquid(ctx, day)
    if universe.empty:
        return []
    rows = []
    for sym in universe["symbol"]:
        rsi = rsi_lookup.get((sym, day))
        if rsi is not None and rsi <= 45:
            rows.append((sym, rsi))
    rows.sort(key=lambda item: item[1])
    return [sym for sym, _ in rows[:GATE_POOL_DEPTH]]


# ---------------------------------------------------------------------------
# Crypto sources (frozen snapshot columns)
# ---------------------------------------------------------------------------


def _crypto(ctx: MarketContext, day: dt.date) -> pd.DataFrame:
    df = ctx.crypto.get(day)
    return pd.DataFrame() if df is None else df


def src_crypto_high_volume(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    return (
        [] if df.empty else _head(df.sort_values("trade_amount_24h", ascending=False))
    )


def src_crypto_oversold(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    if df.empty:
        return []
    m = df[df["rsi"].notna() & (df["rsi"] <= 35)]
    return _head(m.sort_values("rsi", ascending=True))


def src_crypto_momentum(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    return [] if df.empty else _head(df.sort_values("change_rate", ascending=False))


def src_crypto_funding_squeeze(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    if df.empty:
        return []
    m = df[df["funding_rate"].notna() & (df["funding_rate"] < 0)]
    return _head(m.sort_values("funding_rate", ascending=True))


def src_crypto_funding_overheated(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    if df.empty:
        return []
    m = df[df["funding_rate"].notna() & (df["funding_rate"] > 0)]
    return _head(m.sort_values("funding_rate", ascending=False))


def src_crypto_oi_surge(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    if df.empty:
        return []
    m = df[df["oi_change_24h"].notna()]
    return _head(m.sort_values("oi_change_24h", ascending=False))


def src_crypto_long_short_skew(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    if df.empty:
        return []
    m = df[df["long_short_account_ratio"].notna()].copy()
    if m.empty:
        return []
    m["skew"] = (m["long_short_account_ratio"] - 1.0).abs()
    return _head(m.sort_values("skew", ascending=False))


def src_crypto_rsi45(ctx: MarketContext, day: dt.date) -> list[str]:
    df = _crypto(ctx, day)
    if df.empty:
        return []
    m = df[
        df["rsi"].notna()
        & (df["rsi"] <= 45)
        & (df["trade_amount_24h"].fillna(0) >= CRYPTO_MIN_TRADE_AMOUNT_KRW)
    ]
    return _head(m.sort_values("rsi", ascending=True))


# ---------------------------------------------------------------------------
# Control + benchmark
# ---------------------------------------------------------------------------


def src_random(ctx: MarketContext, day: dt.date, n: int) -> list[str]:
    universe = _liquid(ctx, day)
    if universe.empty:
        return []
    symbols = sorted(universe["symbol"].tolist())
    # hashlib, not hash(): CPython string hashing is per-process randomised,
    # which would make the "seed-fixed" control silently irreproducible.
    digest = hashlib.sha256(
        f"{RANDOM_SEED}|{ctx.market}|{day.isoformat()}".encode()
    ).digest()
    rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
    take = min(n, len(symbols))
    idx = rng.choice(len(symbols), size=take, replace=False)
    return [symbols[i] for i in sorted(idx)]


def src_benchmark(ctx: MarketContext, day: dt.date) -> list[str]:
    return sorted(_liquid(ctx, day)["symbol"].tolist())


# ---------------------------------------------------------------------------
# Gate reconstruction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateEvidence:
    rsi: float | None
    has_strong_support_within: bool
    independent_families_within: int


def evaluate_gate(
    panel, symbol: str, day: dt.date, within_pct: float
) -> GateEvidence | None:
    """Recompute RSI + clustered support levels from bars <= ``day`` only."""
    win = panel.window(symbol, day, SR_WINDOW_BARS)
    if win is None:
        return None
    high, low, close, volume = win
    if close.size == 0:
        return None
    current = float(close[-1])
    rsi = ind.rsi_wilder(close)
    supports, _ = ind.support_resistance(high, low, close, volume, current)
    strong = False
    families: set[str] = set()
    for level in supports:
        dist = abs(level["distance_pct"])
        if dist > within_pct:
            continue
        if level["strength"] == "strong":
            strong = True
        if level["strength"] in ("strong", "moderate"):
            families |= ind.source_families(level["sources"])
    return GateEvidence(rsi, strong, len(families))
