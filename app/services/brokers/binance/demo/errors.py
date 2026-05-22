"""ROB-298 — Demo-side ledger error vocabulary.

Naming convention: ``BinanceDemo*`` for ledger/state errors that apply
across products. Per-product transport/host errors live alongside their
adapters (e.g. ``app.services.brokers.binance.spot_demo.errors``).
"""

from __future__ import annotations


class BinanceDemoLedgerError(Exception):
    """Base class for demo ledger errors."""


class BinanceDemoInvalidStateTransition(BinanceDemoLedgerError):
    """Raised when a state transition is not in the allowed graph.

    Allowed transitions (PR 1):

        planned    → previewed | cancelled | anomaly
        previewed  → validated | cancelled | anomaly
        validated  → submitted | cancelled | anomaly
        submitted  → filled    | cancelled | anomaly
        filled     → closed    | anomaly
        closed     → reconciled| anomaly
        cancelled  → reconciled| anomaly
        reconciled → (terminal)
        anomaly    → (terminal)
    """


class BinanceDemoInvalidProduct(BinanceDemoLedgerError):
    """Raised when a row's ``product`` is not in the allowed enum."""


class BinanceDemoDuplicateClientOrderId(BinanceDemoLedgerError):
    """Raised when an insert collides with an existing client_order_id."""
