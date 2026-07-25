"""Production composition root for the canonical paper execution façade."""

from __future__ import annotations

from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import ExperimentProvenanceVerifier


def build_paper_execution_application(
    *,
    verifier: ExperimentProvenanceVerifier | None,
) -> PaperExecutionApplication:
    """Compose both guarded venue adapters without performing broker or DB I/O.

    Venue imports remain lazy so disabled and unrelated MCP profiles do not load
    any Alpaca/Binance runtime surface. ROB-849 can inject its canonical verifier
    here without redefining the adapter registry or capability ownership.
    """
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter
    from app.services.brokers.binance.paper_adapter import BinanceSpotDemoPaperAdapter
    from app.services.brokers.paper.adapter_registry import PaperAdapterRegistry

    registry = PaperAdapterRegistry()
    registry.register(BinanceSpotDemoPaperAdapter())
    registry.register(AlpacaCryptoPaperAdapter())
    return PaperExecutionApplication(registry=registry, verifier=verifier)


__all__ = ["build_paper_execution_application"]
