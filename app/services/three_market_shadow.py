"""Mutation-free three-market shadow lifecycle helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from research.three_market_shadow.calculations import CONTRACT_HASH, calculate_signal

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


def shadow_decision(market: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a research observation with an unconditional no-order decision."""
    signal = calculate_signal(market, snapshot)
    labels = dict(ACCEPTANCE_LABELS)
    labels["signal_source"] = str(signal["signal_source"])
    return {
        "signal": signal,
        "contract_hash": CONTRACT_HASH,
        "labels": labels,
        "order_count": 0,
        "account_mutations": 0,
        "arm": False,
    }


__all__ = ["ACCEPTANCE_LABELS", "CONTRACT_HASH", "shadow_decision"]
