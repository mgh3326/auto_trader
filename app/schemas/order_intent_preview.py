from typing import Annotated, Literal

from pydantic import BaseModel, Field

NonNegativeFloat = Annotated[float, Field(ge=0)]


class IntentBudgetInput(BaseModel):
    total_krw: float | None = Field(default=None, ge=0)
    per_symbol_budget_krw: dict[str, NonNegativeFloat] = Field(default_factory=dict)
    default_buy_budget_krw: float | None = Field(default=None, ge=0)


class IntentSelectionInput(BaseModel):
    decision_item_id: str
    enabled: bool = True
    budget_krw: float | None = Field(default=None, ge=0)
    quantity_pct: float | None = Field(default=None, ge=0, le=100)
    override_threshold: float | None = Field(default=None, gt=0)


class OrderIntentPreviewRequest(BaseModel):
    budget: IntentBudgetInput = Field(default_factory=IntentBudgetInput)
    selections: list[IntentSelectionInput] = Field(default_factory=list)
    execution_mode: Literal[
        "requires_final_approval",
        "paper_only",
        "dry_run_only",
    ] = "requires_final_approval"


class IntentTriggerPreview(BaseModel):
    metric: Literal["price"]
    operator: Literal["above", "below"]
    threshold: float | None = None
    source: str | None = None


class OrderIntentPreviewItem(BaseModel):
    decision_run_id: str
    decision_item_id: str
    symbol: str
    market: str
    side: Literal["buy", "sell"]
    intent_type: Literal[
        "buy_candidate",
        "trim_candidate",
        "sell_watch",
        "manual_review",
    ]
    status: Literal[
        "invalid",
        "watch_ready",
        "execution_candidate",
        "manual_review_required",
    ]
    execution_mode: Literal[
        "requires_final_approval",
        "paper_only",
        "dry_run_only",
    ]
    budget_krw: float | None = None
    quantity_pct: float | None = None
    trigger: IntentTriggerPreview | None = None
    rationale: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OrderIntentPreviewResponse(BaseModel):
    success: bool = True
    decision_run_id: str
    mode: Literal["preview_only"] = "preview_only"
    intents: list[OrderIntentPreviewItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    discord_brief: str | None = None
