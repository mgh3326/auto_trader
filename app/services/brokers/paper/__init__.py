"""Canonical paper execution façade contracts and capability re-exports."""

from app.services.brokers.capabilities import (
    PAPER_BROKER_CAPABILITIES,
    PaperBrokerCapabilities,
    get_paper_capabilities,
)
from app.services.brokers.paper.adapter_registry import PaperAdapterRegistry
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    ExperimentProvenanceVerifier,
    PaperBrokerPort,
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
    PaperReasonCode,
    PaperRiskSnapshot,
    VerifiedExperimentProvenance,
    VerifiedPaperOrderIntent,
    derive_paper_idempotency_key,
)

__all__ = [
    "ExperimentProvenanceVerifier",
    "PAPER_BROKER_CAPABILITIES",
    "PaperAdapterRegistry",
    "PaperBrokerCapabilities",
    "PaperBrokerPort",
    "PaperExecutionApplication",
    "PaperOperation",
    "PaperOperationResult",
    "PaperOperationStatus",
    "PaperOrderRequest",
    "PaperReasonCode",
    "PaperRiskSnapshot",
    "VerifiedExperimentProvenance",
    "VerifiedPaperOrderIntent",
    "derive_paper_idempotency_key",
    "get_paper_capabilities",
]
