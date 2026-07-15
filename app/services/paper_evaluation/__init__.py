"""ROB-850 read-only 3-view P&L evaluation and promotion gate service.

This package computes three independent evaluation views (Binance broker USDT,
Alpaca broker USD, conservative canonical shadow USDT) and emits a deterministic
conjunctive verdict/evidence for ROB-848 without performing any broker writes,
mutations, or promotion state transitions.

The package intentionally lives outside ``app/services/paper_validation`` because
ROB-848 AST safety guards forbid importing ROB-849 canonical snapshot types and
broker ledger modules inside that subtree.
"""

from __future__ import annotations

from app.services.paper_evaluation.contracts import (
    CalendarDaySemantics,
    EpochIdentity,
    EpochResetReason,
    EvaluationConfig,
    EvaluationConfigError,
    FillCostPolicy,
    GateType,
    GateVerdict,
    MarkFillTiming,
    MinimumEvidence,
    PromotionThresholds,
    ScorecardVerdict,
    VerdictStatus,
    ViewCurrency,
    ViewMapping,
    ViewMetrics,
    ViewName,
    ViewSource,
)
from app.services.paper_evaluation.evaluation_config import compute_config_hash

__all__ = [
    "CalendarDaySemantics",
    "EvaluationConfig",
    "EvaluationConfigError",
    "EpochIdentity",
    "EpochResetReason",
    "FillCostPolicy",
    "GateVerdict",
    "GateType",
    "MarkFillTiming",
    "MinimumEvidence",
    "PromotionThresholds",
    "ScorecardVerdict",
    "ViewCurrency",
    "ViewMapping",
    "ViewMetrics",
    "ViewName",
    "ViewSource",
    "VerdictStatus",
    "compute_config_hash",
]
