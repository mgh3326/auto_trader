"""Read-only data loading for the screener bakeoff.

Everything here is a SELECT.  No INSERT/UPDATE/DELETE statement exists in
this package.  The DSN is read from ``BAKEOFF_DATABASE_URL`` (or
``DATABASE_URL``) and the ``postgresql+asyncpg://`` prefix is normalised.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.screener_bakeoff.spec import MIN_PARTITION_ROWS


def _dsn() -> str:
    raw = os.environ.get("BAKEOFF_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("BAKEOFF_DATABASE_URL or DATABASE_URL must be set")
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def _fetch(sql: str, *args) -> list[dict]:
    import asyncpg

    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch(sql, *args)
    finally:
        await conn.close()
    return [dict(r) for r in rows]


def fetch_df(sql: str, *args) -> pd.DataFrame:
    rows = asyncio.run(_fetch(sql, *args))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in df.columns:
        if (
            df[col].dtype == object
            and len(df)
            and isinstance(
                df[col].dropna().iloc[0] if df[col].notna().any() else None,
                (int, float),
            )
        ):
            continue
    return df


def _to_float(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Price panels
# ---------------------------------------------------------------------------


@dataclass
class PricePanel:
    """Per-symbol point-in-time bar arrays plus a market session calendar.

    ``dates[sym]`` is strictly increasing; index ``i`` in ``dates[sym]``
    aligns with index ``i`` in close/high/low/volume.
    """

    market: str
    dates: dict[str, np.ndarray]
    close: dict[str, np.ndarray]
    high: dict[str, np.ndarray]
    low: dict[str, np.ndarray]
    volume: dict[str, np.ndarray]
    calendar: np.ndarray  # sorted unique session dates across the market
    has_intraday_range: bool

    def index_of(self, symbol: str, day: dt.date) -> int | None:
        arr = self.dates.get(symbol)
        if arr is None:
            return None
        pos = int(np.searchsorted(arr, day))
        if pos < arr.size and arr[pos] == day:
            return pos
        return None

    def window(self, symbol: str, day: dt.date, bars: int):
        """(high, low, close, volume) for the trailing ``bars`` ending at ``day``.

        Returns ``None`` when the symbol has no bar exactly on ``day``.  Only
        bars at or before ``day`` are ever returned — the look-ahead guard.
        """
        idx = self.index_of(symbol, day)
        if idx is None:
            return None
        lo = max(0, idx + 1 - bars)
        sl = slice(lo, idx + 1)
        return (
            self.high[symbol][sl],
            self.low[symbol][sl],
            self.close[symbol][sl],
            self.volume[symbol][sl],
        )


def _panel_from_df(market: str, df: pd.DataFrame, has_range: bool) -> PricePanel:
    dates: dict[str, np.ndarray] = {}
    close: dict[str, np.ndarray] = {}
    high: dict[str, np.ndarray] = {}
    low: dict[str, np.ndarray] = {}
    volume: dict[str, np.ndarray] = {}
    if df.empty:
        return PricePanel(
            market, dates, close, high, low, volume, np.array([]), has_range
        )
    df = df.sort_values(["symbol", "d"])
    for sym, grp in df.groupby("symbol", sort=False):
        dates[sym] = grp["d"].to_numpy()
        close[sym] = grp["close"].to_numpy(dtype=float)
        high[sym] = grp["high"].to_numpy(dtype=float)
        low[sym] = grp["low"].to_numpy(dtype=float)
        volume[sym] = grp["volume"].to_numpy(dtype=float)
    calendar = np.array(sorted(df["d"].unique()))
    return PricePanel(market, dates, close, high, low, volume, calendar, has_range)


_KR_CANDLE_SQL = """
select symbol, (time at time zone 'Asia/Seoul')::date as d,
       high::float8 as high, low::float8 as low, close::float8 as close,
       volume::float8 as volume
from kr_candles_1d
where venue = 'KRX'
"""

_US_CANDLE_SQL = """
select symbol, d, high, low, close, volume from (
  select symbol,
         (time at time zone 'Asia/Seoul')::date as d,
         high::float8 as high, low::float8 as low, close::float8 as close,
         volume::float8 as volume,
         row_number() over (
           partition by symbol, (time at time zone 'Asia/Seoul')::date
           order by volume desc, exchange
         ) as rn
  from us_candles_1d
) t where rn = 1
"""

#: Crypto bar coverage in crypto_candles_1d is far too sparse (38 instruments in
#: the latest month) to serve as the crypto price panel.  The crypto price path
#: is therefore the frozen daily snapshot close series instead — see
#: ``load_crypto_price_panel``.
_CRYPTO_CANDLE_SQL = """
select i.venue_symbol as symbol, (c.time at time zone 'Asia/Seoul')::date as d,
       c.high::float8 as high, c.low::float8 as low, c.close::float8 as close,
       c.base_volume::float8 as volume
from crypto_candles_1d c
join crypto_instruments i on i.id = c.instrument_id
where i.venue = 'upbit' and i.product = 'spot'
"""


def load_kr_price_panel() -> PricePanel:
    return _panel_from_df("kr", fetch_df(_KR_CANDLE_SQL), True)


def load_us_price_panel() -> PricePanel:
    return _panel_from_df("us", fetch_df(_US_CANDLE_SQL), True)


def load_crypto_price_panel(snap: pd.DataFrame) -> PricePanel:
    """Close-only panel built from the frozen crypto snapshot close series."""
    df = snap[["symbol", "snapshot_date", "latest_close"]].copy()
    df = df.rename(columns={"snapshot_date": "d", "latest_close": "close"})
    df = df.dropna(subset=["close"])
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["volume"] = 0.0
    return _panel_from_df("crypto", df, False)


# ---------------------------------------------------------------------------
# Snapshot panels (the frozen, decision-date source columns)
# ---------------------------------------------------------------------------

_SCREENER_SNAP_SQL = """
select symbol, snapshot_date, latest_close::float8 as latest_close,
       change_rate::float8 as change_rate,
       consecutive_up_days, week_change_rate::float8 as week_change_rate,
       daily_volume::float8 as daily_volume,
       daily_turnover::float8 as daily_turnover
from invest_screener_snapshots
where market = $1 and snapshot_date >= $2
"""

_KR_FUND_SQL = """
select symbol, snapshot_date, price::float8 as price, change_rate::float8 as change_rate,
       market_cap::float8 as market_cap, volume::float8 as volume,
       per::float8 as per, pbr::float8 as pbr, roe_ttm::float8 as roe_ttm,
       dividend_yield::float8 as dividend_yield,
       payout_ratio_ttm::float8 as payout_ratio_ttm,
       gross_margin_ttm::float8 as gross_margin_ttm,
       revenue_yoy::float8 as revenue_yoy, eps_yoy::float8 as eps_yoy,
       eps_qoq::float8 as eps_qoq,
       continuous_dividend_payout::float8 as continuous_dividend_payout,
       continuous_dividend_growth::float8 as continuous_dividend_growth,
       week_high_52::float8 as week_high_52, week_high_52_date,
       rsi14::float8 as rsi14
from invest_kr_fundamentals_snapshots
where snapshot_date >= $1
"""

_US_VAL_SQL = """
select symbol, snapshot_date, per::float8 as per, pbr::float8 as pbr,
       roe::float8 as roe, dividend_yield::float8 as dividend_yield,
       market_cap::float8 as market_cap, high_52w::float8 as high_52w, high_52w_date
from market_valuation_snapshots
where market = 'us' and source = 'tvscreener' and snapshot_date >= $1
"""

_FLOW_SQL = """
select symbol, snapshot_date, foreign_net::float8 as foreign_net,
       institution_net::float8 as institution_net, double_buy,
       foreign_consecutive_buy_days, change_rate::float8 as change_rate,
       close::float8 as close, volume::float8 as volume
from investor_flow_snapshots
where market = 'kr' and snapshot_date >= $1
"""

_CRYPTO_SNAP_SQL = """
select symbol, snapshot_date, latest_close::float8 as latest_close,
       change_rate::float8 as change_rate,
       trade_amount_24h::float8 as trade_amount_24h,
       rsi::float8 as rsi, adx::float8 as adx,
       funding_rate::float8 as funding_rate,
       open_interest_usd::float8 as open_interest_usd,
       oi_change_24h::float8 as oi_change_24h,
       long_short_account_ratio::float8 as long_short_account_ratio,
       market_warning
from invest_crypto_screener_snapshots
where snapshot_date >= $1
"""


def load_screener_snapshots(market: str, since: dt.date) -> pd.DataFrame:
    return fetch_df(_SCREENER_SNAP_SQL, market, since)


def load_kr_fundamentals(since: dt.date) -> pd.DataFrame:
    return fetch_df(_KR_FUND_SQL, since)


def load_us_valuation(since: dt.date) -> pd.DataFrame:
    return fetch_df(_US_VAL_SQL, since)


def load_investor_flow(since: dt.date) -> pd.DataFrame:
    return fetch_df(_FLOW_SQL, since)


def load_crypto_snapshots(since: dt.date) -> pd.DataFrame:
    return fetch_df(_CRYPTO_SNAP_SQL, since)


def complete_partition_dates(
    df: pd.DataFrame, min_rows: int = MIN_PARTITION_ROWS
) -> list[dt.date]:
    """Snapshot dates whose partition is materially complete."""
    if df.empty:
        return []
    counts = df.groupby("snapshot_date").size()
    return sorted(d for d, n in counts.items() if n >= min_rows)
