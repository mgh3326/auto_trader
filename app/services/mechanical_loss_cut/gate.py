"""The live gate for a mechanical loss-cut (ROB-1460).

Read by the auto-approve classifier. Everything here can only refuse. A refusal
sends the proposal to the ordinary human approval card.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.schemas.trading_policy import MechanicalLossCutMarket
from app.services.market_events.session_calendar import regular_session_bounds
from app.services.mechanical_loss_cut.rules import is_parking_instrument

MECHANICAL_STRATEGY = "mechanical_loss_cut"
POLICY_MARKET: dict[str, str] = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
}
_SESSION_ZONE = {"kr": "Asia/Seoul", "us": "America/New_York"}

#: Closed vocabulary of why a mechanical proposal went to the human card.
MECHANICAL_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        "switch_off",
        "market_not_allowed",
        "account_not_allowed",
        "thresholds_unconfirmed",
        "mode_not_expanded",
        "parking_instrument",
        "backfilled_plan",
        "binding_mismatch",
        "session_closed",
        "price_recovered",
        "not_underwater",
        "average_cost_unavailable",
        "limit_outside_band",
    }
)


def _csv(value: Any) -> frozenset[str]:
    if type(value) is not str:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def live_switch_refusal(*, market: Any, account_mode: Any) -> str | None:
    """The switch AND the market AND the account mode. Exact ``True`` only."""
    if settings.MECHANICAL_LOSS_CUT_LIVE_ENABLED is not True:
        return "switch_off"
    policy_market = POLICY_MARKET.get(market) if type(market) is str else None
    if policy_market is None or policy_market not in _csv(
        settings.MECHANICAL_LOSS_CUT_LIVE_MARKETS
    ):
        return "market_not_allowed"
    if account_mode not in _csv(settings.MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES):
        return "account_not_allowed"
    return None


def regular_session_open(market: Any, now: datetime | None) -> bool:
    """Crypto never closes. Equities need the exchange's regular session.

    A loss-cut proposal is exempt from the approval window so that a human can
    act on it at any hour. Without this check that exemption would let an
    automatic sell go out with nobody watching and the market closed.
    """
    if market == "crypto":
        return True
    policy_market = POLICY_MARKET.get(market) if type(market) is str else None
    if (
        policy_market not in _SESSION_ZONE
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        return False
    local = now.astimezone(ZoneInfo(_SESSION_ZONE[policy_market]))
    bounds = regular_session_bounds(policy_market, local.date())
    instant = now.astimezone(UTC)
    return bounds is not None and bounds[0] <= instant < bounds[1]


@dataclass(frozen=True, slots=True)
class MechanicalLossCutBinding:
    """Facts from the durable decision row, never from the proposal's own fields.

    A proposal can claim any strategy label. Only the job writes the decision
    row, so this row is what proves a proposal is mechanical.
    """

    decision_id: int
    account_mode: str
    market: str
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal
    backfilled: bool
    params: MechanicalLossCutMarket


def binding_refusal(
    *, group: Any, rung: Any, binding: MechanicalLossCutBinding, mode: str, now: Any
) -> str | None:
    """Everything that does not need the fresh preview's prices."""
    refusal = live_switch_refusal(
        market=getattr(group, "market", None),
        account_mode=getattr(group, "account_mode", None),
    )
    if refusal is not None:
        return refusal
    if binding.params.thresholds_confirmed is not True:
        return "thresholds_unconfirmed"
    if mode != "expanded":
        return "mode_not_expanded"
    if is_parking_instrument(getattr(group, "symbol", None), group.market):
        return "parking_instrument"
    if binding.backfilled and binding.params.live_include_backfilled is not True:
        return "backfilled_plan"
    if not (
        (getattr(group, "action", None) or "place") == "place"
        and getattr(group, "order_type", None) == "limit"
        and getattr(rung, "side", None) == "sell"
        and group.account_mode == binding.account_mode
        and group.market == binding.market
        and group.symbol == binding.symbol
        and Decimal(str(getattr(rung, "quantity", None))) == binding.quantity
        and Decimal(str(getattr(rung, "limit_price", None))) == binding.limit_price
    ):
        return "binding_mismatch"
    if not regular_session_open(group.market, now):
        return "session_closed"
    return None


def price_refusal(
    *,
    binding: MechanicalLossCutBinding,
    current_price: Decimal,
    limit_price: Decimal,
    average_cost: Decimal | None,
) -> str | None:
    """Re-check the trigger against the fresh preview, just before sending."""
    if average_cost is None or average_cost <= 0:
        return "average_cost_unavailable"
    if current_price >= binding.stop_price:
        return "price_recovered"
    if current_price >= average_cost:
        return "not_underwater"
    slippage = Decimal(str(binding.params.order.max_slippage_pct)) / Decimal("100")
    if limit_price < current_price * (Decimal("1") - slippage):
        return "limit_outside_band"
    return None
