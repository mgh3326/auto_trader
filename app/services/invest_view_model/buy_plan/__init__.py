"""Read-only "매수 계획 (트리거 보드)" aggregate for /invest (§144차).

The package is split so the arithmetic is testable without a database, a
broker, or a network: :mod:`computation` is pure, :mod:`service` does the
reads. Nothing here mutates a broker, an order, a watch, or a proposal — the
whole surface is a projection of existing read models plus the operator-owned
``config/trading_policy.yaml``.
"""

from app.services.invest_view_model.buy_plan.computation import (
    APPROXIMATION_NOTICE,
    AveragingSample,
    AveragingTurnPoint,
    approval_lane_for,
    averaging_additional_notional,
    averaging_turn_point,
)

__all__ = [
    "APPROXIMATION_NOTICE",
    "AveragingSample",
    "AveragingTurnPoint",
    "approval_lane_for",
    "averaging_additional_notional",
    "averaging_turn_point",
]
