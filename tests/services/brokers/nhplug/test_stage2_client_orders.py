"""Offline transport tests for the NHPLUG Stage 2 order boundary (#711).

Every boundary from the operator decision gets a direct test here:
host/scheme/port/path + act_no re-verification after build, acct_type=03
only, the default-off gate plus per-call confirm, no redirects, limit-only.
No real NH API call is made; all transports are ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.services.brokers.nhplug import client as client_module
from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.client import (
    ACCOUNT_INFO_PATH,
    ALLOWED_MUTATION_PATHS,
    CANCEL_PATH,
    CASH_BUY_PATH,
    CASH_SELL_PATH,
    DAILY_ORDER_EXECUTION_PATH,
    MOCK_HOST,
    MOCK_PORT,
    MODIFY_PATH,
    NHPlugMockClient,
)
from app.services.brokers.nhplug.contracts import (
    DryRunConfirmContract,
    issue_committed_intent,
)
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockConfigurationError,
    NHPlugMockDisabled,
    NHPlugMockDispatchUncertain,
    NHPlugMockEndpointError,
    NHPlugMockOrderRefused,
    NHPlugMockReadOnlyEndpointError,
)

pytestmark = pytest.mark.unit

MOCK_ACCOUNT = "MOCK-ACCOUNT-03"
CONFIRMED = DryRunConfirmContract(dry_run=False, confirm=True)
ACK = {"rsp_cd": "00000", "Output_0": {"mkt_orr_no": 1000123}}


class _Tokens:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return "unit-test-token"


class _Broker:
    """Records every request that reaches the transport."""

    def __init__(
        self, respond: Callable[[httpx.Request], httpx.Response] | None = None
    ):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == ACCOUNT_INFO_PATH and request.url.host == MOCK_HOST:
            return self._accounts()
        if self._respond is not None:
            return self._respond(request)
        if request.url.path == ACCOUNT_INFO_PATH:
            return httpx.Response(
                200,
                json={
                    "rsp_cd": "00000",
                    "Output_0": [
                        {"acct_no": MOCK_ACCOUNT, "acct_type": "03"},
                        {"acct_no": "LIVE-ACCOUNT-01", "acct_type": "01"},
                    ],
                },
            )
        return httpx.Response(200, json=ACK)

    @staticmethod
    def _accounts() -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rsp_cd": "00000",
                "Output_0": [
                    {"acct_no": MOCK_ACCOUNT, "acct_type": "03"},
                    {"acct_no": "LIVE-ACCOUNT-01", "acct_type": "01"},
                ],
            },
        )

    @property
    def order_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path in ALLOWED_MUTATION_PATHS]


async def _bound_client(broker: _Broker) -> tuple[NHPlugMockClient, _Tokens]:
    """A client verified through its own /n2/acctinfo call; counters reset."""

    tokens = _Tokens()
    client = NHPlugMockClient(
        app_key="test-key",
        app_secret="test-secret",
        token_provider=tokens,
        transport=httpx.MockTransport(broker),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NHPLUG_MOCK_ENABLED", "true")
        await client.verify_and_bind_mock_account(MOCK_ACCOUNT)
    tokens.calls = 0
    broker.requests.clear()
    return client, tokens


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)["Input_0"]


def test_mutation_allowlist_is_exactly_four_krx_order_paths() -> None:
    assert ALLOWED_MUTATION_PATHS == frozenset(
        {
            "/krstock/order/v1/cashBuy",
            "/krstock/order/v1/cashSell",
            "/krstock/order/v1/modify",
            "/krstock/order/v1/cancel",
        }
    )


# --- boundary 4: default-off gate + per-call confirm -----------------------


@pytest.mark.asyncio
async def test_gate_off_refuses_orders_before_token_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHPLUG_MOCK_ENABLED", raising=False)
    broker = _Broker()
    client, tokens = await _bound_client(broker)
    with pytest.raises(NHPlugMockDisabled):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )
    assert tokens.calls == 0 and broker.requests == []


@pytest.mark.parametrize(
    "value", ("true", "false", "1", "yes", "TRUE", " true", "True", "")
)
@pytest.mark.asyncio
async def test_only_exact_true_arms_the_gate(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", value)
    broker = _Broker()
    client, _ = await _bound_client(broker)
    if value == "true":
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )
        assert len(broker.order_requests) == 1
    else:
        with pytest.raises(NHPlugMockDisabled):
            await _submit(
                client,
                side="buy",
                symbol="005930",
                quantity=1,
                price=50000,
                authorization=CONFIRMED,
            )
        assert broker.requests == []


@pytest.mark.parametrize(
    "authorization",
    (
        DryRunConfirmContract(),
        DryRunConfirmContract(dry_run=False, confirm=False),
        DryRunConfirmContract(dry_run=True, confirm=True),
        DryRunConfirmContract(dry_run=0, confirm=1),  # type: ignore[arg-type]
        None,
        {"dry_run": False, "confirm": True},
    ),
    ids=("default", "unconfirmed", "dry_run_confirmed", "truthy_ints", "none", "dict"),
)
@pytest.mark.parametrize("operation", ("buy", "sell", "modify", "cancel"))
@pytest.mark.asyncio
async def test_every_mutation_needs_exact_dry_run_false_and_confirm_true(
    armed: None, authorization: Any, operation: str
) -> None:
    broker = _Broker()
    client, tokens = await _bound_client(broker)
    with pytest.raises(NHPlugMockOrderRefused, match="confirm=True"):
        if operation in {"buy", "sell"}:
            await _submit(
                client,
                side=operation,
                symbol="005930",
                quantity=1,
                price=50000,
                authorization=authorization,
            )
        elif operation == "modify":
            await _modify(
                client,
                original_order_no=1000123,
                symbol="005930",
                quantity=1,
                price=49500,
                full_quantity=True,
                authorization=authorization,
            )
        else:
            await _cancel(
                client,
                original_order_no=1000123,
                symbol="005930",
                quantity=None,
                authorization=authorization,
            )
    assert tokens.calls == 0
    assert broker.requests == []


# --- limit-only ------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_buy_and_sell_send_exact_krx_limit_bodies(armed: None) -> None:
    broker = _Broker()
    client, _ = await _bound_client(broker)
    buy = await _submit(
        client,
        side="buy",
        symbol="005930",
        quantity=2,
        price=50000,
        authorization=CONFIRMED,
    )
    await _submit(
        client,
        side="sell",
        symbol="005930",
        quantity=1,
        price=60000,
        authorization=CONFIRMED,
    )
    assert buy == ACK
    assert [r.url.path for r in broker.order_requests] == [
        CASH_BUY_PATH,
        CASH_SELL_PATH,
    ]
    for request in broker.order_requests:
        assert (request.url.scheme, request.url.host, request.url.port) == (
            "https",
            MOCK_HOST,
            MOCK_PORT,
        )
    assert _body(broker.order_requests[0]) == {
        "act_no": MOCK_ACCOUNT,
        "iem_cd": "005930",
        "orr_qty": 2,
        "orr_pr": 50000,
        "nmn_pr_tp_cd": "01",
        "orr_cnd_dit_cd": "00",
        "ssl_nmn_pr_dit_cd": "00",
        "rmt_mkt_cd": "KRX",
        "sor_mkt_sli_yn": "N",
    }


@pytest.mark.parametrize("price", (None, 0, -1, 1.5, "50000", True, 100_000_001))
@pytest.mark.asyncio
async def test_missing_or_invalid_limit_price_is_refused_before_token(
    armed: None, price: Any
) -> None:
    broker = _Broker()
    client, tokens = await _bound_client(broker)
    with pytest.raises(NHPlugMockOrderRefused):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=price,
            authorization=CONFIRMED,
        )
    assert tokens.calls == 0 and broker.requests == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("nmn_pr_tp_cd", "05"),  # market
        ("nmn_pr_tp_cd", "12"),  # best-limit
        ("nmn_pr_tp_cd", "61"),  # pre-market after-hours
        ("orr_cnd_dit_cd", "01"),  # IOC
        ("rmt_mkt_cd", "SOR"),
        ("rmt_mkt_cd", "NXT"),
        ("sor_mkt_sli_yn", "Y"),
        ("orr_amt", 50000),  # amount-based order
        ("sop_cnd_pr", 49000),  # stop
        ("act_no", "LIVE-ACCOUNT-01"),
    ),
)
@pytest.mark.asyncio
async def test_built_body_is_rechecked_immediately_before_send(
    armed: None, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    """A request altered after the pre-token check never reaches the socket."""

    original_build = httpx.AsyncClient.build_request

    def tampering_build(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> Any:
        body = kwargs.get("json")
        if isinstance(body, dict) and "Input_0" in body:
            body = {"Input_0": {**body["Input_0"], field: value}}
            kwargs["json"] = body
        return original_build(self, *args, **kwargs)

    broker = _Broker()
    client, _ = await _bound_client(broker)
    monkeypatch.setattr(httpx.AsyncClient, "build_request", tampering_build)
    with pytest.raises((NHPlugMockOrderRefused, NHPlugMockAccountRejected)):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )
    assert broker.requests == []


# --- boundary 1: host/scheme/port/path/act_no after build -------------------


@pytest.mark.parametrize(
    ("url", "error"),
    (
        ("https://api.nhplug.com:8443" + CASH_BUY_PATH, NHPlugMockEndpointError),
        ("https://moapi.nhplug.com:443" + CASH_BUY_PATH, NHPlugMockEndpointError),
        ("http://moapi.nhplug.com:8443" + CASH_BUY_PATH, NHPlugMockEndpointError),
        ("https://moapi.n2plug.com:8443" + CASH_BUY_PATH, NHPlugMockEndpointError),
        (
            "https://moapi.nhplug.com:8443/krstock/order/v1/creditBuy",
            NHPlugMockReadOnlyEndpointError,
        ),
    ),
)
@pytest.mark.asyncio
async def test_resolved_order_request_is_rechecked_before_send(
    armed: None, monkeypatch: pytest.MonkeyPatch, url: str, error: type[Exception]
) -> None:
    original_build = httpx.AsyncClient.build_request

    def redirecting_build(self: httpx.AsyncClient, method: str, _path: str, **kw: Any):
        return original_build(self, method, url, **kw)

    broker = _Broker()
    client, _ = await _bound_client(broker)
    monkeypatch.setattr(httpx.AsyncClient, "build_request", redirecting_build)
    with pytest.raises(error):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )
    assert broker.requests == []


@pytest.mark.parametrize(
    ("side", "swapped_path"),
    (("buy", CASH_SELL_PATH), ("sell", CASH_BUY_PATH)),
)
@pytest.mark.asyncio
async def test_order_path_must_be_exactly_the_intended_one(
    armed: None, monkeypatch: pytest.MonkeyPatch, side: str, swapped_path: str
) -> None:
    """A buy body can never leave on the sell route (or vice versa)."""

    original_build = httpx.AsyncClient.build_request

    def swap_route(self: httpx.AsyncClient, method: str, _path: str, **kw: Any):
        return original_build(self, method, swapped_path, **kw)

    broker = _Broker()
    client, _ = await _bound_client(broker)
    monkeypatch.setattr(httpx.AsyncClient, "build_request", swap_route)
    with pytest.raises(NHPlugMockReadOnlyEndpointError, match="different path"):
        await _submit(
            client,
            side=side,
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )
    assert broker.requests == []


@pytest.mark.asyncio
async def test_listing_act_no_is_rechecked_after_build(
    armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_build = httpx.AsyncClient.build_request

    def swap_account(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> Any:
        body = kwargs["json"]
        kwargs["json"] = {"Input_0": {**body["Input_0"], "act_no": "LIVE-ACCOUNT-01"}}
        return original_build(self, *args, **kwargs)

    broker = _Broker()
    client, _ = await _bound_client(broker)
    monkeypatch.setattr(httpx.AsyncClient, "build_request", swap_account)
    with pytest.raises(NHPlugMockAccountRejected):
        await client.fetch_order_listing_page(order_date="20260925", scope="open")
    assert broker.requests == []


@pytest.mark.asyncio
async def test_orders_are_impossible_without_a_bound_mock_allowlist(
    armed: None,
) -> None:
    broker = _Broker()
    client = NHPlugMockClient(
        app_key="k",
        app_secret="s",
        token_provider=_Tokens(),
        transport=httpx.MockTransport(broker),
    )
    with pytest.raises(NHPlugMockAccountRejected):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )
    assert broker.requests == []


# --- boundary 2: acct_type=03 only -----------------------------------------


@pytest.mark.parametrize("account_type", ("01", "02"))
def test_live_account_types_can_never_be_bound_for_orders(account_type: str) -> None:
    with pytest.raises(NHPlugMockAccountRejected):
        MockAccountAllowlist.from_acctinfo_response(
            payload={
                "rsp_cd": "00000",
                "Output_0": [{"acct_no": "ACCOUNT", "acct_type": account_type}],
            },
            configured_account_no="ACCOUNT",
        )


def test_conflicting_type_for_the_same_number_rejects_the_whole_response() -> None:
    with pytest.raises(NHPlugMockAccountRejected, match="conflicting"):
        MockAccountAllowlist.from_acctinfo_response(
            payload={
                "rsp_cd": "00000",
                "Output_0": [
                    {"acct_no": "ACCOUNT", "acct_type": "03"},
                    {"acct_no": "ACCOUNT", "acct_type": "01"},
                ],
            },
            configured_account_no="ACCOUNT",
        )


# --- boundary 5: no redirects ----------------------------------------------


@pytest.mark.asyncio
async def test_order_redirect_is_never_followed(armed: None) -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307,
            headers={"Location": "https://api.nhplug.com:8443" + CASH_BUY_PATH},
        )

    broker = _Broker(redirect)
    client, _ = await _bound_client(broker)
    with pytest.raises(NHPlugMockDispatchUncertain):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )
    assert [r.url.host for r in broker.requests] == [MOCK_HOST]


@pytest.mark.asyncio
async def test_listing_redirect_is_never_followed(armed: None) -> None:
    broker = _Broker(
        lambda request: httpx.Response(
            302, headers={"Location": "https://example.invalid/steal"}
        )
    )
    client, _ = await _bound_client(broker)
    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_order_listing_page(order_date="20260925", scope="all")
    assert len(broker.requests) == 1


# --- dispatch uncertainty --------------------------------------------------


@pytest.mark.parametrize(
    "respond",
    (
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        lambda request: httpx.Response(200, content=b"<html>"),
        lambda request: httpx.Response(500, json={"rsp_cd": "99999"}),
        lambda request: httpx.Response(200, json=["not", "an", "object"]),
    ),
    ids=("timeout", "non_json", "http_500", "non_object"),
)
@pytest.mark.asyncio
async def test_failures_at_or_after_send_are_dispatch_uncertain(
    armed: None, respond: Callable[[httpx.Request], httpx.Response]
) -> None:
    broker = _Broker(respond)
    client, _ = await _bound_client(broker)
    with pytest.raises(NHPlugMockDispatchUncertain):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
        )


# --- modify / cancel / listing shapes --------------------------------------


@pytest.mark.asyncio
async def test_modify_and_cancel_bodies(armed: None) -> None:
    broker = _Broker()
    client, _ = await _bound_client(broker)
    await _modify(
        client,
        original_order_no=1000123,
        symbol="005930",
        quantity=1,
        price=49500,
        full_quantity=True,
        authorization=CONFIRMED,
    )
    await _cancel(
        client,
        original_order_no=1000130,
        symbol="005930",
        quantity=None,
        authorization=CONFIRMED,
    )
    await _cancel(
        client,
        original_order_no=1000131,
        symbol="005930",
        quantity=2,
        authorization=CONFIRMED,
    )
    modify, full_cancel, partial_cancel = broker.order_requests
    assert modify.url.path == MODIFY_PATH
    assert _body(modify) == {
        "act_no": MOCK_ACCOUNT,
        "org_mkt_orr_no": 1000123,
        "all_pat_dit_cd": "1",
        "iem_cd": "005930",
        "cor_qty": 1,
        "cor_pr": 49500,
        "sop_cnd_pr": 0,
        "rmt_mkt_cd": "KRX",
        "sor_mkt_sli_yn": "N",
    }
    assert full_cancel.url.path == CANCEL_PATH
    assert _body(full_cancel) == {
        "act_no": MOCK_ACCOUNT,
        "org_mkt_orr_no": 1000130,
        "all_pat_dit_cd": "1",
        "iem_cd": "005930",
    }
    assert _body(partial_cancel)["all_pat_dit_cd"] == "2"
    assert _body(partial_cancel)["cor_qty"] == 2


@pytest.mark.parametrize("order_no", (0, -5, True, "1000123", 10_000_000_000))
@pytest.mark.asyncio
async def test_invalid_original_order_numbers_are_refused(
    armed: None, order_no: Any
) -> None:
    broker = _Broker()
    client, tokens = await _bound_client(broker)
    with pytest.raises(NHPlugMockOrderRefused):
        await _cancel(
            client,
            original_order_no=order_no,
            symbol="005930",
            quantity=None,
            authorization=CONFIRMED,
        )
    assert tokens.calls == 0 and broker.requests == []


@pytest.mark.asyncio
async def test_listing_page_sends_scope_date_and_continuation(armed: None) -> None:
    broker = _Broker(
        lambda request: httpx.Response(
            200,
            json={"rsp_cd": "00165", "Output_1": []},
            headers={"cts": "NEXTKEY", "cts_flag": "Y"},
        )
    )
    client, _ = await _bound_client(broker)
    result = await client.fetch_order_listing_page(
        order_date="20260925", scope="open", continuation_key="PREVKEY"
    )
    request = broker.requests[0]
    assert request.url.path == DAILY_ORDER_EXECUTION_PATH
    assert request.headers["cts"] == "PREVKEY"
    assert _body(request) == {
        "orr_dt": "20260925",
        "act_no": MOCK_ACCOUNT,
        "orr_mkt_cd": "00",
        "ost_cns_dit": "2",
    }
    assert (result.continuation_key, result.continuation_flag) == ("NEXTKEY", "Y")


@pytest.mark.parametrize(
    ("kwargs", "error"),
    (
        ({"order_date": "2026-09-25", "scope": "all"}, NHPlugMockConfigurationError),
        (
            {"order_date": "20260925", "scope": "everything"},
            NHPlugMockConfigurationError,
        ),
    ),
)
@pytest.mark.asyncio
async def test_listing_rejects_bad_inputs(
    armed: None, kwargs: dict[str, str], error: type[Exception]
) -> None:
    broker = _Broker()
    client, _ = await _bound_client(broker)
    with pytest.raises(error):
        await client.fetch_order_listing_page(**kwargs)
    assert broker.requests == []


def test_client_module_has_no_market_price_type_constant() -> None:
    assert client_module.LIMIT_PRICE_TYPE_CODE == "01"
    assert not any(
        value == "05"
        for name, value in vars(client_module).items()
        if name.isupper() and isinstance(value, str)
    )


# --- tester round 1 regressions ---------------------------------------------


def test_allowlist_cannot_be_hand_built_for_a_live_account() -> None:
    """Only /n2/acctinfo parsing can create an allowlist (tester finding 2)."""

    with pytest.raises(NHPlugMockAccountRejected, match="derived"):
        MockAccountAllowlist(
            configured_account_no="12345678-01",
            allowed_account_numbers=frozenset({"12345678-01"}),
            account_type_counts=(("01", 1),),
        )


def _caller_allowlist() -> MockAccountAllowlist:
    return MockAccountAllowlist.from_acctinfo_response(
        payload={
            "rsp_cd": "00000",
            "Output_0": [{"acct_no": MOCK_ACCOUNT, "acct_type": "03"}],
        },
        configured_account_no=MOCK_ACCOUNT,
    )


@pytest.mark.asyncio
async def test_caller_bound_allowlist_enables_reads_but_never_orders(
    armed: None,
) -> None:
    broker = _Broker()
    client = NHPlugMockClient(
        app_key="k",
        app_secret="s",
        token_provider=_Tokens(),
        transport=httpx.MockTransport(broker),
    )
    client.bind_account_allowlist(_caller_allowlist())
    with pytest.raises(NHPlugMockAccountRejected, match="own"):
        await _submit_with(client, CONFIRMED)
    assert broker.requests == []


@pytest.mark.asyncio
async def test_rebinding_after_verification_revokes_order_capability(
    armed: None,
) -> None:
    broker = _Broker()
    client, _ = await _bound_client(broker)
    client.bind_account_allowlist(_caller_allowlist())
    with pytest.raises(NHPlugMockAccountRejected):
        await _submit_with(client, CONFIRMED)
    assert broker.order_requests == []


class _LyingContract(DryRunConfirmContract):
    @property
    def authorizes_send(self) -> bool:  # type: ignore[override]
        return True


@pytest.mark.parametrize(
    "authorization",
    (
        _LyingContract(dry_run=True, confirm=False),
        _LyingContract(dry_run=False, confirm=True),
    ),
    ids=("lying_dry_run", "subclass_even_if_confirmed"),
)
@pytest.mark.asyncio
async def test_contract_subclasses_are_refused(armed: None, authorization: Any) -> None:
    broker = _Broker()
    client, tokens = await _bound_client(broker)
    with pytest.raises(NHPlugMockOrderRefused):
        await _submit_with(client, authorization)
    assert tokens.calls == 0 and broker.requests == []


@pytest.mark.parametrize(
    "authorization",
    (DryRunConfirmContract(), DryRunConfirmContract(dry_run=True, confirm=True), None),
    ids=("default", "dry_run_confirmed", "none"),
)
@pytest.mark.asyncio
async def test_dispatcher_itself_enforces_authorization(
    armed: None, authorization: Any
) -> None:
    """Calling the private dispatcher directly cannot skip the confirm gate."""

    broker = _Broker()
    client, tokens = await _bound_client(broker)
    with pytest.raises(NHPlugMockOrderRefused):
        await client._post_mutation(
            path=CASH_BUY_PATH,
            input_0={
                "act_no": MOCK_ACCOUNT,
                "iem_cd": "005930",
                "orr_qty": 1,
                "orr_pr": 50000,
                "nmn_pr_tp_cd": "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
            authorization=authorization,
            intent=_intent(
                "place", symbol="005930", side="buy", quantity=1, price=50000
            ),
        )
    assert tokens.calls == 0 and broker.requests == []


async def _submit_with(client: NHPlugMockClient, authorization: Any) -> Any:
    return await _submit(
        client,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        authorization=authorization,
    )


def _intent(
    operation: str,
    *,
    symbol: Any,
    side: Any = None,
    quantity: Any = None,
    price: Any = None,
    original: Any = None,
) -> Any:
    return issue_committed_intent(
        ledger_row_id=1,
        client_request_id=uuid.uuid4().hex,
        operation=operation,  # type: ignore[arg-type]
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        original_order_no=original,
    )


async def _submit(client: NHPlugMockClient, **kwargs: Any) -> Any:
    kwargs.setdefault(
        "intent",
        _intent(
            "place",
            symbol=kwargs.get("symbol"),
            side=kwargs.get("side"),
            quantity=kwargs.get("quantity"),
            price=kwargs.get("price"),
        ),
    )
    return await client.submit_limit_order(**kwargs)


async def _modify(client: NHPlugMockClient, **kwargs: Any) -> Any:
    kwargs.setdefault(
        "intent",
        _intent(
            "modify",
            symbol=kwargs.get("symbol"),
            quantity=kwargs.get("quantity"),
            price=kwargs.get("price"),
            original=kwargs.get("original_order_no"),
        ),
    )
    return await client.modify_limit_order(**kwargs)


async def _cancel(client: NHPlugMockClient, **kwargs: Any) -> Any:
    kwargs.setdefault(
        "intent",
        _intent(
            "cancel",
            symbol=kwargs.get("symbol"),
            quantity=kwargs.get("quantity"),
            original=kwargs.get("original_order_no"),
        ),
    )
    return await client.cancel_order(**kwargs)


# --- tester round 2: no committed ledger intent, no send --------------------


@pytest.mark.parametrize(
    "intent_factory",
    (
        lambda: None,
        lambda: {"operation": "place"},
        lambda: _intent("place", symbol="005930", side="sell", quantity=1, price=50000),
        lambda: _intent("place", symbol="005930", side="buy", quantity=2, price=50000),
        lambda: _intent("place", symbol="005930", side="buy", quantity=1, price=49999),
        lambda: _intent("place", symbol="000660", side="buy", quantity=1, price=50000),
        lambda: _intent("cancel", symbol="005930", original=1),
    ),
    ids=(
        "none",
        "dict",
        "wrong_side",
        "wrong_qty",
        "wrong_price",
        "wrong_symbol",
        "wrong_operation",
    ),
)
@pytest.mark.asyncio
async def test_order_needs_a_matching_committed_ledger_intent(
    armed: None, intent_factory: Any
) -> None:
    broker = _Broker()
    client, tokens = await _bound_client(broker)
    with pytest.raises(NHPlugMockOrderRefused, match="intent"):
        await client.submit_limit_order(
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
            intent=intent_factory(),
        )
    assert tokens.calls == 0 and broker.requests == []


def test_intents_cannot_be_constructed_outside_the_issuer() -> None:
    from app.services.brokers.nhplug.contracts import CommittedOrderIntent

    with pytest.raises(ValueError, match="ledger service"):
        CommittedOrderIntent(
            ledger_row_id=1,
            client_request_id="x",
            operation="place",
            symbol="005930",
            side="buy",
            quantity=1,
            price=50000,
            original_order_no=None,
        )


@pytest.mark.asyncio
async def test_an_intent_is_consumed_once_even_after_a_failed_send(
    armed: None,
) -> None:
    broker = _Broker(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("x")))
    client, _ = await _bound_client(broker)
    intent = _intent("place", symbol="005930", side="buy", quantity=1, price=50000)
    with pytest.raises(NHPlugMockDispatchUncertain):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
            intent=intent,
        )
    with pytest.raises(NHPlugMockOrderRefused, match="already used"):
        await _submit(
            client,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            authorization=CONFIRMED,
            intent=intent,
        )
    assert len(broker.order_requests) == 1
