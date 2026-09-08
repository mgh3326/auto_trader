"""NHPLUG mock-only domestic cash order mutations.

This is intentionally the sole owner of the NH domestic order route literals.
All sends require both mock gates, a broker-derived ``acct_type=03`` allowlist,
and a final resolved request host/port/path check with redirects disabled.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, Final

import httpx

from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.client import (
    MOCK_BASE_URL,
    assert_resolved_mock_request,
)
from app.services.brokers.nhplug.errors import (
    NHPlugMockConfigurationError,
    NHPlugMockOrderRejected,
    NHPlugMockResponseError,
)
from app.services.brokers.nhplug.gating import _assert_mock_orders_enabled

CASH_BUY_PATH: Final[str] = "/krstock/order/v1/cashBuy"
CASH_SELL_PATH: Final[str] = "/krstock/order/v1/cashSell"
CANCEL_PATH: Final[str] = "/krstock/order/v1/cancel"
ALLOWED_ORDER_PATHS: Final[frozenset[str]] = frozenset(
    {CASH_BUY_PATH, CASH_SELL_PATH, CANCEL_PATH}
)
_KRX_SYMBOL = re.compile(r"^\d{6}$")
TokenProvider = Callable[[], Awaitable[str]]


class NHDomesticOrderClient:
    """Small, closed cash-limit order client for the dedicated mirror account."""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        token_provider: TokenProvider,
        account_allowlist: MockAccountAllowlist,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not all(isinstance(v, str) and v.strip() for v in (app_key, app_secret)):
            raise NHPlugMockConfigurationError("NHPLUG app credentials are required")
        account_allowlist.assert_allowed(account_allowlist.configured_account_no)
        self._app_key, self._app_secret = app_key, app_secret
        self._token_provider, self._allowlist = token_provider, account_allowlist
        self._transport, self._timeout = transport, timeout

    @staticmethod
    def _order_values(
        *, symbol: str, quantity: int, price: int
    ) -> tuple[str, int, int]:
        if not isinstance(symbol, str) or _KRX_SYMBOL.fullmatch(symbol) is None:
            raise NHPlugMockConfigurationError(
                "symbol must be an exact six-digit KRX code"
            )
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise NHPlugMockConfigurationError("quantity must be a positive integer")
        if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
            raise NHPlugMockConfigurationError("price must be a positive integer")
        return symbol, quantity, price

    async def place_buy_order(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        symbol, quantity, price = self._order_values(
            symbol=symbol, quantity=quantity, price=price
        )
        return await self._post(
            CASH_BUY_PATH,
            {
                "act_no": self._allowlist.configured_account_no,
                "iem_cd": symbol,
                "orr_qty": quantity,
                "orr_pr": price,
                "nmn_pr_tp_cd": "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
        )

    async def place_sell_order(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        symbol, quantity, price = self._order_values(
            symbol=symbol, quantity=quantity, price=price
        )
        return await self._post(
            CASH_SELL_PATH,
            {
                "act_no": self._allowlist.configured_account_no,
                "iem_cd": symbol,
                "orr_qty": quantity,
                "orr_pr": price,
                "nmn_pr_tp_cd": "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
        )

    async def cancel_order(
        self, *, symbol: str, original_order_no: str
    ) -> dict[str, Any]:
        if not isinstance(symbol, str) or _KRX_SYMBOL.fullmatch(symbol) is None:
            raise NHPlugMockConfigurationError(
                "symbol must be an exact six-digit KRX code"
            )
        if (
            not isinstance(original_order_no, str)
            or not original_order_no.strip().isdigit()
        ):
            raise NHPlugMockConfigurationError("original_order_no must be numeric")
        return await self._post(
            CANCEL_PATH,
            {
                "act_no": self._allowlist.configured_account_no,
                "org_mkt_orr_no": original_order_no.strip(),
                "all_pat_dit_cd": "1",
                "iem_cd": symbol,
            },
        )

    async def _post(self, path: str, input_0: dict[str, Any]) -> dict[str, Any]:
        _assert_mock_orders_enabled()
        if path not in ALLOWED_ORDER_PATHS:
            raise NHPlugMockConfigurationError("NHPLUG order path is not allowlisted")
        self._allowlist.assert_allowed(self._allowlist.configured_account_no)
        token = await self._token_provider()
        if not isinstance(token, str) or not token.strip():
            raise NHPlugMockResponseError(
                "NHPLUG OAuth provider returned no access token"
            )
        headers = {
            "x-client-id": self._app_key,
            "x-client-secret": self._app_secret,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        async with httpx.AsyncClient(
            base_url=MOCK_BASE_URL,
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
        ) as client:
            request = client.build_request(
                "POST", path, headers=headers, json={"Input_0": input_0}
            )
            assert_resolved_mock_request(request, allowed_paths=ALLOWED_ORDER_PATHS)
            self._allowlist.assert_allowed(self._allowlist.configured_account_no)
            response = await client.send(request)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise NHPlugMockResponseError("NHPLUG order response was not JSON") from exc
        if not isinstance(payload, dict):
            raise NHPlugMockResponseError("NHPLUG order response was not an object")
        code = payload.get("rsp_cd")
        if not isinstance(code, str) or code != "00000":
            raise NHPlugMockOrderRejected(response_code=str(code or "unknown"))
        return dict(payload)
