"""A KR-only, scheduleless and shadow-first execution vertical slice.

This module deliberately contains no broker client or order mutation primitive.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from research.three_market_shadow.calculations import calculate_signal

Stage = Literal[
    "decision",
    "order_intent",
    "kis_pre_submit",
    "fill",
    "position_reconcile",
    "discord",
]
EventSink = Callable[[dict[str, object]], None]

ACCEPTANCE_LABELS: dict[str, str] = {
    "purpose": "execution_acceptance",
    "sample_class": "ACCEPTANCE_ONLY",
    "signal_source": "UNTESTED_RESEARCH_SHADOW",
    "evidence_preserved": "YES",
    "scoring_eligible": "EXCLUDE",
    "forecast_calibration_eligible": "EXCLUDE",
    "trade_performance_eligible": "EXCLUDE",
    "strategy_promotion_credit": "NONE",
    "paper_go_live_credit": "NONE",
    "operational_reliability_eligible": "UNIDENTIFIABLE",
    "completion_name": "BROKER_ACCEPTANCE_PASSED",
}


@dataclass(frozen=True)
class LeanRunResult:
    correlation_id: str
    status: Literal["shadow_complete", "failed"]
    decision: Literal["NO_ORDER"]
    stages: tuple[Stage, ...]


class SyntheticNoOpStrategy:
    """KR research shadow adapter; it can never authorize an order."""

    def evaluate(self, snapshot: Mapping[str, object]) -> dict[str, object]:
        signal = calculate_signal("kr", snapshot)
        return {
            "decision": "NO_ORDER",
            "symbol": snapshot.get("symbol", "005930"),
            **signal,
        }


class ShadowOnlyKisPreSubmit:
    """KIS pre-submit observation with no submit operation by construction."""

    def inspect(self, intent: Mapping[str, object]) -> dict[str, object]:
        return {
            "mode": "shadow",
            "accepted_for_submission": False,
            "blocked_reason": "account_ownership_unconfirmed",
            "competing_surface": "watch_auto_execute_mock",
            "concurrent_writer_action": "refuse_and_report",
            "intent": dict(intent),
        }


def _event(
    correlation_id: str,
    stage: Stage,
    status: str,
    input_data: Mapping[str, object],
    output_data: Mapping[str, object],
) -> dict[str, object]:
    return {
        "correlation_id": correlation_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "stage": stage,
        "status": status,
        "input": dict(input_data),
        "output": dict(output_data),
    }


def run_once(
    snapshot: Mapping[str, object],
    *,
    correlation_id: str,
    emit: EventSink,
    strategy: SyntheticNoOpStrategy | None = None,
    pre_submit: ShadowOnlyKisPreSubmit | None = None,
) -> LeanRunResult:
    """Run exactly one observable, mutation-free KR shadow lifecycle."""

    strategy = strategy or SyntheticNoOpStrategy()
    pre_submit = pre_submit or ShadowOnlyKisPreSubmit()
    stages: list[Stage] = []
    try:
        decision_input = {"symbol": snapshot.get("symbol", "005930")}
        decision = strategy.evaluate(snapshot)
        decision["labels"] = dict(ACCEPTANCE_LABELS)
        emit(_event(correlation_id, "decision", "completed", decision_input, decision))
        stages.append("decision")

        intent = {
            "kind": "no_order",
            "side": None,
            "quantity": None,
            "decision": decision["decision"],
        }
        emit(_event(correlation_id, "order_intent", "completed", decision, intent))
        stages.append("order_intent")

        pre_submit_result = pre_submit.inspect(intent)
        emit(
            _event(
                correlation_id,
                "kis_pre_submit",
                "blocked",
                intent,
                pre_submit_result,
            )
        )
        stages.append("kis_pre_submit")

        fill = {"attempted": False, "evidence": None, "status": "not_applicable"}
        emit(_event(correlation_id, "fill", "skipped", pre_submit_result, fill))
        stages.append("fill")

        reconcile = {
            "attempted": False,
            "broker_read": False,
            "status": "not_applicable_no_order",
        }
        emit(_event(correlation_id, "position_reconcile", "skipped", fill, reconcile))
        stages.append("position_reconcile")

        discord = {
            "kind": "shadow_complete",
            "must_notify": True,
            "message": "KR lean shadow completed: NO_ORDER; KIS pre-submit blocked",
        }
        emit(_event(correlation_id, "discord", "completed", reconcile, discord))
        stages.append("discord")
        return LeanRunResult(
            correlation_id=correlation_id,
            status="shadow_complete",
            decision="NO_ORDER",
            stages=tuple(stages),
        )
    except Exception as exc:
        failure = {
            "kind": "failure",
            "must_notify": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        emit(_event(correlation_id, "discord", "failed", {}, failure))
        return LeanRunResult(
            correlation_id=correlation_id,
            status="failed",
            decision="NO_ORDER",
            stages=tuple(stages + ["discord"]),
        )


def result_dict(result: LeanRunResult) -> dict[str, object]:
    return asdict(result)
