# app/schemas/trade_retrospective.py
"""ROB-647 — validated JSONB payloads for postmortem structuring.

``review.trade_retrospectives`` gains additive nullable columns whose JSONB
values MUST be structurally validated (evidence_snapshot's fully-unvalidated
passthrough is the anti-pattern we are avoiding — see
trade_retrospective_service.py). These pydantic models are the typed contract
for ``intended_vs_happened`` and ``next_actions``; the service coerces raw dicts
through them and raises ``RetrospectiveValidationError`` on any violation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# trigger_type is a CLOSED set kept in lock-step with the DB CHECK
# (ck_trade_retrospectives_trigger_type) and the alembic migration. It is
# deliberately distinct from ``outcome``: kis reconcile collapses expired ->
# cancelled (kis_live_ledger.py), so ``expired`` only survives as a trigger_type.
VALID_TRIGGER_TYPES: frozenset[str] = frozenset(
    {
        "fill",
        "partial_fill",
        "rejected_order",
        "cancelled",
        "expired",
        "thesis_change",
        "policy_violation",
        "stale_evidence",
        "guardrail_block",
        "stop_loss",
    }
)

# root_cause_class — ported from tradingcodex's postmortem taxonomy.
VALID_ROOT_CAUSE_CLASSES: frozenset[str] = frozenset(
    {
        "user_input",
        "analysis",
        "policy",
        "execution",
        "harness",
    }
)

_VALID_NEXT_ACTION_STATUS: frozenset[str] = frozenset({"open", "in_progress", "done"})


class Deviation(BaseModel):
    """One dimension where intent diverged from what happened.

    Richer than a scalar plan_price/fill_price pair: it names the dimension
    (price/timing/size/…) and carries planned vs actual plus an optional
    numeric delta.
    """

    model_config = ConfigDict(extra="forbid")

    dimension: str
    planned: Any | None = None
    actual: Any | None = None
    delta: float | None = None
    unit: str | None = None
    note: str | None = None

    @field_validator("dimension")
    @classmethod
    def _dimension_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("dimension must be a non-empty string")
        return v


class IntendedVsHappened(BaseModel):
    """Structured intended-vs-happened deviation record."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    deviations: list[Deviation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _at_least_one_signal(self) -> IntendedVsHappened:
        if not self.deviations and not (self.summary and self.summary.strip()):
            raise ValueError(
                "intended_vs_happened requires a summary or at least one deviation"
            )
        return self


class NextAction(BaseModel):
    """A follow-up action derived from the postmortem.

    ``issue_id`` stores an external issue key (legacy Paperclip name; current
    Linear ROB key) and mirrors ``trade_journal.paperclip_issue_id`` — issue
    creation is the caller/session's job; the repo never adds a Linear API client.
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    owner: str | None = None
    issue_id: str | None = None
    status: str | None = None
    due_kst_date: str | None = None

    @field_validator("action")
    @classmethod
    def _action_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("action must be a non-empty string")
        return v

    @field_validator("status")
    @classmethod
    def _status_allowed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _VALID_NEXT_ACTION_STATUS:
            raise ValueError(
                f"invalid status: {v} (allowed: {sorted(_VALID_NEXT_ACTION_STATUS)})"
            )
        return v
