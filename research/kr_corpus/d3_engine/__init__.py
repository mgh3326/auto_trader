"""Deterministic, research-only D3 event-driven portfolio engine.

The package is deliberately isolated from the existing Stage-B implementation and
from every broker, account, database, scheduler, and in-process LLM surface.
"""

from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.models import (
    Arm,
    Bar,
    CashflowView,
    DataView,
    EngineConfig,
    PortfolioRunInput,
)

__all__ = [
    "Arm",
    "Bar",
    "CashflowView",
    "DataView",
    "EngineConfig",
    "PortfolioEngine",
    "PortfolioRunInput",
]
