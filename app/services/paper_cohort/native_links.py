"""Read-only resolution of existing venue-native paper ledger identities."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.review import AlpacaPaperOrderLedger
from app.services.alpaca_paper_submit_service import (
    build_canonical_payload,
    derive_automated_key,
)
from app.services.brokers.paper.contracts import (
    PaperOrderRequest,
    VerifiedExperimentProvenance,
    derive_paper_idempotency_key,
)
from app.services.paper_cohort.contracts import PaperCohortError


class NativeOrderIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Literal["binance", "alpaca"]
    ledger_kind: Literal["binance_demo_order_ledger", "alpaca_paper_order_ledger"]
    ledger_row_id: int
    client_order_id: str
    broker_order_id: str


class NativeOrderResolver:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self,
        venue: str,
        client_order_id: str,
        broker_order_id: str,
    ) -> NativeOrderIdentity:
        if venue == "binance":
            row = await self._session.scalar(
                select(BinanceDemoOrderLedger).where(
                    BinanceDemoOrderLedger.client_order_id == client_order_id,
                    BinanceDemoOrderLedger.product == "spot",
                )
            )
            kind = "binance_demo_order_ledger"
        elif venue == "alpaca":
            row = await self._session.scalar(
                select(AlpacaPaperOrderLedger).where(
                    AlpacaPaperOrderLedger.client_order_id == client_order_id,
                    AlpacaPaperOrderLedger.record_kind == "execution",
                    AlpacaPaperOrderLedger.account_mode == "alpaca_paper",
                )
            )
            kind = "alpaca_paper_order_ledger"
        else:
            raise PaperCohortError("unsupported_capability")
        if (
            row is None
            or row.broker_order_id is None
            or row.broker_order_id != broker_order_id
        ):
            raise PaperCohortError("native_order_identity_mismatch")
        return NativeOrderIdentity(
            venue=venue,
            ledger_kind=kind,
            ledger_row_id=row.id,
            client_order_id=row.client_order_id,
            broker_order_id=row.broker_order_id,
        )

    async def resolve_prepared(
        self,
        request: PaperOrderRequest,
        provenance: VerifiedExperimentProvenance,
    ) -> NativeOrderIdentity:
        """Resolve deterministic ROB-845 native identity without mutation.

        This is deliberately ledger-only.  A missing row means the immutable
        intent was never submitted and recovery must not create it.
        """

        idempotency_key = derive_paper_idempotency_key(provenance)
        if request.venue.value == "binance":
            digest = hashlib.sha256(f"{idempotency_key}:root".encode()).hexdigest()[:24]
            client_order_id = f"rob845r-{digest}"
        elif request.venue.value == "alpaca":
            canonical = build_canonical_payload(
                symbol=request.symbol,
                side=request.side,
                type=request.order_type,
                time_in_force=request.time_in_force,
                qty=request.qty,
                notional=request.notional,
                limit_price=request.price,
                asset_class="crypto",
            )
            client_order_id = derive_automated_key(
                correlation_id=hashlib.sha256(idempotency_key.encode()).hexdigest(),
                snapshot_id=request.market_snapshot_id,
                canonical=canonical,
            )
        else:  # pragma: no cover - enum and capability gate constrain this
            raise PaperCohortError("unsupported_capability")
        return await self._resolve_client(request.venue.value, client_order_id)

    async def _resolve_client(
        self, venue: str, client_order_id: str
    ) -> NativeOrderIdentity:
        if venue == "binance":
            row = await self._session.scalar(
                select(BinanceDemoOrderLedger).where(
                    BinanceDemoOrderLedger.client_order_id == client_order_id,
                    BinanceDemoOrderLedger.product == "spot",
                )
            )
            kind = "binance_demo_order_ledger"
        elif venue == "alpaca":
            row = await self._session.scalar(
                select(AlpacaPaperOrderLedger).where(
                    AlpacaPaperOrderLedger.client_order_id == client_order_id,
                    AlpacaPaperOrderLedger.record_kind == "execution",
                    AlpacaPaperOrderLedger.account_mode == "alpaca_paper",
                )
            )
            kind = "alpaca_paper_order_ledger"
        else:
            raise PaperCohortError("unsupported_capability")
        if row is None or row.broker_order_id is None:
            raise PaperCohortError("native_order_not_found")
        return NativeOrderIdentity(
            venue=venue,
            ledger_kind=kind,
            ledger_row_id=row.id,
            client_order_id=row.client_order_id,
            broker_order_id=row.broker_order_id,
        )


__all__ = ["NativeOrderIdentity", "NativeOrderResolver"]
