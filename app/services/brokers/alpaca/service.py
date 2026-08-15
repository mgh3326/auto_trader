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
    AlpacaPaperIdentityMismatch,
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
from app.services.brokers.client_order_ids import (
    BrokerClientIdTarget,
    assert_broker_client_order_id,
)


class AlpacaPaperBrokerService:
    def __init__(
        self,
        transport: HTTPTransport | None = None,
        settings: AlpacaPaperSettings | None = None,
        *,
        profile: str | None = None,
    ) -> None:
        if settings is None:
            settings = (
                AlpacaPaperSettings.from_app_settings()
                if profile is None
                else AlpacaPaperSettings.from_app_settings(profile=profile)
            )

        if (
            profile == "clean"
            or settings.expected_account_id_suffix is not None
            or settings.expected_account_number_suffix is not None
        ) and (
            not settings.expected_account_id_suffix
            or not settings.expected_account_number_suffix
        ):
            raise AlpacaPaperConfigurationError(
                "clean Alpaca route requires expected account identity suffixes"
            )

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
        self._identity_verified = False
        self._identity_check_in_progress = False
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
        skip_identity = bool(kwargs.pop("_skip_identity", False))
        if (
            not skip_identity
            and not self._identity_verified
            and (
                self._settings.expected_account_id_suffix is not None
                or self._settings.expected_account_number_suffix is not None
            )
        ):
            await self._verify_physical_identity()
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

    async def _verify_physical_identity(self) -> None:
        """Bind configured credentials to the configured physical account.

        The expected values are deployment configuration. They are deliberately
        not derived from ``profile`` or from a crypto/equity label.
        """
        if self._identity_verified:
            return
        if self._identity_check_in_progress:
            raise AlpacaPaperConfigurationError(
                "Alpaca account identity check re-entered"
            )
        self._identity_check_in_progress = True
        try:
            data = await self._request("GET", "/v2/account", _skip_identity=True)
            actual_id = str(data.get("id") or "").strip().lower()
            actual_number = str(data.get("account_number") or "").strip().lower()
            expected_id = (
                str(self._settings.expected_account_id_suffix or "").strip().lower()
            )
            expected_number = (
                str(self._settings.expected_account_number_suffix or "").strip().lower()
            )
            if not actual_id.endswith(expected_id) or not actual_number.endswith(
                expected_number
            ):
                raise AlpacaPaperIdentityMismatch(
                    "Alpaca account physical identity mismatch; refusing broker access"
                )
            self._identity_verified = True
        finally:
            self._identity_check_in_progress = False

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

    async def get_position(self, symbol: str) -> Position | None:
        """Read-only current paper position for a symbol (None if flat / 404).

        Used as the fresh sell-eligibility evidence right before a sell POST.
        Never mutates. Alpaca positions use the slashless symbol for crypto
        (e.g. BTC/USD -> BTCUSD); callers should pass the broker symbol form.
        """
        try:
            data = await self._request("GET", f"/v2/positions/{symbol}")
        except AlpacaPaperRequestError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        if not data:
            return None
        return Position.model_validate(data)

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
        if request.client_order_id is not None:
            assert_broker_client_order_id(
                target=BrokerClientIdTarget.ALPACA_PAPER,
                client_order_id=request.client_order_id,
            )
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

    async def get_order_by_client_order_id(self, client_order_id: str) -> Order | None:
        """Read-only lookup of a paper order by client_order_id.

        Returns None when no order exists for the id (HTTP 404). Used only for
        crash-after-success reconciliation — this never mutates and never POSTs.
        """
        assert_broker_client_order_id(
            target=BrokerClientIdTarget.ALPACA_PAPER,
            client_order_id=client_order_id,
        )
        try:
            data = await self._request(
                "GET",
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_order_id},
            )
        except AlpacaPaperRequestError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        if not data:
            return None
        return Order.model_validate(data)

    async def list_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
        direction: str | None = None,
    ) -> list[Fill]:
        params: dict[str, str | int] = {}
        if after is not None:
            params["after"] = after.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        if page_token is not None:
            params["page_token"] = page_token
        effective_page_size = page_size if page_size is not None else limit
        if effective_page_size is not None:
            params["page_size"] = effective_page_size
        if direction is not None:
            params["direction"] = direction
        data = await self._request("GET", "/v2/account/activities/FILL", params=params)
        if not data:
            return []
        return [Fill.model_validate(item) for item in data]
