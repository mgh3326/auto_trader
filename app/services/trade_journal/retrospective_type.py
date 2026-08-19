"""Stored retrospective-type contract without a schema migration (ROB-1285).

``review.trade_retrospectives.evidence_snapshot`` is the existing durable JSONB
evidence envelope.  Position-intake retrospectives use its reserved
``retrospective_type`` key; ordinary execution retrospectives omit the key and
therefore remain backward-compatible ``execution`` rows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_

from app.models.review import TradeRetrospective

RETROSPECTIVE_TYPE_KEY = "retrospective_type"
EXECUTION_RETROSPECTIVE_TYPE = "execution"
INTAKE_RETROSPECTIVE_TYPE = "intake"


def retrospective_type_from_snapshot(snapshot: Any) -> str:
    """Return the typed row kind, treating all legacy rows as execution."""
    if isinstance(snapshot, dict):
        value = snapshot.get(RETROSPECTIVE_TYPE_KEY)
        if value == INTAKE_RETROSPECTIVE_TYPE:
            return INTAKE_RETROSPECTIVE_TYPE
    return EXECUTION_RETROSPECTIVE_TYPE


def is_intake_retrospective(row: TradeRetrospective) -> bool:
    return (
        retrospective_type_from_snapshot(row.evidence_snapshot)
        == INTAKE_RETROSPECTIVE_TYPE
    )


def sql_is_learning_eligible():
    """SQL predicate excluding non-execution intake rows from learning reads."""
    stored_type = TradeRetrospective.evidence_snapshot[RETROSPECTIVE_TYPE_KEY].astext
    return or_(
        TradeRetrospective.evidence_snapshot.is_(None),
        stored_type.is_(None),
        stored_type != INTAKE_RETROSPECTIVE_TYPE,
    )


__all__ = [
    "EXECUTION_RETROSPECTIVE_TYPE",
    "INTAKE_RETROSPECTIVE_TYPE",
    "RETROSPECTIVE_TYPE_KEY",
    "is_intake_retrospective",
    "retrospective_type_from_snapshot",
    "sql_is_learning_eligible",
]
