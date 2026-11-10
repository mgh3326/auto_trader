"""Pure retrospective type and P&L evidence classification."""

from typing import Any

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


# ROB-1488 — why `realized_pnl` is NULL. A closed vocabulary derived at read
# time so a genuinely non-applicable value is not confused with a filled sell
# whose realized P&L was not recorded. This classification never fabricates a
# P&L and never writes a row; it only labels evidence the row already carries.
PNL_STATUS_RECORDED = "recorded"
PNL_STATUS_RECORDED_OTHER_PNL = "recorded_other_pnl"
PNL_STATUS_POSITION_INTAKE = "position_intake"
PNL_STATUS_NO_FILL_EVIDENCE = "no_fill_evidence"
PNL_STATUS_NO_FILL_OUTCOME = "no_fill_outcome"
PNL_STATUS_UNKNOWN_OUTCOME = "unknown_outcome"
PNL_STATUS_BUY_LEG = "buy_leg"
PNL_STATUS_UNKNOWN_SIDE = "unknown_side"
PNL_STATUS_MISSING_REALIZED_PNL = "missing_realized_pnl"

PNL_STATUS_CLASS: dict[str, str] = {
    PNL_STATUS_RECORDED: "recorded",
    PNL_STATUS_RECORDED_OTHER_PNL: "recorded",
    PNL_STATUS_POSITION_INTAKE: "not_applicable",
    PNL_STATUS_NO_FILL_EVIDENCE: "unknown",
    PNL_STATUS_NO_FILL_OUTCOME: "not_applicable",
    PNL_STATUS_UNKNOWN_OUTCOME: "unknown",
    PNL_STATUS_BUY_LEG: "not_applicable",
    PNL_STATUS_UNKNOWN_SIDE: "unknown",
    PNL_STATUS_MISSING_REALIZED_PNL: "missing",
}

_FILLED_OUTCOMES = frozenset({"filled", "partially_filled"})
_NO_FILL_OUTCOMES = frozenset({"unfilled", "rejected", "cancelled"})


def classify_pnl_status(row: TradeRetrospective) -> str:
    """Label existing P&L evidence without converting or inventing a value.

    ``recorded_other_pnl`` means another P&L field is present, not that the
    absolute realized amount is known. ``missing_realized_pnl`` is reserved
    for evidenced filled sells with no P&L field populated; cause is not inferred.
    Position intake is not an execution and has no realized-P&L obligation.
    """
    if is_intake_retrospective(row):
        return PNL_STATUS_POSITION_INTAKE
    if row.realized_pnl is not None:
        return PNL_STATUS_RECORDED
    if any(
        value is not None
        for value in (
            row.pnl_pct,
            row.total_pnl_krw,
            row.fx_pnl_krw,
            row.security_pnl_usd,
            row.security_pnl_krw,
        )
    ):
        return PNL_STATUS_RECORDED_OTHER_PNL
    if not row.fill_evidence_available:
        return PNL_STATUS_NO_FILL_EVIDENCE
    if row.outcome in _NO_FILL_OUTCOMES:
        return PNL_STATUS_NO_FILL_OUTCOME
    if row.outcome not in _FILLED_OUTCOMES:
        return PNL_STATUS_UNKNOWN_OUTCOME
    if row.side == "buy":
        return PNL_STATUS_BUY_LEG
    if row.side is None:
        return PNL_STATUS_UNKNOWN_SIDE
    return PNL_STATUS_MISSING_REALIZED_PNL
