"""hk 5946 — 모의 선물 주문 어댑터(`FuturesOrders`): 게이트·호스트·본문·재전송 없음."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.brokers.kis.circuit_breaker import KISCircuitOpen
from app.services.brokers.kis.futures_orders import (
    FuturesOrders,
    FuturesPreDispatchError,
)
from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.kis_futures_mock import constants as C  # noqa: N812


def run(coro):
    return asyncio.run(coro)


class Parent:
    _is_mock_client = True
    _hdr_base = {"appkey": "k"}

    def __init__(
        self, *, base_url=C.MOCK_HOST, token_error=None, send_error=None, is_mock=True
    ):
        self._is_mock_client = is_mock
        self._settings = SimpleNamespace(kis_base_url=base_url, kis_access_token="tok")
        self.token_error, self.send_error = token_error, send_error
        self.calls = []

    async def _ensure_token(self):
        if self.token_error:
            raise self.token_error

    def _kis_url(self, path):
        return self._settings.kis_base_url + path

    async def _request_with_rate_limit(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.send_error:
            raise self.send_error
        return {"rt_cd": "0", "output": {"ODNO": "1"}}


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    monkeypatch.setenv(C.ENABLED_ENV, "true")


def place(parent, **kw):
    args = {
        "account": "12345678",
        "symbol": "A05612",
        "side": "buy",
        "quantity": 1,
        "price": Decimal("400.00"),
    }
    args.update(kw)
    return run(FuturesOrders(parent).place(**args))


def test_place_body_headers_and_no_retry_settings():
    parent = Parent()
    place(parent)
    method, url, kw = parent.calls[0]
    assert method == "POST" and url == C.MOCK_HOST + C.ORDER_PATH
    assert kw["headers"]["tr_id"] == "VTTO1101U" == C.TR_PLACE
    assert kw["retry_request_errors"] is False and kw["max_retries_override"] == 0
    body = kw["json_body"]
    assert body["CANO"] == "12345678" and body["ACNT_PRDT_CD"] == "03"
    assert body["SHTN_PDNO"] == "A05612" and "PDNO" not in body  # 주식 PDNO 아님
    assert body["SLL_BUY_DVSN_CD"] == "02" and body["ORD_QTY"] == "1"
    assert body["UNIT_PRICE"] == "400.00"
    assert (body["NMPR_TYPE_CD"], body["ORD_DVSN_CD"], body["KRX_NMPR_CNDT_CD"]) == (
        "01",
        "01",
        "0",
    )


def test_sell_side_code_and_cancel_body():
    parent = Parent()
    place(parent, side="sell")
    assert parent.calls[0][2]["json_body"]["SLL_BUY_DVSN_CD"] == "01"
    run(
        FuturesOrders(parent).cancel(account="12345678", original_order_no="0000012345")
    )
    _, url, kw = parent.calls[1]
    assert url == C.MOCK_HOST + C.CANCEL_PATH and kw["headers"]["tr_id"] == "VTTO1103U"
    assert kw["json_body"]["ORGN_ODNO"] == "0000012345"
    assert (
        kw["json_body"]["RVSE_CNCL_DVSN_CD"] == "02"
        and kw["json_body"]["RMN_QTY_YN"] == "Y"
    )
    assert kw["retry_request_errors"] is False and kw["max_retries_override"] == 0


@pytest.mark.parametrize(
    ("parent", "stage"),
    [
        (Parent(base_url="https://openapi.koreainvestment.com:9443"), "not_mock_host"),
        (Parent(base_url=C.MOCK_HOST + "/"), None),  # 끝 슬래시는 정규화된다
        (Parent(is_mock=False), "not_mock_host"),
        (Parent(token_error=RuntimeError("x")), "token"),
        (Parent(send_error=KISCircuitOpen(1.0)), "KISCircuitOpen"),
        (Parent(send_error=PreSendFreshnessError(("x",))), "PreSendFreshnessError"),
    ],
)
def test_provably_unsent_failures_are_pre_dispatch_errors(parent, stage):
    if stage is None:
        place(parent)
        assert len(parent.calls) == 1
        return
    with pytest.raises(FuturesPreDispatchError) as info:
        place(parent)
    assert info.value.stage == stage
    if stage in {"not_mock_host", "token"}:
        assert parent.calls == []


def test_env_gate_must_be_exactly_true(monkeypatch):
    monkeypatch.setenv(C.ENABLED_ENV, "1")
    parent = Parent()
    with pytest.raises(FuturesPreDispatchError) as info:
        place(parent)
    assert info.value.stage == "env_gate_disabled" and parent.calls == []


@pytest.mark.parametrize(
    "account", ["", "1234567", "123456789", "1234567a", "１２３４５６７８"]
)
def test_account_must_be_eight_ascii_digits(account):
    parent = Parent()
    with pytest.raises(FuturesPreDispatchError):
        place(parent, account=account)
    assert parent.calls == []


@pytest.mark.parametrize(
    "kw",
    [{"side": "hold"}, {"quantity": 2}, {"quantity": 0}, {"price": Decimal("0")}],
)
def test_order_shape_is_refused_at_the_adapter_too(kw):
    parent = Parent()
    with pytest.raises(FuturesPreDispatchError):
        place(parent, **kw)
    assert parent.calls == []


def test_unknown_transport_failure_is_not_a_pre_dispatch_error():
    # HTTP 요청이 만들어졌을 수 있다 — 호출자가 uncertain 으로 다룬다.
    parent = Parent(send_error=TimeoutError())
    with pytest.raises(TimeoutError):
        place(parent)


def test_non_object_response_is_an_error_not_an_ack():
    class Bad(Parent):
        async def _request_with_rate_limit(self, *a, **k):
            return "ok"

    with pytest.raises(RuntimeError):
        place(Bad())


def test_only_the_mock_tr_ids_exist():
    assert {C.TR_PLACE, C.TR_CANCEL} == {"VTTO1101U", "VTTO1103U"}
    import inspect

    from app.services.brokers.kis import futures_orders

    source = inspect.getsource(futures_orders) + inspect.getsource(C)
    for live in ("TTTO1101U", "TTTO1103U", "STTN1101U", "TTTN1103U", "STTN"):
        assert live not in source.replace("VTTO", "")  # 실전·야간 TR 상수 없음


def test_daily_history_uses_mock_get_and_complete_pagination(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    monkeypatch.delenv(C.ENABLED_ENV)
    parent = Parent()
    first = {"ord_dt": "20261126", "odno": "1"}
    second = {"ord_dt": "20261126", "odno": "2"}
    pages = iter(
        [
            (
                {
                    "rt_cd": "0",
                    "output1": [first],
                    "output2": {"fee_smtl": "52"},
                    "ctx_area_fk200": "a",
                    "ctx_area_nk200": "b",
                },
                {"tr_cont": "M"},
            ),
            ({"rt_cd": "0", "output1": [second], "output2": {}}, {"tr_cont": "D"}),
        ]
    )

    async def fetch(method, url, **kwargs):
        parent.calls.append((method, url, kwargs))
        return next(pages)

    parent._request_with_rate_limit_with_headers = fetch
    result = run(
        FuturesOrders(parent).daily_history(account="12345678", trading_day="20261126")
    )
    assert result == [first, second]
    for method, url, request in parent.calls:
        assert method == "GET"
        assert (
            url == C.MOCK_HOST + "/uapi/domestic-futureoption/v1/trading/inquire-ccnl"
        )
        assert request["headers"]["tr_id"] == "VTTO5201R"
        params = request["params"]
        assert params["STRT_ORD_DT"] == params["END_ORD_DT"] == "20261126"
        assert params["ACNT_PRDT_CD"] == "03"
    assert parent.calls[1][2]["headers"]["tr_cont"] == "N"
    assert parent.calls[1][2]["params"]["CTX_AREA_NK200"] == "b"


def test_daily_history_refuses_incomplete_page(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    parent = Parent()

    async def fetch(*args, **kwargs):
        return {"rt_cd": "0", "output1": [{"odno": "1"}]}, {"tr_cont": "M"}

    parent._request_with_rate_limit_with_headers = fetch
    with pytest.raises(RuntimeError, match="연속조회"):
        run(
            FuturesOrders(parent).daily_history(
                account="12345678", trading_day="20261126"
            )
        )


def test_daily_history_refuses_live_client_before_token(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    parent = Parent(
        is_mock=False, token_error=AssertionError("token must not be called")
    )
    with pytest.raises(FuturesPreDispatchError, match="not_mock_host"):
        run(
            FuturesOrders(parent).daily_history(
                account="12345678", trading_day="20261126"
            )
        )
