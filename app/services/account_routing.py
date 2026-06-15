from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Market = Literal["kr", "us"]

DEFAULT_ACCOUNT_COSTS: dict[str, Any] = {
    "version": 1,
    "routing": {
        "position_consolidation_threshold_bps": {
            "kr": 25.0,
            "us": 40.0,
        }
    },
    "accounts": {
        "kis_domestic": {
            "broker": "kis",
            "label": "KIS domestic",
            "markets": {"kr": {"commission_bps": 14.7, "fx_spread_bps": 0.0}},
        },
        "kis_overseas": {
            "broker": "kis",
            "label": "KIS overseas",
            "markets": {"us": {"commission_bps": 25.0, "fx_spread_bps": 20.0}},
        },
        "toss": {
            "broker": "toss",
            "label": "Toss",
            "limits": {"max_order_notional_krw": 1_000_000.0},
            "markets": {
                "kr": {"commission_bps": 0.0, "fx_spread_bps": 0.0},
                "us": {"commission_bps": 10.0, "fx_spread_bps": 1.7},
            },
        },
    },
}


@dataclass(frozen=True)
class MarketCostProfile:
    commission_bps: float
    fx_spread_bps: float


@dataclass(frozen=True)
class AccountCostProfiles:
    raw: dict[str, Any]
    source: str
    review_required: bool

    def threshold_bps(self, market: Market) -> float:
        routing = self.raw.get("routing") if isinstance(self.raw, dict) else {}
        thresholds = (
            routing.get("position_consolidation_threshold_bps", {})
            if isinstance(routing, dict)
            else {}
        )
        fallback = DEFAULT_ACCOUNT_COSTS["routing"]["position_consolidation_threshold_bps"][market]
        return float(thresholds.get(market, fallback))

    def account(self, account_id: str) -> dict[str, Any]:
        accounts = self.raw.get("accounts", {}) if isinstance(self.raw, dict) else {}
        value = accounts.get(account_id, {})
        return value if isinstance(value, dict) else {}

    def market_profile(self, account_id: str, market: Market) -> MarketCostProfile:
        account = self.account(account_id)
        markets = account.get("markets", {}) if isinstance(account, dict) else {}
        profile = markets.get(market, {}) if isinstance(markets, dict) else {}
        default = DEFAULT_ACCOUNT_COSTS["accounts"].get(account_id, {}).get("markets", {}).get(market, {})
        return MarketCostProfile(
            commission_bps=float(profile.get("commission_bps", default.get("commission_bps", 0.0))),
            fx_spread_bps=float(profile.get("fx_spread_bps", default.get("fx_spread_bps", 0.0))),
        )

    def max_order_notional_krw(self, account_id: str) -> float | None:
        account = self.account(account_id)
        limits = account.get("limits", {}) if isinstance(account, dict) else {}
        value = limits.get("max_order_notional_krw") if isinstance(limits, dict) else None
        return None if value is None else float(value)


@dataclass(frozen=True)
class AccountRoutingInput:
    symbol: str
    market: Market
    side: str
    quantity: float
    price: float
    usd_krw: float | None
    account_costs: dict[str, Any] | None
    capital_snapshot: dict[str, Any]
    holdings_snapshot: dict[str, Any]


def build_cost_profiles(value: dict[str, Any] | None) -> AccountCostProfiles:
    if not isinstance(value, dict) or int(value.get("version", 0) or 0) != 1:
        return AccountCostProfiles(
            raw=DEFAULT_ACCOUNT_COSTS,
            source="default_seed",
            review_required=True,
        )
    return AccountCostProfiles(raw=value, source="user_setting", review_required=False)


def compact_cost_profile(
    account_id: str,
    market: Market,
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    profiles = build_cost_profiles(value)
    if account_id not in _candidate_accounts(market):
        return None
    profile = profiles.market_profile(account_id, market)
    return {
        "commission_bps": profile.commission_bps,
        "fx_spread_bps": profile.fx_spread_bps,
        "source": profiles.source,
        "review_required": profiles.review_required,
    }


def _candidate_accounts(market: Market) -> tuple[str, str]:
    if market == "kr":
        return ("kis_domestic", "toss")
    return ("kis_overseas", "toss")


def _orderable_by_account_currency(snapshot: dict[str, Any]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for row in snapshot.get("accounts") or []:
        account = str(row.get("account") or "")
        currency = str(row.get("currency") or "KRW").upper()
        orderable = float(row.get("orderable") or 0.0)
        bucket = result.setdefault(account, {"KRW": 0.0, "USD": 0.0})
        if currency in bucket:
            bucket[currency] += orderable
    return result


def _existing_accounts(snapshot: dict[str, Any], *, symbol: str, market: Market) -> list[str]:
    normalized = symbol.strip().upper() if market == "us" else symbol.strip()
    found: list[str] = []
    for account in snapshot.get("accounts") or []:
        account_id = str(account.get("account") or "")
        if account_id not in _candidate_accounts(market):
            continue
        for position in account.get("positions") or []:
            pos_symbol = str(position.get("symbol") or "")
            comparable = pos_symbol.upper() if market == "us" else pos_symbol
            if comparable == normalized and account_id not in found:
                found.append(account_id)
    return found


def _notional(input: AccountRoutingInput) -> dict[str, float | None | str]:
    if input.market == "us":
        if input.usd_krw is None or input.usd_krw <= 0:
            raise ValueError("usd_krw is required for US account routing")
        notional_usd = float(input.quantity) * float(input.price)
        return {
            "currency": "USD",
            "notional_usd": notional_usd,
            "notional_krw": notional_usd * float(input.usd_krw),
            "usd_krw": float(input.usd_krw),
        }
    return {
        "currency": "KRW",
        "notional_usd": None,
        "notional_krw": float(input.quantity) * float(input.price),
        "usd_krw": None,
    }


def _cost_row(
    *,
    account_id: str,
    market: Market,
    notional_krw: float,
    notional_usd: float | None,
    cash: dict[str, float],
    usd_krw: float | None,
    profiles: AccountCostProfiles,
) -> dict[str, Any]:
    profile = profiles.market_profile(account_id, market)
    ineligible_reason = None
    usd_orderable = float(cash.get("USD") or 0.0)
    krw_orderable = float(cash.get("KRW") or 0.0)
    usd_orderable_krw = usd_orderable * float(usd_krw or 0.0)
    orderable_krw = krw_orderable + usd_orderable_krw
    max_notional = profiles.max_order_notional_krw(account_id)
    if max_notional is not None and notional_krw > max_notional:
        ineligible_reason = "notional_limit_exceeded"
    elif orderable_krw < notional_krw:
        ineligible_reason = "insufficient_orderable_cash"
    commission_base = notional_krw
    commission_cost_krw = commission_base * profile.commission_bps / 10_000.0
    fx_notional_krw = (
        max(0.0, notional_krw - usd_orderable_krw) if market == "us" else 0.0
    )
    fx_cost_krw = fx_notional_krw * profile.fx_spread_bps / 10_000.0
    return {
        "eligible": ineligible_reason is None,
        "commission_bps": profile.commission_bps,
        "fx_spread_bps": profile.fx_spread_bps,
        "commission_cost_krw": commission_cost_krw,
        "fx_notional_krw": fx_notional_krw,
        "fx_cost_krw": fx_cost_krw,
        "total_cost_krw": commission_cost_krw + fx_cost_krw,
        "orderable_krw": orderable_krw,
        "orderable_usd": usd_orderable,
        "orderable_cash_krw": krw_orderable,
        "notional_usd": notional_usd,
        "ineligible_reason": ineligible_reason,
    }


def suggest_account_from_snapshot(input: AccountRoutingInput) -> dict[str, Any]:
    if input.side.lower() != "buy":
        raise ValueError("suggest_order_account supports buy side only")
    if input.market not in ("kr", "us"):
        raise ValueError("suggest_order_account supports kr/us markets only")
    if input.quantity <= 0:
        raise ValueError("quantity must be positive")
    if input.price <= 0:
        raise ValueError("price must be positive")

    profiles = build_cost_profiles(input.account_costs)
    notional = _notional(input)
    notional_krw = float(notional["notional_krw"] or 0.0)
    notional_usd = notional["notional_usd"]
    orderable = _orderable_by_account_currency(input.capital_snapshot)
    candidates = _candidate_accounts(input.market)
    cost_comparison = {
        account: _cost_row(
            account_id=account,
            market=input.market,
            notional_krw=notional_krw,
            notional_usd=float(notional_usd) if notional_usd is not None else None,
            cash=orderable.get(account, {}),
            usd_krw=input.usd_krw,
            profiles=profiles,
        )
        for account in candidates
    }
    eligible = {
        account: row
        for account, row in cost_comparison.items()
        if row["eligible"]
    }
    existing = _existing_accounts(
        input.holdings_snapshot,
        symbol=input.symbol,
        market=input.market,
    )
    notes = ["Advisory only. Operator must choose the final order account."]
    if input.market == "us":
        notes.append(
            "US recommendations use a stronger consolidation threshold because FX basis and tax lots split by account."
        )
    data_quality = []
    if profiles.review_required:
        data_quality.append("using_default_account_costs_review_required")

    if not eligible:
        return {
            "success": False,
            "advisory_only": True,
            "symbol": input.symbol,
            "market": input.market,
            "side": input.side.lower(),
            "quantity": input.quantity,
            "price": input.price,
            "notional": notional,
            "recommended_account": None,
            "cost_comparison": cost_comparison,
            "position_consolidation": {
                "existing_accounts": existing,
                "decision": "no_eligible_account",
                "distribution_warning": False,
            },
            "reason_codes": ["no_eligible_account"],
            "data_quality": data_quality,
            "notes": notes,
            "errors": input.capital_snapshot.get("errors", []) + input.holdings_snapshot.get("errors", []),
        }

    cheapest = min(eligible, key=lambda key: eligible[key]["total_cost_krw"])
    threshold_bps = profiles.threshold_bps(input.market)
    threshold_amount = notional_krw * threshold_bps / 10_000.0
    reason_codes: list[str] = []
    preferred_existing = existing[0] if len(existing) == 1 else None
    decision = "no_existing_position"
    recommended = cheapest
    foregone_savings = None
    savings_vs_existing = None
    distribution_warning = False
    existing_ineligible = False

    if len(existing) > 1:
        decision = "already_split_cheapest_eligible"
        reason_codes.append("already_split")
    elif preferred_existing is not None:
        if preferred_existing not in eligible:
            decision = "existing_account_ineligible"
            existing_ineligible = True
            reason_codes.append("existing_account_ineligible")
        else:
            existing_cost = eligible[preferred_existing]["total_cost_krw"]
            cheapest_cost = eligible[cheapest]["total_cost_krw"]
            savings_vs_existing = max(0.0, existing_cost - cheapest_cost)
            if cheapest != preferred_existing and savings_vs_existing >= threshold_amount:
                decision = "break_for_cost"
                recommended = cheapest
                distribution_warning = True
                reason_codes.append("distribution_warning")
            else:
                decision = "keep_existing"
                recommended = preferred_existing
                foregone_savings = savings_vs_existing
                reason_codes.append("existing_position_below_threshold")
    else:
        reason_codes.append("lowest_total_cost")

    return {
        "success": True,
        "advisory_only": True,
        "symbol": input.symbol,
        "market": input.market,
        "side": input.side.lower(),
        "quantity": input.quantity,
        "price": input.price,
        "notional": notional,
        "recommended_account": recommended,
        "cost_comparison": cost_comparison,
        "position_consolidation": {
            "existing_accounts": existing,
            "preferred_existing_account": preferred_existing,
            "threshold_bps": threshold_bps,
            "threshold_amount_krw": threshold_amount,
            "savings_vs_existing_krw": savings_vs_existing,
            "decision": decision,
            "foregone_savings_krw": foregone_savings,
            "distribution_warning": distribution_warning,
            "existing_account_ineligible": existing_ineligible,
            "note": _consolidation_note(decision),
        },
        "reason_codes": reason_codes,
        "data_quality": data_quality,
        "notes": notes,
        "errors": input.capital_snapshot.get("errors", []) + input.holdings_snapshot.get("errors", []),
    }


def _consolidation_note(decision: str) -> str:
    if decision == "keep_existing":
        return "Existing position consolidation wins because savings are below threshold."
    if decision == "break_for_cost":
        return "Cheaper account exceeds the consolidation break threshold; distribution warning applies."
    if decision == "existing_account_ineligible":
        return "Existing position account is not eligible for this buy size."
    if decision == "already_split_cheapest_eligible":
        return "Position is already split across candidate accounts; cheapest eligible account wins."
    if decision == "no_eligible_account":
        return "No compared account has enough eligible buying power."
    return "No existing KIS/Toss position; cheapest eligible account wins."


__all__ = [
    "DEFAULT_ACCOUNT_COSTS",
    "AccountCostProfiles",
    "AccountRoutingInput",
    "MarketCostProfile",
    "build_cost_profiles",
    "compact_cost_profile",
    "suggest_account_from_snapshot",
]
