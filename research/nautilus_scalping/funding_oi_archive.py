"""ROB-356 (PR1) — pure parsers for Binance USD-M funding + open-interest archives.

Decompressed-CSV in, normalized rows out. The network/zip read is the operator-gated
builder's job (PR3); keeping parsing pure (stdlib, no network) makes the PIT semantics
unit-testable without touching ``data.binance.vision``.

Confirmed archive schemas (read-only public probe, ROB-356):

    futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-YYYY-MM.zip
        calc_time,funding_interval_hours,last_funding_rate
        - calc_time: epoch MS UTC (kept as-is)
        - funding_interval_hours: per-row int (8h->4h changes live in the data)
        - last_funding_rate: realized rate, KNOWN ONLY AT/AFTER calc_time

    futures/um/daily/metrics/{sym}/{sym}-metrics-YYYY-MM-DD.zip
        create_time,symbol,sum_open_interest,sum_open_interest_value,
        count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
        count_long_short_ratio,sum_taker_long_short_vol_ratio
        - create_time: STRING UTC datetime ("YYYY-MM-DD HH:MM:SS"), 5-min grid
        - duplicate (symbol, create_time) rows occur -> deduped (first wins)
        - ratio columns may be blank OR CSV-quoted-empty ("") -> None (OI still parses)

No OHLCV/volume/wick proxy is ever used for OI: ``sum_open_interest`` comes straight
from the metrics archive.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime

_FUNDING_HEADER = "calc_time"
_METRICS_HEADER = "create_time"


@dataclass(frozen=True)
class FundingRow:
    """One realized funding observation. ``last_funding_rate`` is known at/after
    ``calc_time`` (the known-after assumption downstream features must respect)."""

    calc_time: int  # epoch ms UTC
    funding_interval_hours: int
    last_funding_rate: float


@dataclass(frozen=True)
class MetricRow:
    """One open-interest / positioning snapshot. ``create_time`` is the observation
    time (epoch ms UTC). Ratio fields are ``None`` when blank in the archive."""

    create_time: int  # epoch ms UTC
    symbol: str
    sum_open_interest: float
    sum_open_interest_value: float
    count_toptrader_long_short_ratio: float | None
    sum_toptrader_long_short_ratio: float | None
    count_long_short_ratio: float | None
    sum_taker_long_short_vol_ratio: float | None


def _metrics_time_to_epoch_ms(s: str) -> int:
    """Parse the metrics ``create_time`` string as UTC epoch ms."""
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _float_or_none(cell: str) -> float | None:
    cell = cell.strip()
    return float(cell) if cell else None


def _data_rows(text: str, header_token: str):
    """Yield parsed CSV field-lists for non-blank, non-header rows.

    Uses ``csv.reader`` (not ``str.split(",")``) so CSV-quoted-empty cells (``""``,
    common for ratio columns on low-liquidity / delisted symbols) unquote to ``""``
    rather than surviving as the literal 2-char string — see ROB-360/ROB-361.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.split(",", 1)[0] == header_token:  # header
            continue
        lines.append(line)
    return csv.reader(lines)


def parse_funding_csv(text: str) -> list[FundingRow]:
    """Parse a fundingRate CSV; rows sorted ascending by ``calc_time``."""
    rows = [
        FundingRow(
            calc_time=int(c[0]),
            funding_interval_hours=int(c[1]),
            last_funding_rate=float(c[2]),
        )
        for c in _data_rows(text, _FUNDING_HEADER)
    ]
    return sorted(rows, key=lambda r: r.calc_time)


def parse_metrics_csv(text: str) -> list[MetricRow]:
    """Parse a daily metrics CSV; deduped by ``(symbol, create_time)`` (first wins),
    sorted ascending by ``create_time``."""
    seen: set[tuple[str, int]] = set()
    rows: list[MetricRow] = []
    for c in _data_rows(text, _METRICS_HEADER):
        ts = _metrics_time_to_epoch_ms(c[0])
        symbol = c[1].strip()
        key = (symbol, ts)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            MetricRow(
                create_time=ts,
                symbol=symbol,
                sum_open_interest=float(c[2]),
                sum_open_interest_value=float(c[3]),
                count_toptrader_long_short_ratio=_float_or_none(c[4]),
                sum_toptrader_long_short_ratio=_float_or_none(c[5]),
                count_long_short_ratio=_float_or_none(c[6]),
                sum_taker_long_short_vol_ratio=_float_or_none(c[7]),
            )
        )
    return sorted(rows, key=lambda r: r.create_time)
