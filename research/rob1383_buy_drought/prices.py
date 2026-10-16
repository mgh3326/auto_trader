"""Public daily bars with explicit session open/close timestamps.

Sources (no broker credential, no operating DB, no localhost DB):

* KR  -> yfinance ``<code>.KS`` then ``<code>.KQ`` (KRX regular session only)
* US  -> yfinance (symbol converted by ``app.core.symbol.to_yahoo_symbol``)
* crypto -> Upbit public REST ``/v1/candles/days`` (no auth)
* benchmarks -> ``^KS11``, ``SPY``, ``KRW-BTC``

Every frame carries ``session_date, open, high, low, close, volume,
open_ts, close_ts`` (UTC). ``close_ts`` is when the bar is *complete*:

* KR      session_date 15:30 Asia/Seoul
* US      session_date 16:00 America/New_York (DST aware)
* crypto  session_date 00:00 UTC + 1 day (= next 09:00 KST, Upbit day roll)

Bars whose ``close_ts`` is after the fetch moment are dropped (a partial bar
is not a daily bar). A fetch failure is returned as ``None`` and recorded by
the caller as a per-row missing reason -- never silently skipped.

Raw prices are used (``auto_adjust=False``): order prices and stated support
levels are raw. yfinance still split-adjusts; ``split_events`` reports any
split inside the study range so the caller can flag affected rows.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.symbol import to_yahoo_symbol

LOG = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

STUDY_START = date(2025, 7, 1)
INTRADAY_START = date(2026, 8, 14)  # cohort + orders start 2026-08-20
STUDY_END_EXCLUSIVE = date(2026, 10, 18)

BENCHMARK = {"kr": "^KS11", "us": "SPY", "crypto": "KRW-BTC"}

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "prices"

_COLUMNS = [
    "session_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_ts",
    "close_ts",
]


def session_bounds(market: str, session_date: date) -> tuple[datetime, datetime]:
    """UTC (open, close) of one daily bar for ``market``."""

    if market == "kr":
        o = datetime(
            session_date.year, session_date.month, session_date.day, 9, 0, tzinfo=KST
        )
        c = datetime(
            session_date.year, session_date.month, session_date.day, 15, 30, tzinfo=KST
        )
    elif market == "us":
        o = datetime(
            session_date.year, session_date.month, session_date.day, 9, 30, tzinfo=NY
        )
        c = datetime(
            session_date.year, session_date.month, session_date.day, 16, 0, tzinfo=NY
        )
    elif market == "crypto":
        o = datetime(
            session_date.year, session_date.month, session_date.day, tzinfo=UTC
        )
        c = o + timedelta(days=1)
    else:
        raise ValueError(f"unknown market {market!r}")
    return o.astimezone(UTC), c.astimezone(UTC)


_HOURLY_COLUMNS = [
    "session_date",
    "start_ts",
    "end_ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
]


def finalise_hourly(market: str, rows: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Attach ``session_date`` and ``end_ts`` to hourly bars (``start_ts`` UTC).

    ``end_ts`` is the next bar's start within the same session; a session's
    last bar ends at the session close (yfinance KR folds 14:00-15:30 into one
    bar). Crypto bars are 60 minutes and the session is the Upbit UTC day.
    """

    if rows.empty:
        return pd.DataFrame(columns=_HOURLY_COLUMNS)
    rows = rows.dropna(subset=["open", "high", "low", "close"]).copy()
    rows["start_ts"] = pd.to_datetime(rows["start_ts"], utc=True)
    rows = (
        rows.drop_duplicates("start_ts").sort_values("start_ts").reset_index(drop=True)
    )
    if market == "crypto":
        rows["session_date"] = rows["start_ts"].dt.date
        rows["end_ts"] = rows["start_ts"] + pd.Timedelta(minutes=60)
    else:
        tz = KST if market == "kr" else NY
        rows["session_date"] = rows["start_ts"].dt.tz_convert(tz).dt.date
        ends = []
        for i, (d, _start) in enumerate(
            zip(rows["session_date"], rows["start_ts"], strict=True)
        ):
            if i + 1 < len(rows) and rows["session_date"].iloc[i + 1] == d:
                ends.append(rows["start_ts"].iloc[i + 1])
            else:
                ends.append(pd.Timestamp(session_bounds(market, d)[1]))
        rows["end_ts"] = ends
    rows = rows[rows["end_ts"] <= pd.Timestamp(now)]
    rows = rows[(rows["low"] > 0) & (rows["high"] > 0)]
    return rows[_HOURLY_COLUMNS].reset_index(drop=True)


def _finalise(market: str, rows: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(columns=_COLUMNS)
    rows = rows.copy()
    rows["session_date"] = pd.to_datetime(rows["session_date"]).dt.date
    bounds = [session_bounds(market, d) for d in rows["session_date"]]
    rows["open_ts"] = [b[0] for b in bounds]
    rows["close_ts"] = [b[1] for b in bounds]
    rows = rows[rows["close_ts"] <= now]
    rows = rows.dropna(subset=["open", "high", "low", "close"])
    rows = rows[(rows["high"] > 0) & (rows["low"] > 0) & (rows["close"] > 0)]
    rows = rows.drop_duplicates("session_date").sort_values("session_date")
    return rows[_COLUMNS].reset_index(drop=True)


@dataclass
class PriceStore:
    """Fetch-once, parquet-cached daily bar store.

    ``missing`` maps ``(market, symbol)`` to the failure reason so each table
    can report how many rows it lost to price data.
    """

    cache_dir: Path = DEFAULT_CACHE_DIR
    offline: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    missing: dict[tuple[str, str], str] = field(default_factory=dict)
    resolved_ticker: dict[tuple[str, str], str] = field(default_factory=dict)
    split_events: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    _mem: dict[tuple[str, str], pd.DataFrame | None] = field(default_factory=dict)
    _hourly_mem: dict[tuple[str, str], pd.DataFrame | None] = field(
        default_factory=dict
    )
    hourly_missing: dict[tuple[str, str], str] = field(default_factory=dict)

    def hourly(self, market: str, symbol: str) -> pd.DataFrame | None:
        """Hourly regular-session bars from INTRADAY_START, or ``None``."""

        key = (market, symbol)
        if key in self._hourly_mem:
            return self._hourly_mem[key]
        path = self.cache_dir / "hourly" / self._path(market, symbol).name
        frame: pd.DataFrame | None = None
        if path.exists():
            frame = pd.read_parquet(path)
            for col in ("start_ts", "end_ts"):
                frame[col] = pd.to_datetime(frame[col], utc=True)
            frame["session_date"] = pd.to_datetime(frame["session_date"]).dt.date
        elif not self.offline and key not in self.missing:
            try:
                frame = (
                    self._fetch_upbit_hourly(symbol)
                    if market == "crypto"
                    else self._fetch_yahoo_hourly(market, symbol)
                )
            except Exception as exc:  # noqa: BLE001 -- recorded below
                LOG.warning("hourly fetch failed %s %s: %s", market, symbol, exc)
                self.hourly_missing[key] = f"fetch_error:{type(exc).__name__}"
                frame = None
            if frame is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                out = frame.copy()
                out["session_date"] = pd.to_datetime(out["session_date"])
                out.to_parquet(path, index=False)
        if frame is not None and frame.empty:
            frame = None
        if frame is None and key not in self.hourly_missing:
            self.hourly_missing[key] = (
                "offline_cache_miss" if self.offline else "no_hourly_bars"
            )
        self._hourly_mem[key] = frame
        return frame

    def _fetch_yahoo_hourly(self, market: str, symbol: str) -> pd.DataFrame:
        import yfinance as yf

        ticker = self.resolved_ticker.get((market, symbol))
        if ticker is None:
            self.bars(market, symbol)
            ticker = self.resolved_ticker.get((market, symbol))
        if ticker is None:
            return pd.DataFrame(columns=_HOURLY_COLUMNS)
        hist = yf.Ticker(ticker).history(
            start=INTRADAY_START.isoformat(),
            end=STUDY_END_EXCLUSIVE.isoformat(),
            interval="60m",
            auto_adjust=False,
            prepost=False,
        )
        if hist is None or hist.empty:
            return pd.DataFrame(columns=_HOURLY_COLUMNS)
        hist = hist.reset_index()
        ts_col = hist.columns[0]
        frame = pd.DataFrame(
            {
                "start_ts": pd.to_datetime(hist[ts_col], utc=True),
                "open": hist["Open"].astype(float),
                "high": hist["High"].astype(float),
                "low": hist["Low"].astype(float),
                "close": hist["Close"].astype(float),
                "volume": hist["Volume"].astype(float),
            }
        )
        return finalise_hourly(market, frame, self.now)

    def _fetch_upbit_hourly(self, market_code: str) -> pd.DataFrame:
        start = datetime(
            INTRADAY_START.year, INTRADAY_START.month, INTRADAY_START.day, tzinfo=UTC
        )
        rows = self._upbit_paged("minutes/60", market_code, start, max_pages=12)
        if rows is None:
            return pd.DataFrame(columns=_HOURLY_COLUMNS)
        frame = pd.DataFrame(
            {
                "start_ts": [r["candle_date_time_utc"] + "+00:00" for r in rows],
                "open": [float(r["opening_price"]) for r in rows],
                "high": [float(r["high_price"]) for r in rows],
                "low": [float(r["low_price"]) for r in rows],
                "close": [float(r["trade_price"]) for r in rows],
                "volume": [float(r["candle_acc_trade_volume"]) for r in rows],
            }
        )
        frame = frame[
            pd.to_datetime(frame["start_ts"], utc=True) >= pd.Timestamp(start)
        ]
        return finalise_hourly("crypto", frame, self.now)

    def bars(self, market: str, symbol: str) -> pd.DataFrame | None:
        key = (market, symbol)
        if key in self._mem:
            return self._mem[key]
        frame = self._load_cached(market, symbol)
        if frame is None and not self.offline:
            frame = self._fetch(market, symbol)
            if frame is not None:
                self._store_cached(market, symbol, frame)
        if frame is None and key not in self.missing:
            self.missing[key] = "offline_cache_miss" if self.offline else "fetch_failed"
        if frame is not None and frame.empty:
            self.missing[key] = "no_bars"
            frame = None
        self._mem[key] = frame
        return frame

    def benchmark(self, market: str) -> pd.DataFrame | None:
        return self.bars(market, BENCHMARK[market])

    # ------------------------------------------------------------------ cache
    def _path(self, market: str, symbol: str) -> Path:
        safe = symbol.replace("/", "_").replace("^", "IDX_")
        return self.cache_dir / f"{market}__{safe}.parquet"

    def _meta_path(self, market: str, symbol: str) -> Path:
        return self._path(market, symbol).with_suffix(".json")

    def _load_cached(self, market: str, symbol: str) -> pd.DataFrame | None:
        path = self._path(market, symbol)
        meta_path = self._meta_path(market, symbol)
        if not path.exists() or not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text())
        if meta.get("status") != "ok":
            self.missing[(market, symbol)] = meta.get("status", "fetch_failed")
            return (
                pd.DataFrame(columns=_COLUMNS)
                if meta.get("status") == "no_bars"
                else None
            )
        frame = pd.read_parquet(path)
        frame["session_date"] = pd.to_datetime(frame["session_date"]).dt.date
        for col in ("open_ts", "close_ts"):
            frame[col] = pd.to_datetime(frame[col], utc=True)
        if meta.get("ticker"):
            self.resolved_ticker[(market, symbol)] = meta["ticker"]
        if meta.get("splits"):
            self.split_events[(market, symbol)] = list(meta["splits"])
        return frame

    def _store_cached(self, market: str, symbol: str, frame: pd.DataFrame) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        out = frame.copy()
        out["session_date"] = pd.to_datetime(out["session_date"])
        out.to_parquet(self._path(market, symbol), index=False)
        self._meta_path(market, symbol).write_text(
            json.dumps(
                {
                    "status": "ok" if not frame.empty else "no_bars",
                    "ticker": self.resolved_ticker.get((market, symbol)),
                    "splits": self.split_events.get((market, symbol), []),
                    "fetched_at": self.now.isoformat(),
                }
            )
        )

    # ------------------------------------------------------------------ fetch
    def _fetch(self, market: str, symbol: str) -> pd.DataFrame | None:
        try:
            if market == "crypto":
                return self._fetch_upbit(symbol)
            return self._fetch_yahoo(market, symbol)
        except Exception as exc:  # noqa: BLE001 -- recorded, never swallowed silently
            LOG.warning("price fetch failed %s %s: %s", market, symbol, exc)
            self.missing[(market, symbol)] = f"fetch_error:{type(exc).__name__}"
            return None

    def _fetch_yahoo(self, market: str, symbol: str) -> pd.DataFrame | None:
        import yfinance as yf

        if symbol.startswith("^") or market == "us":
            candidates = [symbol if symbol.startswith("^") else to_yahoo_symbol(symbol)]
        else:
            candidates = [f"{symbol}.KS", f"{symbol}.KQ"]
        for ticker in candidates:
            hist = yf.Ticker(ticker).history(
                start=STUDY_START.isoformat(),
                end=STUDY_END_EXCLUSIVE.isoformat(),
                interval="1d",
                auto_adjust=False,
                actions=True,
            )
            if hist is None or hist.empty:
                continue
            hist = hist.reset_index()
            date_col = "Date" if "Date" in hist.columns else hist.columns[0]
            frame = pd.DataFrame(
                {
                    "session_date": pd.to_datetime(hist[date_col])
                    .dt.tz_localize(None)
                    .dt.date,
                    "open": hist["Open"].astype(float),
                    "high": hist["High"].astype(float),
                    "low": hist["Low"].astype(float),
                    "close": hist["Close"].astype(float),
                    "volume": hist["Volume"].astype(float),
                }
            )
            if "Stock Splits" in hist.columns:
                splits = hist.loc[hist["Stock Splits"].fillna(0) != 0, date_col]
                if len(splits):
                    self.split_events[(market, symbol)] = [
                        str(pd.Timestamp(d).date()) for d in splits
                    ]
            self.resolved_ticker[(market, symbol)] = ticker
            return _finalise(market, frame, self.now)
        self.missing[(market, symbol)] = "no_bars"
        return pd.DataFrame(columns=_COLUMNS)

    def _upbit_paged(
        self, path: str, market_code: str, start: datetime, max_pages: int
    ) -> list[dict] | None:
        """Page backwards through an Upbit candle endpoint until ``start``.

        Returns ``None`` (and records the reason) when the market is unknown.
        """

        rows: list[dict] = []
        to: str | None = None
        for _ in range(max_pages):
            params = {"market": market_code, "count": "200"}
            if to:
                params["to"] = to
            url = f"https://api.upbit.com/v1/candles/{path}?" + urllib.parse.urlencode(
                params
            )
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            payload = None
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 -- fixed https host
                        payload = json.loads(resp.read())
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429 and attempt < 3:
                        time.sleep(1.0 + attempt)
                        continue
                    if exc.code in (400, 404):
                        self.missing.setdefault(
                            ("crypto", market_code), f"upbit_http_{exc.code}"
                        )
                        return None
                    raise
            time.sleep(0.13)
            if not payload:
                break
            rows.extend(payload)
            oldest = min(p["candle_date_time_utc"] for p in payload)
            oldest_dt = datetime.fromisoformat(oldest).replace(tzinfo=UTC)
            if oldest_dt <= start or len(payload) < 200:
                break
            to = oldest_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return rows

    def _fetch_upbit(self, market_code: str) -> pd.DataFrame | None:
        start = datetime(
            STUDY_START.year, STUDY_START.month, STUDY_START.day, tzinfo=UTC
        )
        rows = self._upbit_paged("days", market_code, start, max_pages=10)
        if rows is None:
            return pd.DataFrame(columns=_COLUMNS)
        self.resolved_ticker[("crypto", market_code)] = market_code
        if not rows:
            return pd.DataFrame(columns=_COLUMNS)
        frame = pd.DataFrame(
            {
                # Upbit day candle opens at 09:00 KST == 00:00 UTC of this date.
                "session_date": [r["candle_date_time_utc"][:10] for r in rows],
                "open": [float(r["opening_price"]) for r in rows],
                "high": [float(r["high_price"]) for r in rows],
                "low": [float(r["low_price"]) for r in rows],
                "close": [float(r["trade_price"]) for r in rows],
                "volume": [float(r["candle_acc_trade_volume"]) for r in rows],
            }
        )
        frame = frame[pd.to_datetime(frame["session_date"]).dt.date >= STUDY_START]
        return _finalise("crypto", frame, self.now)
