"""ROB-107 deterministic stage generators for committee decision sessions.

Pure functions only. No I/O, no broker / KIS / scheduler / credential
imports. Each generator takes typed inputs and returns Pydantic artifacts
that the committee workflow stores in
``trading_decision_sessions.artifacts``.

The Bull/Bear debate classifier follows the rule examples called out in the
ROB-107 spec ("RSI > 70 -> Bear evidence", "support bounce -> Bull evidence",
"overweight position -> Bear evidence", "positive news -> Bull evidence").
LLM-driven debate is intentionally out of scope for this MVP.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.trading_decisions import (
    CommitteeDebateClaim,
    CommitteePortfolioApproval,
    CommitteeResearchDebate,
    CommitteeRiskReview,
    CommitteeTraderActionLiteral,
    CommitteeTraderDraft,
)

SIMULATION_ACCOUNT_MODES: frozenset[str] = frozenset({"kis_mock", "alpaca_paper"})


@dataclass(frozen=True)
class TechnicalSnapshot:
    symbol: str
    current_price: Decimal | None = None
    nearest_support: Decimal | None = None
    nearest_resistance: Decimal | None = None
    rsi: Decimal | None = None


@dataclass(frozen=True)
class NewsSnapshot:
    headline: str
    risk_category: str | None = None  # "positive" / "negative" / "neutral"
    included: bool = True


@dataclass(frozen=True)
class PortfolioSnapshot:
    symbol: str
    held: bool = False
    portfolio_weight_pct: Decimal | None = None


# ---------- Bull/Bear classifier ----------


def classify_research_debate(
    *,
    technical: Iterable[TechnicalSnapshot] = (),
    news: Iterable[NewsSnapshot] = (),
    portfolio: Iterable[PortfolioSnapshot] = (),
) -> CommitteeResearchDebate:
    """Build a research debate from deterministic rules.

    Rules (mirrors the spec examples):
      - RSI > 70  -> Bear (technical, high)
      - RSI < 30  -> Bull (technical, medium)
      - price within 2% of nearest support, above support -> Bull (technical)
      - price below nearest support -> Bear (technical, high)
      - news.risk_category == "positive" -> Bull (news)
      - news.risk_category == "negative" -> Bear (news)
      - portfolio.held and weight > 15% -> Bear (portfolio)
    """
    bull: list[CommitteeDebateClaim] = []
    bear: list[CommitteeDebateClaim] = []

    for snap in technical:
        sym = snap.symbol
        if snap.rsi is not None:
            if snap.rsi >= Decimal("70"):
                bear.append(
                    CommitteeDebateClaim(
                        text=f"{sym} RSI {snap.rsi} overbought (>=70)",
                        weight="high",
                        source="technical",
                    )
                )
            elif snap.rsi <= Decimal("30"):
                bull.append(
                    CommitteeDebateClaim(
                        text=f"{sym} RSI {snap.rsi} oversold (<=30)",
                        weight="medium",
                        source="technical",
                    )
                )

        if snap.current_price is not None and snap.nearest_support is not None:
            support = snap.nearest_support
            if support > 0 and snap.current_price < support:
                bear.append(
                    CommitteeDebateClaim(
                        text=f"{sym} broke nearest support {support}",
                        weight="high",
                        source="technical",
                    )
                )
            elif support > 0:
                gap = (snap.current_price - support) / support
                if Decimal("0") <= gap <= Decimal("0.02"):
                    bull.append(
                        CommitteeDebateClaim(
                            text=f"{sym} bouncing off support {support}",
                            weight="medium",
                            source="technical",
                        )
                    )

    for n in news:
        if not n.included:
            continue
        category = (n.risk_category or "").lower()
        if category in {"positive", "tailwind"}:
            bull.append(
                CommitteeDebateClaim(text=n.headline, weight="medium", source="news")
            )
        elif category in {"negative", "headwind", "risk"}:
            bear.append(
                CommitteeDebateClaim(text=n.headline, weight="medium", source="news")
            )

    for p in portfolio:
        if (
            p.held
            and p.portfolio_weight_pct is not None
            and p.portfolio_weight_pct > Decimal("15")
        ):
            bear.append(
                CommitteeDebateClaim(
                    text=(
                        f"{p.symbol} portfolio weight "
                        f"{p.portfolio_weight_pct}% — overweight"
                    ),
                    weight="medium",
                    source="portfolio",
                )
            )

    summary = None
    if bull or bear:
        summary = f"bull_signals={len(bull)} bear_signals={len(bear)}"
    return CommitteeResearchDebate(bull_case=bull, bear_case=bear, summary=summary)


# ---------- Trader draft ----------


def build_trader_draft(
    *,
    symbol: str,
    action_hint: CommitteeTraderActionLiteral,
    risk_review: CommitteeRiskReview | None = None,
    confidence: str = "medium",
    price_plan: str | None = None,
    size_plan: str | None = None,
    rationale: str | None = None,
    invalidation_condition: str | None = None,
    next_step_recommendation: str | None = None,
) -> CommitteeTraderDraft:
    """Build a trader-draft artifact. If risk verdict is vetoed, force AVOID."""
    action: CommitteeTraderActionLiteral = action_hint
    if risk_review is not None and risk_review.verdict == "vetoed":
        action = "AVOID"
    return CommitteeTraderDraft(
        symbol=symbol,
        action=action,
        price_plan=price_plan,
        size_plan=size_plan,
        rationale=rationale,
        confidence=confidence,  # type: ignore[arg-type]
        invalidation_condition=invalidation_condition,
        next_step_recommendation=next_step_recommendation,
    )


# ---------- Auto-approval ----------


def build_auto_approval(
    *,
    risk_review: CommitteeRiskReview,
    account_mode: str | None,
) -> CommitteePortfolioApproval:
    """Auto-approve KIS mock / Alpaca paper sessions when risk is not vetoed.

    Matches the spec safety boundary: simulation sessions are auto-approved by
    simulation policy; non-simulation modes are out of scope for auto-approval
    in this MVP and remain "modified" so a human gate is required.
    """
    if risk_review.verdict == "vetoed":
        return CommitteePortfolioApproval(
            verdict="vetoed",
            notes=f"risk_review_vetoed: {risk_review.notes or 'no detail'}",
            approved_at=datetime.now(UTC),
        )
    if account_mode in SIMULATION_ACCOUNT_MODES:
        return CommitteePortfolioApproval(
            verdict="approved",
            notes="simulation_policy: auto-approved (live execution disabled)",
            approved_at=datetime.now(UTC),
        )
    return CommitteePortfolioApproval(
        verdict="modified",
        notes="auto-approval out of scope for non-simulation account_mode",
        approved_at=None,
    )
