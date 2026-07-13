"""Read-only resolution of existing venue-native paper ledger identities."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.review import AlpacaPaperOrderLedger
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


__all__ = ["NativeOrderIdentity", "NativeOrderResolver"]
