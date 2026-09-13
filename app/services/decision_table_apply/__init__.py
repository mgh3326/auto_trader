"""Idempotent application of validated decision-table artifacts."""

from app.services.decision_table_apply.service import (
    DecisionTableApplyDependencies,
    apply_decision_table,
)

__all__ = ["DecisionTableApplyDependencies", "apply_decision_table"]
