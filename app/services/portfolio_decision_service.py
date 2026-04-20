import asyncio
import logging
from datetime import datetime
from typing import Any

from app.mcp_server.tooling.fundamentals._support_resistance import (
    get_support_resistance_impl as _get_support_resistance_impl,
)
from app.mcp_server.tooling.market_data_quotes import _get_indicators_impl

logger = logging.getLogger(__name__)

# Heuristic constants
NEAR_SUPPORT_PCT = 3.0
NEAR_RESISTANCE_PCT = 3.0
TARGET_NEAR_PCT = 5.0
STOP_NEAR_PCT = 5.0
HIGH_WEIGHT_PCT = 15.0
PROFIT_TRIM_PCT = 8.0
LOSS_WATCH_PCT = -6.0
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0


def _to_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PortfolioDecisionService:
    def __init__(self, *, overview_service, dashboard_service) -> None:
        self.overview_service = overview_service
        self.dashboard_service = dashboard_service

    async def build_decision_slate(
        self,
        *,
        user_id: int,
        market: str = "ALL",
        account_keys: list[str] | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        """Build a deterministic decision slate for the user's current portfolio."""
        # 1. Fetch overview
        overview = await self.overview_service.get_overview(
            user_id=user_id,
            market=market,
            account_keys=account_keys,
            q=q,
            skip_missing_prices=False,
        )
        positions = overview.get("positions") or []
        facets = overview.get("facets") or {}

        # 2. Fetch journals in batch
        current_prices = {
            p["symbol"]: p.get("current_price") for p in positions if p.get("symbol")
        }
        journals = await self.dashboard_service.get_journals_batch(
            list(current_prices.keys()),
            current_prices=current_prices,
        )

        # 3. Fetch support/resistance and indicators concurrently
        # We cap concurrency to avoid overloading external APIs
        semaphore = asyncio.Semaphore(10)

        async def fetch_context(p: dict[str, Any]) -> dict[str, Any]:
            symbol = p["symbol"]
            market_type = p["market_type"]
            async with semaphore:
                sr_task = _get_support_resistance_impl(symbol, market=market_type)
                ind_task = _get_indicators_impl(symbol, ["rsi"], market=market_type)
                sr, ind = await asyncio.gather(
                    sr_task, ind_task, return_exceptions=True
                )
                return {
                    "symbol": symbol,
                    "sr": sr
                    if not isinstance(sr, Exception)
                    else {"status": "unavailable", "error": str(sr)},
                    "ind": ind
                    if not isinstance(ind, Exception)
                    else {"indicators": {}},
                }

        contexts_list = await asyncio.gather(*[fetch_context(p) for p in positions])
        contexts = {c["symbol"]: c for c in contexts_list}

        # 4. Build symbol groups and items
        symbol_groups = []
        summary_counts = {
            "symbols": 0,
            "decision_items": 0,
            "actionable_items": 0,
            "manual_review_items": 0,
            "auto_candidate_items": 0,
            "missing_context_items": 0,
            "by_action": {
                "buy_candidate": 0,
                "trim_candidate": 0,
                "sell_watch": 0,
                "hold": 0,
                "manual_review": 0,
            },
            "by_market": {},
        }

        for p in positions:
            symbol = p["symbol"]
            market_type = p["market_type"]
            journal = journals.get(symbol)
            context = contexts.get(symbol) or {
                "sr": {"status": "unavailable"},
                "ind": {"indicators": {}},
            }

            weights = self._build_weights(positions, p)
            group = self._build_symbol_group(p, journal, context, weights)
            symbol_groups.append(group)

            # Update summary
            summary_counts["symbols"] += 1
            summary_counts["by_market"][market_type] = (
                summary_counts["by_market"].get(market_type, 0) + 1
            )
            for item in group["items"]:
                summary_counts["decision_items"] += 1
                action = item["action"]
                summary_counts["by_action"][action] = (
                    summary_counts["by_action"].get(action, 0) + 1
                )

                if action in ("buy_candidate", "trim_candidate", "sell_watch"):
                    summary_counts["actionable_items"] += 1
                if action == "manual_review":
                    summary_counts["manual_review_items"] += 1
                if "missing_context" in item.get("badges", []):
                    summary_counts["missing_context_items"] += 1

                # Phase 1: auto_executable is always false
                if item["execution_boundary"].get("auto_executable"):
                    summary_counts["auto_candidate_items"] += 1

        run_id = f"runtime-{datetime.now().isoformat()}"

        return {
            "success": True,
            "decision_run": {
                "id": run_id,
                "generated_at": datetime.now(),
                "mode": "analysis_only",
                "persisted": False,
                "source": "portfolio_decision_service_v1",
            },
            "filters": {
                "market": market,
                "account_keys": account_keys or [],
                "q": q,
            },
            "summary": summary_counts,
            "facets": facets,
            "symbol_groups": symbol_groups,
            "warnings": [],
        }

    def _build_symbol_group(
        self,
        position: dict[str, Any],
        journal: dict[str, Any] | None,
        context: dict[str, Any],
        weights: dict[str, float | None],
    ) -> dict[str, Any]:
        symbol = position["symbol"]
        market_type = position["market_type"]
        sr_raw = context.get("sr") or {"status": "unavailable"}
        ind_raw = context.get("ind") or {"indicators": {}}
        rsi = _to_optional_float(
            (ind_raw.get("indicators") or {}).get("rsi", {}).get("14")
        )

        # Map SR
        sr_context = self._map_sr_context(sr_raw)
        group_warnings = []
        if sr_context["status"] == "unavailable":
            group_warnings.append("support_resistance_unavailable")
        if journal is None:
            group_warnings.append("journal_missing")

        # Build items
        execution_boundary = self._build_execution_boundary(position)
        items = self._classify_actions(
            position, journal, sr_context, weights, rsi, execution_boundary
        )

        return {
            "market_type": market_type,
            "symbol": symbol,
            "name": position.get("name", symbol),
            "detail_url": f"/portfolio/positions/{market_type.lower()}/{symbol}",
            "position": {
                "quantity": _to_optional_float(position.get("quantity")),
                "avg_price": _to_optional_float(position.get("avg_price")),
                "current_price": _to_optional_float(position.get("current_price")),
                "evaluation": _to_optional_float(position.get("evaluation")),
                "evaluation_krw": _to_optional_float(position.get("evaluation_krw")),
                "profit_loss": _to_optional_float(position.get("profit_loss")),
                "profit_loss_krw": _to_optional_float(position.get("profit_loss_krw")),
                "profit_rate": _to_optional_float(position.get("profit_rate")),
                "portfolio_weight_pct": weights.get("portfolio_weight_pct"),
                "market_weight_pct": weights.get("market_weight_pct"),
                "components": position.get("components", []),
            },
            "journal": journal,
            "support_resistance": sr_context,
            "items": items,
            "warnings": group_warnings,
        }

    def _map_sr_context(self, sr_raw: dict[str, Any]) -> dict[str, Any]:
        status = sr_raw.get("status", "available")
        if "error" in sr_raw:
            status = "unavailable"

        def map_level(level: Any) -> dict[str, Any] | None:
            if isinstance(level, dict):
                price = _to_optional_float(level.get("price"))
                distance_pct = _to_optional_float(level.get("distance_pct"))
                if price is None or distance_pct is None:
                    return None
                return {
                    "price": price,
                    "distance_pct": distance_pct,
                    "strength": level.get("strength", "weak"),
                    "sources": level.get("sources", []),
                }
            return None

        supports = [
            mapped
            for mapped in (map_level(level) for level in (sr_raw.get("supports") or []))
            if mapped is not None
        ]
        resistances = [
            mapped
            for mapped in (
                map_level(level) for level in (sr_raw.get("resistances") or [])
            )
            if mapped is not None
        ]
        nearest_support = map_level(sr_raw.get("nearest_support")) or (
            supports[0] if supports else None
        )
        nearest_resistance = map_level(sr_raw.get("nearest_resistance")) or (
            resistances[0] if resistances else None
        )

        if status == "unavailable":
            nearest_support = None
            nearest_resistance = None
            supports = []
            resistances = []

        return {
            "status": status,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "supports": supports,
            "resistances": resistances,
        }

    def _build_execution_boundary(self, position: dict[str, Any]) -> dict[str, Any]:
        components = position.get("components") or []
        brokers = {
            str(component.get("broker") or "").lower()
            for component in components
            if component.get("broker")
        }
        sources = {
            str(component.get("source") or "").lower()
            for component in components
            if component.get("source")
        }
        market_type = str(position.get("market_type") or "").upper()
        is_kis_only = bool(brokers) and brokers <= {"kis"} and "manual" not in sources
        channel = (
            "kis_candidate"
            if is_kis_only and market_type in {"KR", "US"}
            else "manual_review"
        )
        manual_only = channel == "manual_review"
        return {
            "mode": "analysis_only",
            "channel": channel,
            "auto_executable": False,
            "manual_only": manual_only,
            "reason": "Phase 1 does not expose execution.",
        }

    def _classify_actions(
        self,
        position: dict[str, Any],
        journal: dict[str, Any] | None,
        sr: dict[str, Any],
        weights: dict[str, float | None],
        rsi: float | None,
        execution_boundary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        items = []
        symbol = position["symbol"]
        current_price = _to_optional_float(position.get("current_price"))
        profit_rate = _to_optional_float(position.get("profit_rate")) or 0
        portfolio_weight_pct = weights.get("portfolio_weight_pct") or 0

        # 1. Manual Review
        if (
            current_price is None
            or current_price <= 0
            or (not journal and sr["status"] == "unavailable")
        ):
            items.append(
                self._build_decision_item(
                    id=f"{symbol}:manual_review:missing_context",
                    action="manual_review",
                    label="Manual review required",
                    priority="high",
                    current_price=current_price,
                    rationale=[
                        "Current price or market context (journal, S/R) is missing."
                    ],
                    execution_boundary=execution_boundary,
                    badges=["analysis_only", "missing_context"],
                    warnings=["missing_context"],
                )
            )
            return items

        # 2. Sell Watch
        stop_dist = (journal or {}).get("stop_distance_pct")
        if stop_dist is not None and stop_dist >= -STOP_NEAR_PCT:
            items.append(
                self._build_decision_item(
                    id=f"{symbol}:sell_watch:stop_near",
                    action="sell_watch",
                    label="Sell watch (Stop near)",
                    priority="high",
                    current_price=current_price,
                    action_price=(journal or {}).get("stop_loss"),
                    action_price_source="journal_stop",
                    delta_from_current_pct=stop_dist,
                    anchor={
                        "type": "stop_loss",
                        "price": (journal or {}).get("stop_loss"),
                        "distance_pct": stop_dist,
                    },
                    rationale=["Price is approaching the journal stop-loss level."],
                    execution_boundary=execution_boundary,
                )
            )
        elif profit_rate * 100 <= LOSS_WATCH_PCT and not journal:
            items.append(
                self._build_decision_item(
                    id=f"{symbol}:sell_watch:loss_threshold",
                    action="sell_watch",
                    label="Sell watch (Loss threshold)",
                    priority="medium",
                    current_price=current_price,
                    rationale=[
                        f"Unrealized loss exceeded {LOSS_WATCH_PCT}% without an active journal."
                    ],
                    execution_boundary=execution_boundary,
                )
            )

        # 3. Trim Candidate
        target_dist = (journal or {}).get("target_distance_pct")
        res_dist = (sr.get("nearest_resistance") or {}).get("distance_pct")

        if portfolio_weight_pct >= HIGH_WEIGHT_PCT:
            items.append(
                self._build_decision_item(
                    id=f"{symbol}:trim_candidate:high_weight",
                    action="trim_candidate",
                    label="Trim candidate (High weight)",
                    priority="medium",
                    current_price=current_price,
                    rationale=[
                        f"Position weight ({portfolio_weight_pct:.1f}%) exceeds the high-weight threshold ({HIGH_WEIGHT_PCT}%)."
                    ],
                    execution_boundary=execution_boundary,
                )
            )
        elif target_dist is not None and 0 <= target_dist <= TARGET_NEAR_PCT:
            items.append(
                self._build_decision_item(
                    id=f"{symbol}:trim_candidate:target_near",
                    action="trim_candidate",
                    label="Trim candidate (Target near)",
                    priority="medium",
                    current_price=current_price,
                    action_price=(journal or {}).get("target_price"),
                    action_price_source="journal_target",
                    delta_from_current_pct=target_dist,
                    anchor={
                        "type": "target",
                        "price": (journal or {}).get("target_price"),
                        "distance_pct": target_dist,
                    },
                    rationale=["Price is approaching the journal target price."],
                    execution_boundary=execution_boundary,
                )
            )
        elif (
            res_dist is not None
            and 0 <= res_dist <= NEAR_RESISTANCE_PCT
            and profit_rate > 0
        ):
            items.append(
                self._build_decision_item(
                    id=f"{symbol}:trim_candidate:resistance_near",
                    action="trim_candidate",
                    label="Trim candidate (Resistance near)",
                    priority="low",
                    current_price=current_price,
                    action_price=(sr.get("nearest_resistance") or {}).get("price"),
                    action_price_source="nearest_resistance",
                    delta_from_current_pct=res_dist,
                    anchor={
                        "type": "resistance",
                        "price": (sr.get("nearest_resistance") or {}).get("price"),
                        "distance_pct": res_dist,
                    },
                    rationale=[
                        "Price is near a major resistance level while in profit."
                    ],
                    execution_boundary=execution_boundary,
                )
            )

        # 4. Buy Candidate (held only)
        sup_dist = (sr.get("nearest_support") or {}).get("distance_pct")
        if (
            sup_dist is not None
            and abs(sup_dist) <= NEAR_SUPPORT_PCT
            and portfolio_weight_pct < HIGH_WEIGHT_PCT
        ):
            if rsi is not None and rsi <= RSI_OVERSOLD:
                items.append(
                    self._build_decision_item(
                        id=f"{symbol}:buy_candidate:support_rsi",
                        action="buy_candidate",
                        label="Buy candidate (Support + RSI)",
                        priority="medium",
                        current_price=current_price,
                        action_price=(sr.get("nearest_support") or {}).get("price"),
                        action_price_source="nearest_support",
                        delta_from_current_pct=sup_dist,
                        anchor={
                            "type": "support",
                            "price": (sr.get("nearest_support") or {}).get("price"),
                            "distance_pct": sup_dist,
                        },
                        rationale=[
                            "Price is near support and RSI is in oversold territory."
                        ],
                        execution_boundary=execution_boundary,
                    )
                )

        # 5. Hold
        if not items:
            items.append(
                self._build_decision_item(
                    id=f"{symbol}:hold:default",
                    action="hold",
                    label="Hold",
                    priority="low",
                    current_price=current_price,
                    rationale=[
                        "No strong buy/sell/trim triggers identified. Maintaining current position."
                    ],
                    execution_boundary=execution_boundary,
                )
            )

        return items

    def _build_decision_item(
        self,
        id: str,
        action: str,
        label: str,
        current_price: float,
        priority: str = "low",
        action_price: float | None = None,
        action_price_source: str | None = None,
        delta_from_current_pct: float | None = None,
        anchor: dict[str, Any] | None = None,
        rationale: list[str] | None = None,
        execution_boundary: dict[str, Any] | None = None,
        badges: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": id,
            "action": action,
            "label": label,
            "priority": priority,
            "current_price": current_price,
            "action_price": action_price,
            "action_price_source": action_price_source,
            "delta_from_current_pct": delta_from_current_pct,
            "anchor": anchor,
            "rationale": rationale or [],
            "execution_boundary": execution_boundary
            or {
                "mode": "analysis_only",
                "channel": None,
                "auto_executable": False,
                "manual_only": False,
                "reason": "Phase 1 does not expose execution.",
            },
            "badges": badges or ["analysis_only"],
            "warnings": warnings or [],
        }

    def _round_pct(self, value: float | None) -> float | None:
        if value is None:
            return None
        return round(value, 1)

    def _build_weights(
        self,
        positions: list[dict[str, Any]],
        base: dict[str, Any],
    ) -> dict[str, float | None]:
        def get_eval_krw(p: dict) -> float | None:
            market_type = str(p.get("market_type") or "").upper()
            val = p.get("evaluation_krw")
            if val is not None:
                return float(val)
            if market_type == "US":
                return None
            return float(p.get("evaluation", 0) or 0)

        base_evaluation_krw = get_eval_krw(base)
        if base_evaluation_krw in (None, 0):
            return {"portfolio_weight_pct": None, "market_weight_pct": None}

        portfolio_values = [get_eval_krw(p) for p in positions]
        total_portfolio_eval_krw = (
            sum(value for value in portfolio_values if value is not None)
            if all(value is not None for value in portfolio_values)
            else None
        )

        market_type = base.get("market_type")
        same_market_values = [
            get_eval_krw(p) for p in positions if p.get("market_type") == market_type
        ]
        total_same_market_eval_krw = (
            sum(value for value in same_market_values if value is not None)
            if all(value is not None for value in same_market_values)
            else None
        )

        portfolio_weight = (
            (base_evaluation_krw / total_portfolio_eval_krw) * 100
            if total_portfolio_eval_krw not in (None, 0)
            else None
        )
        market_weight = (
            (base_evaluation_krw / total_same_market_eval_krw) * 100
            if total_same_market_eval_krw not in (None, 0)
            else None
        )

        return {
            "portfolio_weight_pct": self._round_pct(portfolio_weight),
            "market_weight_pct": self._round_pct(market_weight),
        }
