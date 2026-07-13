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


class BinanceDemoDuplicateAcknowledgement(BinanceDemoLedgerError):
    """Raised when a broker ack is replayed onto a second ledger row (ROB-844).

    A non-null ``(product, venue_host, broker_order_id)`` may attach to exactly
    one row (partial-unique index ``uq_binance_demo_ledger_broker_ack``). This
    typed error is the normalized, stable ``duplicate_acknowledgement`` result —
    the service converts the underlying ``IntegrityError`` here so it never
    leaks to the executor / MCP boundary. Carries ``result`` for structured
    callers.
    """

    result = "duplicate_acknowledgement"


class BinanceDemoCredentialError(Exception):
    """Base class for shared Demo credential resolution errors (ROB-302).

    Distinct from the ledger hierarchy: credential resolution applies across
    products (spot + futures) and is read at client construction time, before
    any HTTP/DB activity. Lane ``from_env`` methods catch these and re-raise the
    lane-specific ``Binance{Spot,Futures}DemoMissingCredentials`` so existing
    fail-closed contracts (and the smoke CLI's catch blocks) are preserved.
    """


class BinanceDemoMissingCredentials(BinanceDemoCredentialError):
    """No usable Demo credential pair found for the requested product.

    Neither the product-specific pair nor the canonical ``BINANCE_DEMO_*`` pair
    was present.
    """


class BinanceDemoIncompleteCredentialOverride(BinanceDemoCredentialError):
    """A credential source supplied only half of a key/secret pair.

    Per Codex review #2: when a product-specific override sets the key XOR the
    secret, we MUST fail closed rather than backfill the missing half from the
    canonical pair — pairing a product key with a canonical secret would submit
    a mismatched HMAC credential. Same guard applies to a half-set canonical
    pair.
    """
