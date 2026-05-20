"""ROB-286 — Ledger sub-package for the testnet execution adapter.

The repository (``BinanceTestnetLedgerRepository``) is service-internal.
Use ``BinanceTestnetLedgerService`` as the public write surface. The
test ``test_ledger_service::test_repository_not_importable_externally``
asserts that ``app.services.brokers.binance.testnet.ledger.repository._public_export``
raises ``ImportError`` — the convention is satisfied by-construction
because ``_public_export`` is a private symbol inside the repository
module, not a submodule.
"""

from __future__ import annotations

from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)

__all__ = ["BinanceTestnetLedgerService"]
