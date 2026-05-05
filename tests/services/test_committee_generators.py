"""ROB-107 unit tests for deterministic committee generators."""

from decimal import Decimal

import pytest

from app.schemas.trading_decisions import CommitteeRiskReview
from app.services.trading_decisions.committee_generators import (
    NewsSnapshot,
    PortfolioSnapshot,
    TechnicalSnapshot,
    build_auto_approval,
    build_trader_draft,
    classify_research_debate,
)

# ---------- Bull/Bear classifier ----------


@pytest.mark.unit
def test_rsi_over_70_classifies_as_bear():
    debate = classify_research_debate(
        technical=[
            TechnicalSnapshot(symbol="AAPL", rsi=Decimal("75")),
        ]
    )
    assert any("RSI" in c.text for c in debate.bear_case)
    assert all("RSI" not in c.text for c in debate.bull_case)


@pytest.mark.unit
def test_rsi_under_30_classifies_as_bull():
    debate = classify_research_debate(
        technical=[TechnicalSnapshot(symbol="AAPL", rsi=Decimal("25"))]
    )
    assert any("RSI" in c.text for c in debate.bull_case)


@pytest.mark.unit
def test_support_break_classifies_as_bear():
    debate = classify_research_debate(
        technical=[
            TechnicalSnapshot(
                symbol="AAPL",
                current_price=Decimal("169"),
                nearest_support=Decimal("170"),
            )
        ]
    )
    assert any("broke nearest support" in c.text for c in debate.bear_case)


@pytest.mark.unit
def test_support_bounce_classifies_as_bull():
    # 1.5% above support → within 2% bounce window
    debate = classify_research_debate(
        technical=[
            TechnicalSnapshot(
                symbol="AAPL",
                current_price=Decimal("172.55"),
                nearest_support=Decimal("170"),
            )
        ]
    )
    assert any("bouncing off support" in c.text for c in debate.bull_case)


@pytest.mark.unit
def test_positive_news_classifies_as_bull():
    debate = classify_research_debate(
        news=[NewsSnapshot(headline="Earnings beat", risk_category="positive")]
    )
    assert any(c.text == "Earnings beat" for c in debate.bull_case)


@pytest.mark.unit
def test_negative_news_classifies_as_bear():
    debate = classify_research_debate(
        news=[NewsSnapshot(headline="DOJ probe", risk_category="negative")]
    )
    assert any(c.text == "DOJ probe" for c in debate.bear_case)


@pytest.mark.unit
def test_excluded_news_is_ignored():
    debate = classify_research_debate(
        news=[
            NewsSnapshot(headline="Excluded", risk_category="positive", included=False)
        ]
    )
    assert debate.bull_case == []
    assert debate.bear_case == []


@pytest.mark.unit
def test_overweight_position_classifies_as_bear():
    debate = classify_research_debate(
        portfolio=[
            PortfolioSnapshot(
                symbol="AAPL", held=True, portfolio_weight_pct=Decimal("18")
            )
        ]
    )
    assert any("overweight" in c.text for c in debate.bear_case)


@pytest.mark.unit
def test_summary_counts_signals():
    debate = classify_research_debate(
        technical=[TechnicalSnapshot(symbol="AAPL", rsi=Decimal("75"))],
        news=[NewsSnapshot(headline="ok", risk_category="positive")],
    )
    assert debate.summary == "bull_signals=1 bear_signals=1"


# ---------- Trader draft ----------


@pytest.mark.unit
def test_trader_draft_forces_avoid_when_risk_vetoed():
    risk = CommitteeRiskReview(verdict="vetoed", notes="liquidity_low")
    draft = build_trader_draft(
        symbol="AAPL",
        action_hint="BUY",
        risk_review=risk,
    )
    assert draft.action == "AVOID"
    assert draft.is_live_order is False


@pytest.mark.unit
def test_trader_draft_keeps_action_when_risk_approved():
    risk = CommitteeRiskReview(verdict="approved")
    draft = build_trader_draft(symbol="AAPL", action_hint="BUY", risk_review=risk)
    assert draft.action == "BUY"


# ---------- Auto-approval ----------


@pytest.mark.unit
def test_auto_approval_for_kis_mock_when_risk_not_vetoed():
    approval = build_auto_approval(
        risk_review=CommitteeRiskReview(verdict="approved"),
        account_mode="kis_mock",
    )
    assert approval.verdict == "approved"
    assert approval.notes is not None and "simulation_policy" in approval.notes


@pytest.mark.unit
def test_auto_approval_for_alpaca_paper_when_risk_not_vetoed():
    approval = build_auto_approval(
        risk_review=CommitteeRiskReview(verdict="approved"),
        account_mode="alpaca_paper",
    )
    assert approval.verdict == "approved"


@pytest.mark.unit
def test_auto_approval_blocks_when_risk_vetoed():
    approval = build_auto_approval(
        risk_review=CommitteeRiskReview(verdict="vetoed", notes="too_risky"),
        account_mode="alpaca_paper",
    )
    assert approval.verdict == "vetoed"


@pytest.mark.unit
def test_auto_approval_out_of_scope_for_kis_live():
    approval = build_auto_approval(
        risk_review=CommitteeRiskReview(verdict="approved"),
        account_mode="kis_live",
    )
    assert approval.verdict == "modified"
    assert approval.notes is not None
    assert "out of scope" in approval.notes


@pytest.mark.unit
def test_auto_approval_out_of_scope_when_account_mode_none():
    approval = build_auto_approval(
        risk_review=CommitteeRiskReview(verdict="approved"),
        account_mode=None,
    )
    assert approval.verdict == "modified"
