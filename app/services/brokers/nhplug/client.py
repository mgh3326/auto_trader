"""Pinned NHPLUG mock data client and single Stage 2 order dispatch owner.

This module never imports the OAuth implementation and never contains the
production hostname.  It has an exact mock host-and-port allowlist, a short
read-only path allowlist checked before token resolution, and no mutation API.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Final
from uuid import UUID

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
from app.services.brokers.nhplug.order_evidence import (
    OrderListing,
    assemble_listing,
    classify_listing_page,
)
from app.services.nhplug_mock.account_identity import KeyMaterial, resolve_account_ref
from app.services.nhplug_mock.intent import ORDER_PATHS, body_digest, build_body
from app.services.nhplug_mock.ledger import (
    Claim,
    DispatchOutcome,
    LeaseIdentity,
    LedgerConflict,
    NHPlugMockLedger,
)
from app.services.nhplug_mock.outcome import (
    ResponseMeta,
    classify,
    extract_order_no,
    parse_order_response,
)
from app.services.nhplug_mock.readiness import Stage2Readiness
from app.services.nhplug_mock.transport import (
    GatedTransport,
    Stage2Timing,
    clock_boottime,
)

MOCK_BASE_URL: Final[str] = "https://moapi.nhplug.com:8443"
MOCK_HOST: Final[str] = "moapi.nhplug.com"
MOCK_PORT: Final[int] = 8443

ACCOUNT_INFO_PATH: Final[str] = "/n2/acctinfo"
BALANCE_PATH: Final[str] = "/krstock/inquiry/v1/balance"
QUOTE_PATH: Final[str] = "/krstock/quote/v1/currentPrice"
DAILY_ORDER_EXECUTION_PATH: Final[str] = "/krstock/inquiry/v1/dailyOrderExecution"
ALLOWED_READONLY_PATHS: Final[frozenset[str]] = frozenset(
    {ACCOUNT_INFO_PATH, BALANCE_PATH, QUOTE_PATH}
)

_SUCCESS_RESPONSE_CODES: Final[frozenset[str]] = frozenset(
    {"00000", "00166", "00221", "13578"}
)
_KR_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^\d{6}$")
_ALLOWED_MARKETS: Final[frozenset[str]] = frozenset({"KRX"})
TokenProvider = Callable[[], Awaitable[str]]
_record_tasks: set[asyncio.Task[bool]] = set()


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


def _assert_resolved_mock_request(
    request: httpx.Request, *, stage2_listing: bool = False
) -> None:
    """Revalidate resolved host, port, and path immediately before dispatch."""

    if (
        request.url.scheme != "https"
        or request.url.host != MOCK_HOST
        or request.url.port != MOCK_PORT
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG data request resolved outside the pinned mock HTTPS endpoint"
        )
    if not (stage2_listing and request.url.path == DAILY_ORDER_EXECUTION_PATH):
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
        self._order_allowlist_verified = False

    async def verify_and_bind_mock_account(self, configured_account_no: str) -> None:
        """Stage 2 authority must come from this client's fresh account response."""

        payload = await self.list_accounts()
        allowlist = MockAccountAllowlist.from_acctinfo_response(
            payload=payload, configured_account_no=configured_account_no
        )
        self.bind_account_allowlist(allowlist)
        self._order_allowlist_verified = True

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

    async def fetch_order_listing(
        self, *, order_date: str, scope: str, max_pages: int = 100
    ) -> OrderListing:
        """Fetch every page of one mock account order scope for manual reconciliation."""

        _assert_mock_enabled()
        Stage2Readiness.from_env().assert_ready()
        if type(order_date) is not str or re.fullmatch(r"[0-9]{8}", order_date) is None:
            raise NHPlugMockConfigurationError("order_date must be YYYYMMDD")
        scope_code = {"all": "0", "filled": "1", "open": "2"}.get(scope)
        if (
            scope_code is None
            or type(max_pages) is not int
            or max_pages < 1
            or max_pages > 100
        ):
            raise NHPlugMockConfigurationError("invalid listing scope or page limit")
        allowlist = self._require_account_allowlist()
        act_no = allowlist.configured_account_no
        allowlist.assert_allowed(act_no)
        pages = []
        continuation: str | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            token = await self._token_provider()
            if type(token) is not str or not token.strip():
                raise NHPlugMockResponseError(
                    "NHPLUG OAuth provider returned no access token"
                )
            headers = {
                "x-client-id": self._app_key,
                "x-client-secret": self._app_secret,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            }
            if continuation is not None:
                headers["cts"] = continuation
            async with httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as http:
                request = http.build_request(
                    "POST",
                    DAILY_ORDER_EXECUTION_PATH,
                    headers=headers,
                    json={
                        "Input_0": {
                            "orr_dt": order_date,
                            "act_no": act_no,
                            "orr_mkt_cd": "00",
                            "ost_cns_dit": scope_code,
                        }
                    },
                )
                _assert_resolved_mock_request(request, stage2_listing=True)
                if json.loads(request.content) != {
                    "Input_0": {
                        "orr_dt": order_date,
                        "act_no": act_no,
                        "orr_mkt_cd": "00",
                        "ost_cns_dit": scope_code,
                    }
                }:
                    raise NHPlugMockConfigurationError(
                        "listing request changed after build"
                    )
                allowlist.assert_allowed(act_no)
                response = await http.send(request)
            try:
                payload = response.json() if response.status_code == 200 else None
            except ValueError:
                payload = None
            page = classify_listing_page(
                payload,
                header_continuation_key=response.headers.get("cts"),
                header_continuation_flag=response.headers.get("cts_flag"),
            )
            pages.append(page)
            if not page.usable or not page.has_next:
                break
            continuation = page.continuation_key
            if continuation is None or continuation in seen:
                break
            seen.add(continuation)
        return assemble_listing(
            scope, pages, truncated=bool(pages and pages[-1].has_next)
        )

    async def dispatch_claimed_order(
        self,
        ledger: NHPlugMockLedger,
        row_id: int,
        request_id: UUID,
        intent_digest: str,
        account_ref: UUID,
        *,
        keys: dict[int, KeyMaterial],
        identity: LeaseIdentity,
        readiness: Stage2Readiness,
        timing: Stage2Timing,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> DispatchOutcome:
        """The only order HTTP send site; accepts no mutable body fields."""

        _assert_mock_enabled()
        if type(self) is not NHPlugMockClient or type(ledger) is not NHPlugMockLedger:
            raise LedgerConflict("dispatcher_or_ledger_invalid")
        if type(readiness) is not Stage2Readiness:
            raise LedgerConflict("readiness_invalid")
        readiness.assert_ready()
        Stage2Readiness.from_env().assert_ready()
        if type(timing) is not Stage2Timing:
            raise LedgerConflict("timing_invalid")
        timing.validate()
        if (
            type(dry_run) is not bool
            or type(confirm) is not bool
            or dry_run is not False
            or confirm is not True
        ):
            raise LedgerConflict("confirmation_required")
        if not self._order_allowlist_verified:
            raise LedgerConflict("broker_account_verification_required")
        allowlist = self._require_account_allowlist()
        verified_act_no = allowlist.configured_account_no
        allowlist.assert_allowed(verified_act_no)
        resolved = await resolve_account_ref(
            ledger.engine, verified_act_no, keys, create=True
        )
        if resolved != account_ref:
            raise LedgerConflict("account_ref_mismatch")
        claim = await ledger.claim(
            row_id,
            request_id,
            intent_digest,
            account_ref,
            identity,
            claim_window_seconds=timing.claim_window_seconds,
        )
        try:
            path, input_0 = build_body(claim.intent, verified_act_no)
            if body_digest(claim.intent) != claim.digest:
                raise LedgerConflict("digest_mismatch")
            token = await self._token_provider()
            if type(token) is not str or not token.strip():
                raise LedgerConflict("oauth_token_unavailable")
            request = httpx.Request(
                "POST",
                self._base_url + path,
                headers={
                    "x-client-id": self._app_key,
                    "x-client-secret": self._app_secret,
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json={"Input_0": input_0},
            )
            self._assert_order_request(request, path, claim, verified_act_no)
            # A second account-registry read catches rotation before the fence.
            if (
                await resolve_account_ref(
                    ledger.engine, input_0["act_no"], keys, create=True
                )
                != claim.account_ref
            ):
                raise LedgerConflict("account_ref_mismatch")
            success_codes, no_order_codes = await ledger.proof_codes(path)
            transport = GatedTransport()
            t0 = clock_boottime()
            transport.arm(t0 + timing.first_write_seconds)
        except BaseException:
            await ledger.withdraw(claim, "pre_send_refusal")
            raise
        # The fence transaction begins only after t0. An ambiguous commit never sends.
        fenced = await ledger.fence(
            claim,
            lease_seconds=timing.lease_seconds,
            lock_timeout_ms=timing.lock_timeout_ms,
        )
        if not fenced:
            raise LedgerConflict("fence_rejected")
        evidence_no: str | None = None
        meta: ResponseMeta | None = None
        parsed = None
        failure: BaseException | None = None
        try:
            try:
                remaining = max(0.0, t0 + timing.send_seconds - clock_boottime())
                async with asyncio.timeout(remaining):
                    async with httpx.AsyncClient(
                        transport=transport, follow_redirects=False
                    ) as http:
                        self._assert_order_request(
                            request, path, claim, verified_act_no
                        )
                        response = await http.send(request)
                        raw = await response.aread()
                        evidence_no = extract_order_no(raw)
                        meta = ResponseMeta.of(response)
                        parsed = parse_order_response(raw)
            finally:
                await transport.hard_close(timing.close_seconds)
        except BaseException as exc:
            failure = exc
        try:
            outcome = classify(
                path,
                meta,
                parsed,
                evidence_no,
                failure,
                success_codes=success_codes,
                no_order_codes=no_order_codes,
            )
        except BaseException:
            outcome = DispatchOutcome("uncertain", "classify_failed", evidence_no)
        record = asyncio.create_task(ledger.record_final(claim, outcome))
        _record_tasks.add(record)
        record.add_done_callback(_record_tasks.discard)
        cancelled_during_record = False
        while not record.done():
            try:
                await asyncio.shield(record)
            except asyncio.CancelledError:
                cancelled_during_record = True
        # Propagate a recording failure: the row remains sending until manual
        # recovery, and this dispatcher must never send a second request.
        await record
        if cancelled_during_record:
            raise asyncio.CancelledError
        if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise failure
        return outcome

    @staticmethod
    def _assert_order_request(
        request: httpx.Request, path: str, claim: Claim, verified_act_no: str
    ) -> None:
        if (
            type(path) is not str
            or path not in ORDER_PATHS
            or (
                request.url.scheme != "https"
                or request.url.host != MOCK_HOST
                or request.url.port != MOCK_PORT
                or request.url.path != path
                or request.method != "POST"
                or request.url.query
            )
        ):
            raise NHPlugMockEndpointError(
                "order request escaped the mock order allowlist"
            )
        try:
            body = json.loads(request.content)
        except (ValueError, TypeError):
            raise LedgerConflict("order_body_invalid") from None
        intent = claim.intent
        if intent.operation_kind == "place":
            expected_path = (
                "/krstock/order/v1/cashBuy"
                if intent.side == "buy"
                else "/krstock/order/v1/cashSell"
            )
            expected = {
                "act_no": verified_act_no,
                "iem_cd": intent.symbol,
                "orr_qty": intent.quantity,
                "orr_pr": intent.price,
                "nmn_pr_tp_cd": "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            }
        elif intent.operation_kind == "modify":
            expected_path = "/krstock/order/v1/modify"
            expected = {
                "act_no": verified_act_no,
                "org_mkt_orr_no": intent.original_order_id,
                "all_pat_dit_cd": "1" if intent.amend_scope == "full" else "2",
                "iem_cd": intent.symbol,
                "cor_qty": intent.quantity,
                "cor_pr": intent.price,
                "sop_cnd_pr": 0,
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            }
        else:
            expected_path = "/krstock/order/v1/cancel"
            expected = {
                "act_no": verified_act_no,
                "org_mkt_orr_no": intent.original_order_id,
                "all_pat_dit_cd": "1" if intent.amend_scope == "full" else "2",
                "iem_cd": intent.symbol,
            }
            if intent.quantity is not None:
                expected["cor_qty"] = intent.quantity
        if (
            path != expected_path
            or type(body) is not dict
            or body != {"Input_0": expected}
        ):
            raise LedgerConflict("order_body_differs_from_claim")

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
