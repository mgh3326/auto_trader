"""Order Intent Preview v0 — pure transformation, zero side effects."""

from __future__ import annotations

from typing import Any, Literal

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
_MANUAL_REVIEW_ACTIONS = {"manual_review"}
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
                item_id = item.get("id")
                selection = selection_map.get(item_id) if item_id else None
                intent = self._build_intent_for_item(
                    run_id=run_id,
                    group=group,
                    item=item,
                    request=request,
                    selection=selection,
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

    @staticmethod
    def _side_for_action(action: str | None) -> Literal["buy", "sell"]:
        if action in _BUY_ACTIONS:
            return "buy"
        # Explicit contract: sell-style actions and manual_review map to "sell"
        # so they share the buy/sell schema. manual_review is gated separately
        # by status="manual_review_required" and trigger=None.
        return "sell"

    @staticmethod
    def _resolve_buy_budget(
        request: OrderIntentPreviewRequest,
        group: dict[str, Any],
        selection: IntentSelectionInput | None,
    ) -> tuple[float | None, list[str]]:
        if selection is not None and selection.budget_krw is not None:
            return selection.budget_krw, []
        per_symbol = request.budget.per_symbol_budget_krw
        symbol = group["symbol"]
        if symbol in per_symbol:
            return per_symbol[symbol], []
        if request.budget.default_buy_budget_krw is not None:
            return request.budget.default_buy_budget_krw, []
        return None, ["missing_buy_budget"]

    @staticmethod
    def _resolve_sell_quantity_pct(
        action: str | None,
        selection: IntentSelectionInput | None,
    ) -> float | None:
        if selection is not None and selection.quantity_pct is not None:
            return selection.quantity_pct
        return _DEFAULT_SELL_QTY_PCT.get(action) if action is not None else None

    @staticmethod
    def _resolve_threshold(
        item: dict[str, Any],
        selection: IntentSelectionInput | None,
    ) -> tuple[float | None, str | None]:
        if selection is not None and selection.override_threshold is not None:
            return selection.override_threshold, "override"
        raw = item.get("action_price")
        if raw is None:
            return None, None
        return float(raw), item.get("action_price_source")

    @staticmethod
    def _build_trigger_and_status(
        side: Literal["buy", "sell"],
        action: str | None,
        threshold: float | None,
        threshold_source: str | None,
        item: dict[str, Any],
    ) -> tuple[IntentTriggerPreview | None, str]:
        if action == "manual_review" or threshold is None:
            return None, "manual_review_required"
        operator = "below" if side == "buy" else "above"
        trigger = IntentTriggerPreview(
            metric="price",
            operator=operator,
            threshold=threshold,
            source=threshold_source,
        )
        if side == "sell":
            current_price = item.get("current_price")
            if current_price is not None and float(current_price) >= threshold:
                return trigger, "execution_candidate"
            return trigger, "watch_ready"
        return trigger, "watch_ready"

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
        if selection is not None and not selection.enabled:
            return None

        side = self._side_for_action(action)

        budget_krw: float | None = None
        warnings: list[str] = []
        if side == "buy":
            budget_krw, warnings = self._resolve_buy_budget(request, group, selection)

        quantity_pct: float | None = None
        if side == "sell" and action != "manual_review":
            quantity_pct = self._resolve_sell_quantity_pct(action, selection)

        threshold, threshold_source = self._resolve_threshold(item, selection)
        trigger, status = self._build_trigger_and_status(
            side, action, threshold, threshold_source, item
        )

        return OrderIntentPreviewItem(
            decision_run_id=run_id,
            decision_item_id=item["id"],
            symbol=group["symbol"],
            market=group["market_type"],
            side=side,
            intent_type=action,
            status=status,
            execution_mode=request.execution_mode,
            budget_krw=budget_krw,
            quantity_pct=quantity_pct,
            trigger=trigger,
            warnings=warnings,
        )
