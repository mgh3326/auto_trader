"""ROB-692 — read-only stock-detail deterministic recommendation + R:R.

Reuses `analyze_stock_impl` (the only place that assembles the `analysis`
dict `build_recommendation_for_equity` consumes: quote + indicators +
support_resistance + opinions + valuation, then the ROB-486
insufficient-inputs floor) instead of re-deriving that ~250-line fetch/floor
pipeline. This module only composes and reshapes an already-computed,
already-floored recommendation for the web transport.

Fulfills ROB-690's deferred R:R "Step 4": the recommendation's
entry(=current price, or top buy_zone)/stop(=stop_loss)/target(=nearest
sell_target) triple is fed into `resolve_direction` / `build_trade_setup`
(`app/services/investment_reports/risk_reward.py`) — the same helper wired
in `app/services/investment_reports/ingestion.py`. Fail-closed: any
non-`long` direction (sell/hold recommendations, since a hold/sell reco has
no long entry to price) or a degenerate/mismatched price triangle omits the
R:R chip entirely rather than showing a misleading ratio.

No new judgment/model. No broker/order/watch mutation (read-only).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.mcp_server.tooling.analysis_analyze import analyze_stock_impl
from app.schemas.invest_stock_detail_recommendation import (
    RecoTradeSetup,
    RecoZone,
    StockDetailRecommendationResponse,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import SymbolNotFound
from app.services.investment_reports.risk_reward import (
    build_trade_setup,
    resolve_direction,
)

RecommendationMarket = Literal["kr", "us"]
AnalyzeStockImpl = Callable[..., Awaitable[dict[str, Any]]]

_DEFAULT_RECOMMENDATION: dict[str, Any] = {
    "action": "hold",
    "confidence": "low",
    "rsi14": None,
    "buy_zones": [],
    "sell_targets": [],
    "stop_loss": None,
    "reasoning": "",
    "insufficient_inputs": [],
}


def _action_to_side(action: Any) -> str | None:
    if action == "buy":
        return "buy"
    if action == "sell":
        return "sell"
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_as_of(raw: Any) -> datetime:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(UTC)


def _derive_trade_setup(
    reco: dict[str, Any], current_price: Any
) -> RecoTradeSetup | None:
    """R:R for a `long` setup only — fail-closed on any other outcome.

    Only `action == "buy"` resolves to `long` (via `resolve_direction`);
    `sell` resolves to `exit` and `hold` resolves to `unknown` — both skip.
    entry defaults to `current_price`; when that's missing, falls back to
    the highest (topmost) `buy_zone` price. stop/target come straight off
    the recommendation's `stop_loss` / nearest (`[0]`) `sell_target`.
    """
    direction = resolve_direction(
        side=_action_to_side(reco.get("action")),
        intent="",
        item_kind="action",
        explicit_direction=None,
    )
    if direction != "long":
        return None

    entry_value = current_price
    if entry_value is None:
        buy_zones = reco.get("buy_zones") or []
        if buy_zones:
            entry_value = buy_zones[-1].get("price")

    entry = _to_decimal(entry_value)
    stop = _to_decimal(reco.get("stop_loss"))
    sell_targets = reco.get("sell_targets") or []
    target = _to_decimal(sell_targets[0].get("price")) if sell_targets else None

    if entry is None or stop is None or target is None:
        return None

    setup = build_trade_setup(
        entry_levels=[entry],
        quantities=[None],
        stop=stop,
        target=target,
        direction="long",
    )
    if setup.status != "computed" or setup.headline is None:
        return None

    return RecoTradeSetup(
        direction=setup.direction,
        entry=str(setup.headline.entry),
        stop=str(setup.stop),
        target=str(setup.target),
        risk_pct=str(setup.headline.risk_pct),
        reward_pct=str(setup.headline.reward_pct),
        rr_ratio=str(setup.headline.rr_ratio),
    )


@dataclass(frozen=True, slots=True)
class StockDetailRecommendationProviders:
    analyze: AnalyzeStockImpl = analyze_stock_impl


DEFAULT_RECOMMENDATION_PROVIDERS = StockDetailRecommendationProviders()


async def build_stock_detail_recommendation(
    *,
    market: RecommendationMarket,
    symbol: str,
    providers: StockDetailRecommendationProviders = DEFAULT_RECOMMENDATION_PROVIDERS,
) -> StockDetailRecommendationResponse:
    try:
        analysis = await providers.analyze(symbol, market=market)
    except ValueError as exc:
        raise SymbolNotFound(f"{market} symbol not found: {symbol}") from exc

    reco: dict[str, Any] = analysis.get("recommendation") or _DEFAULT_RECOMMENDATION
    quote = analysis.get("quote") or {}
    current_price = quote.get("price") or quote.get("current_price")

    trade_setup = _derive_trade_setup(reco, current_price)

    profile = analysis.get("profile")
    name = profile.get("name") if isinstance(profile, dict) else None

    return StockDetailRecommendationResponse(
        market=market,
        symbol=analysis.get("symbol") or symbol,
        name=name,
        as_of=_parse_as_of(analysis.get("derived_as_of")),
        current_price=current_price,
        action=reco.get("action", "hold"),
        confidence=reco.get("confidence", "low"),
        rsi14=reco.get("rsi14"),
        reasoning=reco.get("reasoning", ""),
        insufficient_inputs=list(reco.get("insufficient_inputs") or []),
        buy_zones=[RecoZone(**zone) for zone in (reco.get("buy_zones") or [])],
        sell_targets=[RecoZone(**zone) for zone in (reco.get("sell_targets") or [])],
        stop_loss=reco.get("stop_loss"),
        trade_setup=trade_setup,
    )
