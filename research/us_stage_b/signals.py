"""Exact, US-only signal calculations for the frozen Stage-B candidates.

All time offsets refer to the caller-supplied corpus-session index.  Formula
parameters come from the parsed packet binding, while shared US signal helpers
are deliberately neither imported nor consulted.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any

from .registry import CandidateBinding, mandatory_labels
from .source import USStageBDailyBar

__all__ = [
    "ADV20_PRE_WINDOW",
    "SignalObservation",
    "USSignalInputError",
    "evaluate_signal",
    "tie_break_digest",
]


ADV20_PRE_WINDOW = 20
"""Frozen ``ADV20_pre`` window from the packet's common US notation."""


class USSignalInputError(ValueError):
    """Aligned source history cannot be evaluated as a frozen US signal."""


@dataclass(frozen=True)
class SignalObservation:
    """One deterministic signal-stage artifact, including rejected observations."""

    strategy_id: str
    contract_hash: str
    labels: tuple[str, ...]
    symbol: str
    session_date: date
    universe_eligible: bool
    technical_signal: bool
    no_active_position: bool
    signal: bool
    exclusion_reason: str | None
    adv20_pre_proxy: float | None
    tie_break_sha256: str
    metrics: Mapping[str, float]
    stages: Mapping[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": list(self.labels),
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "universe_eligible": self.universe_eligible,
            "technical_signal": self.technical_signal,
            "no_active_position": self.no_active_position,
            "signal": self.signal,
            "exclusion_reason": self.exclusion_reason,
            "adv20_pre_proxy": self.adv20_pre_proxy,
            "tie_break_sha256": self.tie_break_sha256,
            "metrics": dict(self.metrics),
            "stages": dict(self.stages),
        }


def tie_break_digest(
    strategy_id: str,
    session_date: date,
    symbol: str,
) -> bytes:
    """Implement ``sha256(strategy_id || session_date || symbol)`` exactly."""

    return hashlib.sha256(
        f"{strategy_id}{session_date.isoformat()}{symbol}".encode()
    ).digest()


def evaluate_signal(
    candidate: CandidateBinding,
    *,
    symbol: str,
    session_date: date,
    history: Sequence[USStageBDailyBar | None],
    no_active_position: bool,
) -> SignalObservation:
    """Evaluate one candidate using only history through ``session_date``.

    ``history`` must be aligned to the corpus session index and terminate at the
    signal session.  Missing observations remain missing; no interpolation,
    filling, clipping, or cross-sectional ranking occurs in this function.
    """

    if candidate.strategy_id == "US-TS-MOM-CONT-Z126-H20-v1":
        return _evaluate_mom(
            candidate,
            symbol=symbol,
            session_date=session_date,
            history=history,
            no_active_position=no_active_position,
        )
    if candidate.strategy_id == "US-TS-REV-SHORT-Z3-T126-H3-v1":
        return _evaluate_rev(
            candidate,
            symbol=symbol,
            session_date=session_date,
            history=history,
            no_active_position=no_active_position,
        )
    if candidate.strategy_id == "US-TS-VOLBREAK-C55-V2-H10-v1":
        return _evaluate_volbreak(
            candidate,
            symbol=symbol,
            session_date=session_date,
            history=history,
            no_active_position=no_active_position,
        )
    raise USSignalInputError(
        f"unsupported strategy_id {candidate.strategy_id!r}; fallback is forbidden"
    )


def _evaluate_mom(
    candidate: CandidateBinding,
    *,
    symbol: str,
    session_date: date,
    history: Sequence[USStageBDailyBar | None],
    no_active_position: bool,
) -> SignalObservation:
    trend_lookback = _int_parameter(candidate, "trend_lookback_sessions")
    volatility_lookback = _int_parameter(candidate, "trend_vol_lookback_sessions")
    confirmation_lookback = _int_parameter(candidate, "confirmation_lookback_sessions")
    sma_lookback = _int_parameter(candidate, "sma_lookback_sessions")
    required = trend_lookback + 1
    base = _base_observation(
        symbol=symbol,
        session_date=session_date,
        history=history,
        required_close_count=required,
    )
    if base is None:
        return _excluded(
            candidate,
            symbol=symbol,
            session_date=session_date,
            no_active_position=no_active_position,
            reason="insufficient_or_invalid_required_history",
        )
    bars, adv20_pre_proxy = base
    closes = [_close(bar) for bar in bars]
    latest_returns = _one_session_returns(closes[-(volatility_lookback + 1) :])
    sigma = _sample_standard_deviation(latest_returns)
    adv_ok = adv20_pre_proxy >= _float_parameter(candidate, "adv20_min_usd")
    if sigma is None or sigma == 0.0:
        return _observation(
            candidate,
            symbol=symbol,
            session_date=session_date,
            universe_eligible=adv_ok,
            technical_signal=False,
            no_active_position=no_active_position,
            reason="adv20_below_minimum" if not adv_ok else "sigma63_zero_or_invalid",
            adv20_pre_proxy=adv20_pre_proxy,
            metrics={"sigma63_ddof1": sigma} if sigma is not None else {},
            stages={
                "required_history": True,
                "adv20_minimum": adv_ok,
                "sigma_defined": False,
                "no_active_position": no_active_position,
            },
        )
    r126 = closes[-1] / closes[-(trend_lookback + 1)] - 1.0
    r21 = closes[-1] / closes[-(confirmation_lookback + 1)] - 1.0
    sma63 = sum(closes[-sma_lookback:]) / sma_lookback
    z126 = r126 / (sigma * math.sqrt(trend_lookback))
    z_ok = z126 >= _float_parameter(candidate, "trend_z_min")
    r21_ok = r21 > _float_parameter(candidate, "confirmation_return_min")
    sma_ok = closes[-1] > sma63
    technical_signal = adv_ok and z_ok and r21_ok and sma_ok
    return _observation(
        candidate,
        symbol=symbol,
        session_date=session_date,
        universe_eligible=adv_ok,
        technical_signal=technical_signal,
        no_active_position=no_active_position,
        reason=_reason(
            adv_ok=adv_ok,
            technical_signal=technical_signal,
            no_active_position=no_active_position,
        ),
        adv20_pre_proxy=adv20_pre_proxy,
        metrics={
            "r126": r126,
            "sigma63_ddof1": sigma,
            "z126": z126,
            "r21": r21,
            "sma63": sma63,
        },
        stages={
            "required_history": True,
            "adv20_minimum": adv_ok,
            "z126_threshold": z_ok,
            "r21_confirmation": r21_ok,
            "sma63_confirmation": sma_ok,
            "no_active_position": no_active_position,
        },
    )


def _evaluate_rev(
    candidate: CandidateBinding,
    *,
    symbol: str,
    session_date: date,
    history: Sequence[USStageBDailyBar | None],
    no_active_position: bool,
) -> SignalObservation:
    trend_lookback = _int_parameter(candidate, "trend_lookback_sessions")
    shock_lookback = _int_parameter(candidate, "shock_lookback_sessions")
    volatility_lookback = _int_parameter(candidate, "shock_vol_lookback_sessions")
    required = trend_lookback + 1
    base = _base_observation(
        symbol=symbol,
        session_date=session_date,
        history=history,
        required_close_count=required,
    )
    if base is None:
        return _excluded(
            candidate,
            symbol=symbol,
            session_date=session_date,
            no_active_position=no_active_position,
            reason="insufficient_or_invalid_required_history",
        )
    bars, adv20_pre_proxy = base
    closes = [_close(bar) for bar in bars]
    latest_returns = _one_session_returns(closes[-(volatility_lookback + 1) :])
    sigma = _sample_standard_deviation(latest_returns)
    adv_ok = adv20_pre_proxy >= _float_parameter(candidate, "adv20_min_usd")
    if sigma is None or sigma == 0.0:
        return _observation(
            candidate,
            symbol=symbol,
            session_date=session_date,
            universe_eligible=adv_ok,
            technical_signal=False,
            no_active_position=no_active_position,
            reason="adv20_below_minimum" if not adv_ok else "sigma60_zero_or_invalid",
            adv20_pre_proxy=adv20_pre_proxy,
            metrics={"sigma60_ddof1": sigma} if sigma is not None else {},
            stages={
                "required_history": True,
                "adv20_minimum": adv_ok,
                "sigma_defined": False,
                "no_active_position": no_active_position,
            },
        )
    r3 = closes[-1] / closes[-(shock_lookback + 1)] - 1.0
    r126 = closes[-1] / closes[-(trend_lookback + 1)] - 1.0
    z3 = r3 / (sigma * math.sqrt(shock_lookback))
    z_ok = z3 <= _float_parameter(candidate, "shock_z_max")
    trend_ok = r126 > _float_parameter(candidate, "trend_return_min")
    technical_signal = adv_ok and z_ok and trend_ok
    return _observation(
        candidate,
        symbol=symbol,
        session_date=session_date,
        universe_eligible=adv_ok,
        technical_signal=technical_signal,
        no_active_position=no_active_position,
        reason=_reason(
            adv_ok=adv_ok,
            technical_signal=technical_signal,
            no_active_position=no_active_position,
        ),
        adv20_pre_proxy=adv20_pre_proxy,
        metrics={
            "r3": r3,
            "sigma60_ddof1": sigma,
            "z3": z3,
            "r126": r126,
        },
        stages={
            "required_history": True,
            "adv20_minimum": adv_ok,
            "z3_threshold": z_ok,
            "r126_trend": trend_ok,
            "no_active_position": no_active_position,
        },
    )


def _evaluate_volbreak(
    candidate: CandidateBinding,
    *,
    symbol: str,
    session_date: date,
    history: Sequence[USStageBDailyBar | None],
    no_active_position: bool,
) -> SignalObservation:
    breakout_lookback = _int_parameter(candidate, "breakout_lookback_sessions")
    volume_lookback = _int_parameter(candidate, "volume_lookback_sessions")
    required = breakout_lookback + 1
    base = _base_observation(
        symbol=symbol,
        session_date=session_date,
        history=history,
        required_close_count=required,
    )
    if base is None:
        return _excluded(
            candidate,
            symbol=symbol,
            session_date=session_date,
            no_active_position=no_active_position,
            reason="insufficient_or_invalid_required_history",
        )
    bars, adv20_pre_proxy = base
    closes = [_close(bar) for bar in bars]
    current_volume = bars[-1].volume
    previous_volumes = [bar.volume for bar in bars[-(volume_lookback + 1) : -1]]
    adv_ok = adv20_pre_proxy >= _float_parameter(candidate, "adv20_min_usd")
    if not _finite_positive(current_volume) or not all(
        _finite_positive(value) for value in previous_volumes
    ):
        return _observation(
            candidate,
            symbol=symbol,
            session_date=session_date,
            universe_eligible=adv_ok,
            technical_signal=False,
            no_active_position=no_active_position,
            reason=(
                "adv20_below_minimum"
                if not adv_ok
                else "invalid_current_or_prior_volume"
            ),
            adv20_pre_proxy=adv20_pre_proxy,
            metrics={},
            stages={
                "required_history": True,
                "adv20_minimum": adv_ok,
                "volume_ratio_defined": False,
                "no_active_position": no_active_position,
            },
        )
    median_volume = _median([float(value) for value in previous_volumes])
    if median_volume == 0.0:
        return _observation(
            candidate,
            symbol=symbol,
            session_date=session_date,
            universe_eligible=adv_ok,
            technical_signal=False,
            no_active_position=no_active_position,
            reason="adv20_below_minimum" if not adv_ok else "median_volume_zero",
            adv20_pre_proxy=adv20_pre_proxy,
            metrics={},
            stages={
                "required_history": True,
                "adv20_minimum": adv_ok,
                "volume_ratio_defined": False,
                "no_active_position": no_active_position,
            },
        )
    prior_close_high55 = max(closes[-(breakout_lookback + 1) : -1])
    r1 = closes[-1] / closes[-2] - 1.0
    volume_ratio20 = float(current_volume) / median_volume
    close_breakout = closes[-1] > prior_close_high55
    r1_ok = r1 > _float_parameter(candidate, "daily_return_min")
    volume_ok = volume_ratio20 >= _float_parameter(
        candidate, "volume_ratio_min"
    ) and volume_ratio20 <= _float_parameter(candidate, "volume_ratio_max")
    technical_signal = adv_ok and close_breakout and r1_ok and volume_ok
    return _observation(
        candidate,
        symbol=symbol,
        session_date=session_date,
        universe_eligible=adv_ok,
        technical_signal=technical_signal,
        no_active_position=no_active_position,
        reason=_reason(
            adv_ok=adv_ok,
            technical_signal=technical_signal,
            no_active_position=no_active_position,
        ),
        adv20_pre_proxy=adv20_pre_proxy,
        metrics={
            "prior_close_high55": prior_close_high55,
            "r1": r1,
            "volume_median20": median_volume,
            "volume_ratio20": volume_ratio20,
        },
        stages={
            "required_history": True,
            "adv20_minimum": adv_ok,
            "prior_close_high55_breakout": close_breakout,
            "r1_positive": r1_ok,
            "volume_ratio_2_to_10": volume_ok,
            "no_active_position": no_active_position,
        },
    )


def _base_observation(
    *,
    symbol: str,
    session_date: date,
    history: Sequence[USStageBDailyBar | None],
    required_close_count: int,
) -> tuple[tuple[USStageBDailyBar, ...], float] | None:
    if len(history) < required_close_count:
        return None
    required = tuple(history[-required_close_count:])
    if any(bar is None for bar in required):
        return None
    bars = tuple(bar for bar in required if bar is not None)
    if any(bar.symbol != symbol for bar in bars):
        raise USSignalInputError("history contains a bar for a different symbol")
    if bars[-1].session_date != session_date:
        raise USSignalInputError("history does not terminate at the signal session")
    if not all(_finite_positive(bar.adjusted_close) for bar in bars):
        return None
    previous = tuple(history[-(ADV20_PRE_WINDOW + 1) : -1])
    if len(previous) != ADV20_PRE_WINDOW or any(bar is None for bar in previous):
        return None
    prior_bars = tuple(bar for bar in previous if bar is not None)
    if not all(
        _finite_positive(bar.adjusted_close) and _finite_positive(bar.volume)
        for bar in prior_bars
    ):
        return None
    dollar_values = [
        float(bar.adjusted_close) * float(bar.volume) for bar in prior_bars
    ]
    if not all(math.isfinite(value) and value > 0.0 for value in dollar_values):
        return None
    return bars, sum(dollar_values) / ADV20_PRE_WINDOW


def _excluded(
    candidate: CandidateBinding,
    *,
    symbol: str,
    session_date: date,
    no_active_position: bool,
    reason: str,
) -> SignalObservation:
    return _observation(
        candidate,
        symbol=symbol,
        session_date=session_date,
        universe_eligible=False,
        technical_signal=False,
        no_active_position=no_active_position,
        reason=reason,
        adv20_pre_proxy=None,
        metrics={},
        stages={
            "required_history": False,
            "no_active_position": no_active_position,
        },
    )


def _observation(
    candidate: CandidateBinding,
    *,
    symbol: str,
    session_date: date,
    universe_eligible: bool,
    technical_signal: bool,
    no_active_position: bool,
    reason: str | None,
    adv20_pre_proxy: float | None,
    metrics: Mapping[str, float | None],
    stages: Mapping[str, bool],
) -> SignalObservation:
    signal = technical_signal and no_active_position
    resolved_reason = reason
    if signal:
        resolved_reason = None
    elif technical_signal and not no_active_position:
        resolved_reason = "active_position"
    metric_values = {
        name: float(value)
        for name, value in metrics.items()
        if value is not None and math.isfinite(float(value))
    }
    return SignalObservation(
        strategy_id=candidate.strategy_id,
        contract_hash=candidate.contract_hash,
        labels=mandatory_labels(candidate.strategy_id),
        symbol=symbol,
        session_date=session_date,
        universe_eligible=universe_eligible,
        technical_signal=technical_signal,
        no_active_position=no_active_position,
        signal=signal,
        exclusion_reason=resolved_reason,
        adv20_pre_proxy=adv20_pre_proxy,
        tie_break_sha256=tie_break_digest(
            candidate.strategy_id, session_date, symbol
        ).hex(),
        metrics=MappingProxyType(metric_values),
        stages=MappingProxyType(dict(stages)),
    )


def _reason(
    *,
    adv_ok: bool,
    technical_signal: bool,
    no_active_position: bool,
) -> str | None:
    if not adv_ok:
        return "adv20_below_minimum"
    if not technical_signal:
        return "signal_conditions_not_met"
    if not no_active_position:
        return "active_position"
    return None


def _close(bar: USStageBDailyBar) -> float:
    value = bar.adjusted_close
    if not _finite_positive(value):  # pragma: no cover - guarded by _base_observation
        raise USSignalInputError("required adjusted close is not finite and positive")
    return float(value)


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _one_session_returns(closes: Sequence[float]) -> tuple[float, ...]:
    if len(closes) < 2:
        raise USSignalInputError("one-session return window needs at least two closes")
    returns = tuple(
        current / previous - 1.0
        for previous, current in zip(closes, closes[1:], strict=False)
    )
    if not all(math.isfinite(value) for value in returns):
        raise USSignalInputError("one-session return is non-finite")
    return returns


def _sample_standard_deviation(values: Sequence[float]) -> float | None:
    """Return the packet-defined sample standard deviation (ddof=1)."""

    if len(values) < 2 or not all(math.isfinite(value) for value in values):
        return None
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _median(values: Sequence[float]) -> float:
    if not values:
        raise USSignalInputError("median requires at least one value")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _int_parameter(candidate: CandidateBinding, name: str) -> int:
    value = candidate.parameter(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise USSignalInputError(
            f"{candidate.strategy_id} parameter {name!r} is not a positive integer"
        )
    return value


def _float_parameter(candidate: CandidateBinding, name: str) -> float:
    value = candidate.parameter(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise USSignalInputError(
            f"{candidate.strategy_id} parameter {name!r} is not numeric"
        )
    numeric = float(value)
    if not math.isfinite(numeric):
        raise USSignalInputError(
            f"{candidate.strategy_id} parameter {name!r} is not finite"
        )
    return numeric
