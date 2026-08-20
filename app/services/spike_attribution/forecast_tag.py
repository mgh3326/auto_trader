"""Hook ⓑ — build ``forecast_save`` kwargs that pre-register one spike (pure).

This module does not write. It returns ready-to-save kwargs; the session or the
operator decides whether to record them. The attribution record travels inside
``forecast_target``, which is how the spike is 박제-ed with its evidence links
without adding a table: ``review.trade_forecasts`` is already the learning-loop
spine, and a row there is resolvable, greppable, and survives a redeploy.

Payloads are tagged so they cannot be mistaken for a live thesis, a calibration
sample, or a promotion trigger.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.services.spike_attribution.attribute import scored_class
from app.services.spike_attribution.contract import SpikeAttribution
from app.services.spike_attribution.spec import (
    EXPERIMENT_ID,
    PINNED_SPEC_SHA256,
    PRE_REGISTRATION,
    spec_sha256,
)

_TAG = PRE_REGISTRATION["forecast_tagging"]
_FT = PRE_REGISTRATION["follow_through"]
_WINDOWS: tuple[int, ...] = tuple(_FT["windows_trading_days"])
_CALENDAR_OFFSET_BY_WINDOW: dict[int, int] = {
    int(key): int(value)
    for key, value in _TAG["review_date_calendar_offset_days_by_window"].items()
}
_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us"}
_RETAINED_BOUNDARY = Decimal("0.5")

# Keys that would turn an observation row into an execution path. Their absence
# is asserted on every payload rather than assumed.
FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "order_proposal",
        "proposal_id",
        "place_order",
        "max_action",
        "watch_condition",
        "approval_hash",
        "confirm",
        "account_mode",
    }
)


class ForecastTagError(RuntimeError):
    """Raised when a payload could not be built safely."""


def target_price(attribution: SpikeAttribution) -> Decimal:
    """The retained/faded boundary price for this spike."""

    event = attribution.event
    return event.prev_close + _RETAINED_BOUNDARY * (event.close - event.prev_close)


def target_direction(attribution: SpikeAttribution) -> str:
    return "at_or_above" if attribution.event.direction == "up" else "at_or_below"


def prereg_skipped_reason(attribution: SpikeAttribution) -> str | None:
    """Why this event is not pre-registered at all, or None if it is."""

    event = attribution.event
    if event.close == event.prev_close:
        # retention_ratio has no denominator here, so a row would carry a
        # target price equal to prev_close and a verdict nothing can produce.
        return "zero_denominator_close_equals_prev_close"
    return None


def build_prereg_forecasts(
    attribution: SpikeAttribution,
    *,
    created_by: str,
) -> list[dict[str, Any]]:
    """Return one ``forecast_save`` kwargs dict per pre-registered window.

    Empty when :func:`prereg_skipped_reason` gives a reason — callers surface
    that reason rather than an unexplained empty list.
    """

    author = (created_by or "").strip()
    if not author:
        raise ForecastTagError("created_by is required")
    if spec_sha256() != PINNED_SPEC_SHA256:
        raise ForecastTagError(
            "pre-registration spec hash does not match its pinned value"
        )
    market = attribution.event.market
    if market not in _INSTRUMENT:
        raise ForecastTagError(f"unsupported market: {market!r}")
    if prereg_skipped_reason(attribution) is not None:
        return []

    price = target_price(attribution)
    if price <= 0:
        raise ForecastTagError("target_price must be positive")

    event = attribution.event
    session_date = event.session_date
    klass = scored_class(attribution)
    payloads: list[dict[str, Any]] = []
    for window in _WINDOWS:
        forecast_target: dict[str, Any] = {
            "kind": _TAG["kind"],
            "direction": target_direction(attribution),
            "target_price": float(price),
            "outcome_rule_version": _TAG["outcome_rule_version"],
            "experiment_id": EXPERIMENT_ID,
            "cohort": _TAG["cohort"],
            "promote": False,
            "live_gate_impact": False,
            "spec_sha256": PINNED_SPEC_SHA256,
            "window_trading_days": window,
            "scored_class": klass,
            "attribution_types": list(attribution.attribution_types),
            "unattributed": attribution.unattributed,
            "unattributed_reason": attribution.unattributed_reason,
            # The record itself — event, eligible candidates with their links,
            # what was rejected and why, and which materials could not be read.
            "attribution_record": attribution.as_dict(),
            "calibration_eligibility": _TAG["calibration_eligibility"],
            "trade_performance_eligibility": _TAG["trade_performance_eligibility"],
            "scoring_authority": _TAG["scoring_authority"],
            "do_not_use_forecast_resolve_as_experiment_score": _TAG[
                "do_not_use_forecast_resolve_as_experiment_score"
            ],
        }
        leaked = FORBIDDEN_PAYLOAD_KEYS.intersection(forecast_target)
        if leaked:
            raise ForecastTagError(
                f"promotion keys leaked into forecast_target: {sorted(leaked)}"
            )
        payload = {
            "created_by": author,
            "symbol": event.symbol,
            "instrument_type": _INSTRUMENT[market],
            "forecast_target": forecast_target,
            "probability": float(_TAG["probability_placeholder"]),
            "review_date": (
                session_date + timedelta(days=_CALENDAR_OFFSET_BY_WINDOW[window])
            ).isoformat(),
            "forecast_start_date": session_date.isoformat(),
            "horizon": f"{window}d",
            "session_label": _TAG["session_label"],
            "correlation_id": f"{attribution.correlation_id}:{window}d",
            "contrary_evidence": (
                "spike attribution observation row; candidates are causes not "
                "proven causal, and an unattributed row stays unattributed. "
                "Do not promote to proposal, order, or watch."
            ),
        }
        leaked_top = FORBIDDEN_PAYLOAD_KEYS.intersection(payload)
        if leaked_top:
            raise ForecastTagError(
                f"promotion keys leaked into forecast payload: {sorted(leaked_top)}"
            )
        payloads.append(payload)
    return payloads


__all__ = [
    "FORBIDDEN_PAYLOAD_KEYS",
    "ForecastTagError",
    "build_prereg_forecasts",
    "prereg_skipped_reason",
    "target_direction",
    "target_price",
]
