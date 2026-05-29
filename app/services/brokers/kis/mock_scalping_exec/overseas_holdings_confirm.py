"""ROB-364 — pure normalization helpers for the KIS mock **overseas/US**
holdings-delta confirmed-smoke.

The overseas smoke (``scripts/kis_mock_overseas_holdings_delta_smoke.py``) is the
US counterpart to the domestic ROB-358 smoke. It cannot reuse the domestic
``KisMockBroker`` (which reads ``fetch_domestic_balance_snapshot`` and routes the
cleanup SELL through the KR-only scalping-exit validator), so it talks to the
overseas order client directly and layers these small, broker-free helpers on
top of the shared, market-agnostic ``classify_fill_by_delta`` /
``derive_fill_price`` kernel:

* :func:`extract_overseas_holdings_qty` — per-symbol share count from KIS
  overseas holdings rows (``ovrs_pdno`` / ``ovrs_cblc_qty``), symbol-normalized
  so a KIS-format ``BRK/B`` matches the DB-format ``BRK.B`` the smoke passes.
  Returns ``0`` when the symbol is absent (the holdings read pre-filters to
  nonzero positions, so absence means "we hold zero"); a *read failure* raises
  upstream and the caller fails closed to ``None``.
* :func:`latest_close_from_minute_frame` / :func:`latest_timestamp_from_minute_frame`
  — read the most-recent candle from an overseas minute frame. The frame is
  sorted ascending by ``datetime`` (see ``_base_market_data._build_ohlcv_dataframe``),
  so the latest candle is the LAST row.
* :func:`quote_is_fresh` — fail-closed wall-clock staleness gate (absolute skew,
  so a stale-because-closed bar AND a future-skewed bar both read as not fresh).

USD cash/margin is OPSQ0002-blocked in KIS mock overseas, so the cash-delta
branch of ``derive_fill_price`` is never available here; the smoke passes
``cash=None`` and the fill price is always the submitted limit (``limit_fallback``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.symbol import to_db_symbol


def extract_overseas_holdings_qty(
    rows: Sequence[Mapping[str, Any]], symbol: str
) -> Decimal:
    """Sum the held quantity for ``symbol`` across KIS overseas holdings rows.

    Rows come from ``fetch_my_us_stocks`` / ``fetch_my_overseas_stocks`` and carry
    ``ovrs_pdno`` (KIS-format symbol) and ``ovrs_cblc_qty`` (share count). Both the
    row symbol and the requested ``symbol`` are normalized via ``to_db_symbol`` so
    ``BRK/B`` (KIS) matches ``BRK.B`` (DB). Returns ``Decimal("0")`` when the symbol
    is not present (the holdings read pre-filters to nonzero positions, so absence
    means a zero position — a read *failure* is a raised exception handled by the
    caller, never an empty list).
    """
    target = to_db_symbol(str(symbol))
    total = Decimal("0")
    for row in rows:
        pdno = row.get("ovrs_pdno")
        if pdno is None:
            continue
        if to_db_symbol(str(pdno)) != target:
            continue
        raw = str(row.get("ovrs_cblc_qty", "0")).replace(",", "").strip() or "0"
        total += Decimal(raw)
    return total


def latest_close_from_minute_frame(frame: Any) -> Decimal | None:
    """Return the latest candle's close as a positive ``Decimal``, else ``None``.

    ``None`` (no usable quote) when the frame is empty, lacks a ``close`` column,
    or the latest close is non-positive / unparseable.
    """
    if frame is None or len(frame) == 0 or "close" not in getattr(frame, "columns", []):
        return None
    raw = frame.iloc[-1]["close"]
    try:
        close = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return close if close > 0 else None


def latest_timestamp_from_minute_frame(frame: Any) -> dt.datetime | None:
    """Return the latest candle's ``datetime`` as a python ``datetime``, else ``None``."""
    if (
        frame is None
        or len(frame) == 0
        or "datetime" not in getattr(frame, "columns", [])
    ):
        return None
    raw = frame.iloc[-1]["datetime"]
    to_pydatetime = getattr(raw, "to_pydatetime", None)
    if callable(to_pydatetime):
        return to_pydatetime()
    return raw if isinstance(raw, dt.datetime) else None


def quote_is_fresh(
    latest_ts: dt.datetime,
    now: dt.datetime,
    *,
    max_staleness_seconds: float,
) -> bool:
    """Fail-closed quote freshness gate.

    Fresh iff the latest candle is within ``max_staleness_seconds`` of ``now`` in
    *absolute* terms, so both a market-closed (far-past) bar and a clock/tz-skewed
    (far-future) bar read as not fresh. ``latest_ts`` and ``now`` must both be
    tz-aware or both naive (the caller localizes the naive exchange timestamp).
    """
    return abs((now - latest_ts).total_seconds()) <= max_staleness_seconds


__all__ = [
    "extract_overseas_holdings_qty",
    "latest_close_from_minute_frame",
    "latest_timestamp_from_minute_frame",
    "quote_is_fresh",
]
