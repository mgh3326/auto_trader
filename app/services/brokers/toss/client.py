from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import settings
from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.dto import (
    TossAccount,
    TossListedStock,
    TossMarketIndicatorPrice,
    TossMarketInvestorTradingPage,
    TossMarketInvestorTradingRecord,
    TossOrderOperationResult,
    TossOrderPlacementResult,
    TossRankings,
    TossStockInvestorTradingPage,
    TossStockInvestorTradingRecord,
    TossWarningInfo,
    parse_accounts,
    parse_buying_power,
    parse_candles,
    parse_commissions,
    parse_holdings,
    parse_listed_stocks,
    parse_market_indicator_prices,
    parse_market_investor_trading_page,
    parse_order,
    parse_order_operation_result,
    parse_order_placement_result,
    parse_orders,
    parse_prices,
    parse_rankings,
    parse_sellable_quantity,
    parse_stock_investor_trading_page,
    parse_stocks,
    parse_warnings,
)
from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossResponseContractError,
    parse_toss_response,
)
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

    @property
    def selected_account_seq(self) -> int | None:
        """Account sequence this client will send on account-scoped reads."""
        return self._account_seq

    @property
    def snapshot_scope_identity(self) -> str:
        """Non-secret identity for shared-snapshot cache scoping (ROB-1310 R10).

        Exposes the same endpoint/client-fingerprint/account-selection
        material used by the settings-derived global scope
        (``_snapshot_cache_scope``), so a caller that explicitly injects a
        ``TossReadClient`` -- rather than letting
        ``fetch_toss_portfolio_snapshot`` create one internally -- can still
        safely opt into the shared snapshot cache under this client's own
        scope instead of being bypassed as untrusted. Never the raw base URL,
        client id, or account sequence; those never leave this property.
        """
        base_url = str(self._client.base_url).rstrip("/")
        fingerprint = self._token_manager.client_fingerprint
        account = self._account_seq if self._account_seq is not None else "auto"
        return "|".join([base_url, fingerprint, str(account)])

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

    async def warnings(self, symbol: str) -> list[TossWarningInfo]:
        return parse_warnings(
            await self._request(
                "GET",
                f"/api/v1/stocks/{symbol}/warnings",
                group=TossApiGroup.STOCK,
            )
        )

    # ------------------------------------------------------------------
    # #1064 read-only market-data surfaces. Every call is a GET through the
    # shared configured limiter via ``_request`` — no private limiter, no
    # order-group calls, no account header (these endpoints need only the
    # OAuth token). Rate-limit groups follow the official openapi.json's
    # per-path "Rate Limits Group" labels (STOCK_TRADING_TREND 10/s,
    # MARKET_INDICATOR 10/s, RANKING 5/s, STOCK_ALL 1/s per
    # openapi-docs/overview.md) rather than MARKET_DATA, whose official 15/s
    # cap would over-admit RANKING (5/s) and STOCK_ALL (1/s) traffic.
    #
    # Request-side patterns quote the official param schemas: KrSymbol
    # ^[A-Za-z0-9.\-]+$, until format=date (YYYY-MM-DD), market-indicator
    # symbols param ^[A-Za-z0-9_,]+$.
    # ------------------------------------------------------------------

    _KR_SYMBOL_RE = re.compile(r"[A-Za-z0-9.\-]+", re.ASCII)
    _UNTIL_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
    _INDICATOR_SYMBOLS_RE = re.compile(r"[A-Za-z0-9_]+(?:,[A-Za-z0-9_]+)*", re.ASCII)

    @classmethod
    def _check_kr_symbol(cls, symbol: str) -> None:
        # Charset allowlist (spec KrSymbol pattern) plus a dot-segment guard:
        # all-dot/hyphen values like ".." would be normalized into the URL
        # path by httpx. fullmatch so a trailing newline cannot smuggle past.
        if not cls._KR_SYMBOL_RE.fullmatch(symbol) or not symbol.strip(".-"):
            raise ValueError(f"Invalid Toss KR symbol: {symbol!r}")

    @classmethod
    def _check_count(cls, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("Toss count must be an integer")
        if not 1 <= count <= 100:
            raise ValueError("Toss count must be 1..100")

    @classmethod
    def _check_until(cls, until: str | None) -> None:
        if until is not None and not cls._UNTIL_DATE_RE.fullmatch(until):
            raise ValueError("Toss until cursor must be YYYY-MM-DD")

    async def stock_investor_trading(
        self,
        symbol: str,
        *,
        count: int = 10,
        until: str | None = None,
    ) -> TossStockInvestorTradingPage:
        """GET /api/v1/stocks/{symbol}/investor-trading (group STOCK_TRADING_TREND).

        Daily individual/foreigner/institution/other-corporation trading
        volumes for one KR symbol, newest first. ``until`` is the documented
        inclusive YYYY-MM-DD cursor; a page's ``next_until`` is passed back as
        ``until`` to continue pagination. Same-day records are provisional and
        may carry null sections.
        """
        self._check_kr_symbol(symbol)
        self._check_count(count)
        self._check_until(until)
        params: dict[str, Any] = {"count": count}
        if until is not None:
            params["until"] = until
        return parse_stock_investor_trading_page(
            await self._request(
                "GET",
                f"/api/v1/stocks/{symbol}/investor-trading",
                group=TossApiGroup.STOCK_TRADING_TREND,
                params=params,
            )
        )

    async def collect_stock_investor_trading(
        self,
        symbol: str,
        *,
        count: int = 100,
        until: str | None = None,
        max_pages: int = 100,
    ) -> list[TossStockInvestorTradingRecord]:
        """Follow ``nextUntil`` pagination and return all collected records.

        Terminates when a page reports a falsy ``next_until`` or carries no
        records. A ``next_until`` identical to the cursor just sent means the
        server made no progress — a TossResponseContractError, not a retry.
        ``max_pages`` bounds the walk; hitting it with a pending cursor raises
        ValueError rather than silently truncating history.
        """
        records: list[TossStockInvestorTradingRecord] = []
        cursor = until
        for _ in range(max_pages):
            page = await self.stock_investor_trading(symbol, count=count, until=cursor)
            records.extend(page.records)
            if not page.next_until or not page.records:
                return records
            if page.next_until == cursor:
                raise TossResponseContractError(
                    "stocks/{symbol}/investor-trading: non-advancing "
                    f"nextUntil {page.next_until!r} for {symbol}"
                )
            cursor = page.next_until
        raise ValueError(
            f"Toss investor-trading pagination exceeded max_pages={max_pages} "
            f"for {symbol}"
        )

    async def market_indicator_prices(
        self, symbols: list[str] | tuple[str, ...]
    ) -> list[TossMarketIndicatorPrice]:
        """GET /api/v1/market-indicators/prices (group MARKET_INDICATOR).

        ``symbols`` is the documented comma-separated catalog (KOSPI, KOSDAQ,
        KR_BOND_*), max 200 per request.
        """
        symbols_param = self._symbols_param(symbols)
        if not self._INDICATOR_SYMBOLS_RE.fullmatch(symbols_param):
            raise ValueError(
                f"Invalid Toss market-indicator symbols: {symbols_param!r}"
            )
        return parse_market_indicator_prices(
            await self._request(
                "GET",
                "/api/v1/market-indicators/prices",
                group=TossApiGroup.MARKET_INDICATOR,
                params={"symbols": symbols_param},
            )
        )

    _MARKET_INDICATOR_SYMBOLS = frozenset({"KOSPI", "KOSDAQ"})
    _INVESTOR_TRADING_INTERVALS = frozenset({"1d", "1w", "1mo", "1y"})

    async def market_indicator_investor_trading(
        self,
        symbol: str,
        *,
        interval: str,
        count: int = 10,
        until: str | None = None,
    ) -> TossMarketInvestorTradingPage:
        """GET /api/v1/market-indicators/{symbol}/investor-trading
        (group MARKET_INDICATOR). KOSPI/KOSDAQ only; ``interval`` is required
        by the spec (1d/1w/1mo/1y). Same ``until``/``nextUntil`` cursor scheme
        as the stock endpoint.
        """
        if symbol not in self._MARKET_INDICATOR_SYMBOLS:
            raise ValueError(
                "Toss market-indicator investor-trading supports KOSPI/KOSDAQ only"
            )
        if interval not in self._INVESTOR_TRADING_INTERVALS:
            raise ValueError("Toss investor-trading interval must be 1d/1w/1mo/1y")
        self._check_count(count)
        self._check_until(until)
        params: dict[str, Any] = {"interval": interval, "count": count}
        if until is not None:
            params["until"] = until
        return parse_market_investor_trading_page(
            await self._request(
                "GET",
                f"/api/v1/market-indicators/{symbol}/investor-trading",
                group=TossApiGroup.MARKET_INDICATOR,
                params=params,
            )
        )

    async def collect_market_indicator_investor_trading(
        self,
        symbol: str,
        *,
        interval: str,
        count: int = 100,
        until: str | None = None,
        max_pages: int = 100,
    ) -> list[TossMarketInvestorTradingRecord]:
        """Follow ``nextUntil`` pagination; same termination contract as
        ``collect_stock_investor_trading``.
        """
        records: list[TossMarketInvestorTradingRecord] = []
        cursor = until
        for _ in range(max_pages):
            page = await self.market_indicator_investor_trading(
                symbol, interval=interval, count=count, until=cursor
            )
            records.extend(page.records)
            if not page.next_until or not page.records:
                return records
            if page.next_until == cursor:
                raise TossResponseContractError(
                    "market-indicators/{symbol}/investor-trading: "
                    f"non-advancing nextUntil {page.next_until!r} for {symbol}"
                )
            cursor = page.next_until
        raise ValueError(
            f"Toss market-indicator investor-trading pagination exceeded "
            f"max_pages={max_pages} for {symbol}"
        )

    _RANKING_TYPES = frozenset(
        {
            "MARKET_TRADING_AMOUNT",
            "MARKET_TRADING_VOLUME",
            "TOP_GAINERS",
            "TOP_LOSERS",
            "TOSS_SECURITIES_TRADING_AMOUNT",
            "TOSS_SECURITIES_TRADING_VOLUME",
        }
    )
    _RANKING_DURATIONS = frozenset({"realtime", "1d", "1w", "1mo", "3mo", "6mo", "1y"})
    _RANKING_NO_REALTIME = frozenset({"TOP_GAINERS", "TOP_LOSERS"})
    _MARKET_COUNTRIES = frozenset({"KR", "US"})

    async def rankings(
        self,
        *,
        ranking_type: str,
        market_country: str,
        duration: str,
        count: int = 100,
        exclude_investment_caution: bool = False,
    ) -> TossRankings:
        """GET /api/v1/rankings (group RANKING).

        ``ranking_type``: MARKET_TRADING_AMOUNT, MARKET_TRADING_VOLUME,
        TOP_GAINERS, TOP_LOSERS, TOSS_SECURITIES_TRADING_AMOUNT,
        TOSS_SECURITIES_TRADING_VOLUME. ``market_country``: KR or US.
        ``duration``: realtime/1d/1w/1mo/3mo/6mo/1y — TOP_GAINERS and
        TOP_LOSERS do not support realtime (spec-mandated 400).
        """
        if ranking_type not in self._RANKING_TYPES:
            raise ValueError(f"Unsupported Toss ranking type: {ranking_type}")
        if market_country not in self._MARKET_COUNTRIES:
            raise ValueError(f"Unsupported Toss ranking market: {market_country}")
        if duration not in self._RANKING_DURATIONS:
            raise ValueError(f"Unsupported Toss ranking duration: {duration}")
        if duration == "realtime" and ranking_type in self._RANKING_NO_REALTIME:
            raise ValueError(
                f"Toss ranking type {ranking_type} does not support realtime"
            )
        self._check_count(count)
        return parse_rankings(
            await self._request(
                "GET",
                "/api/v1/rankings",
                group=TossApiGroup.RANKING,
                params={
                    "type": ranking_type,
                    "marketCountry": market_country,
                    "duration": duration,
                    "excludeInvestmentCaution": str(exclude_investment_caution).lower(),
                    "count": count,
                },
            )
        )

    _STOCKS_ALL_MARKETS = frozenset(
        {"KOSPI", "KOSDAQ", "NYSE", "NASDAQ", "AMEX", "KR_ETC", "US_ETC"}
    )
    _STOCKS_ALL_STATUSES = frozenset({"SCHEDULED", "ACTIVE", "DELISTED"})
    _STOCKS_ALL_SECURITY_TYPES = frozenset(
        {
            "STOCK",
            "FOREIGN_STOCK",
            "DEPOSITARY_RECEIPT",
            "INFRASTRUCTURE_FUND",
            "REIT",
            "ETF",
            "FOREIGN_ETF",
            "ETN",
            "STOCK_WARRANTS",
        }
    )

    async def stocks_all(
        self,
        *,
        market: str,
        status: str | None = None,
        security_type: str | None = None,
        common_share: bool | None = None,
    ) -> list[TossListedStock]:
        """GET /api/v1/stocks/all (group STOCK_ALL).

        ``market`` is required (KOSPI/KOSDAQ/NYSE/NASDAQ/AMEX/KR_ETC/US_ETC).
        ``status`` accepts DELISTED to surface delisted rows; the ListedStock
        schema itself carries no status field.
        """
        if market not in self._STOCKS_ALL_MARKETS:
            raise ValueError(f"Unsupported Toss stocks/all market: {market}")
        if status is not None and status not in self._STOCKS_ALL_STATUSES:
            raise ValueError(f"Unsupported Toss stocks/all status: {status}")
        if (
            security_type is not None
            and security_type not in self._STOCKS_ALL_SECURITY_TYPES
        ):
            raise ValueError(
                f"Unsupported Toss stocks/all securityType: {security_type}"
            )
        params: dict[str, Any] = {"market": market}
        if status is not None:
            params["status"] = status
        if security_type is not None:
            params["securityType"] = security_type
        if common_share is not None:
            params["commonShare"] = str(common_share).lower()
        return parse_listed_stocks(
            await self._request(
                "GET",
                "/api/v1/stocks/all",
                group=TossApiGroup.STOCK_ALL,
                params=params,
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
