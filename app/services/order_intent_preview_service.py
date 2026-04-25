"""Order Intent Preview v0 — pure transformation, zero side effects."""

from __future__ import annotations

from typing import Any

from app.schemas.order_intent_preview import (
    IntentSelectionInput,
    IntentTriggerPreview,
    OrderIntentPreviewItem,
    OrderIntentPreviewRequest,
    OrderIntentPreviewResponse,
)
from app.services.portfolio_decision_service import (
    PortfolioDecisionRunNotFoundError,  # noqa: F401  (re-exported for callers)
    PortfolioDecisionService,
)

_BUY_ACTIONS = {"buy_candidate"}
_SELL_ACTIONS = {"trim_candidate", "sell_watch"}
_DEFAULT_SELL_QTY_PCT = {"trim_candidate": 30.0, "sell_watch": 100.0}


class OrderIntentPreviewService:
    def __init__(self, *, decision_service: PortfolioDecisionService) -> None:
        self._decision_service = decision_service

    async def build_preview(
        self,
        *,
        user_id: int,
        run_id: str,
        request: OrderIntentPreviewRequest,
    ) -> OrderIntentPreviewResponse:
        payload = await self._decision_service.get_decision_run(
            user_id=user_id,
            run_id=run_id,
        )
        intents: list[OrderIntentPreviewItem] = []
        warnings: list[str] = []

        selection_map = self._selections_by_id(request.selections)

        for group in payload.get("symbol_groups", []):
            for item in group.get("items", []):
                intent = self._build_intent_for_item(
                    run_id=run_id,
                    group=group,
                    item=item,
                    request=request,
                    selection=selection_map.get(item.get("id", "")),
                )
                if intent is not None:
                    intents.append(intent)

        return OrderIntentPreviewResponse(
            decision_run_id=run_id,
            intents=intents,
            warnings=warnings,
        )

    @staticmethod
    def _selections_by_id(
        selections: list[IntentSelectionInput],
    ) -> dict[str, IntentSelectionInput]:
        return {s.decision_item_id: s for s in selections}

    def _build_intent_for_item(
        self,
        *,
        run_id: str,
        group: dict[str, Any],
        item: dict[str, Any],
        request: OrderIntentPreviewRequest,
        selection: IntentSelectionInput | None = None,
    ) -> OrderIntentPreviewItem | None:
        action = item.get("action")
        if action == "hold":
            return None

        if action in _BUY_ACTIONS:
            side = "buy"
        else:
            side = "sell"

        threshold: float | None = None
        threshold_source: str | None = None
        if selection is not None and selection.override_threshold is not None:
            threshold = selection.override_threshold
            threshold_source = "override"
        else:
            threshold_raw = item.get("action_price")
            if threshold_raw is not None:
                threshold = float(threshold_raw)
                threshold_source = item.get("action_price_source")

        operator = "below" if side == "buy" else "above"
        trigger: IntentTriggerPreview | None = None
        status = "manual_review_required"
        if action != "manual_review" and threshold is not None:
            threshold_f = float(threshold)
            trigger = IntentTriggerPreview(
                metric="price",
                operator=operator,
                threshold=threshold_f,
                source=threshold_source,
            )
            if side == "sell":
                current_price = item.get("current_price")
                if current_price is not None and float(current_price) >= threshold_f:
                    status = "execution_candidate"
                else:
                    status = "watch_ready"
            else:
                status = "watch_ready"

        return OrderIntentPreviewItem(
            decision_run_id=run_id,
            decision_item_id=item["id"],
            symbol=group["symbol"],
            market=group["market_type"],
            side=side,
            intent_type=action,
            status=status,
            execution_mode=request.execution_mode,
            trigger=trigger,
        )
