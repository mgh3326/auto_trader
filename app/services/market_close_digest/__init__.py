"""ROB-1297 market-close digest — deterministic read-only aggregation."""

from app.services.market_close_digest.service import run_market_close_digest
from app.services.market_close_digest.types import DigestRunResult, DigestSnapshot

__all__ = [
    "DigestRunResult",
    "DigestSnapshot",
    "run_market_close_digest",
]
