"""Exact candidate signal and pre-registered ablation evaluation.

Each implementation receives a source-parsed :class:`CandidateDefinition`.
The formulas name their upstream parameters by key; their numerical values are
read from the verbatim registry rather than transcribed into Python constants.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType
from typing import Literal

from .registry import CandidateDefinition, CandidateParseError
from .source import DailyBar

__all__ = [
    "Arm",
    "SignalEvaluation",
    "evaluate_signal",
    "nearest_rank_quantile",
]


Arm = Literal["full", "ablation"]


@dataclass(frozen=True)
class SignalEvaluation:
    """One symbol-day evaluation, including serialisable intermediate stages."""

    strategy_id: str
    contract_hash: str
    arm: Arm
    venue: str
    symbol: str
    signal_session: date
    eligible: bool
    signal: bool
    exclusion_reason: str | None
    stages: Mapping[str, bool]
    metrics: Mapping[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "arm": self.arm,
            "venue": self.venue,
            "symbol": self.symbol,
            "signal_session": self.signal_session.isoformat(),
            "eligible": self.eligible,
            "signal": self.signal,
            "exclusion_reason": self.exclusion_reason,
            "stages": dict(sorted(self.stages.items())),
            "metrics": dict(sorted(self.metrics.items())),
        }


def nearest_rank_quantile(values: Sequence[float], probability: float) -> float:
    """Return Q_p = sorted[ceil(p*n)-1], refusing non-finite inputs.

    The caller must pass a historical sample only.  This helper intentionally
    has no current-bar argument so it cannot silently include one.
    """
    if not values:
        raise ValueError("nearest-rank quantile requires at least one value")
    if not 0.0 < probability <= 1.0:
        raise ValueError("nearest-rank probability must be in (0, 1]")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("nearest-rank quantile rejects non-finite samples")
    rank = math.ceil(probability * len(values))
    return sorted(values)[rank - 1]


def evaluate_signal(
    candidate: CandidateDefinition,
    history: Sequence[DailyBar | None],
    *,
    arm: Arm,
) -> SignalEvaluation:
    """Evaluate exactly one candidate/arm against one contiguous symbol history."""
    if arm not in {"full", "ablation"}:
        raise ValueError(f"unsupported signal arm: {arm!r}")
    venue, symbol, session = _history_identity(history)
    invalid_history_reason = _history_problem(candidate, history)
    if invalid_history_reason is not None:
        return _excluded(
            candidate,
            arm=arm,
            venue=venue,
            symbol=symbol,
            session=session,
            reason=invalid_history_reason,
        )

    bars = tuple(bar for bar in history if bar is not None)
    if candidate.strategy_id == "CR-SPOT-ETR-01":
        return _evaluate_etr(candidate, bars, arm=arm)
    if candidate.strategy_id == "CR-SPOT-TPR-01":
        return _evaluate_tpr(candidate, bars, arm=arm)
    if candidate.strategy_id == "CR-SPOT-CEB-01":
        return _evaluate_ceb(candidate, bars, arm=arm)
    raise CandidateParseError(
        f"no implementation exists for strategy_id={candidate.strategy_id!r}; "
        "no family/name fallback is permitted"
    )


def _history_identity(
    history: Sequence[DailyBar | None],
) -> tuple[str, str, date]:
    present = next((bar for bar in reversed(history) if bar is not None), None)
    if present is None:
        return "", "", date.min
    return present.venue, present.symbol, present.session


def _history_problem(
    candidate: CandidateDefinition,
    history: Sequence[DailyBar | None],
) -> str | None:
    if len(history) != candidate.required_history_days:
        return "required_history_length_mismatch"
    if any(bar is None for bar in history):
        return "missing_required_history"
    bars = tuple(bar for bar in history if bar is not None)
    first = bars[0]
    if any(bar.venue != first.venue or bar.symbol != first.symbol for bar in bars):
        return "mixed_venue_or_symbol_history"
    for previous, current in zip(bars, bars[1:], strict=False):
        if current.session != previous.session + timedelta(days=1):
            return "non_contiguous_required_history"
    return None


def _excluded(
    candidate: CandidateDefinition,
    *,
    arm: Arm,
    venue: str,
    symbol: str,
    session: date,
    reason: str,
) -> SignalEvaluation:
    return SignalEvaluation(
        strategy_id=candidate.strategy_id,
        contract_hash=candidate.contract_hash,
        arm=arm,
        venue=venue,
        symbol=symbol,
        signal_session=session,
        eligible=False,
        signal=False,
        exclusion_reason=reason,
        stages=MappingProxyType({}),
        metrics=MappingProxyType({}),
    )


def _result(
    candidate: CandidateDefinition,
    bars: Sequence[DailyBar],
    *,
    arm: Arm,
    eligible: bool,
    signal: bool,
    reason: str | None,
    stages: Mapping[str, bool],
    metrics: Mapping[str, float],
) -> SignalEvaluation:
    current = bars[-1]
    return SignalEvaluation(
        strategy_id=candidate.strategy_id,
        contract_hash=candidate.contract_hash,
        arm=arm,
        venue=current.venue,
        symbol=current.symbol,
        signal_session=current.session,
        eligible=eligible,
        signal=signal,
        exclusion_reason=reason,
        stages=MappingProxyType(dict(stages)),
        metrics=MappingProxyType(dict(metrics)),
    )


def _is_finite_positive(value: float) -> bool:
    return math.isfinite(value) and value > 0.0


def _valid_ohlc(bar: DailyBar, *, require_nonflat: bool = False) -> bool:
    if not all(
        _is_finite_positive(value) for value in (bar.open, bar.high, bar.low, bar.close)
    ):
        return False
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
        return False
    if require_nonflat and bar.high <= bar.low:
        return False
    return bar.high >= bar.low


def _valid_closes(bars: Sequence[DailyBar]) -> bool:
    return all(_is_finite_positive(bar.close) for bar in bars)


def _median(values: Sequence[float]) -> float:
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("median requires finite values")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _int_parameter(candidate: CandidateDefinition, name: str) -> int:
    value = candidate.parameter(name)
    if isinstance(value, bool) or int(value) != value:
        raise CandidateParseError(
            f"{candidate.strategy_id}: parameter {name!r} must be an integer"
        )
    return int(value)


def _float_parameter(candidate: CandidateDefinition, name: str) -> float:
    value = float(candidate.parameter(name))
    if not math.isfinite(value):
        raise CandidateParseError(
            f"{candidate.strategy_id}: parameter {name!r} must be finite"
        )
    return value


def _evaluate_etr(
    candidate: CandidateDefinition,
    bars: Sequence[DailyBar],
    *,
    arm: Arm,
) -> SignalEvaluation:
    return_days = _int_parameter(candidate, "return_history_days")
    volume_days = _int_parameter(candidate, "quote_volume_median_days")
    if len(bars) != return_days + 2:
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="etr_required_history_contract_mismatch",
            stages={},
            metrics={},
        )
    if not _valid_closes(bars):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_required_close_history",
            stages={},
            metrics={},
        )

    previous_returns = tuple(
        math.log(bars[index].close / bars[index - 1].close)
        for index in range(1, len(bars) - 1)
    )
    if len(previous_returns) != return_days:
        raise CandidateParseError("ETR historical return sample length drift")
    q10 = nearest_rank_quantile(
        previous_returns,
        _float_parameter(candidate, "return_tail_quantile"),
    )
    current = bars[-1]
    r1 = math.log(current.close / bars[-2].close)
    tail_day = r1 <= q10
    metrics: dict[str, float] = {"r1": r1, "q10_r1": q10}

    if arm == "ablation":
        _populate_etr_ablation_ranking_metrics(
            candidate,
            bars,
            q10=q10,
            r1=r1,
            metrics=metrics,
        )
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=True,
            signal=tail_day,
            reason=None,
            stages={"tail_day": tail_day},
            metrics=metrics,
        )

    if not _valid_ohlc(current, require_nonflat=True):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_current_ohlc",
            stages={"tail_day": tail_day},
            metrics=metrics,
        )
    if not (
        _is_finite_positive(current.base_volume)
        and _is_finite_positive(current.quote_volume)
    ):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_current_volume",
            stages={"tail_day": tail_day},
            metrics=metrics,
        )
    volume_sample = tuple(bar.quote_volume for bar in bars[-(volume_days + 1) : -1])
    if len(volume_sample) != volume_days:
        raise CandidateParseError("ETR quote-volume sample length drift")
    try:
        volume_median = _median(volume_sample)
    except ValueError:
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_quote_volume_history",
            stages={"tail_day": tail_day},
            metrics=metrics,
        )
    if not _is_finite_positive(volume_median):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="nonpositive_quote_volume_median",
            stages={"tail_day": tail_day},
            metrics=metrics,
        )
    price_range = current.high - current.low
    clv = (current.close - current.low) / price_range
    lower_wick = (min(current.open, current.close) - current.low) / price_range
    qv_ratio = current.quote_volume / volume_median
    metrics.update(
        {
            "clv": clv,
            "lower_wick": lower_wick,
            "qv_ratio": qv_ratio,
            "tail_severity": q10 - r1,
        }
    )
    clv_ok = clv >= _float_parameter(candidate, "close_location_min")
    wick_ok = lower_wick >= _float_parameter(candidate, "lower_wick_fraction_min")
    volume_ok = qv_ratio >= _float_parameter(candidate, "quote_volume_ratio_min")
    return _result(
        candidate,
        bars,
        arm=arm,
        eligible=True,
        signal=tail_day and clv_ok and wick_ok and volume_ok,
        reason=None,
        stages={
            "tail_day": tail_day,
            "close_location": clv_ok,
            "lower_wick": wick_ok,
            "quote_volume": volume_ok,
        },
        metrics=metrics,
    )


def _populate_etr_ablation_ranking_metrics(
    candidate: CandidateDefinition,
    bars: Sequence[DailyBar],
    *,
    q10: float,
    r1: float,
    metrics: dict[str, float],
) -> None:
    """Populate only valid ETR ranking inputs without changing ablation admission.

    The pre-registered ablation remains ``tail_day`` alone.  Ranking is a
    separate execution concern, so the full-arm thresholds are intentionally
    not consulted here.  Inputs that fail the full path's existing raw-data
    validity checks remain absent and therefore retain the engine's established
    last-place treatment for missing ranking values.
    """
    metrics["tail_severity"] = q10 - r1
    current = bars[-1]
    if not _valid_ohlc(current, require_nonflat=True):
        return
    if not (
        _is_finite_positive(current.base_volume)
        and _is_finite_positive(current.quote_volume)
    ):
        return

    volume_days = _int_parameter(candidate, "quote_volume_median_days")
    volume_sample = tuple(bar.quote_volume for bar in bars[-(volume_days + 1) : -1])
    if len(volume_sample) != volume_days:
        raise CandidateParseError("ETR quote-volume sample length drift")
    try:
        volume_median = _median(volume_sample)
    except ValueError:
        return
    if not _is_finite_positive(volume_median):
        return

    price_range = current.high - current.low
    metrics.update(
        {
            "clv": (current.close - current.low) / price_range,
            "qv_ratio": current.quote_volume / volume_median,
        }
    )


def _sma(closes: Sequence[float], days: int) -> float:
    if len(closes) != days:
        raise ValueError("SMA sample length drift")
    return sum(closes) / days


def _evaluate_tpr(
    candidate: CandidateDefinition,
    bars: Sequence[DailyBar],
    *,
    arm: Arm,
) -> SignalEvaluation:
    trend_days = _int_parameter(candidate, "long_trend_sma_days")
    slope_lag = _int_parameter(candidate, "long_trend_slope_lag_days")
    pullback_days = _int_parameter(candidate, "pullback_sma_days")
    integrity_days = _int_parameter(candidate, "pullback_integrity_window_days")
    breakout_days = _int_parameter(candidate, "breakout_exclusion_days")
    volume_days = _int_parameter(candidate, "quote_volume_median_days")
    if len(bars) != trend_days + slope_lag:
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="tpr_required_history_contract_mismatch",
            stages={},
            metrics={},
        )
    if not all(_valid_ohlc(bar) for bar in bars):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_required_price_history",
            stages={},
            metrics={},
        )

    closes = tuple(bar.close for bar in bars)
    current = bars[-1]
    sma100 = _sma(closes[-trend_days:], trend_days)
    sma100_lagged = _sma(closes[-(trend_days + slope_lag) : -slope_lag], trend_days)
    sma20_current = _sma(closes[-pullback_days:], pullback_days)
    sma20_previous = _sma(closes[-(pullback_days + 1) : -1], pullback_days)
    prior_high = max(bar.high for bar in bars[-(breakout_days + 1) : -1])
    # The upstream interval is i = t-5, ..., t: five elapsed day lags and
    # therefore six inclusive observations.  Do not collapse it to five rows.
    integrity_smas = tuple(
        _sma(closes[index - trend_days + 1 : index + 1], trend_days)
        for index in range(len(bars) - integrity_days - 1, len(bars))
    )
    integrity_min = min(
        bar.close - sma_value
        for bar, sma_value in zip(
            bars[-(integrity_days + 1) :], integrity_smas, strict=True
        )
    )
    trend_state = sma100 > sma100_lagged and current.close > sma100
    cross_back_above = (
        bars[-2].close <= sma20_previous and current.close > sma20_current
    )
    trend_integrity = integrity_min >= 0.0
    green_candle = current.close > current.open
    not_breakout = current.close <= prior_high
    metrics: dict[str, float] = {
        "sma100": sma100,
        "sma100_lagged": sma100_lagged,
        "sma20": sma20_current,
        "prior_20d_high": prior_high,
        "trend_slope": sma100 / sma100_lagged - 1.0,
        "pullback_extension": current.close / sma20_current - 1.0,
        "integrity_min": integrity_min,
    }

    if arm == "ablation":
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=True,
            signal=trend_state,
            reason=None,
            stages={"trend_state": trend_state},
            metrics=metrics,
        )

    if not (
        _is_finite_positive(current.base_volume)
        and _is_finite_positive(current.quote_volume)
    ):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_current_volume",
            stages={"trend_state": trend_state},
            metrics=metrics,
        )
    volume_sample = tuple(bar.quote_volume for bar in bars[-(volume_days + 1) : -1])
    try:
        volume_median = _median(volume_sample)
    except ValueError:
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_quote_volume_history",
            stages={"trend_state": trend_state},
            metrics=metrics,
        )
    if not _is_finite_positive(volume_median):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="nonpositive_quote_volume_median",
            stages={"trend_state": trend_state},
            metrics=metrics,
        )
    qv_ratio = current.quote_volume / volume_median
    volume_ok = qv_ratio >= _float_parameter(candidate, "quote_volume_ratio_min")
    pullback_setup = (
        trend_state
        and cross_back_above
        and trend_integrity
        and green_candle
        and not_breakout
    )
    metrics["qv_ratio"] = qv_ratio
    return _result(
        candidate,
        bars,
        arm=arm,
        eligible=True,
        signal=pullback_setup and volume_ok,
        reason=None,
        stages={
            "trend_state": trend_state,
            "cross_back_above": cross_back_above,
            "trend_integrity": trend_integrity,
            "green_candle": green_candle,
            "not_breakout": not_breakout,
            "pullback_setup": pullback_setup,
            "quote_volume": volume_ok,
        },
        metrics=metrics,
    )


def _normalized_true_range(current: DailyBar, previous_close: float) -> float:
    return (
        max(
            current.high - current.low,
            abs(current.high - previous_close),
            abs(current.low - previous_close),
        )
        / previous_close
    )


def _evaluate_ceb(
    candidate: CandidateDefinition,
    bars: Sequence[DailyBar],
    *,
    arm: Arm,
) -> SignalEvaluation:
    atr_days = _int_parameter(candidate, "normalized_true_range_window_days")
    reference_count = _int_parameter(candidate, "compression_reference_count")
    breakout_days = _int_parameter(candidate, "breakout_lookback_days")
    volume_days = _int_parameter(candidate, "quote_volume_median_days")
    expected_history = reference_count + atr_days + 2
    if len(bars) != expected_history:
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="ceb_required_history_contract_mismatch",
            stages={},
            metrics={},
        )
    if not all(_valid_ohlc(bar) for bar in bars):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_required_price_history",
            stages={},
            metrics={},
        )

    ntr = (0.0,) + tuple(
        _normalized_true_range(bar, previous.close)
        for previous, bar in zip(bars, bars[1:], strict=False)
    )
    atr20 = tuple(
        _sma(ntr[index - atr_days + 1 : index + 1], atr_days)
        if index >= atr_days
        else math.nan
        for index in range(len(ntr))
    )
    current_index = len(bars) - 1
    reference_start = current_index - reference_count - 1
    reference_end = current_index - 1
    reference = tuple(atr20[index] for index in range(reference_start, reference_end))
    if len(reference) != reference_count or not all(
        math.isfinite(value) for value in reference
    ):
        raise CandidateParseError("CEB compression reference sample length drift")
    cutoff = nearest_rank_quantile(
        reference,
        _float_parameter(candidate, "compression_quantile"),
    )
    current = bars[-1]
    prior_bars = bars[-(breakout_days + 1) : -1]
    prior_high = max(bar.high for bar in prior_bars)
    ntr_history = ntr[-(breakout_days + 1) : -1]
    current_ntr = ntr[-1]
    clv_denominator = current.high - current.low
    if clv_denominator <= 0.0:
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="flat_current_range",
            stages={},
            metrics={},
        )
    clv = (current.close - current.low) / clv_denominator
    compression_state = atr20[-2] <= cutoff
    raw_breakout = current.close > prior_high
    metrics: dict[str, float] = {
        "atr20_t_minus_1": atr20[-2],
        "compression_cutoff": cutoff,
        "prior_20d_high": prior_high,
        "breakout_extension": current.close / prior_high - 1.0,
        "clv": clv,
    }
    if arm == "ablation":
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=True,
            signal=raw_breakout,
            reason=None,
            stages={
                "compression_state": compression_state,
                "raw_20d_breakout": raw_breakout,
            },
            metrics=metrics,
        )

    if not _is_finite_positive(current.quote_volume):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_current_quote_volume",
            stages={
                "compression_state": compression_state,
                "raw_20d_breakout": raw_breakout,
            },
            metrics=metrics,
        )
    try:
        ntr_median = _median(ntr_history)
        volume_median = _median(
            tuple(bar.quote_volume for bar in bars[-(volume_days + 1) : -1])
        )
    except ValueError:
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="invalid_ntr_or_quote_volume_history",
            stages={
                "compression_state": compression_state,
                "raw_20d_breakout": raw_breakout,
            },
            metrics=metrics,
        )
    if not (_is_finite_positive(ntr_median) and _is_finite_positive(volume_median)):
        return _result(
            candidate,
            bars,
            arm=arm,
            eligible=False,
            signal=False,
            reason="nonpositive_ntr_or_quote_volume_median",
            stages={
                "compression_state": compression_state,
                "raw_20d_breakout": raw_breakout,
            },
            metrics=metrics,
        )
    range_ratio = current_ntr / ntr_median
    qv_ratio = current.quote_volume / volume_median
    clv_ok = clv >= _float_parameter(candidate, "close_location_min")
    range_ok = range_ratio >= _float_parameter(candidate, "range_expansion_ratio_min")
    volume_ok = qv_ratio >= _float_parameter(candidate, "quote_volume_ratio_min")
    metrics.update({"range_ratio": range_ratio, "qv_ratio": qv_ratio})
    return _result(
        candidate,
        bars,
        arm=arm,
        eligible=True,
        signal=compression_state and raw_breakout and clv_ok and range_ok and volume_ok,
        reason=None,
        stages={
            "compression_state": compression_state,
            "raw_20d_breakout": raw_breakout,
            "close_location": clv_ok,
            "range_expansion": range_ok,
            "quote_volume": volume_ok,
        },
        metrics=metrics,
    )
