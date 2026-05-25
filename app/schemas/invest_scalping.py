"""ROB-315 Phase 2 — request bodies for the read/review `/invest/api/scalping`
surface. Responses are serialized as plain dicts by the router (Decimals as
strings); only the mutating request bodies need validation here."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

Product = Literal["spot", "usdm_futures"]
ReviewDecision = Literal["review", "keep", "adjust", "pause", "disable"]
ReviewStatus = Literal["draft", "reviewed", "locked"]
ActionType = Literal[
    "parameter_change",
    "investigate",
    "pause",
    "resume",
    "add_guard",
    "data_quality",
    "no_change",
]
ActionStatus = Literal["open", "applied", "skipped", "superseded"]


class ScalpingDraftRequest(BaseModel):
    review_date: date
    product: Product
    session_tag: str = ""


class ScalpingReviewPatchRequest(BaseModel):
    """Operator-editable review fields only. Unset fields are left untouched
    (PATCH semantics via ``model_dump(exclude_unset=True)``)."""

    observation: str | None = None
    root_cause: str | None = None
    improvement: str | None = None
    next_run_plan: str | None = None
    decision: ReviewDecision | None = None
    status: ReviewStatus | None = None


class ScalpingActionCreateRequest(BaseModel):
    action_type: ActionType
    title: str
    rationale: str | None = None
    target_component: str | None = None
    proposed_change: str | None = None
    expected_effect: str | None = None


class ScalpingActionPatchRequest(BaseModel):
    status: ActionStatus | None = None
    title: str | None = None
    rationale: str | None = None
    target_component: str | None = None
    proposed_change: str | None = None
    expected_effect: str | None = None
