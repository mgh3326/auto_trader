"""ROB-356 (PR2) — PIT-safe funding + open-interest feature construction (pure).

Joins parsed funding rows (``funding_oi_archive.FundingRow``) onto the open-interest
metrics grid (``MetricRow``) under strict point-in-time rules so a later
crowding/deleveraging study cannot see the future or a survivor-only tail.

Join rule (locked in the ROB-356 plan):
  * canonical feature grid = OI metrics timestamps (5-min, UTC) — the dense crowding
    axis. Funding (monthly, ~3/day) is the sparse context, not the grid.
  * funding attached by BACKWARD as-of join: for an OI row at ``t`` take the most
    recent funding row with ``calc_time <= t``. Realized ``last_funding_rate`` and the
    per-row ``funding_interval_hours`` therefore reach an OI row only at/after the
    funding ``calc_time`` (known-after). Rows before the first funding carry None.
  * ``delisted_at`` is EXCLUSIVE: rows at/after it are dropped (frozen-tail guard).
  * per-symbol OI-start bounding is implicit — the grid starts at the first OI row, so
    funding observations earlier than OI simply have no row to attach to.

OI features come straight from ``sum_open_interest`` (actual exchange OI). No
OHLCV/volume/wick proxy is used for OI. Rolling z-scores use population std and are
bounded to 0.0 on a zero-variance window (never NaN/inf).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from funding_oi_archive import FundingRow, MetricRow


@dataclass(frozen=True)
class FeatureRow:
    ts: int  # epoch ms UTC == OI create_time (the PIT observation time)
    symbol: str
    # --- open interest (actual exchange OI) ---
    sum_open_interest: float
    sum_open_interest_value: float
    oi_delta: float | None
    oi_pct_change: float | None
    oi_zscore: float | None
    # --- positioning passthrough (from the metrics archive) ---
    count_toptrader_long_short_ratio: float | None
    sum_toptrader_long_short_ratio: float | None
    count_long_short_ratio: float | None
    sum_taker_long_short_vol_ratio: float | None
    # --- funding (backward as-of, known-after) ---
    funding_calc_time: int | None
    last_funding_rate: float | None
    funding_interval_hours: int | None
    funding_rate_zscore: float | None


def trim_metrics(rows: list[MetricRow], delisted_at: int | None) -> list[MetricRow]:
    """Drop OI rows at/after ``delisted_at`` (EXCLUSIVE). ``None`` keeps all."""
    if delisted_at is None:
        return rows
    return [r for r in rows if r.create_time < delisted_at]


def trim_funding(rows: list[FundingRow], delisted_at: int | None) -> list[FundingRow]:
    """Drop funding rows at/after ``delisted_at`` (EXCLUSIVE). ``None`` keeps all."""
    if delisted_at is None:
        return rows
    return [r for r in rows if r.calc_time < delisted_at]


def asof_funding(ts: int, funding: list[FundingRow]) -> FundingRow | None:
    """Most recent funding row with ``calc_time <= ts``; ``None`` if none yet known.

    ``funding`` must be ascending by ``calc_time`` (the parser guarantees this).
    """
    times = [f.calc_time for f in funding]
    i = bisect.bisect_right(times, ts)
    return funding[i - 1] if i > 0 else None


def _rolling_zscore(values: list[float], window: int) -> list[float | None]:
    """Causal rolling z-score (population std) over the trailing ``window`` values.

    ``None`` until ``window`` observations exist; ``0.0`` on a zero-variance window
    (bounded, never NaN/inf).
    """
    out: list[float | None] = []
    for idx in range(len(values)):
        if idx + 1 < window:
            out.append(None)
            continue
        win = values[idx + 1 - window : idx + 1]
        mean = sum(win) / window
        var = sum((v - mean) ** 2 for v in win) / window
        if var <= 0.0:
            out.append(0.0)
        else:
            out.append((values[idx] - mean) / var**0.5)
    return out


def build_features(
    symbol: str,
    funding: list[FundingRow],
    metrics: list[MetricRow],
    delisted_at: int | None = None,
    oi_window: int = 30,
    funding_window: int = 30,
) -> list[FeatureRow]:
    """Build PIT-safe funding+OI feature rows on the OI grid.

    ``funding``/``metrics`` are the parsed (ascending) rows for one symbol.
    """
    funding = trim_funding(funding, delisted_at)
    metrics = trim_metrics(metrics, delisted_at)

    # funding-rate causal z-score on the funding series, then as-of attached
    f_z_by_calc: dict[int, float | None] = {}
    if funding:
        zs = _rolling_zscore([f.last_funding_rate for f in funding], funding_window)
        f_z_by_calc = {f.calc_time: z for f, z in zip(funding, zs, strict=True)}

    oi_vals = [m.sum_open_interest for m in metrics]
    oi_z = _rolling_zscore(oi_vals, oi_window)

    feats: list[FeatureRow] = []
    prev_oi: float | None = None
    for m, z in zip(metrics, oi_z, strict=True):
        oi_delta = None if prev_oi is None else m.sum_open_interest - prev_oi
        oi_pct = (
            None
            if prev_oi is None or prev_oi == 0.0
            else (m.sum_open_interest - prev_oi) / prev_oi
        )
        af = asof_funding(m.create_time, funding)
        feats.append(
            FeatureRow(
                ts=m.create_time,
                symbol=symbol,
                sum_open_interest=m.sum_open_interest,
                sum_open_interest_value=m.sum_open_interest_value,
                oi_delta=oi_delta,
                oi_pct_change=oi_pct,
                oi_zscore=z,
                count_toptrader_long_short_ratio=m.count_toptrader_long_short_ratio,
                sum_toptrader_long_short_ratio=m.sum_toptrader_long_short_ratio,
                count_long_short_ratio=m.count_long_short_ratio,
                sum_taker_long_short_vol_ratio=m.sum_taker_long_short_vol_ratio,
                funding_calc_time=af.calc_time if af else None,
                last_funding_rate=af.last_funding_rate if af else None,
                funding_interval_hours=af.funding_interval_hours if af else None,
                funding_rate_zscore=f_z_by_calc.get(af.calc_time) if af else None,
            )
        )
        prev_oi = m.sum_open_interest
    return feats
