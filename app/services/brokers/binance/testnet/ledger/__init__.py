"""ROB-286 — Ledger sub-package for the testnet execution adapter.

The repository (``BinanceTestnetLedgerRepository``) is service-internal.
Use ``BinanceTestnetLedgerService`` as the public write surface. The
test ``test_ledger_service::test_repository_import_boundary_enforced``
walks every ``app/**.py`` file with the AST module and fails if any
file other than ``app/services/brokers/binance/testnet/ledger/service.py``
imports the repository module or class.
"""

from __future__ import annotations

from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)

__all__ = ["BinanceTestnetLedgerService"]
