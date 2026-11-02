"""Closed vocabularies for strategy sleeves (ROB-1470).

``CANARY_RULES`` copies the canary design of the ROB-1458 rules from
``docs/research/rob-1465-trade-retrospective.md`` §4 (auto_trader#2267) as a
reference: the design's metric and horizon text and the markets each rule may
run in. It holds no number the verdict uses: the binding pre-registration
(metric, minimum effect, H, maximum duration; ROB-1468) and every amount are set
by the operator per sleeve version.

A changed metric or horizon is a new epoch (§4 "Common to all arms"): add a new
rule id, never edit an existing entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models.strategy_sleeves import (
    SLEEVE_ACCOUNT_MARKETS,
    SLEEVE_CURRENCY_BY_MARKET,
    SLEEVE_STATES,
)

#: The actor the dispatch gate writes when it stops a sleeve at its loss limit.
SYSTEM_LOSS_GATE_ACTOR = "system:sleeve_loss_gate"


class SleeveState(StrEnum):
    SHADOW = "shadow"
    LIVE = "live"
    PAUSED = "paused"
    STOPPED = "stopped"


assert tuple(s.value for s in SleeveState) == SLEEVE_STATES


class SleeveGateReason(StrEnum):
    """Why a sleeve-tagged order went to the human approval card."""

    NOT_LIVE = "sleeve_not_live"
    LOSS_LIMIT_REACHED = "sleeve_loss_limit_reached"
    BUDGET_EXCEEDED = "sleeve_budget_exceeded"
    PNL_UNMEASURED = "sleeve_pnl_unmeasured"
    USED_UNMEASURED = "sleeve_used_unmeasured"
    UNREADABLE = "sleeve_unreadable"


@dataclass(frozen=True)
class CanaryRule:
    rule_id: str
    design_section: str
    title: str
    markets: frozenset[str]
    metric: str
    horizon: str


CANARY_RULES: dict[str, CanaryRule] = {
    rule.rule_id: rule
    for rule in (
        CanaryRule(
            rule_id="rob1458.us_near_market_entry",
            design_section="4.1",
            title="US near-market entry instead of deep limits",
            markets=frozenset({"equity_us"}),
            metric=(
                "intent-level D+20 return net of the US index, paired by intent; "
                "fill rate secondary"
            ),
            horizon="50 matured intents or 60 US sessions, whichever is later",
        ),
        CanaryRule(
            rule_id="rob1458.us_add_at_minus8",
            design_section="4.2",
            title="US mechanical 50% add at the first close <= -8%",
            markets=frozenset({"equity_us"}),
            metric=(
                "D+20 return of the added tranche against the same quantity "
                "not added, index-neutral"
            ),
            horizon="20 matured adds or 60 US sessions",
        ),
        CanaryRule(
            rule_id="rob1458.kr_cut_minus20",
            design_section="4.3",
            title="KR -20% cut (live arm opens only after the shadow gate)",
            markets=frozenset({"equity_kr"}),
            metric=(
                "cut-minus-actual, notional-weighted, at D+20 and to the end of "
                "the window; share of episodes where the cut beat the actual"
            ),
            horizon="20 touched episodes with 60 KR sessions matured",
        ),
        CanaryRule(
            rule_id="rob1458.parking_idle_cash",
            design_section="4.4",
            title="KR and US parking only cash that will stay idle",
            markets=frozenset({"equity_kr", "equity_us"}),
            metric="median lot return net of fees minus the account's cash rate",
            horizon="10 closed parking lots",
        ),
        CanaryRule(
            rule_id="rob1458.crypto_sell_expiry",
            design_section="4.5",
            title="Crypto expiry on resting sells at +8% or more",
            markets=frozenset({"crypto"}),
            metric="per band: fill rate, median days to fill, rest-vs-sell-now",
            horizon="30 sells per band, or 60 days",
        ),
        CanaryRule(
            rule_id="rob1458.crypto_b3",
            design_section="4.6",
            title="Crypto B3 rule set (live only after the mock verdict)",
            markets=frozenset({"crypto"}),
            metric=(
                "return per unit against the crypto index per unit; max drawdown "
                "of the book"
            ),
            horizon="60 crypto sessions or 30 completed round trips",
        ),
    )
}

ACCOUNT_MARKETS = frozenset(SLEEVE_ACCOUNT_MARKETS)
CURRENCY_BY_MARKET = dict(SLEEVE_CURRENCY_BY_MARKET)

#: Proposal account mode -> execution_ledger (broker, account_mode).
LEDGER_IDENTITY: dict[str, tuple[str, str]] = {
    "kis_live": ("kis", "live"),
    "toss_live": ("toss", "live"),
    "upbit": ("upbit", "live"),
}
assert {mode for mode, _market in SLEEVE_ACCOUNT_MARKETS} == set(LEDGER_IDENTITY)
