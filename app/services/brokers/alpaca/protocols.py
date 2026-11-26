from datetime import date as Date
from datetime import datetime
from typing import Protocol

from app.services.brokers.alpaca.schemas import (
    AccountSnapshot,
    Asset,
    CashBalance,
    CryptoFeeActivity,
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
        order_id: str | None = None,
        date: Date | None = None,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
        direction: str | None = None,
    ) -> list[Fill]: ...

    async def list_order_fills(self, *, order_id: str) -> list[Fill]: ...

    async def list_fills_for_date(self, *, date: Date) -> list[Fill]: ...

    async def list_crypto_fee_activities(
        self, *, order_id: str
    ) -> list[CryptoFeeActivity]: ...
