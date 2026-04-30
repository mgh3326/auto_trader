from datetime import datetime
from typing import Protocol

from app.services.brokers.alpaca.schemas import (
    AccountSnapshot,
    Asset,
    CashBalance,
    Fill,
    Order,
    OrderRequest,
    Position,
)


class AlpacaPaperBrokerProtocol(Protocol):
    async def get_account(self) -> AccountSnapshot: ...

    async def get_cash(self) -> CashBalance: ...

    async def list_positions(self) -> list[Position]: ...

    async def list_assets(
        self,
        *,
        status: str | None = None,
        asset_class: str | None = None,
    ) -> list[Asset]: ...

    async def submit_order(self, request: OrderRequest) -> Order: ...

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[Order]: ...

    async def cancel_order(self, order_id: str) -> None: ...

    async def get_order(self, order_id: str) -> Order: ...

    async def list_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[Fill]: ...
