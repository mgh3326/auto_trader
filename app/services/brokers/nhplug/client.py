"""Pinned NHPLUG mock data client: reads plus confirmed KRX limit orders.

This module never imports the OAuth implementation and never contains the
production hostname.  It has an exact mock host-and-port allowlist and two
closed path allowlists (reads and mutations) checked before token resolution.

Stage 2 adds exactly four mutation paths (cash buy, cash sell, modify, cancel)
and one read path (daily order/execution listing).  Every mutation:

1. requires the ``NHPLUG_MOCK_ENABLED`` master gate at dispatch time;
2. requires a per-call :class:`DryRunConfirmContract` with ``dry_run=False``
   and ``confirm=True`` (exact booleans) before any token or socket I/O;
3. is a KRX limit order shape only (``nmn_pr_tp_cd=01``, no condition, no
   stop price, no amount-based order, ``rmt_mkt_cd=KRX``, no SOR split);
4. is re-verified after the request is built, immediately before ``send``:
   scheme, host, port, path, the body ``act_no`` against the broker-derived
   ``acct_type=03`` allowlist, and the limit-only body shape.

Credit, reserved, and SOR orders, other markets, and every other route stay
out of reach: the path sets below are exact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.contracts import (
    ClaimedOrder,
    DryRunConfirmContract,
    ExpectedOrder,
)
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockBrokerRejected,
    NHPlugMockClaimRejected,
    NHPlugMockConfigurationError,
    NHPlugMockDispatchUncertain,
    NHPlugMockEndpointError,
    NHPlugMockOrderRefused,
    NHPlugMockReadOnlyEndpointError,
    NHPlugMockResponseError,
)
from app.services.brokers.nhplug.gating import _assert_mock_enabled
from app.services.nhplug_mock.ledger_service import NHPlugMockLedgerService

MOCK_BASE_URL: Final[str] = "https://moapi.nhplug.com:8443"
MOCK_HOST: Final[str] = "moapi.nhplug.com"
MOCK_PORT: Final[int] = 8443

ACCOUNT_INFO_PATH: Final[str] = "/n2/acctinfo"
BALANCE_PATH: Final[str] = "/krstock/inquiry/v1/balance"
QUOTE_PATH: Final[str] = "/krstock/quote/v1/currentPrice"
DAILY_ORDER_EXECUTION_PATH: Final[str] = "/krstock/inquiry/v1/dailyOrderExecution"
ALLOWED_READONLY_PATHS: Final[frozenset[str]] = frozenset(
    {ACCOUNT_INFO_PATH, BALANCE_PATH, QUOTE_PATH, DAILY_ORDER_EXECUTION_PATH}
)
_ACCOUNT_SCOPED_READ_PATHS: Final[frozenset[str]] = frozenset(
    {BALANCE_PATH, DAILY_ORDER_EXECUTION_PATH}
)

# Stage 2 mutation surface: exactly these four routes.  Credit, reserved,
# and every non-krstock order route are deliberately absent.
CASH_BUY_PATH: Final[str] = "/krstock/order/v1/cashBuy"
CASH_SELL_PATH: Final[str] = "/krstock/order/v1/cashSell"
MODIFY_PATH: Final[str] = "/krstock/order/v1/modify"
CANCEL_PATH: Final[str] = "/krstock/order/v1/cancel"
ALLOWED_MUTATION_PATHS: Final[frozenset[str]] = frozenset(
    {CASH_BUY_PATH, CASH_SELL_PATH, MODIFY_PATH, CANCEL_PATH}
)
_NEW_ORDER_PATHS: Final[frozenset[str]] = frozenset({CASH_BUY_PATH, CASH_SELL_PATH})

# Limit-only order shape.  ``01`` is the vendor's 보통가 (plain limit) code;
# ``05`` (market) and every other price type are refused.
LIMIT_PRICE_TYPE_CODE: Final[str] = "01"
NO_ORDER_CONDITION_CODE: Final[str] = "00"
NORMAL_SHORT_SELL_CODE: Final[str] = "00"
MOCK_ORDER_MARKET: Final[str] = "KRX"
NO_SOR_SPLIT: Final[str] = "N"
FULL_QUANTITY_CODE: Final[str] = "1"
PARTIAL_QUANTITY_CODE: Final[str] = "2"
_NEW_ORDER_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "act_no",
        "iem_cd",
        "orr_qty",
        "orr_pr",
        "nmn_pr_tp_cd",
        "orr_cnd_dit_cd",
        "ssl_nmn_pr_dit_cd",
        "rmt_mkt_cd",
        "sor_mkt_sli_yn",
    }
)
_MODIFY_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "act_no",
        "org_mkt_orr_no",
        "all_pat_dit_cd",
        "iem_cd",
        "cor_qty",
        "cor_pr",
        "sop_cnd_pr",
        "rmt_mkt_cd",
        "sor_mkt_sli_yn",
    }
)
_CANCEL_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {"act_no", "org_mkt_orr_no", "all_pat_dit_cd", "iem_cd", "cor_qty"}
)
# Hard sanity bounds on a single mock order request; not a sizing policy.
MAX_ORDER_QUANTITY: Final[int] = 1_000_000
MAX_ORDER_PRICE_KRW: Final[int] = 100_000_000
MAX_ORDER_NUMBER: Final[int] = 9_999_999_999

# Order-listing filter (``ost_cns_dit``): 0=all, 1=filled, 2=open.
LISTING_SCOPE_CODES: Final[Mapping[str, str]] = {"all": "0", "filled": "1", "open": "2"}
_ALL_ORDER_MARKETS_CODE: Final[str] = "00"

_SUCCESS_RESPONSE_CODES: Final[frozenset[str]] = frozenset(
    {"00000", "00166", "00221", "13578"}
)
_KR_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^\d{6}$")
_ORDER_DATE_RE: Final[re.Pattern[str]] = re.compile(r"^\d{8}$")
_ALLOWED_MARKETS: Final[frozenset[str]] = frozenset({"KRX"})
TokenProvider = Callable[[], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """One broker response: the JSON object plus continuation headers only."""

    payload: dict[str, Any] = field(repr=False)
    continuation_key: str | None = field(default=None, repr=False)
    continuation_flag: str | None = None


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


def _assert_mutation_path(path: str) -> None:
    if path not in ALLOWED_MUTATION_PATHS:
        raise NHPlugMockOrderRefused("NHPLUG order path is not allowlisted")


def _assert_resolved_mock_request(
    request: httpx.Request,
    *,
    allowed_paths: frozenset[str],
    expected_path: str | None = None,
) -> None:
    """Revalidate resolved scheme, host, port, and path immediately before send."""

    if (
        request.url.scheme != "https"
        or request.url.host != MOCK_HOST
        or request.url.port != MOCK_PORT
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG data request resolved outside the pinned mock HTTPS endpoint"
        )
    if request.url.path not in allowed_paths:
        raise NHPlugMockReadOnlyEndpointError(
            "NHPLUG request resolved to a non-allowlisted path"
        )
    if expected_path is not None and request.url.path != expected_path:
        raise NHPlugMockReadOnlyEndpointError(
            "NHPLUG request resolved to a different path than intended"
        )


def _built_input(request: httpx.Request) -> dict[str, Any]:
    """Decode the exact bytes about to be sent; never trust the pre-build dict."""

    try:
        body = json.loads(request.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NHPlugMockConfigurationError(
            "NHPLUG request body is not the documented JSON envelope"
        ) from exc
    if not isinstance(body, dict) or set(body) != {"Input_0"}:
        raise NHPlugMockConfigurationError(
            "NHPLUG request body is not the documented Input_0 envelope"
        )
    input_0 = body["Input_0"]
    if not isinstance(input_0, dict):
        raise NHPlugMockConfigurationError("NHPLUG Input_0 must be an object")
    return input_0


def _assert_built_account(
    request: httpx.Request, *, allowlist: MockAccountAllowlist
) -> None:
    """Re-verify the ``act_no`` inside the built request against the allowlist."""

    act_no = _built_input(request).get("act_no")
    if not isinstance(act_no, str) or act_no != allowlist.configured_account_no:
        raise NHPlugMockAccountRejected(
            "built request does not carry the configured mock account"
        )
    allowlist.assert_allowed(act_no)


def _is_positive_int(value: object, *, ceiling: int) -> bool:
    return type(value) is int and 0 < value <= ceiling


def _assert_limit_only_body(path: str, input_0: Mapping[str, Any]) -> None:
    """Refuse any body that is not a KRX limit-order shape for its path."""

    if path in _NEW_ORDER_PATHS:
        if set(input_0) != _NEW_ORDER_BODY_KEYS:
            raise NHPlugMockOrderRefused(
                "new order body must be the exact KRX limit-order key set"
            )
        if input_0.get("nmn_pr_tp_cd") != LIMIT_PRICE_TYPE_CODE:
            raise NHPlugMockOrderRefused(
                "only limit orders (nmn_pr_tp_cd=01) are permitted; "
                "market and other price types are refused"
            )
        if (
            input_0.get("orr_cnd_dit_cd") != NO_ORDER_CONDITION_CODE
            or input_0.get("ssl_nmn_pr_dit_cd") != NORMAL_SHORT_SELL_CODE
        ):
            raise NHPlugMockOrderRefused(
                "order condition and short-sell codes must be the plain defaults"
            )
        if not _is_positive_int(
            input_0.get("orr_qty"), ceiling=MAX_ORDER_QUANTITY
        ) or not _is_positive_int(input_0.get("orr_pr"), ceiling=MAX_ORDER_PRICE_KRW):
            raise NHPlugMockOrderRefused(
                "limit orders need a positive integer quantity and price"
            )
    elif path == MODIFY_PATH:
        if set(input_0) != _MODIFY_BODY_KEYS:
            raise NHPlugMockOrderRefused("modify body must be the exact key set")
        if (
            not _is_positive_int(input_0.get("cor_qty"), ceiling=MAX_ORDER_QUANTITY)
            or not _is_positive_int(input_0.get("cor_pr"), ceiling=MAX_ORDER_PRICE_KRW)
            or input_0.get("sop_cnd_pr") != 0
        ):
            raise NHPlugMockOrderRefused(
                "modify needs a positive integer quantity and limit price, no stop"
            )
        if input_0.get("all_pat_dit_cd") not in {
            FULL_QUANTITY_CODE,
            PARTIAL_QUANTITY_CODE,
        }:
            raise NHPlugMockOrderRefused("modify scope code is not recognized")
    elif path == CANCEL_PATH:
        if not set(input_0) <= _CANCEL_BODY_KEYS or not {
            "act_no",
            "org_mkt_orr_no",
            "all_pat_dit_cd",
            "iem_cd",
        } <= set(input_0):
            raise NHPlugMockOrderRefused("cancel body must be the exact key set")
        scope = input_0.get("all_pat_dit_cd")
        if scope == FULL_QUANTITY_CODE:
            if "cor_qty" in input_0:
                raise NHPlugMockOrderRefused("a full cancel carries no quantity")
        elif scope == PARTIAL_QUANTITY_CODE:
            if not _is_positive_int(input_0.get("cor_qty"), ceiling=MAX_ORDER_QUANTITY):
                raise NHPlugMockOrderRefused("a partial cancel needs a quantity")
        else:
            raise NHPlugMockOrderRefused("cancel scope code is not recognized")
    else:
        raise NHPlugMockOrderRefused("NHPLUG order path is not allowlisted")

    if path in {MODIFY_PATH, CANCEL_PATH} and not _is_positive_int(
        input_0.get("org_mkt_orr_no"), ceiling=MAX_ORDER_NUMBER
    ):
        raise NHPlugMockOrderRefused("original order number must be a positive int")
    symbol = input_0.get("iem_cd")
    if not isinstance(symbol, str) or _KR_SYMBOL_RE.fullmatch(symbol) is None:
        raise NHPlugMockOrderRefused("symbol must be an exact six-digit KRX code")
    if path != CANCEL_PATH and (
        input_0.get("rmt_mkt_cd") != MOCK_ORDER_MARKET
        or input_0.get("sor_mkt_sli_yn") != NO_SOR_SPLIT
    ):
        raise NHPlugMockOrderRefused("orders are pinned to KRX without SOR split")


def _assert_send_authorized(authorization: object) -> None:
    """The per-call double gate's second half: exact dry_run=False + confirm=True.

    The exact type is required (a subclass could override ``authorizes_send``)
    and both fields are compared by identity with the bool singletons.
    """

    if (
        type(authorization) is not DryRunConfirmContract
        or authorization.dry_run is not False
        or authorization.confirm is not True
    ):
        raise NHPlugMockOrderRefused(
            "NHPLUG mock order dispatch requires dry_run=False and confirm=True"
        )


def _assert_order_ledger(ledger: object) -> NHPlugMockLedgerService:
    """Only the real ledger service may claim rows (no fakes, no subclasses)."""

    if type(ledger) is not NHPlugMockLedgerService:
        raise NHPlugMockOrderRefused(
            "NHPLUG mock orders are dispatched only through the ledger service claim"
        )
    return ledger


def _assert_claim_matches(
    claimed: object,
    *,
    ledger_row_id: int,
    client_request_id: str,
    expected: ExpectedOrder,
) -> ClaimedOrder:
    """Defense in depth: the claimed row is exactly the one requested."""

    if type(claimed) is not ClaimedOrder or (
        claimed.ledger_row_id,
        claimed.client_request_id,
        claimed.operation,
        claimed.symbol,
        claimed.side,
        claimed.quantity,
        claimed.price,
        claimed.original_order_no,
    ) != (
        ledger_row_id,
        client_request_id,
        expected.operation,
        expected.symbol,
        expected.side,
        expected.quantity,
        expected.price,
        expected.original_order_no,
    ):
        raise NHPlugMockOrderRefused("claimed ledger row does not match the request")
    return claimed


def _body_from_claim(
    claimed: ClaimedOrder, *, act_no: str, full_quantity: bool | None
) -> tuple[str, dict[str, Any]]:
    """Build the order body from the claimed ledger row and nothing else."""

    symbol = _require_kr_symbol(claimed.symbol)
    if claimed.operation == "place":
        if claimed.side == "buy":
            path = CASH_BUY_PATH
        elif claimed.side == "sell":
            path = CASH_SELL_PATH
        else:
            raise NHPlugMockOrderRefused("side must be 'buy' or 'sell'")
        return path, {
            "act_no": act_no,
            "iem_cd": symbol,
            "orr_qty": _require_quantity(claimed.quantity, "quantity"),
            "orr_pr": _require_price(claimed.price, "price"),
            "nmn_pr_tp_cd": LIMIT_PRICE_TYPE_CODE,
            "orr_cnd_dit_cd": NO_ORDER_CONDITION_CODE,
            "ssl_nmn_pr_dit_cd": NORMAL_SHORT_SELL_CODE,
            "rmt_mkt_cd": MOCK_ORDER_MARKET,
            "sor_mkt_sli_yn": NO_SOR_SPLIT,
        }
    if claimed.operation == "modify":
        if type(full_quantity) is not bool:
            raise NHPlugMockOrderRefused("modify scope must be stated explicitly")
        return MODIFY_PATH, {
            "act_no": act_no,
            "org_mkt_orr_no": _require_order_no(claimed.original_order_no),
            "all_pat_dit_cd": FULL_QUANTITY_CODE
            if full_quantity
            else PARTIAL_QUANTITY_CODE,
            "iem_cd": symbol,
            "cor_qty": _require_quantity(claimed.quantity, "quantity"),
            "cor_pr": _require_price(claimed.price, "price"),
            "sop_cnd_pr": 0,
            "rmt_mkt_cd": MOCK_ORDER_MARKET,
            "sor_mkt_sli_yn": NO_SOR_SPLIT,
        }
    if claimed.operation == "cancel":
        if claimed.price is not None:
            raise NHPlugMockOrderRefused("a cancel carries no price")
        body: dict[str, Any] = {
            "act_no": act_no,
            "org_mkt_orr_no": _require_order_no(claimed.original_order_no),
            "all_pat_dit_cd": FULL_QUANTITY_CODE
            if claimed.quantity is None
            else PARTIAL_QUANTITY_CODE,
            "iem_cd": symbol,
        }
        if claimed.quantity is not None:
            body["cor_qty"] = _require_quantity(claimed.quantity, "quantity")
        return CANCEL_PATH, body
    raise NHPlugMockOrderRefused("unknown order operation")


def _assert_built_body_is_claimed(
    claimed: ClaimedOrder, path: str, input_0: Mapping[str, Any]
) -> None:
    """The exact bytes about to be sent must carry the claimed values."""

    quantity_key = "orr_qty" if path in _NEW_ORDER_PATHS else "cor_qty"
    price_key = "orr_pr" if path in _NEW_ORDER_PATHS else "cor_pr"
    matches = (
        input_0.get("iem_cd") == claimed.symbol
        and input_0.get(quantity_key) == claimed.quantity
    )
    if path == CANCEL_PATH:
        matches = matches and price_key not in input_0
    else:
        matches = matches and input_0.get(price_key) == claimed.price
    if path != CASH_BUY_PATH and path != CASH_SELL_PATH:
        matches = matches and input_0.get("org_mkt_orr_no") == claimed.original_order_no
    if not matches:
        raise NHPlugMockOrderRefused("built order body differs from the claimed row")


def _require_kr_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or _KR_SYMBOL_RE.fullmatch(symbol) is None:
        raise NHPlugMockOrderRefused("symbol must be an exact six-digit KRX code")
    return symbol


def _require_quantity(value: object, name: str) -> int:
    if not _is_positive_int(value, ceiling=MAX_ORDER_QUANTITY):
        raise NHPlugMockOrderRefused(f"{name} must be a positive integer")
    return int(value)  # type: ignore[arg-type]


def _require_order_no(value: object) -> int:
    if not _is_positive_int(value, ceiling=MAX_ORDER_NUMBER):
        raise NHPlugMockOrderRefused("original order number must be a positive int")
    return int(value)  # type: ignore[arg-type]


def _require_price(value: object, name: str) -> int:
    if not _is_positive_int(value, ceiling=MAX_ORDER_PRICE_KRW):
        raise NHPlugMockOrderRefused(
            f"{name} must be a positive integer KRW limit price; "
            "market orders are not supported"
        )
    return int(value)  # type: ignore[arg-type]


class NHPlugMockClient:
    """Mock data client with no generic arbitrary-endpoint dispatch."""

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
        # Orders require an allowlist this client derived from its own
        # /n2/acctinfo response (``verify_and_bind_mock_account``), never one
        # handed in by a caller.
        self._order_allowlist: MockAccountAllowlist | None = None

    async def verify_and_bind_mock_account(self, configured_account_no: str) -> None:
        """Fetch /n2/acctinfo on this client and bind the acct_type=03 account.

        This is the only way to make order dispatch possible on a client.
        """

        payload = await self.list_accounts()
        allowlist = MockAccountAllowlist.from_acctinfo_response(
            payload=payload, configured_account_no=configured_account_no
        )
        allowlist.assert_allowed(allowlist.configured_account_no)
        self._account_allowlist = allowlist
        self._order_allowlist = allowlist

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
        # A caller-supplied allowlist enables reads only, never orders.
        self._order_allowlist = None

    def _require_order_allowlist(self) -> MockAccountAllowlist:
        allowlist = self._order_allowlist
        if allowlist is None or allowlist is not self._account_allowlist:
            raise NHPlugMockAccountRejected(
                "orders require an account verified by this client's own "
                "/n2/acctinfo request"
            )
        return allowlist

    def _require_account_allowlist(self) -> MockAccountAllowlist:
        allowlist = self._account_allowlist
        if allowlist is None:
            raise NHPlugMockConfigurationError(
                "a broker-verified mock account allowlist is required for account-scoped reads"
            )
        return allowlist

    @property
    def bound_account_no(self) -> str:
        """The configured account after broker verification (never logged)."""

        return self._require_account_allowlist().configured_account_no

    async def list_accounts(self) -> dict[str, Any]:
        """Read the documented account list used to establish the allowlist."""

        return await self._post_readonly(path=ACCOUNT_INFO_PATH, input_0={})

    async def fetch_balance(
        self, *, act_no: str, continuation_key: str | None = None
    ) -> dict[str, Any]:
        """Read domestic holdings after account verification at both guard points."""

        result = await self._dispatch_read(
            path=BALANCE_PATH,
            input_0={
                "act_no": act_no,
                "bnc_bse_cd": "5",
                "ltg_aot_dit_cd": "9",
                "aet_bse": "2",
                "qut_dit_cd": "UNT",
                "aly_qut_cd": "1",
            },
            act_no=act_no,
            continuation_key=continuation_key,
        )
        _assert_stage_one_success(result.payload)
        return result.payload

    async def fetch_balance_page(
        self, *, continuation_key: str | None = None
    ) -> DispatchResult:
        """One balance page with continuation headers for complete reads."""

        act_no = self.bound_account_no
        return await self._dispatch_read(
            path=BALANCE_PATH,
            input_0={
                "act_no": act_no,
                "bnc_bse_cd": "5",
                "ltg_aot_dit_cd": "9",
                "aet_bse": "2",
                "qut_dit_cd": "UNT",
                "aly_qut_cd": "1",
            },
            act_no=act_no,
            continuation_key=continuation_key,
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

    async def fetch_order_listing_page(
        self,
        *,
        order_date: str,
        scope: str,
        continuation_key: str | None = None,
    ) -> DispatchResult:
        """One raw page of the daily order/execution listing.

        The payload is returned unjudged: an empty or absent row block is not
        evidence of "no orders".  ``order_evidence`` classifies each page and
        the reconcile layer cross-checks two independent scopes.
        """

        if (
            not isinstance(order_date, str)
            or _ORDER_DATE_RE.fullmatch(order_date) is None
        ):
            raise NHPlugMockConfigurationError("order_date must be YYYYMMDD")
        scope_code = LISTING_SCOPE_CODES.get(scope)
        if scope_code is None:
            raise NHPlugMockConfigurationError("listing scope must be all/filled/open")
        act_no = self.bound_account_no
        return await self._dispatch_read(
            path=DAILY_ORDER_EXECUTION_PATH,
            input_0={
                "orr_dt": order_date,
                "act_no": act_no,
                "orr_mkt_cd": _ALL_ORDER_MARKETS_CODE,
                "ost_cns_dit": scope_code,
            },
            act_no=act_no,
            continuation_key=continuation_key,
        )

    async def dispatch_claimed_order(
        self,
        *,
        ledger: NHPlugMockLedgerService,
        ledger_row_id: int,
        client_request_id: str,
        expected: ExpectedOrder,
        authorization: DryRunConfirmContract,
        full_quantity: bool | None = None,
    ) -> dict[str, Any]:
        """Send exactly the committed body of one ledger row, at most once.

        No quantity, price, or order object is accepted from the caller: the
        row is claimed atomically immediately before send and the body is
        built from the claimed values only (``expected`` is a match filter).
        """

        _assert_send_authorized(authorization)
        return await self._post_mutation(
            ledger=ledger,
            ledger_row_id=ledger_row_id,
            client_request_id=client_request_id,
            expected=expected,
            authorization=authorization,
            full_quantity=full_quantity,
        )

    async def _post_readonly(
        self,
        *,
        path: str,
        input_0: dict[str, Any],
        act_no: str | None = None,
    ) -> dict[str, Any]:
        """Stage 1 read with the business-code judgment applied."""

        result = await self._dispatch_read(path=path, input_0=input_0, act_no=act_no)
        _assert_stage_one_success(result.payload)
        return result.payload

    async def _dispatch_read(
        self,
        *,
        path: str,
        input_0: dict[str, Any],
        act_no: str | None = None,
        continuation_key: str | None = None,
    ) -> DispatchResult:
        """Guard before token I/O, then guard the resolved request before send."""

        _assert_mock_enabled()
        _assert_readonly_path(path)
        if self._base_url != MOCK_BASE_URL:
            raise NHPlugMockEndpointError(
                "NHPLUG data base endpoint changed after construction"
            )
        account_allowlist: MockAccountAllowlist | None = None
        if path != ACCOUNT_INFO_PATH:
            account_allowlist = self._require_account_allowlist()
            verified_act_no = account_allowlist.configured_account_no
            if path in _ACCOUNT_SCOPED_READ_PATHS:
                if not isinstance(act_no, str) or input_0.get("act_no") != act_no:
                    raise NHPlugMockConfigurationError(
                        "account-scoped reads require the bound configured account"
                    )
                verified_act_no = act_no
            elif act_no is not None:
                raise NHPlugMockConfigurationError(
                    "only account-scoped reads may supply an account number"
                )
            if verified_act_no != account_allowlist.configured_account_no:
                raise NHPlugMockAccountRejected(
                    "account-scoped reads may use only the configured mock account"
                )
            account_allowlist.assert_allowed(verified_act_no)

        headers = await self._headers(continuation_key=continuation_key)
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
            _assert_resolved_mock_request(
                request, allowed_paths=ALLOWED_READONLY_PATHS, expected_path=path
            )
            # Second independent account check immediately before the send site,
            # made against the exact bytes about to leave the process.
            if account_allowlist is not None:
                account_allowlist.assert_allowed(
                    account_allowlist.configured_account_no
                )
                if path in _ACCOUNT_SCOPED_READ_PATHS:
                    _assert_built_account(request, allowlist=account_allowlist)
            response = await client.send(request)
        return _parse_response(response)

    async def _post_mutation(
        self,
        *,
        ledger: NHPlugMockLedgerService,
        ledger_row_id: int,
        client_request_id: str,
        expected: ExpectedOrder,
        authorization: DryRunConfirmContract,
        full_quantity: bool | None,
    ) -> dict[str, Any]:
        """The only order dispatcher: claim, build from the claim, check, send."""

        _assert_send_authorized(authorization)
        claim_ledger = _assert_order_ledger(ledger)
        if type(expected) is not ExpectedOrder:
            raise NHPlugMockOrderRefused("an explicit expected order is required")
        _assert_mock_enabled()
        if self._base_url != MOCK_BASE_URL:
            raise NHPlugMockEndpointError(
                "NHPLUG data base endpoint changed after construction"
            )
        account_allowlist = self._require_order_allowlist()
        account_allowlist.assert_allowed(account_allowlist.configured_account_no)

        headers = await self._headers(continuation_key=None)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
            # A redirect could forward APP credentials and an order body to
            # another origin; a 3xx is never followed.
            follow_redirects=False,
        ) as client:
            # Durable single-use claim, committed before any byte is sent.
            claimed = await claim_ledger.claim_for_dispatch(
                row_id=ledger_row_id,
                client_request_id=client_request_id,
                expected=expected,
            )
            if claimed is None:
                raise NHPlugMockClaimRejected(
                    "ledger row is not claimable (already dispatched, replayed, "
                    "or different from the committed order)"
                )
            claimed = _assert_claim_matches(
                claimed,
                ledger_row_id=ledger_row_id,
                client_request_id=client_request_id,
                expected=expected,
            )
            path, input_0 = _body_from_claim(
                claimed,
                act_no=account_allowlist.configured_account_no,
                full_quantity=full_quantity,
            )
            _assert_mutation_path(path)
            _assert_limit_only_body(path, input_0)
            request = client.build_request(
                "POST", path, headers=headers, json={"Input_0": input_0}
            )
            # Everything above is provably pre-dispatch.  Recheck the built
            # request immediately before send: scheme, host, port, path,
            # account, the limit-only shape, and the claimed values.
            _assert_resolved_mock_request(
                request, allowed_paths=ALLOWED_MUTATION_PATHS, expected_path=path
            )
            _assert_built_account(request, allowlist=account_allowlist)
            _assert_limit_only_body(path, _built_input(request))
            _assert_built_body_is_claimed(claimed, path, _built_input(request))
            try:
                response = await client.send(request)
            except Exception as exc:
                raise NHPlugMockDispatchUncertain(
                    "NHPLUG order request may have reached the broker"
                ) from exc
        try:
            return _parse_response(response).payload
        except Exception as exc:
            raise NHPlugMockDispatchUncertain(
                "NHPLUG order response could not be read"
            ) from exc

    async def _headers(self, *, continuation_key: str | None) -> dict[str, str]:
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
        if continuation_key is not None:
            if not isinstance(continuation_key, str) or not continuation_key.strip():
                raise NHPlugMockConfigurationError("continuation key must be non-empty")
            headers["cts"] = continuation_key
        return headers


def _parse_response(response: httpx.Response) -> DispatchResult:
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise NHPlugMockResponseError("NHPLUG mock response was not JSON") from exc
    if not isinstance(payload, dict):
        raise NHPlugMockResponseError("NHPLUG mock response was not an object")
    key = (response.headers.get("cts") or "").strip() or None
    flag = (response.headers.get("cts_flag") or "").strip().upper() or None
    return DispatchResult(
        payload=dict(payload), continuation_key=key, continuation_flag=flag
    )


def _assert_stage_one_success(payload: Mapping[str, Any]) -> None:
    response_code = payload.get("rsp_cd")
    if (
        not isinstance(response_code, str)
        or response_code not in _SUCCESS_RESPONSE_CODES
    ):
        raise NHPlugMockBrokerRejected(response_code=str(response_code or "unknown"))
