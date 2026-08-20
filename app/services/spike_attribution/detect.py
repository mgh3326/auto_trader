"""ROB-1303 spike detection + evidence-window arithmetic (pure).

stdlib only, plus the repo's existing halt-suspicion classifier: no DB, no
network, no clock. Everything the caller needs is injected, so the pre-
registered spike definition can be unit-tested against hand-written bars.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.services.halt_detection import HaltBar, classify_bars
from app.services.spike_attribution.contract import DailyBar, SpikeEvent
from app.services.spike_attribution.spec import PRE_REGISTRATION

_DETECTION = PRE_REGISTRATION["spike_detection"]
_WINDOW = PRE_REGISTRATION["evidence_window"]

ABS_CHANGE_PCT_MIN: Decimal = Decimal(str(_DETECTION["abs_change_pct_min"]))
BASIS_CLOSE_TO_CLOSE = "close_to_close"
BASIS_INTRADAY_EXTREME = "intraday_extreme"

# Bars this detector needs behind the spike day before it can rule on halt
# suspicion. Fewer bars is not evidence of liveness — see ``HaltSuspicion``.
HALT_LOOKBACK_BARS = 3

_HUNDRED = Decimal("100")


class SpikeDetectionError(ValueError):
    """Raised when the caller's inputs cannot support a deterministic verdict."""


def session_close_at(market: str, session_date: dt.date) -> dt.datetime:
    """Local session close for ``session_date``, as a tz-aware datetime."""

    try:
        hhmm = _WINDOW["session_close_local"][market]
        tzname = _WINDOW["session_close_tz"][market]
    except KeyError as exc:  # pragma: no cover - guarded by callers
        raise SpikeDetectionError(f"unsupported market: {market!r}") from exc
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime.combine(
        session_date, dt.time(hour=hour, minute=minute), tzinfo=ZoneInfo(tzname)
    )


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator * _HUNDRED).quantize(Decimal("0.0001"))


def classify_bar(
    *,
    market: str,
    symbol: str,
    bar: DailyBar,
    prev_bar: DailyBar,
) -> SpikeEvent | None:
    """Return a :class:`SpikeEvent` when ``bar`` clears the pinned threshold.

    ``prev_bar`` must be the immediately preceding row of the same daily
    series; the caller owns that ordering because only it knows the calendar.
    """

    prev_close = prev_bar.close
    if prev_close <= 0:
        raise SpikeDetectionError(
            f"{symbol}: non-positive prev_close {prev_close} cannot yield a percentage"
        )

    c2c = _pct(bar.close - prev_close, prev_close)
    up_pct = _pct(bar.high - prev_close, prev_close)
    down_pct = _pct(bar.low - prev_close, prev_close)
    # The intraday extreme is whichever end travelled further from prev_close.
    extreme = up_pct if abs(up_pct) >= abs(down_pct) else down_pct

    triggered: list[str] = []
    if abs(c2c) >= ABS_CHANGE_PCT_MIN:
        triggered.append(BASIS_CLOSE_TO_CLOSE)
    if abs(extreme) >= ABS_CHANGE_PCT_MIN:
        triggered.append(BASIS_INTRADAY_EXTREME)
    if not triggered:
        return None

    # Direction describes the *session outcome*, not the basis that fired,
    # because the follow-through anchor is the close: a bar that gapped +6% and
    # closed -1% is a down session, and calling it "up" would flip the sign of
    # the retention denominator against its own label. Only a genuinely
    # unchanged close falls back to the intraday extreme that triggered it.
    lead = c2c if c2c != 0 else extreme
    direction = "up" if lead > 0 else "down"

    return SpikeEvent(
        market=market,
        symbol=symbol,
        session_date=bar.session_date,
        direction=direction,
        prev_close=prev_close,
        close=bar.close,
        high=bar.high,
        low=bar.low,
        close_to_close_pct=c2c,
        intraday_extreme_pct=extreme,
        triggered_bases=tuple(triggered),
        window_start_exclusive=session_close_at(market, prev_bar.session_date),
        window_end_inclusive=session_close_at(market, bar.session_date),
    )


def detect_spikes(
    *,
    market: str,
    symbol: str,
    bars: list[DailyBar],
    session_date: dt.date,
) -> tuple[SpikeEvent | None, dict[str, object]]:
    """Detect a spike on ``session_date`` from an ascending daily series.

    Returns ``(event_or_none, diagnostics)``. ``diagnostics`` always carries the
    halt-suspicion verdict so an exclusion is reported with its symbol and its
    reason rather than silently dropped (ROB-1236).
    """

    ordered = sorted(bars, key=lambda row: row.session_date)
    dates = [row.session_date for row in ordered]
    if len(set(dates)) != len(dates):
        raise SpikeDetectionError(f"{symbol}: duplicate session dates in daily series")
    try:
        idx = dates.index(session_date)
    except ValueError:
        return None, {"skipped": "no_bar_for_session_date"}
    if idx == 0:
        return None, {"skipped": _DETECTION["no_prev_close_verdict"]}

    lookback = ordered[max(0, idx + 1 - HALT_LOOKBACK_BARS) : idx + 1]
    halt = classify_bars(
        [
            HaltBar(
                close=row.close,
                high=row.high,
                low=row.low,
                volume=row.volume,
                open=row.open,
            )
            for row in lookback
        ]
    )
    diagnostics: dict[str, object] = {"halted_suspect": halt.to_dict()}
    if halt.suspected:
        diagnostics["skipped"] = "halted_suspect"
        return None, diagnostics

    event = classify_bar(
        market=market, symbol=symbol, bar=ordered[idx], prev_bar=ordered[idx - 1]
    )
    if event is None:
        diagnostics["skipped"] = "below_threshold"
    return event, diagnostics


__all__ = [
    "ABS_CHANGE_PCT_MIN",
    "BASIS_CLOSE_TO_CLOSE",
    "BASIS_INTRADAY_EXTREME",
    "HALT_LOOKBACK_BARS",
    "SpikeDetectionError",
    "classify_bar",
    "detect_spikes",
    "session_close_at",
]
