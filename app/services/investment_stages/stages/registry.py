"""Stage registry and factory (ROB-279)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.investment_stages.stages.bear_reducer import BearReducerStage
from app.services.investment_stages.stages.bull_reducer import BullReducerStage
from app.services.investment_stages.stages.candidate_universe import CandidateUniverseStage
from app.services.investment_stages.stages.market import MarketStage
from app.services.investment_stages.stages.news import NewsStage
from app.services.investment_stages.stages.portfolio_journal import PortfolioJournalStage
from app.services.investment_stages.stages.risk_review import RiskReviewStage
from app.services.investment_stages.stages.watch_context import WatchContextStage

if TYPE_CHECKING:
    from app.services.ai_providers.gemini_provider import GeminiProvider
    from app.services.investment_stages.budget import StageLLMBudget
    from app.services.investment_stages.stages.base import Stage


def get_default_v1_stages(
    provider: GeminiProvider, budget: StageLLMBudget
) -> list[Stage]:
    """Returns the ordered list of default stages for ROB-279 v1."""
    return [
        MarketStage(),
        NewsStage(),
        PortfolioJournalStage(),
        WatchContextStage(),
        CandidateUniverseStage(),
        BullReducerStage(provider, budget),
        BearReducerStage(provider, budget),
        RiskReviewStage(provider, budget),
    ]
