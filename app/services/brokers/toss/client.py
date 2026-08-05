from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import settings
from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.dto import (
    TossAccount,
    TossOrderOperationResult,
    TossOrderPlacementResult,
    TossWarningInfo,
    parse_accounts,
    parse_buying_power,
    parse_candles,
    parse_commissions,
    parse_holdings,
    parse_order,
    parse_order_operation_result,
    parse_order_placement_result,
    parse_orders,
    parse_prices,
    parse_sellable_quantity,
    parse_stocks,
    parse_warnings,
)
from app.services.brokers.toss.errors import TossApiResponseError, parse_toss_response
from app.services.brokers.toss.health import publish_toss_api_error
from app.services.brokers.toss.rate_limiter import (
    TossApiGroup,
    TossRateLimiter,
    TossRateLimitHeaders,
    get_shared_rate_limiter,
    parse_rate_limit_headers,
    retry_delay_seconds,
)
from app.services.brokers.toss.transport import DEFAULT_TOSS_BASE_URL, build_toss_client

_TOKEN_CODES = {"invalid-token", "expired-token"}
_GET_REISSUABLE_NON_JSON_STATUSES = {403}
PreSendHook = Callable[[], Awaitable[None]]
ResponseObserver = Callable[[TossApiGroup, int, TossRateLimitHeaders], None]


def _should_retry_get_non_json_auth_error(
    method: str, exc: TossApiResponseError
) -> bool:
    return (
        method.upper() == "GET"
        and exc.status_code in _GET_REISSUABLE_NON_JSON_STATUSES
        and exc.envelope.code == "non-json-response"
    )


class TossReadClient:
    def __init__(
        self,
        *,
        token_manager: TossOAuthTokenManager,
        account_seq: int | None = None,
        base_url: str = DEFAULT_TOSS_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: TossRateLimiter | None = None,
        retry_on_429: bool = True,
        retry_auth_reissue: bool = True,
        response_observer: ResponseObserver | None = None,
        publish_error_signals: bool = True,
    ) -> None:
        self._token_manager = token_manager
        self._account_seq = account_seq
        self._client = build_toss_client(base_url=base_url, transport=transport)
        self._rate_limiter = rate_limiter or TossRateLimiter()
        # The default preserves the established live-client behaviour.  A
        # separately approved bulk reader can disable both retries so one
        # upstream failure stops only that reader and never churns the shared
        # OAuth token.
        self._retry_on_429 = retry_on_429
        self._retry_auth_reissue = retry_auth_reissue
        self._response_observer = response_observer
        self._publish_error_signals = publish_error_signals

    @classmethod
    def from_settings(cls, settings_obj: Any = settings) -> TossReadClient:
        base_url = (
            getattr(settings_obj, "toss_api_base_url", None) or DEFAULT_TOSS_BASE_URL
        )
        limiter = get_shared_rate_limiter()
        return cls(
            token_manager=TossOAuthTokenManager.from_settings(
                settings_obj, rate_limiter=limiter
            ),
            account_seq=getattr(settings_obj, "toss_api_account_seq", None),
            base_url=str(base_url),
            rate_limiter=limiter,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _publish_error(
        self,
        *,
        status_code: int | None,
        error_type: str,
    ) -> None:
        if self._publish_error_signals:
            await publish_toss_api_error(
                status_code=status_code,
                error_type=error_type,
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        group: TossApiGroup,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        account_required: bool = False,
        pre_send_hook: PreSendHook | None = None,
    ) -> Any:
        await self._rate_limiter.acquire(group)
        token = await self._token_manager.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account_required:
            headers["X-Tossinvest-Account"] = str(await self._resolve_account_seq())

        async def send() -> tuple[httpx.Response, TossRateLimitHeaders]:
            if pre_send_hook is not None:
                await pre_send_hook()
            response = await self._client.request(
                method, path, params=params, json=json, headers=headers
            )
            rate_limit_headers = parse_rate_limit_headers(response.headers)
            if self._response_observer is not None:
                self._response_observer(group, response.status_code, rate_limit_headers)
            return response, rate_limit_headers

        try:
            response, _rate_limit_headers = await send()
        except httpx.HTTPError as exc:
            await self._publish_error(
                status_code=None,
                error_type=type(exc).__name__,
            )
            raise
        if response.status_code >= 400:
            await self._publish_error(
                status_code=response.status_code,
                error_type="http_response",
            )
        if response.status_code == 429 and self._retry_on_429:
            await asyncio.sleep(
                retry_delay_seconds(response.headers.get("Retry-After"), attempt=0)
            )
            try:
                response, _rate_limit_headers = await send()
            except httpx.HTTPError as exc:
                await self._publish_error(
                    status_code=None,
                    error_type=type(exc).__name__,
                )
                raise
            if response.status_code >= 400:
                await self._publish_error(
                    status_code=response.status_code,
                    error_type="http_response",
                )
        try:
            return parse_toss_response(response)
        except TossApiResponseError as exc:
            if self._retry_auth_reissue and (
                exc.envelope.code in _TOKEN_CODES
                or _should_retry_get_non_json_auth_error(method, exc)
            ):
                token = await self._token_manager.get_access_token(
                    force_reissue=True, failed_token=token
                )
                headers["Authorization"] = f"Bearer {token}"
                try:
                    retry, _rate_limit_headers = await send()
                except httpx.HTTPError as retry_exc:
                    await self._publish_error(
                        status_code=None,
                        error_type=type(retry_exc).__name__,
                    )
                    raise
                if retry.status_code >= 400:
                    await self._publish_error(
                        status_code=retry.status_code,
                        error_type="http_response",
                    )
                return parse_toss_response(retry)
            raise

    async def _resolve_account_seq(self) -> int:
        if self._account_seq is not None:
            return self._account_seq
        accounts = await self.accounts()
        if len(accounts) != 1:
            raise ValueError(
                f"Toss account auto-resolution requires exactly one account; got {len(accounts)}"
            )
        self._account_seq = accounts[0].account_seq
        return self._account_seq

    @staticmethod
    def _symbols_param(symbols: list[str] | tuple[str, ...]) -> str:
        if not 1 <= len(symbols) <= 200:
            raise ValueError("Toss symbol batch size must be 1..200")
        return ",".join(symbols)

    async def accounts(self) -> list[TossAccount]:
        return parse_accounts(
            await self._request("GET", "/api/v1/accounts", group=TossApiGroup.ACCOUNT)
        )

    async def holdings(self, *, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else None
        return parse_holdings(
            await self._request(
                "GET",
                "/api/v1/holdings",
                group=TossApiGroup.ASSET,
                params=params,
                account_required=True,
            )
        )

    async def prices(self, symbols: list[str] | tuple[str, ...]):
        return parse_prices(
            await self._request(
                "GET",
                "/api/v1/prices",
                group=TossApiGroup.MARKET_DATA,
                params={"symbols": self._symbols_param(symbols)},
            )
        )

    async def stocks(self, symbols: list[str] | tuple[str, ...]):
        return parse_stocks(
            await self._request(
                "GET",
                "/api/v1/stocks",
                group=TossApiGroup.STOCK,
                params={"symbols": self._symbols_param(symbols)},
            )
        )

    async def stocks_raw(self, symbols: list[str] | tuple[str, ...]) -> Any:
        """GET the master payload without DTO parsing (ROB-1172 AC1).

        The unparsed body is what an authoritative metadata snapshot hashes; a
        parsed DTO has already dropped fields and cannot be re-hashed faithfully.
        Read-only, same TOSS_API_ENABLED gate as every other Toss read.
        """
        return await self._request(
            "GET",
            "/api/v1/stocks",
            group=TossApiGroup.STOCK,
            params={"symbols": self._symbols_param(symbols)},
        )

    async def warnings(self, symbol: str) -> list[TossWarningInfo]:
        return parse_warnings(
            await self._request(
                "GET",
                f"/api/v1/stocks/{symbol}/warnings",
                group=TossApiGroup.STOCK,
            )
        )

    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> Any:
        if interval not in {"1m", "1d"}:
            raise ValueError("Toss candle interval must be '1m' or '1d'")
        params = {
            key: value
            for key, value in {
                "symbol": symbol,
                "interval": interval,
                "count": count,
                "before": before,
                "adjusted": str(adjusted).lower() if adjusted is not None else None,
            }.items()
            if value is not None
        }
        return parse_candles(
            await self._request(
                "GET",
                "/api/v1/candles",
                group=TossApiGroup.MARKET_DATA_CHART,
                params=params,
            )
        )

    async def exchange_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        date_time: str | None = None,
    ) -> Any:
        params = {
            "baseCurrency": base_currency,
            "quoteCurrency": quote_currency,
        }
        if date_time is not None:
            params["dateTime"] = date_time
        return await self._request(
            "GET",
            "/api/v1/exchange-rate",
            group=TossApiGroup.MARKET_INFO,
            params=params,
        )

    async def market_calendar_kr(self, *, date: str | None = None) -> Any:
        return await self._request(
            "GET",
            "/api/v1/market-calendar/KR",
            group=TossApiGroup.MARKET_INFO,
            params={"date": date} if date else None,
        )

    async def market_calendar_us(self, *, date: str | None = None) -> Any:
        return await self._request(
            "GET",
            "/api/v1/market-calendar/US",
            group=TossApiGroup.MARKET_INFO,
            params={"date": date} if date else None,
        )

    async def list_orders(
        self,
        *,
        status: str,
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ):
        params = {
            key: value
            for key, value in {
                "status": status,
                "symbol": symbol,
                "from": from_date,
                "to": to_date,
                "cursor": cursor,
                "limit": limit,
            }.items()
            if value is not None
        }
        return parse_orders(
            await self._request(
                "GET",
                "/api/v1/orders",
                group=TossApiGroup.ORDER_HISTORY,
                params=params,
                account_required=True,
            )
        )

    async def get_order(self, order_id: str):
        return parse_order(
            await self._request(
                "GET",
                f"/api/v1/orders/{order_id}",
                group=TossApiGroup.ORDER_HISTORY,
                account_required=True,
            )
        )

    async def buying_power(self, *, currency: str):
        return parse_buying_power(
            await self._request(
                "GET",
                "/api/v1/buying-power",
                group=TossApiGroup.ORDER_INFO,
                params={"currency": currency},
                account_required=True,
            )
        )

    async def sellable_quantity(self, *, symbol: str):
        return parse_sellable_quantity(
            await self._request(
                "GET",
                "/api/v1/sellable-quantity",
                group=TossApiGroup.ORDER_INFO,
                params={"symbol": symbol},
                account_required=True,
            )
        )

    async def commissions(self):
        return parse_commissions(
            await self._request(
                "GET",
                "/api/v1/commissions",
                group=TossApiGroup.ORDER_INFO,
                account_required=True,
            )
        )

    async def place_order(
        self,
        payload: dict[str, Any],
        *,
        pre_send_hook: PreSendHook | None = None,
    ) -> TossOrderPlacementResult:
        return parse_order_placement_result(
            await self._request(
                "POST",
                "/api/v1/orders",
                group=TossApiGroup.ORDER,
                json=payload,
                account_required=True,
                pre_send_hook=pre_send_hook,
            )
        )

    async def modify_order(
        self, order_id: str, payload: dict[str, Any]
    ) -> TossOrderOperationResult:
        return parse_order_operation_result(
            await self._request(
                "POST",
                f"/api/v1/orders/{order_id}/modify",
                group=TossApiGroup.ORDER,
                json=payload,
                account_required=True,
            )
        )

    async def cancel_order(
        self,
        order_id: str,
        *,
        pre_send_hook: PreSendHook | None = None,
    ) -> TossOrderOperationResult:
        return parse_order_operation_result(
            await self._request(
                "POST",
                f"/api/v1/orders/{order_id}/cancel",
                group=TossApiGroup.ORDER,
                json={},
                account_required=True,
                pre_send_hook=pre_send_hook,
            )
        )
