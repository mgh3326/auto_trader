from datetime import datetime
from typing import Any

import httpx

from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.endpoints import (
    FORBIDDEN_TRADING_BASE_URLS,
    PAPER_TRADING_BASE_URL,
)
from app.services.brokers.alpaca.exceptions import (
    AlpacaPaperConfigurationError,
    AlpacaPaperEndpointError,
    AlpacaPaperRequestError,
)
from app.services.brokers.alpaca.schemas import (
    AccountSnapshot,
    Asset,
    CashBalance,
    Fill,
    Order,
    OrderRequest,
    Position,
)
from app.services.brokers.alpaca.transport import HTTPTransport, HttpxTransport


class AlpacaPaperBrokerService:
    def __init__(
        self,
        transport: HTTPTransport | None = None,
        settings: AlpacaPaperSettings | None = None,
    ) -> None:
        if settings is None:
            settings = AlpacaPaperSettings.from_app_settings()

        if not settings.api_key or not settings.api_secret:
            raise AlpacaPaperConfigurationError(
                "alpaca_paper_api_key and alpaca_paper_api_secret must both be set"
            )

        base_url = settings.base_url.rstrip("/")

        if base_url in FORBIDDEN_TRADING_BASE_URLS:
            raise AlpacaPaperEndpointError(
                f"Forbidden trading base URL: '{base_url}'. "
                "Only the paper endpoint is allowed."
            )

        if base_url != PAPER_TRADING_BASE_URL:
            raise AlpacaPaperEndpointError(
                f"Trading base URL must be exactly '{PAPER_TRADING_BASE_URL}', "
                f"got '{base_url}'."
            )

        self._settings = settings
        self._transport: HTTPTransport = transport or HttpxTransport(
            base_url=base_url,
            api_key=settings.api_key,
            api_secret=settings.api_secret,
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        try:
            response = await self._transport.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AlpacaPaperRequestError(str(exc)) from exc

        if response.status_code >= 400:
            raise AlpacaPaperRequestError(
                f"HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
            )

        if response.status_code == 204 or not response.content:
            return None

        return response.json()

    async def get_account(self) -> AccountSnapshot:
        data = await self._request("GET", "/v2/account")
        return AccountSnapshot.model_validate(data)

    async def get_cash(self) -> CashBalance:
        data = await self._request("GET", "/v2/account")
        return CashBalance(
            cash=data["cash"],
            buying_power=data["buying_power"],
        )

    async def list_positions(self) -> list[Position]:
        data = await self._request("GET", "/v2/positions")
        if not data:
            return []
        return [Position.model_validate(item) for item in data]

    async def list_assets(
        self,
        *,
        status: str | None = None,
        asset_class: str | None = None,
    ) -> list[Asset]:
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = status
        if asset_class is not None:
            params["asset_class"] = asset_class
        data = await self._request("GET", "/v2/assets", params=params)
        if not data:
            return []
        return [Asset.model_validate(item) for item in data]

    async def submit_order(self, request: OrderRequest) -> Order:
        body = request.model_dump(mode="json", exclude_none=True)
        data = await self._request("POST", "/v2/orders", json=body)
        return Order.model_validate(data)

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[Order]:
        params: dict[str, str | int] = {}
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        data = await self._request("GET", "/v2/orders", params=params)
        if not data:
            return []
        return [Order.model_validate(item) for item in data]

    async def cancel_order(self, order_id: str) -> None:
        await self._request("DELETE", f"/v2/orders/{order_id}")

    async def get_order(self, order_id: str) -> Order:
        data = await self._request("GET", f"/v2/orders/{order_id}")
        return Order.model_validate(data)

    async def list_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[Fill]:
        params: dict[str, str | int] = {}
        if after is not None:
            params["after"] = after.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        if limit is not None:
            params["limit"] = limit
        data = await self._request("GET", "/v2/account/activities/FILL", params=params)
        if not data:
            return []
        return [Fill.model_validate(item) for item in data]
