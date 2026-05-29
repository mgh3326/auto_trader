"""Stage registry and factory (ROB-279, ROB-287).

ROB-287: only deterministic stages are registered. The previous
``bull_reducer`` / ``bear_reducer`` / ``risk_review`` LLM stages and the
``FinalComposer`` were removed when LLM reasoning/composition moved to
Hermes — auto_trader no longer instantiates an in-process LLM provider
for the ``/invest/reports`` staged pipeline.
"""

from __future__ import annotations

from app.services.investment_stages.stages.base import Stage
from app.services.investment_stages.stages.candidate_universe import (
    CandidateUniverseStage,
)
from app.services.investment_stages.stages.market import MarketStage
from app.services.investment_stages.stages.news import NewsStage
from app.services.investment_stages.stages.portfolio_journal import (
    PortfolioJournalStage,
)
from app.services.investment_stages.stages.symbol import SymbolStage
from app.services.investment_stages.stages.watch_context import WatchContextStage


def get_default_v1_stages() -> list[Stage]:
    """Return the deterministic stage set used to derive Hermes context.

    These stages read only persisted snapshot payloads — no LLM calls
    and no external service mutations. The returned list is the v1
    contract surfaced to Hermes via
    :class:`HermesContextExporter`.
    """
    return [
        MarketStage(),
        NewsStage(),
        PortfolioJournalStage(),
        WatchContextStage(),
        CandidateUniverseStage(),
        SymbolStage(),
    ]
