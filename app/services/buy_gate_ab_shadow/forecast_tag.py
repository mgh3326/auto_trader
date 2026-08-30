"""Build forecast_save payloads for B-only (shadow_buy) candidates.

This module does not write. The session must call forecast_save itself.
Payloads are tagged so they cannot be mistaken for a live thesis, a
calibration sample, or a promotion trigger.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.services.buy_gate_ab_shadow.epoch import (
    COLLECTION_EPOCH,
    assert_epoch_seal,
)
from app.services.buy_gate_ab_shadow.evaluate import CandidateEvaluation
from app.services.buy_gate_ab_shadow.spec import (
    EXPERIMENT_ID,
    PINNED_SPEC_SHA256,
    PRE_REGISTRATION,
    spec_sha256,
)

_TAG = PRE_REGISTRATION["forecast_tagging"]
_SIZING = PRE_REGISTRATION["assumed_sizing"]
_WINDOWS: tuple[int, ...] = tuple(PRE_REGISTRATION["windows_trading_days"])
_CALENDAR_OFFSET_BY_WINDOW: dict[int, int] = {5: 7, 20: 28}
_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us"}
_CAP = {"kr": Decimal(str(_SIZING["cap_krw"])), "us": Decimal(str(_SIZING["cap_usd"]))}
_MULTIPLIER = Decimal(str(_SIZING["multiplier"]))

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


def assumed_notional(market: str) -> Decimal:
    return _CAP[market] * _MULTIPLIER


def _review_date(decision: date, window_trading_days: int) -> str:
    offset = _CALENDAR_OFFSET_BY_WINDOW[window_trading_days]
    return (decision + timedelta(days=offset)).isoformat()


def build_shadow_buy_forecasts(
    evaluation: CandidateEvaluation,
    *,
    created_by: str,
) -> list[dict[str, Any]]:
    """Return 5d+20d forecast_save kwargs for a B-only candidate, else []."""

    author = (created_by or "").strip()
    if not author:
        raise ValueError("created_by is required")
    if not evaluation.shadow_buy:
        return []
    if spec_sha256() != PINNED_SPEC_SHA256:
        raise RuntimeError("pre-registration spec hash does not match its pinned value")
    assert_epoch_seal()
    if not COLLECTION_EPOCH.contains_event(
        market=evaluation.market,
        observed_at=evaluation.evaluation_as_of,
    ):
        raise ValueError("evaluation_as_of is outside the sealed collection epoch")

    decision = COLLECTION_EPOCH.session_date(
        evaluation.market, evaluation.evaluation_as_of
    )
    notional = assumed_notional(evaluation.market)
    payloads: list[dict[str, Any]] = []
    for window in _WINDOWS:
        forecast_target = {
            "kind": _TAG["kind"],
            "direction": _TAG["direction"],
            "target_price": float(evaluation.entry_price),
            "outcome_rule_version": _TAG["outcome_rule_version"],
            "experiment_id": EXPERIMENT_ID,
            "variant": "B",
            "cohort": _TAG["cohort"],
            "shadow_buy": True,
            "promote": False,
            "live_gate_impact": False,
            "spec_sha256": PINNED_SPEC_SHA256,
            "policy_projection_sha256": (COLLECTION_EPOCH.policy_projection_sha256),
            "collection_epoch_id": COLLECTION_EPOCH.epoch_id,
            "collection_armed_at": COLLECTION_EPOCH.collection_armed_at.isoformat(),
            "collection_start": COLLECTION_EPOCH.collection_start.isoformat(),
            "collection_end_exclusive": (
                COLLECTION_EPOCH.collection_end_exclusive.isoformat()
            ),
            "evaluation_as_of": evaluation.evaluation_as_of.isoformat(),
            "session_date": decision.isoformat(),
            "entry_price": str(evaluation.entry_price),
            "input_snapshot": dict(evaluation.input_snapshot),
            "input_snapshot_sha256": evaluation.input_snapshot_sha256,
            "assumed_notional": str(notional),
            "window_trading_days": window,
            "support_strength": evaluation.support_strength,
            "calibration_eligibility": _TAG["calibration_eligibility"],
            "trade_performance_eligibility": _TAG["trade_performance_eligibility"],
            "scoring_authority": _TAG["scoring_authority"],
            "do_not_use_forecast_resolve_as_experiment_score": _TAG[
                "do_not_use_forecast_resolve_as_experiment_score"
            ],
        }
        leaked = FORBIDDEN_PAYLOAD_KEYS.intersection(forecast_target)
        if leaked:
            raise RuntimeError(f"promotion keys leaked into forecast_target: {leaked}")
        payload = {
            "created_by": author,
            "symbol": evaluation.symbol,
            "instrument_type": _INSTRUMENT[evaluation.market],
            "forecast_target": forecast_target,
            "probability": float(_TAG["probability_placeholder"]),
            "review_date": _review_date(decision, window),
            "horizon": f"{window}d",
            "session_label": _TAG["session_label"],
            "correlation_id": (
                f"{EXPERIMENT_ID}:{evaluation.market}:{evaluation.symbol}:"
                f"{decision.isoformat()}:{window}d"
            ),
            "contrary_evidence": (
                "shadow_buy; live path keeps variant A (strong support required); "
                "do not promote to proposal, order, or watch"
            ),
        }
        leaked_top = FORBIDDEN_PAYLOAD_KEYS.intersection(payload)
        if leaked_top:
            raise RuntimeError(
                f"promotion keys leaked into forecast payload: {leaked_top}"
            )
        payloads.append(payload)
    return payloads
