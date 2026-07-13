"""Server-observed market evidence for Alpaca Paper submits (ROB-842).

Both the manual and automated submit paths must attach server-observed / persisted
market evidence to their approval packet — there is no origin-based bypass. This
module loads a trusted ``market_quote_snapshots`` row by an opaque, server-issued
id and derives the packet's market-data as-of/source, signal identity and trusted
reference price. It never trusts a caller-supplied timestamp, source, correlation
or ceiling.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.services.crypto_execution_mapping import (
    CryptoExecutionMappingError,
    map_upbit_to_alpaca_paper,
)

# Server hard-cap per-order notional policy (never caller-supplied).
HARD_NOTIONAL_CAP_USD = Decimal("1000")
CRYPTO_HARD_NOTIONAL_CAP_USD = Decimal("50")

# raw_payload.provenance markers that identify a caller/order-derived (not
# server-observed) snapshot — never usable as trusted evidence.
_SYNTHETIC_PROVENANCE: frozenset[str] = frozenset(
    {"smoke", "smoke_synthetic", "operator_synthetic", "order_derived"}
)


class MarketEvidenceError(Exception):
    """Raised with a stable reason code when trusted evidence is unusable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MarketEvidence:
    quote_snapshot_id: int
    content_hash: str
    correlation_id: str
    snapshot_id: str
    market_data_asof: datetime
    market_data_source: str
    signal_symbol: str
    price: Decimal


def hard_notional_cap(asset_class: str) -> Decimal:
    return (
        CRYPTO_HARD_NOTIONAL_CAP_USD
        if asset_class == "crypto"
        else HARD_NOTIONAL_CAP_USD
    )


def _snapshot_content_hash(snap: MarketQuoteSnapshot) -> str:
    blob = "|".join(
        str(x)
        for x in (
            snap.id,
            snap.market,
            snap.symbol,
            snap.source,
            snap.snapshot_at.isoformat(),
            snap.price,
        )
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _snapshot_matches(
    snap: MarketQuoteSnapshot, *, execution_symbol: str, asset_class: str
) -> bool:
    if asset_class == "crypto":
        if snap.market != "crypto":
            return False
        try:
            mapping = map_upbit_to_alpaca_paper(snap.symbol)
        except CryptoExecutionMappingError:
            return False
        return mapping.execution_symbol == execution_symbol
    return snap.market == "us" and (snap.symbol or "").upper() == execution_symbol


async def load_market_evidence(
    db: AsyncSession,
    quote_snapshot_id: int,
    *,
    execution_symbol: str,
    asset_class: str,
    now: datetime,
    max_age: timedelta,
) -> MarketEvidence:
    """Load + validate a trusted market snapshot into server-owned evidence.

    Raises MarketEvidenceError(code=...) for: no_trusted_snapshot,
    snapshot_symbol_mismatch, invalid_snapshot_price, stale_trusted_snapshot.
    """
    snap = (
        await db.execute(
            select(MarketQuoteSnapshot).where(
                MarketQuoteSnapshot.id == int(quote_snapshot_id)
            )
        )
    ).scalar_one_or_none()
    if snap is None:
        raise MarketEvidenceError(
            "no_trusted_snapshot", "no trusted market snapshot for reference"
        )
    # A caller/order-derived (smoke/operator) snapshot is NOT server-observed market
    # evidence — reject it even though it lives in the trusted table under a real
    # source, so a fabricated price can never back a production submit.
    raw = snap.raw_payload if isinstance(snap.raw_payload, dict) else {}
    if (
        raw.get("synthetic") is True
        or str(raw.get("provenance") or "") in _SYNTHETIC_PROVENANCE
    ):
        raise MarketEvidenceError(
            "synthetic_snapshot",
            "snapshot is caller/order-derived (synthetic) and cannot be trusted evidence",
        )
    if not _snapshot_matches(
        snap, execution_symbol=execution_symbol, asset_class=asset_class
    ):
        raise MarketEvidenceError(
            "snapshot_symbol_mismatch",
            f"trusted snapshot symbol {snap.symbol!r} does not map to {execution_symbol!r}",
        )

    try:
        price = Decimal(str(snap.price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketEvidenceError(
            "invalid_snapshot_price", f"snapshot price {snap.price!r} is not a number"
        ) from exc
    if not price.is_finite() or price <= 0:
        raise MarketEvidenceError(
            "invalid_snapshot_price", f"snapshot price {price} is not finite/positive"
        )

    asof = snap.snapshot_at
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    if now - asof > max_age:
        raise MarketEvidenceError(
            "stale_trusted_snapshot",
            f"trusted snapshot as-of {asof.isoformat()} is stale",
        )

    content_hash = _snapshot_content_hash(snap)
    return MarketEvidence(
        quote_snapshot_id=snap.id,
        content_hash=content_hash,
        correlation_id=f"rob842dec-{content_hash}",
        snapshot_id=f"qs{snap.id}-{content_hash}",
        market_data_asof=asof,
        market_data_source=snap.source,
        signal_symbol=snap.symbol,
        price=price,
    )


__all__ = [
    "CRYPTO_HARD_NOTIONAL_CAP_USD",
    "HARD_NOTIONAL_CAP_USD",
    "MarketEvidence",
    "MarketEvidenceError",
    "hard_notional_cap",
    "load_market_evidence",
]
