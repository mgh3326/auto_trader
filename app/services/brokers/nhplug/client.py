"""Pinned NHPLUG mock data client: account, balance, and quote reads only.

This module never imports the OAuth implementation and never contains the
production hostname.  It has an exact mock host-and-port allowlist, a short
read-only path allowlist checked before token resolution, and no mutation API.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, Final

import httpx

from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockBrokerRejected,
    NHPlugMockConfigurationError,
    NHPlugMockEndpointError,
    NHPlugMockReadOnlyEndpointError,
    NHPlugMockResponseError,
)
from app.services.brokers.nhplug.gating import _assert_mock_enabled

MOCK_BASE_URL: Final[str] = "https://moapi.nhplug.com:8443"
MOCK_HOST: Final[str] = "moapi.nhplug.com"
MOCK_PORT: Final[int] = 8443

ACCOUNT_INFO_PATH: Final[str] = "/n2/acctinfo"
BALANCE_PATH: Final[str] = "/krstock/inquiry/v1/balance"
QUOTE_PATH: Final[str] = "/krstock/quote/v1/currentPrice"
ALLOWED_READONLY_PATHS: Final[frozenset[str]] = frozenset(
    {ACCOUNT_INFO_PATH, BALANCE_PATH, QUOTE_PATH}
)

_SUCCESS_RESPONSE_CODES: Final[frozenset[str]] = frozenset(
    {"00000", "00166", "00221", "13578"}
)
_KR_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^\d{6}$")
_ALLOWED_MARKETS: Final[frozenset[str]] = frozenset({"KRX"})
TokenProvider = Callable[[], Awaitable[str]]


def _assert_mock_base_url(base_url: str) -> str:
    """Reject every URL except the exact mock HTTPS host and port."""

    url = httpx.URL(base_url)
    if (
        url.scheme != "https"
        or url.host != MOCK_HOST
        or url.port != MOCK_PORT
        or url.path not in {"", "/"}
        or url.query
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG data client only accepts the pinned mock endpoint"
        )
    return MOCK_BASE_URL


def _assert_readonly_path(path: str) -> None:
    if path not in ALLOWED_READONLY_PATHS:
        raise NHPlugMockReadOnlyEndpointError("NHPLUG data path is not allowlisted")


def _assert_resolved_mock_request(request: httpx.Request) -> None:
    """Revalidate resolved host, port, and path immediately before dispatch."""

    if (
        request.url.scheme != "https"
        or request.url.host != MOCK_HOST
        or request.url.port != MOCK_PORT
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG data request resolved outside the pinned mock HTTPS endpoint"
        )
    _assert_readonly_path(request.url.path)


class NHPlugMockClient:
    """Read-only data client with no generic arbitrary-endpoint dispatch."""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        token_provider: TokenProvider,
        base_url: str = MOCK_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(app_key, str) or not app_key.strip():
            raise NHPlugMockConfigurationError("NHPLUG_APP_KEY is required")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise NHPlugMockConfigurationError("NHPLUG_APP_SECRET is required")
        self._base_url = _assert_mock_base_url(base_url)
        self._app_key = app_key
        self._app_secret = app_secret
        self._token_provider = token_provider
        self._transport = transport
        self._timeout = timeout
        self._account_allowlist: MockAccountAllowlist | None = None

    def bind_account_allowlist(self, account_allowlist: MockAccountAllowlist) -> None:
        """Bind the broker-derived mock account boundary to this dispatcher.

        Account-scoped dispatch is impossible until this one-time binding has
        happened.  The allowlist is intentionally client state rather than a
        caller-selected argument to a generic dispatch helper.
        """

        if not isinstance(account_allowlist, MockAccountAllowlist):
            raise NHPlugMockConfigurationError(
                "a broker-verified mock account allowlist is required"
            )
        account_allowlist.assert_allowed(account_allowlist.configured_account_no)
        self._account_allowlist = account_allowlist

    def _require_account_allowlist(self) -> MockAccountAllowlist:
        allowlist = self._account_allowlist
        if allowlist is None:
            raise NHPlugMockConfigurationError(
                "a broker-verified mock account allowlist is required for account-scoped reads"
            )
        return allowlist

    async def list_accounts(self) -> dict[str, Any]:
        """Read the documented account list used to establish the allowlist."""

        return await self._post_readonly(path=ACCOUNT_INFO_PATH, input_0={})

    async def fetch_balance(self, *, act_no: str) -> dict[str, Any]:
        """Read domestic holdings after account verification at both guard points."""

        return await self._post_readonly(
            path=BALANCE_PATH,
            input_0={
                "act_no": act_no,
                "bnc_bse_cd": "5",
                "ltg_aot_dit_cd": "9",
                "aet_bse": "2",
                "qut_dit_cd": "UNT",
            },
            act_no=act_no,
        )

    async def fetch_quote(
        self,
        *,
        symbol: str,
        market: str,
    ) -> dict[str, Any]:
        """Read one Korean equity quote after the same configured-account check."""

        if not isinstance(symbol, str) or _KR_SYMBOL_RE.fullmatch(symbol) is None:
            raise NHPlugMockConfigurationError(
                "symbol must be an exact six-digit KRX code"
            )
        if market not in _ALLOWED_MARKETS:
            raise NHPlugMockConfigurationError(
                "market must be KRX for this read-only stage"
            )
        return await self._post_readonly(
            path=QUOTE_PATH,
            input_0={"iem_cd": symbol, "market_cd": market},
        )

    async def _post_readonly(
        self,
        *,
        path: str,
        input_0: dict[str, Any],
        act_no: str | None = None,
    ) -> dict[str, Any]:
        """Guard before token I/O, then guard resolved request before send."""

        _assert_mock_enabled()
        _assert_readonly_path(path)
        if self._base_url != MOCK_BASE_URL:
            raise NHPlugMockEndpointError(
                "NHPLUG data base endpoint changed after construction"
            )
        account_allowlist: MockAccountAllowlist | None = None
        verified_act_no: str | None = None
        if path != ACCOUNT_INFO_PATH:
            account_allowlist = self._require_account_allowlist()
            verified_act_no = account_allowlist.configured_account_no
            if path == BALANCE_PATH:
                if not isinstance(act_no, str) or input_0.get("act_no") != act_no:
                    raise NHPlugMockConfigurationError(
                        "balance reads require the bound configured account"
                    )
                verified_act_no = act_no
            elif act_no is not None:
                raise NHPlugMockConfigurationError(
                    "only balance reads may supply an account number"
                )
            if verified_act_no != account_allowlist.configured_account_no:
                raise NHPlugMockAccountRejected(
                    "account-scoped reads may use only the configured mock account"
                )
            account_allowlist.assert_allowed(verified_act_no)

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
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
            # HTTPX retains custom APP credential headers across cross-origin
            # redirects, so this is an APP KEY/SECRET boundary as well as a
            # host-boundary control.
            follow_redirects=False,
        ) as client:
            request = client.build_request(
                "POST", path, headers=headers, json={"Input_0": input_0}
            )
            _assert_resolved_mock_request(request)
            # Second independent account check immediately before the send site.
            if account_allowlist is not None and verified_act_no is not None:
                account_allowlist.assert_allowed(verified_act_no)
            response = await client.send(request)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise NHPlugMockResponseError("NHPLUG mock response was not JSON") from exc
        if not isinstance(payload, dict):
            raise NHPlugMockResponseError("NHPLUG mock response was not an object")
        response_code = payload.get("rsp_cd")
        if (
            not isinstance(response_code, str)
            or response_code not in _SUCCESS_RESPONSE_CODES
        ):
            raise NHPlugMockBrokerRejected(
                response_code=str(response_code or "unknown")
            )
        return dict(payload)
