"""Evaluation contract v1 (ROB-1433): one contract, per-market adapters.

Research-only. Nothing here imports a broker, account, database session,
scheduler or in-process LLM provider; the KR adapter wraps the existing
``research.kr_corpus.d3_engine`` harness instead of rewriting it.
"""

from research.eval_contract.contract import (
    CONTRACT_VERSION,
    HORIZONS,
    CostModel,
    Decision,
    EvaluationReport,
    FillModelResult,
    FillRow,
    Intent,
    Market,
    MissingRegistryReference,
    OrderSpec,
    Outcome,
    Provenance,
    RegistryRef,
    Side,
    sha256_canonical,
)
from research.eval_contract.fill_models import (
    BRACKET,
    ConservativeThrough,
    EmpiricalFillModel,
    FillContext,
    FillModel,
    FillOutcome,
    OptimisticTouch,
    VolumeShare,
)
from research.eval_contract.report import render_report

__all__ = [
    "BRACKET",
    "CONTRACT_VERSION",
    "HORIZONS",
    "ConservativeThrough",
    "CostModel",
    "Decision",
    "EmpiricalFillModel",
    "EvaluationReport",
    "FillContext",
    "FillModel",
    "FillModelResult",
    "FillOutcome",
    "FillRow",
    "Intent",
    "Market",
    "MissingRegistryReference",
    "OptimisticTouch",
    "OrderSpec",
    "Outcome",
    "Provenance",
    "RegistryRef",
    "Side",
    "VolumeShare",
    "render_report",
    "sha256_canonical",
]
