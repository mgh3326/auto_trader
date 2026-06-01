"""snapshot 기반 get_symbol_analysis 계약 (ROB-397 foundation)."""

from app.services.symbol_analysis.authority import (
    AUTHORITY,
    CATEGORIES,
    CORE_CATEGORIES,
    NON_AUTHORITY_SOURCES,
    AuthoritySpec,
)
from app.services.symbol_analysis.contract import (
    ConsensusData,
    DerivedBlock,
    FieldBlock,
    FlowData,
    Freshness,
    GetSymbolAnalysis,
    PriceData,
    PriceLevel,
    Provenance,
    SymbolAnalysis,
    TechnicalData,
    ValuationData,
)
from app.services.symbol_analysis.derived import RULE_VERSION, derive_recommendation
from app.services.symbol_analysis.freshness import compute_is_stale, derive_freshness

__all__ = [
    "AUTHORITY",
    "AuthoritySpec",
    "CATEGORIES",
    "CORE_CATEGORIES",
    "ConsensusData",
    "DerivedBlock",
    "FieldBlock",
    "FlowData",
    "Freshness",
    "GetSymbolAnalysis",
    "NON_AUTHORITY_SOURCES",
    "PriceData",
    "PriceLevel",
    "Provenance",
    "RULE_VERSION",
    "SymbolAnalysis",
    "TechnicalData",
    "ValuationData",
    "compute_is_stale",
    "derive_freshness",
    "derive_recommendation",
]
