"""#1272 (hk 1271 = B): non-USDT balances only under verified single-asset margin.

The Futures Demo account carries immovable default grants (USDC, BTC). The truth
gate check ``account_isolated_1x`` admits them only when the same signed
``GET /fapi/v2/account`` response proves ``multiAssetsMargin`` is exactly the
JSON boolean false. Multi-asset margin on, a missing/null/string/number mode, or
a read error fails closed. Without non-USDT balances the check is unchanged.

These tests drive the real ``H5DemoClient`` over an ``httpx.MockTransport`` and
the real ``run_truth_gate``; no network, credentials or database are touched.
"""

from __future__ import annotations

import ast
import asyncio
import copy
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.brokers.binance.h5 import client as h5_client
from app.services.brokers.binance.h5.client import (
    H5Account,
    H5BrokerTruthUnavailable,
    H5DemoClient,
)
from app.services.brokers.binance.h5.strategy import UNIVERSE
from app.services.brokers.binance.h5.truth_gate import run_truth_gate

pytestmark = pytest.mark.unit

CLIENT_SOURCE = Path(h5_client.__file__)

GRANTS = [
    {"asset": "USDC", "marginBalance": "5000.00000000"},
    {"asset": "BTC", "marginBalance": "0.01000000"},
]


def account_body(*, mode=False, grants=GRANTS, **over):
    body = {
        "canTrade": True,
        "multiAssetsMargin": mode,
        "totalMarginBalance": "1000.00000000",
        "assets": [
            {"asset": "USDT", "marginBalance": "1000.00000000"},
            *copy.deepcopy(grants),
        ],
        "positions": [
            {"symbol": s, "isolated": True, "leverage": "1", "positionSide": "BOTH"}
            for s in UNIVERSE
        ],
    }
    body.update(over)
    return body


def drop_mode(body):
    del body["multiAssetsMargin"]
    return body


READ_BODIES = {
    "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
    "/fapi/v2/positionRisk": [
        {
            "symbol": s,
            "positionAmt": "0",
            "entryPrice": "0",
            "leverage": "1",
            "positionSide": "BOTH",
        }
        for s in UNIVERSE
    ],
    "/fapi/v1/openOrders": [],
}


def make_client(account, *, account_status=200):
    requests: list[httpx.Request] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v2/account":
            return httpx.Response(account_status, json=account)
        return httpx.Response(200, json=READ_BODIES[request.url.path])

    client = H5DemoClient(api_key="FAKE", api_secret="FAKE")
    client._client = httpx.AsyncClient(
        base_url="https://demo-fapi.binance.com",
        transport=httpx.MockTransport(dispatch),
    )
    return client, requests


def read(body, **kwargs) -> H5Account:
    client, _ = make_client(body, **kwargs)
    return asyncio.run(client.read_account())


class State:
    async def list_active_signals(self):
        return ()

    async def list_unresolved_intents(self):
        return ()


class Ledger:
    async def count_open_lifecycles(self):
        return 0

    async def status_distribution(self):
        return {}


def account_check(body, **kwargs):
    client, requests = make_client(body, **kwargs)
    report = asyncio.run(run_truth_gate(client=client, state=State(), ledger=Ledger()))
    checks = {c.name: c for c in report.checks}
    return report, checks["account_isolated_1x"], requests


# --- A1: verified single-asset margin admits the grants, nothing else does -------


def test_grants_pass_with_verified_single_asset_margin_and_are_named():
    report, check, _ = account_check(account_body())
    assert report.verdict == "PASS"
    assert check.ok is True
    assert check.detail == (
        "nav_usdt=1000.00000000 symbols=all "
        "margin_mode=single_asset non_usdt_assets=BTC,USDC"
    )


def test_client_reports_the_grants_and_the_mode_it_read():
    account = read(account_body())
    assert account.non_usdt_assets == ("BTC", "USDC")
    assert account.multi_assets_margin is False
    assert account.nav_usdt == Decimal("1000")
    assert account.per_symbol_isolated_1x == dict.fromkeys(UNIVERSE, True)


MODE_NOT_PROVEN = [
    pytest.param(account_body(mode=True), id="multi_asset_on"),
    pytest.param(drop_mode(account_body()), id="missing"),
    pytest.param(account_body(mode=None), id="null"),
    pytest.param(account_body(mode="false"), id="string_false"),
    pytest.param(account_body(mode="False"), id="string_False"),
    pytest.param(account_body(mode=0), id="zero"),
    pytest.param(account_body(mode=0.0), id="zero_float"),
    pytest.param(account_body(mode=[]), id="empty_list"),
    pytest.param(account_body(mode={}), id="empty_object"),
]


@pytest.mark.parametrize("body", MODE_NOT_PROVEN)
def test_grants_fail_when_single_asset_margin_is_not_proven(body):
    report, check, _ = account_check(body)
    assert report.verdict == "FAIL"
    assert check.ok is False
    assert check.detail == (
        "read failed: H5BrokerTruthUnavailable: "
        "single-asset margin evidence unavailable"
    )
    with pytest.raises(H5BrokerTruthUnavailable, match="single-asset margin"):
        read(body)


@pytest.mark.parametrize("status", [400, 401, 418, 429, 500, 503])
def test_grants_fail_when_the_account_read_errors(status):
    report, check, _ = account_check(account_body(), account_status=status)
    assert report.verdict == "FAIL"
    assert check.ok is False
    assert check.detail == "read failed: HTTPStatusError"


def test_grants_fail_when_the_account_body_is_not_json():
    def dispatch(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client = H5DemoClient(api_key="FAKE", api_secret="FAKE")
    client._client = httpx.AsyncClient(
        base_url="https://demo-fapi.binance.com",
        transport=httpx.MockTransport(dispatch),
    )
    with pytest.raises(H5BrokerTruthUnavailable, match="JSON evidence"):
        asyncio.run(client.read_account())


@pytest.mark.parametrize(
    "grants",
    [
        pytest.param([], id="usdt_only"),
        pytest.param(
            [
                {"asset": "USDC", "marginBalance": "0.00000000"},
                {"asset": "BTC", "marginBalance": "0"},
                {"asset": "BNB", "marginBalance": "-0"},
            ],
            id="zero_foreign_rows",
        ),
    ],
)
def test_without_non_usdt_balance_the_check_is_unchanged(grants):
    report, check, _ = account_check(account_body(grants=grants))
    assert report.verdict == "PASS"
    assert (check.ok, check.detail) == (True, "nav_usdt=1000.00000000 symbols=all")
    assert read(account_body(grants=grants)).non_usdt_assets == ()


@pytest.mark.parametrize("mode", [True, None, "false"])
def test_without_non_usdt_balance_an_unproven_mode_still_fails_as_today(mode):
    _, check, _ = account_check(account_body(grants=[], mode=mode))
    assert check.ok is False
    assert check.detail.endswith("single-asset margin evidence unavailable")


@pytest.mark.parametrize(
    ("grant", "message"),
    [
        ({"asset": "BTC", "marginBalance": "-0.01"}, "foreign account asset exposure"),
        ({"asset": "BTC", "marginBalance": "NaN"}, "foreign account asset exposure"),
        ({"asset": "BTC", "marginBalance": "Infinity"}, "foreign account asset"),
        ({"asset": "BTC", "marginBalance": "abc"}, "non-USDT asset unreadable"),
        ({"asset": "BTC", "marginBalance": None}, "non-USDT asset unreadable"),
        ({"asset": "BTC", "marginBalance": True}, "non-USDT asset unreadable"),
        ({"asset": "BTC"}, "non-USDT asset unreadable"),
        ({"asset": None, "marginBalance": "1"}, "non-USDT asset unreadable"),
        ({"marginBalance": "1"}, "non-USDT asset unreadable"),
        ({"asset": "", "marginBalance": "1"}, "non-USDT asset unreadable"),
        ({"asset": "usdc", "marginBalance": "1"}, "non-USDT asset unreadable"),
        ({"asset": "BTC,ETH", "marginBalance": "1"}, "non-USDT asset unreadable"),
        ({"asset": 7, "marginBalance": "1"}, "non-USDT asset unreadable"),
    ],
)
def test_an_unreadable_or_negative_foreign_row_still_fails(grant, message):
    _, check, _ = account_check(account_body(grants=[grant]))
    assert check.ok is False
    assert message in check.detail


@pytest.mark.parametrize(
    "usdt_margin",
    ["6000.00000000", "999.99999999", None, "x", "NaN"],
)
def test_grants_fail_unless_nav_is_proven_to_be_usdt_only(usdt_margin):
    body = account_body()
    body["assets"][0]["marginBalance"] = usdt_margin
    _, check, _ = account_check(body)
    assert check.ok is False
    assert "USDT" in check.detail and "read failed" in check.detail


def test_duplicate_foreign_rows_are_named_once():
    grants = [*GRANTS, {"asset": "USDC", "marginBalance": "1"}]
    assert read(account_body(grants=grants)).non_usdt_assets == ("BTC", "USDC")


# --- the gate re-checks the mode itself (defense in depth) -----------------------


class StaticClient:
    def __init__(self, account) -> None:
        self.account = account

    async def read_account(self):
        return self.account

    async def get_position_mode(self):
        return type("Mode", (), {"is_hedge_mode": False})()

    async def get_all_positions(self):
        return []

    async def get_all_open_orders(self):
        return type("Orders", (), {"orders": []})()


@pytest.mark.parametrize("mode", [True, None, "false", 0, "single"])
def test_the_gate_refuses_named_assets_without_an_exact_false_mode(mode):
    account = H5Account(
        nav_usdt=Decimal("1000"),
        per_symbol_isolated_1x=dict.fromkeys(UNIVERSE, True),
        non_usdt_assets=("USDC", "BTC"),
        multi_assets_margin=mode,
    )
    report = asyncio.run(
        run_truth_gate(client=StaticClient(account), state=State(), ledger=Ledger())
    )
    check = report.checks[0]
    assert report.verdict == "FAIL"
    assert (check.name, check.ok) == ("account_isolated_1x", False)
    assert check.detail == "non-USDT assets without single-asset margin: BTC,USDC"


def test_isolation_failure_still_reports_the_symbols_first():
    account = H5Account(
        nav_usdt=Decimal("1000"),
        per_symbol_isolated_1x={"BTCUSDT": True, "ETHUSDT": True},
        non_usdt_assets=("USDC",),
        multi_assets_margin=False,
    )
    report = asyncio.run(
        run_truth_gate(client=StaticClient(account), state=State(), ledger=Ledger())
    )
    assert report.checks[0].detail == "not isolated 1x BOTH: SOLUSDT"


# --- A2: same read path, no write or signed mutation -----------------------------


def test_the_gate_run_issues_only_the_same_signed_gets():
    _, _, requests = account_check(account_body())
    assert {r.method for r in requests} == {"GET"}
    paths = [r.url.path for r in requests]
    assert paths.count("/fapi/v2/account") == 1
    assert set(paths) == {"/fapi/v2/account", *READ_BODIES}
    account = next(r for r in requests if r.url.path == "/fapi/v2/account")
    assert account.content == b""
    assert {"signature", "timestamp", "recvWindow"} <= set(account.url.params)


def _function(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(CLIENT_SOURCE.read_text("utf-8"))
    (cls,) = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "H5DemoClient"
    ]
    (fn,) = [
        n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    ]
    return fn


def test_read_account_reaches_the_broker_only_through_one_signed_get():
    fn = _function("read_account")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    on_self = [
        c
        for c in calls
        if isinstance(c.func, ast.Attribute)
        and isinstance(c.func.value, ast.Name)
        and c.func.value.id == "self"
    ]
    assert [c.func.attr for c in on_self] == ["_signed_get"]
    (signed,) = on_self
    assert [ast.literal_eval(a) for a in signed.args] == ["/fapi/v2/account"]
    assert signed.keywords == []
    attrs = {c.func.attr for c in calls if isinstance(c.func, ast.Attribute)}
    assert attrs <= {"_signed_get", "get", "is_finite", "fullmatch", "add"}, attrs
    names = {c.func.id for c in calls if isinstance(c.func, ast.Name)}
    assert names <= {
        "isinstance",
        "Decimal",
        "str",
        "_positive_decimal",
        "H5BrokerTruthUnavailable",
        "H5Account",
        "tuple",
        "sorted",
        "set",
    }, names


def test_the_signed_read_helper_only_gets():
    fn = _function("_signed_get")
    client_calls = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "_client"
    }
    assert client_calls == {"get"}


# --- A4: the guard change carries its deviation record ---------------------------

DEVIATION = (
    CLIENT_SOURCE.parents[5]
    / "docs/contracts/h5-deviation-20261008-single-asset-margin-foreign-balances.md"
)


def test_the_guard_change_has_a_deviation_record_bound_to_the_decision():
    text = DEVIATION.read_text("utf-8")
    assert "hk task 1271 option B" in text
    assert "contract_entry: h5_ls_env_v1_futures_demo_orders_20260928" in text
    assert "affected_policy_keys: []" in text
    for path in (
        "app/services/brokers/binance/h5/client.py",
        "app/services/brokers/binance/h5/truth_gate.py",
    ):
        assert path in text
