"""Frozen-corpus readers for the three markets, normalised to one bar schema.

Every reader is offline: it opens Parquet files under
``/Users/mgh3326/work/herdr-artifacts`` and nothing else.  Sealed holdout
trees are refused through each corpus's own guard, so a mistake raises
instead of silently shrinking the sample.

Normalised frame per symbol, ascending by session, index reset:

    session (datetime64[ns]) | open | high | low | close | volume

plus two derived columns the market adapters own:

    contiguous_prev  bool   previous row is the immediately preceding
                            trading session of this market's calendar
    limit_locked     int    -1 limit-down lock, 0 none, +1 limit-up lock
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from research.crypto_corpus.loader import load_labeled_parquet_files
from research.kr_corpus.backtest.holdout_guard import assert_path_not_holdout
from research.us_corpus.holdout_gate import guard_read

CRYPTO_DATASET = Path(
    "/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1/dataset-labeled/venue=upbit_krw"
)
KR_RUN_DATASET = Path(
    "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/runs/"
    "kr-corpus-v1-20260803-1001/dataset"
)
US_DATASET = Path("/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/dataset/market=us")

BAR_COLUMNS = ["session", "open", "high", "low", "close", "volume"]

# KRX daily price limit: +-15% until 2015-06-14, +-30% from 2015-06-15.
KR_LIMIT_CHANGE_DATE = pd.Timestamp("2015-06-15")
KR_LIMIT_BEFORE = 0.15
KR_LIMIT_AFTER = 0.30
# Adjusted prices and tick rounding move the realised cap off the nominal one.
KR_LIMIT_TOLERANCE = 0.02


@dataclass(frozen=True)
class SymbolBars:
    """One symbol's cleaned daily history plus the rows that were dropped."""

    market: str
    symbol: str
    frame: pd.DataFrame
    dropped_invalid_rows: int
    inconsistent_ohlc_rows: int
    group: str = ""
    segment: str = ""


@dataclass(frozen=True)
class CorpusCoverage:
    market: str
    symbols: int
    rows: int
    first_session: str | None
    last_session: str | None
    dropped_invalid_rows: int
    inconsistent_ohlc_rows: int
    non_contiguous_rows: int


def _clean(frame: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Drop unusable bars and count the OHLC-consistency violations kept."""
    frame = frame.loc[:, BAR_COLUMNS].copy()
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    before = len(frame)
    positive = (
        frame[["open", "high", "low", "close"]].gt(0).all(axis=1)
        & frame["volume"].notna()
        & frame["session"].notna()
    )
    frame = frame.loc[positive]
    dropped = before - len(frame)

    frame = frame.sort_values("session").drop_duplicates("session", keep="last")
    inconsistent = int(
        (
            (frame["high"] < frame[["open", "close"]].max(axis=1))
            | (frame["low"] > frame[["open", "close"]].min(axis=1))
            | (frame["high"] < frame["low"])
        ).sum()
    )
    return frame.reset_index(drop=True), dropped, inconsistent


def _mark_contiguity(
    frame: pd.DataFrame, calendar: pd.DatetimeIndex | None
) -> pd.DataFrame:
    """Flag rows whose previous bar is the previous *calendar* session.

    A close-to-close return that silently bridges a missing session is not a
    24h return.  KR's corpus drops 171k main-scope sessions and the drops skew
    to strong up-closes, so this flag is load-bearing there, not cosmetic.
    """
    frame = frame.copy()
    if calendar is None:
        # Crypto: every UTC day is a session.
        delta = frame["session"].diff()
        frame["contiguous_prev"] = delta == pd.Timedelta(days=1)
    else:
        rank = pd.Series(np.arange(len(calendar)), index=calendar)
        positions = frame["session"].map(rank)
        frame["contiguous_prev"] = positions.diff() == 1
    frame.loc[frame.index[:1], "contiguous_prev"] = False
    frame["contiguous_prev"] = frame["contiguous_prev"].fillna(False).astype(bool)
    return frame


def _mark_no_limit(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["limit_locked"] = 0
    return frame


# --------------------------------------------------------------------------
# Crypto — Upbit KRW daily, crypto-corpus-v1 exploration tree (2017-2024).
# --------------------------------------------------------------------------

_CRYPTO_FILE = re.compile(r"^(?P<symbol>[^/]+)__1d__")


def crypto_symbol_files() -> dict[str, list[Path]]:
    files: dict[str, list[Path]] = {}
    for path_str in sorted(
        glob.glob(str(CRYPTO_DATASET / "year=*" / "*__1d__*.parquet"))
    ):
        path = Path(path_str)
        match = _CRYPTO_FILE.match(path.name)
        if match is None:
            continue
        files.setdefault(match.group("symbol"), []).append(path)
    return files


def load_crypto(symbols: list[str] | None = None) -> Iterator[SymbolBars]:
    """Load Upbit KRW daily bars through the corpus's fail-closed loader.

    ``consumer_intent="time_series"`` is correct and deliberate: every symbol
    is loaded and evaluated on its own timeline, and no ranking or selection
    is ever made across symbols on a shared date.  That is what keeps the
    Upbit XSEC survivorship opt-in from being required — the survivor bias is
    still disclosed in the report.
    """
    per_symbol = crypto_symbol_files()
    for symbol, paths in per_symbol.items():
        if symbols is not None and symbol not in symbols:
            continue
        corpus = load_labeled_parquet_files(tuple(paths), consumer_intent="time_series")
        frame = corpus.table.to_pandas()
        frame = frame.rename(columns={"base_volume": "volume"})
        frame["session"] = pd.to_datetime(
            frame["open_time_utc"], utc=True
        ).dt.tz_localize(None)
        cleaned, dropped, inconsistent = _clean(frame)
        cleaned = _mark_no_limit(_mark_contiguity(cleaned, calendar=None))
        yield SymbolBars(
            market="crypto",
            symbol=symbol,
            frame=cleaned,
            dropped_invalid_rows=dropped,
            inconsistent_ohlc_rows=inconsistent,
            group="upbit_krw",
            segment="upbit_krw",
        )


# --------------------------------------------------------------------------
# KR — kr-corpus-v1 immutable run snapshot (2015-2024, BUILT_WITH_GAPS).
# --------------------------------------------------------------------------


def kr_symbol_files() -> dict[tuple[str, str], list[Path]]:
    files: dict[tuple[str, str], list[Path]] = {}
    for path_str in sorted(
        glob.glob(str(KR_RUN_DATASET / "market=*" / "year=*" / "ticker=*.parquet"))
    ):
        path = Path(path_str)
        assert_path_not_holdout(path)
        market = path.parts[-3].split("=", 1)[1]
        ticker = path.stem.split("=", 1)[1]
        files.setdefault((market, ticker), []).append(path)
    return files


def kr_trading_calendar(
    market_files: dict[tuple[str, str], list[Path]],
) -> pd.DatetimeIndex:
    """Union of every session that survives in the snapshot, per KRX overall.

    Built from the membership partition names rather than the bar rows, so a
    ticker's own missing session is still measured against a real calendar.
    """
    membership_root = KR_RUN_DATASET.parent / "membership"
    sessions = {
        Path(p).stem.split("=", 1)[1]
        for p in glob.glob(
            str(membership_root / "market=*" / "year=*" / "session=*.parquet")
        )
    }
    if not sessions:
        raise FileNotFoundError(f"no KR membership sessions under {membership_root}")
    return pd.DatetimeIndex(sorted(pd.to_datetime(sorted(sessions))))


def _kr_limit_flags(frame: pd.DataFrame) -> pd.Series:
    """+1 limit-up lock, -1 limit-down lock, 0 otherwise.

    A lock is a zero-range bar (``high == low``) whose close-to-close move sits
    at the statutory daily cap.  Both conditions are required: a zero-range bar
    at a normal price move is an illiquid no-trade day, not a lock.
    """
    prev_close = frame["close"].shift(1)
    ret = frame["close"] / prev_close - 1.0
    cap = np.where(
        frame["session"] >= KR_LIMIT_CHANGE_DATE, KR_LIMIT_AFTER, KR_LIMIT_BEFORE
    )
    zero_range = frame["high"] <= frame["low"]
    locked_up = zero_range & (ret >= cap - KR_LIMIT_TOLERANCE)
    locked_down = zero_range & (ret <= -(cap - KR_LIMIT_TOLERANCE))
    return pd.Series(
        np.where(locked_up, 1, np.where(locked_down, -1, 0)),
        index=frame.index,
        dtype=int,
    )


def load_kr(symbols: list[str] | None = None) -> Iterator[SymbolBars]:
    per_symbol = kr_symbol_files()
    calendar = kr_trading_calendar(per_symbol)
    for (market, ticker), paths in per_symbol.items():
        if symbols is not None and ticker not in symbols:
            continue
        frames = []
        for path in paths:
            table = pq.ParquetFile(assert_path_not_holdout(path)).read()
            part = table.to_pandas()
            part["session"] = pd.to_datetime(part["session"])
            frames.append(part)
        frame = pd.concat(frames, ignore_index=True)
        cleaned, dropped, inconsistent = _clean(frame)
        cleaned = _mark_contiguity(cleaned, calendar=calendar)
        cleaned["limit_locked"] = _kr_limit_flags(cleaned)
        yield SymbolBars(
            market="kr",
            symbol=ticker,
            frame=cleaned,
            dropped_invalid_rows=dropped,
            inconsistent_ohlc_rows=inconsistent,
            # No proven common-stock classification exists in kr-corpus-v1
            # (``NO_PROVEN_COMMON_STOCK_ROWS``).  The KRX code convention —
            # common issues end in 0 — is recorded as a *label* so the report
            # can slice on it; it never filters the primary sample.
            group="code_suffix_0" if ticker.endswith("0") else "code_suffix_other",
            segment=market,
        )


# --------------------------------------------------------------------------
# US — us-corpus-v1 exploration tree (2016-2024, SURVIVORSHIP_BIASED=TRUE).
# --------------------------------------------------------------------------


def us_year_files() -> list[Path]:
    paths = [
        guard_read(Path(p), reason="underwater-spike-trim backtest, exploration only")
        for p in sorted(glob.glob(str(US_DATASET / "year=*" / "*.parquet")))
    ]
    if not paths:
        raise FileNotFoundError(f"no US exploration partitions under {US_DATASET}")
    return paths


def load_us(symbols: list[str] | None = None) -> Iterator[SymbolBars]:
    """Load the whole US exploration tree once, then yield per symbol.

    The corpus is stored one Parquet part per year rather than per symbol, so
    a per-symbol read would rescan every year file.  Peak memory is ~7.5M rows
    x 6 numeric columns, which fits comfortably.
    """
    frames = []
    for path in us_year_files():
        part = pq.ParquetFile(path).read(
            columns=["symbol", "session_date", "open", "high", "low", "close", "volume"]
        )
        frames.append(part.to_pandas())
    everything = pd.concat(frames, ignore_index=True)
    everything["session"] = pd.to_datetime(everything["session_date"])
    if symbols is not None:
        everything = everything[everything["symbol"].isin(symbols)]
    calendar = pd.DatetimeIndex(sorted(everything["session"].unique()))
    for symbol, group in everything.groupby("symbol", sort=True):
        cleaned, dropped, inconsistent = _clean(group)
        cleaned = _mark_no_limit(_mark_contiguity(cleaned, calendar=calendar))
        yield SymbolBars(
            market="us",
            symbol=str(symbol),
            frame=cleaned,
            dropped_invalid_rows=dropped,
            inconsistent_ohlc_rows=inconsistent,
            group="common_stock_frozen_universe",
            segment="us",
        )


LOADERS = {"crypto": load_crypto, "kr": load_kr, "us": load_us}


def load_market(market: str, symbols: list[str] | None = None) -> Iterator[SymbolBars]:
    try:
        loader = LOADERS[market]
    except KeyError as exc:  # pragma: no cover - CLI validates first
        raise ValueError(f"unsupported market {market!r}") from exc
    return loader(symbols)


def assert_offline_environment() -> None:
    """Cheap belt-and-braces check that nothing here needs a network or DB.

    The real guarantee is structural (no client import anywhere in the
    package); this only catches an operator who exported a proxy expecting the
    study to phone out.
    """
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if os.environ.get(variable):
            raise RuntimeError(
                f"{variable} is set; this study makes no network calls and a proxy "
                "expectation means the run was configured wrongly"
            )


__all__ = [
    "BAR_COLUMNS",
    "CorpusCoverage",
    "SymbolBars",
    "assert_offline_environment",
    "load_crypto",
    "load_kr",
    "load_market",
    "load_us",
]
