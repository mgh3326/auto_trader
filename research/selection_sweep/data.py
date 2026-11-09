"""Public, credential-free inputs: US Yahoo bars for a seeded name sample and
Binance USDT spot daily candles from the local crypto-universe-v1 artifact.

No ``app`` import, DB, broker or credential. Network goes only through the
data-plane allowlisted GET (`research.data_plane.public_http`).
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import pandas as pd

from . import spec

COVERAGE_SLACK_DAYS = 7
BAR_COLUMNS = ["open", "high", "low", "close", "volume", "traded_value"]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def yahoo_symbol(symbol: str) -> str:
    # DB dot format -> Yahoo hyphen; the one conversion lives in app.core.symbol
    # but this research package must not import app, so mirror it explicitly.
    return symbol.replace(".", "-")


def select_us_sample(pit_csv: Path, *, size: int = spec.US_SAMPLE_SIZE) -> list[str]:
    """Seeded sample of common stocks already listed at ``US_LISTED_BY``.

    Selection reads listing facts only; no price or outcome. Names delisted
    later stay candidates, so how many are unobtainable from Yahoo is a measured
    survivorship number, not a hidden filter.
    """
    rows = list(csv.DictReader(pit_csv.open()))
    cands = sorted(
        {
            r["symbol"]
            for r in rows
            if r["likely_common_stock"] == "true"
            and r["etf"] == "false"
            and r["listed_from"] <= spec.US_LISTED_BY
            and (not r["delisted_by"] or r["delisted_by"] > spec.US_LISTED_BY)
        }
    )
    if size > len(cands):
        raise ValueError("sample larger than candidate set")
    return sorted(random.Random(spec.US_SAMPLE_SEED).sample(cands, size))


def parse_yahoo_chart(body: bytes) -> pd.DataFrame:
    """Split/dividend adjusted OHLC (adjclose factor) + raw volume/traded value."""
    result = json.loads(body)["chart"]["result"][0]
    quote_ = result["indicators"]["quote"][0]
    adj = result["indicators"]["adjclose"][0]["adjclose"]
    frame = pd.DataFrame(
        {
            "date": [
                datetime.fromtimestamp(t, UTC).date()
                for t in result.get("timestamp", [])
            ],
            "open": quote_["open"],
            "high": quote_["high"],
            "low": quote_["low"],
            "close": quote_["close"],
            "volume": quote_["volume"],
            "adjclose": adj,
        }
    )
    frame = frame.dropna().drop_duplicates("date", keep="last")
    factor = frame.adjclose / frame.close
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(frame.date),
            "open": frame.open * factor,
            "high": frame.high * factor,
            "low": frame.low * factor,
            "close": frame.adjclose,
            "volume": frame.volume.astype(float),
            "traded_value": frame.close * frame.volume,
        }
    )
    return out.sort_values("date").reset_index(drop=True)


def fetch_us(
    symbols: list[str], out_dir: Path, *, start: str, end: str, fetch=None
) -> dict:
    """One Yahoo request per symbol (+benchmark). Write-once cache, status log."""
    from research.data_plane.public_http import make_public_fetch

    fetch = fetch or make_public_fetch(timeout=20, attempts=3)
    out_dir.mkdir(parents=True, exist_ok=True)
    request = {"start": start, "end_exclusive": end}
    marker = out_dir / "request.json"
    if marker.exists():
        if json.loads(marker.read_text()) != request:
            raise ValueError(
                "cache already holds a different request window; "
                "use a fresh directory for another period"
            )
    else:
        if any(out_dir.glob("*.csv")):
            raise ValueError(
                "cache holds CSVs without a request window; their period is "
                "unverified, use a fresh directory"
            )
        marker.write_text(json.dumps(request))
    period = urlencode(
        {
            "period1": int(
                datetime.combine(
                    date.fromisoformat(start), datetime.min.time(), UTC
                ).timestamp()
            ),
            "period2": int(
                datetime.combine(
                    date.fromisoformat(end), datetime.min.time(), UTC
                ).timestamp()
            ),
            "interval": "1d",
            "events": "div,splits",
        }
    )
    log = []
    for symbol in [spec.BENCHMARK["us"], *symbols]:
        target = out_dir / f"{symbol}.csv"
        if target.exists():
            log.append({"symbol": symbol, "status": "cached"})
            continue
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{quote(yahoo_symbol(symbol), safe='')}?{period}"
        )
        try:
            response = fetch(url)
            if response.status != 200:
                log.append({"symbol": symbol, "status": f"http_{response.status}"})
                continue
            frame = parse_yahoo_chart(response.body)
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            log.append({"symbol": symbol, "status": f"error_{type(exc).__name__}"})
            continue
        if frame.empty:
            log.append({"symbol": symbol, "status": "empty"})
            continue
        frame.to_csv(target, index=False)
        log.append({"symbol": symbol, "status": "ok", "rows": len(frame)})
    (out_dir / "fetch_log.json").write_text(json.dumps(log, indent=1))
    counts: dict[str, int] = {}
    for item in log:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def load_us(
    cache_dir: Path, symbols: list[str], *, end: str
) -> tuple[dict, pd.DataFrame, dict]:
    """Return (frames, benchmark, availability). Rows after ``end`` are never returned."""
    stop = pd.Timestamp(end)
    frames, missing = {}, []
    for symbol in symbols:
        path = cache_dir / f"{symbol}.csv"
        if not path.exists():
            missing.append(symbol)
            continue
        frame = pd.read_csv(path, parse_dates=["date"])
        frame = frame[frame.date <= stop]
        if not frame.empty:
            frames[symbol] = frame.set_index("date")[BAR_COLUMNS]
    bench = pd.read_csv(cache_dir / f"{spec.BENCHMARK['us']}.csv", parse_dates=["date"])
    bench = bench[bench.date <= stop].set_index("date")[BAR_COLUMNS]
    # A cache fetched for an earlier period must not satisfy a longer request.
    if bench.empty or bench.index.max() < stop - pd.Timedelta(days=COVERAGE_SLACK_DAYS):
        last = None if bench.empty else bench.index.max().date()
        raise ValueError(
            f"US cache ends before {end} (benchmark last bar {last}); "
            "fetch this period into a fresh directory"
        )
    return (
        frames,
        bench,
        {"requested": len(symbols), "obtained": len(frames), "missing": len(missing)},
    )


def load_crypto(
    root: Path, pit_csv: Path, *, end: str
) -> tuple[dict, pd.DataFrame, dict]:
    """Binance USDT spot exploration-split candles only (holdout files are never opened)."""
    stop = pd.Timestamp(end)
    flags = {
        r["symbol"]: r
        for r in csv.DictReader(pit_csv.open())
        if r["venue"] == "binance_usdt_spot"
    }
    frames, dropped = {}, {"leveraged_token": 0, "stable_base": 0, "not_in_pit": 0}
    directory = root / "candles" / "binance_usdt_spot" / "exploration"
    for path in sorted(directory.glob("*.parquet")):
        symbol = path.stem
        flag = flags.get(symbol)
        if flag is None:
            dropped["not_in_pit"] += 1
            continue
        if flag["is_leveraged_token"] == "true":
            dropped["leveraged_token"] += 1
            continue
        if flag["is_stable_base"] == "true":
            dropped["stable_base"] += 1
            continue
        raw = pd.read_parquet(path)
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(raw.day),
                **{
                    c: raw[c].astype(float)
                    for c in ("open", "high", "low", "close", "volume")
                },
                "traded_value": raw.quote_volume.astype(float),
            }
        )
        frame = frame[frame.date <= stop].drop_duplicates("date").set_index("date")
        if not frame.empty:
            frames[symbol] = frame[BAR_COLUMNS]
    bench = frames[spec.BENCHMARK["crypto"]]
    return frames, bench, {"pairs": len(frames), "dropped": dropped}
