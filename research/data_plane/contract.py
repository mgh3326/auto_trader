"""Data contract v1: markets, datasets, the common row fields and the PIT guard.

Every row in every dataset carries the same eight common fields:

``instrument``      symbol in the repo's DB format (KR 6-char code, US dot
                    format ``BRK.B``, crypto venue pair ``BTCUSDT``/``KRW-BTC``)
``venue``           listing venue (``KOSPI``, ``NASDAQ``, ``binance_usdt_spot`` ...)
``event_time``      when the fact happened (bar close, ex-date, listing observed)
``available_at``    the earliest instant a decision process could have known it
``observed_at``     when this collector actually read it from the source
``source``          source identifier (``yahoo_chart``, ``binance_klines`` ...)
``source_version``  version of the source contract this parser understands
``content_hash``    sha256 of the canonical row without ``observed_at``

All timestamps are ISO-8601 strings with an explicit UTC offset.

**Partition date = availability date.** A partition ``date=P`` of a market
holds exactly the rows whose ``available_at`` falls on the local calendar day
``P`` of that market (Asia/Seoul for KR, America/New_York for US, UTC for
crypto). A row whose ``available_at`` is later than ``P`` cannot have been
known on ``P`` and is refused; a row whose ``available_at`` is earlier belongs
to an earlier partition and is refused too. A row observed before it was
available (a forming intraday bar, a skewed clock) is refused. A consumer that
takes only partitions dated ``<= D`` therefore never sees anything that was
unknown on ``D``; the finer join is ``available_at <= decision_time``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CONTRACT_VERSION = "data-plane-v1"

DATASET_UNIVERSE = "universe"
DATASET_BARS = "bars_1d"
DATASET_ACTIONS = "corporate_actions"
DATASETS = (DATASET_UNIVERSE, DATASET_BARS, DATASET_ACTIONS)

COMMON_FIELDS = (
    "instrument",
    "venue",
    "event_time",
    "available_at",
    "observed_at",
    "source",
    "source_version",
    "content_hash",
)

# Dataset-specific fields every row must carry (value may be null only where
# the source has no such field, e.g. a universe row without a security name).
DATASET_FIELDS: Mapping[str, tuple[str, ...]] = {
    DATASET_UNIVERSE: ("listing_status",),
    DATASET_BARS: ("bar_date", "open", "high", "low", "close", "volume"),
    DATASET_ACTIONS: ("action_type",),
}

# Fields that, together with the common identity, make a row unique inside
# one partition. A duplicate key in one partition is refused.
ROW_KEY: Mapping[str, tuple[str, ...]] = {
    DATASET_UNIVERSE: ("instrument", "venue"),
    DATASET_BARS: ("instrument", "venue", "bar_date"),
    DATASET_ACTIONS: (
        "instrument",
        "venue",
        "action_type",
        "event_time",
        "content_hash",
    ),
}

# KR/US daily bars are known at the session close plus this publication lag
# (the same 30-minute convention as kr_delist_repair's 16:00 KST bar time).
EQUITY_BAR_LAG = timedelta(minutes=30)
# A crypto UTC-day candle closes at 00:00 UTC; allow the exchange 5 minutes.
CRYPTO_BAR_LAG = timedelta(minutes=5)


@dataclass(frozen=True)
class MarketSpec:
    name: str
    tz: ZoneInfo
    calendar: str | None  # exchange_calendars code; None = every day


MARKETS: Mapping[str, MarketSpec] = {
    "kr": MarketSpec("kr", ZoneInfo("Asia/Seoul"), "XKRX"),
    "us": MarketSpec("us", ZoneInfo("America/New_York"), "XNYS"),
    "crypto": MarketSpec("crypto", ZoneInfo("UTC"), None),
}


class RefusedRowError(ValueError):
    """A row violates the contract; the partition is not written."""


def check_coverage_floor(value: float) -> float:
    """A coverage floor must be a finite number in [0, 1] (NaN compares false
    with everything and would silently disable the gate)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"coverage floor must be a number, got {value!r}")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(
            f"coverage floor must be finite and within [0, 1], got {value!r}"
        )
    return float(value)


def market_spec(market: str) -> MarketSpec:
    try:
        return MARKETS[market]
    except KeyError as exc:
        raise ValueError(
            f"unknown market {market!r}; expected one of {sorted(MARKETS)}"
        ) from exc


def iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("naive datetime refused; every timestamp needs a UTC offset")
    return ts.astimezone(UTC).isoformat()


def parse_ts(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RefusedRowError(
            f"{field} must be a non-empty ISO-8601 string, got {value!r}"
        )
    try:
        ts = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RefusedRowError(f"{field} is not ISO-8601: {value!r}") from exc
    if ts.tzinfo is None:
        raise RefusedRowError(f"{field} has no UTC offset: {value!r}")
    return ts


def canonical_json(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(row: Mapping[str, Any]) -> str:
    """sha256 of the row without ``observed_at``/``content_hash``.

    ``observed_at`` is excluded so the same fact read on two days hashes the
    same; a changed hash between two observations is a source revision.
    """
    body = {k: v for k, v in row.items() if k not in ("observed_at", "content_hash")}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def make_row(**fields: Any) -> dict[str, Any]:
    row = dict(fields)
    row["content_hash"] = content_hash(row)
    return row


def local_date(ts: datetime, market: str) -> date:
    return ts.astimezone(market_spec(market).tz).date()


def validate_rows(
    market: str, dataset: str, partition_date: date, rows: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Return the rows (as dicts) or raise ``RefusedRowError`` on the first violation."""
    market_spec(market)
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}")
    required = COMMON_FIELDS + DATASET_FIELDS[dataset]
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        missing = [f for f in required if f not in row]
        if missing:
            raise RefusedRowError(f"row lacks {missing}: {dict(row)!r}")
        for field in ("instrument", "venue", "source", "source_version"):
            if not isinstance(row[field], str) or not row[field]:
                raise RefusedRowError(
                    f"{field} must be a non-empty string: {dict(row)!r}"
                )
        parse_ts(row["event_time"], "event_time")
        available_at = parse_ts(row["available_at"], "available_at")
        observed_at = parse_ts(row["observed_at"], "observed_at")
        avail_day = local_date(available_at, market)
        if avail_day > partition_date:
            raise RefusedRowError(
                f"{row['instrument']}: available_at {row['available_at']} is later than "
                f"partition date {partition_date.isoformat()} ({market} local day "
                f"{avail_day.isoformat()}); it could not have been known on that day"
            )
        if avail_day < partition_date:
            raise RefusedRowError(
                f"{row['instrument']}: available_at {row['available_at']} belongs to an "
                f"earlier partition than {partition_date.isoformat()}"
            )
        if observed_at < available_at:
            raise RefusedRowError(
                f"{row['instrument']}: observed_at {row['observed_at']} is before "
                f"available_at {row['available_at']} (forming bar or clock skew)"
            )
        if row["content_hash"] != content_hash(row):
            raise RefusedRowError(
                f"{row['instrument']}: content_hash does not match the row"
            )
        key = tuple(row[f] for f in ROW_KEY[dataset])
        if key in seen:
            raise RefusedRowError(f"duplicate row key {key!r} in one partition")
        seen.add(key)
        out.append(dict(row))
    return out
