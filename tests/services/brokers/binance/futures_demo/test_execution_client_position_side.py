"""ROB-1288 — explicit ``positionSide`` on the Futures Demo adapter.

D2 contract v2 §4.3 makes two things hard requirements for the Futures
cleanup, and both were missing from this adapter:

  1. the position readbacks must **preserve** each row's ``positionSide``
     instead of dropping it, and
  2. ``submit_order`` must **accept** an explicit ``positionSide`` and prove
     the broker agreed, by comparing the response echo.

The rule that shapes every test below is the contract's own sentence: *"v2
does not infer the missing value from quantity sign."* So there is no
defaulting and no deriving anywhere on this path — an absent value is
reported absent, and a path that needs one fails closed. The readback is
where that matters most, because ``positionAmt`` sits right there carrying a
sign that looks like the answer.

Everything here is offline: ``httpx_mock`` serves every response and no
socket is opened. ``confirm=True`` appears only against those mocked
responses — the echo it verifies exists only on the confirmed submit path —
and never against a broker.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOrderSubmitResult,
    FuturesDemoPositionResult,
)
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoHedgeModeBlocked,
    BinanceFuturesDemoPositionSideMismatch,
    BinanceFuturesDemoPositionSideUnavailable,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
    FuturesDemoDryRunResult,
)

_FUTURES_DEMO_BASE = "https://demo-fapi.binance.com"
_ORDER_URL = re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$")
_ORDER_TEST_URL = re.compile(
    r"^https://demo-fapi\.binance\.com/fapi/v1/order/test\?.*$"
)
_POSITION_RISK_URL = re.compile(
    r"^https://demo-fapi\.binance\.com/fapi/v2/positionRisk\?.*$"
)
_OPEN_ORDERS_URL = re.compile(
    r"^https://demo-fapi\.binance\.com/fapi/v1/openOrders\?.*$"
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceFuturesDemoExecutionClient:
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "DUMMY_FUTURES_DEMO_KEY")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "DUMMY_FUTURES_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", _FUTURES_DEMO_BASE)
    return BinanceFuturesDemoExecutionClient.from_env()


def _submit_body(**overrides: object) -> dict[str, object]:
    """A FILLED ``/fapi/v1/order`` response, minus whatever a test removes."""
    body: dict[str, object] = {
        "symbol": "XRPUSDT",
        "orderId": 991288,
        "clientOrderId": "rob1288-cid",
        "transactTime": 1700000000000,
        "price": "0",
        "avgPrice": "0.50",
        "origQty": "10",
        "executedQty": "10",
        "cumQuote": "5.00",
        "status": "FILLED",
        "timeInForce": "GTC",
        "type": "MARKET",
        "side": "SELL",
        "reduceOnly": True,
        "positionSide": "BOTH",
    }
    for key, value in overrides.items():
        if value is _ABSENT:
            body.pop(key, None)
        else:
            body[key] = value
    return body


class _Absent:
    """Sentinel: remove the key entirely rather than set it to ``None``."""


_ABSENT = _Absent()


def _arm_catch_all(httpx_mock) -> None:
    """Arm a maximally *permissive* broker for the pre-HTTP tests.

    These tests claim a refusal happens before any HTTP, so the honest way to
    test them is to make every downstream defence stand aside and see whether
    a request still escapes. Two things follow from that:

    Without any registered response, a guard that stopped working would
    surface as an httpx "no response found" transport error — red, but red
    about the mock rather than about the invariant. And with a *fixed*
    response, it would trip the ``positionSide`` echo check instead, so the
    test would pass on the strength of a different guard than the one it
    names. So the catch-all mirrors back whatever ``positionSide`` the
    request carried: the echo check is satisfied by construction, the call
    would succeed, and the only thing left to fail is the assertion this test
    actually makes — that no request was dispatched at all.

    ``assert_all_responses_were_requested=False`` is what lets the healthy
    path leave the whole thing unused.
    """
    import httpx

    def _mirror(request: httpx.Request) -> httpx.Response:
        stated = request.url.params.get("positionSide")
        body = _submit_body(
            positionSide=stated if stated is not None else _ABSENT,
            side=request.url.params.get("side", "SELL"),
        )
        return httpx.Response(200, json=body)

    httpx_mock.add_callback(_mirror, method="POST", url=_ORDER_URL)
    httpx_mock.add_response(
        method="POST", url=_ORDER_TEST_URL, status_code=200, json={}
    )


# ===========================================================================
# AC1 — PRESERVE: the readbacks carry positionSide through
# ===========================================================================
@pytest.mark.asyncio
async def test_get_all_positions_preserves_position_side_per_row(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """The account-wide readback keeps each row's ``positionSide`` verbatim.

    This is the path ROB-1288 was filed against: the account-wide sweep is
    what a cleanup reads to decide how to close, so a side dropped here is a
    side the caller has to invent.
    """
    httpx_mock.add_response(
        method="GET",
        url=_POSITION_RISK_URL,
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "7.4",
                "entryPrice": "0.5000",
                "leverage": "1",
                "positionSide": "BOTH",
            },
            {
                "symbol": "DOGEUSDT",
                "positionAmt": "-5.0",
                "entryPrice": "0.1",
                "leverage": "1",
                "positionSide": "SHORT",
            },
        ],
    )
    results = await client.get_all_positions()

    assert [r.symbol for r in results] == ["XRPUSDT", "DOGEUSDT"]
    assert [r.position_side for r in results] == ["BOTH", "SHORT"]
    # The pre-existing fields are untouched (additive change, ROB-993 shape).
    assert results[0].position_amt == Decimal("7.4")
    assert results[1].position_amt == Decimal("-5.0")
    assert results[1].is_flat is False
    # And the echo is the broker's, not a rewrite of it: the SHORT row's
    # negative amount did not turn its side into something derived.
    assert results[1].require_position_side() == "SHORT"


@pytest.mark.asyncio
async def test_get_position_preserves_position_side(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """The single-symbol readback preserves it too — same DTO, same rule."""
    httpx_mock.add_response(
        method="GET",
        url=_POSITION_RISK_URL,
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "7.4",
                "entryPrice": "0.5000",
                "leverage": "1",
                "positionSide": "BOTH",
            }
        ],
    )
    result = await client.get_position(symbol="XRPUSDT")
    assert isinstance(result, FuturesDemoPositionResult)
    assert result.position_side == "BOTH"
    assert result.require_position_side() == "BOTH"


@pytest.mark.asyncio
async def test_get_order_preserves_position_side(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """The fill-evidence poll (ROB-305 §4) preserves it as well."""
    httpx_mock.add_response(
        method="GET",
        url=_ORDER_URL,
        status_code=200,
        json=_submit_body(positionSide="BOTH"),
    )
    result = await client.get_order(symbol="XRPUSDT", client_order_id="rob1288-cid")
    assert result.position_side == "BOTH"


@pytest.mark.asyncio
async def test_get_open_orders_preserves_position_side(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """A symbol-scoped open-order row carries Binance's exact positionSide."""
    httpx_mock.add_response(
        method="GET",
        url=_OPEN_ORDERS_URL,
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "orderId": 1289,
                "clientOrderId": "rob1289-open-cid",
                "side": "SELL",
                "origQty": "7.4",
                "status": "NEW",
                "reduceOnly": True,
                "positionSide": "BOTH",
            }
        ],
    )

    result = await client.get_open_orders(symbol="XRPUSDT")

    assert result.orders[0].position_side == "BOTH"
    assert result.orders[0].qty == Decimal("7.4")
    assert result.orders[0].side == "SELL"
    assert result.orders[0].reduce_only is True


@pytest.mark.asyncio
async def test_open_order_without_position_side_stays_none(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Missing positionSide is not inferred from qty, side, or reduceOnly."""
    httpx_mock.add_response(
        method="GET",
        url=_OPEN_ORDERS_URL,
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "orderId": 1290,
                "clientOrderId": "rob1290-open-cid",
                "side": "SELL",
                "origQty": "7.4",
                "status": "NEW",
                "reduceOnly": True,
            }
        ],
    )

    result = await client.get_open_orders(symbol="XRPUSDT")

    assert result.orders[0].position_side is None


@pytest.mark.asyncio
async def test_get_all_open_orders_preserves_position_side_without_symbol(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """The account-wide path preserves positionSide and omits symbol scope."""
    httpx_mock.add_response(
        method="GET",
        url=_OPEN_ORDERS_URL,
        status_code=200,
        json=[
            {
                "symbol": "DOGEUSDT",
                "orderId": 1291,
                "clientOrderId": "rob1291-open-cid",
                "side": "BUY",
                "origQty": "5",
                "status": "NEW",
                "reduceOnly": False,
                "positionSide": "SHORT",
            }
        ],
    )

    result = await client.get_all_open_orders()

    request = httpx_mock.get_requests()[0]
    assert "symbol=" not in str(request.url)
    assert result.orders[0].symbol == "DOGEUSDT"
    assert result.orders[0].position_side == "SHORT"


# ===========================================================================
# AC3 — NO_INFER: absence is reported as absence, never derived
# ===========================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("position_amt", "note"),
    [
        ("7.4", "a long-looking row"),
        ("-5.0", "a short-looking row"),
        ("0.0", "a flat row"),
    ],
)
async def test_readback_without_position_side_stays_none_whatever_the_sign(
    client: BinanceFuturesDemoExecutionClient,
    httpx_mock,
    position_amt: str,
    note: str,
) -> None:
    """🔴 A row with no ``positionSide`` yields ``None`` — for every sign.

    The parametrisation is the point. ``positionAmt`` is the one field that
    could plausibly stand in for the missing side, and contract v2 §4.3 names
    that inference specifically ("v2 does not infer the missing value from
    quantity sign"). Driving a positive, a negative, and a zero amount means
    a ``"LONG" if amt > 0 else "SHORT"`` fallback cannot hide in any branch:
    all three must come back ``None``.
    """
    httpx_mock.add_response(
        method="GET",
        url=_POSITION_RISK_URL,
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": position_amt,
                "entryPrice": "0.5000",
                "leverage": "1",
            }
        ],
    )
    results = await client.get_all_positions()

    assert results[0].position_side is None, note
    assert results[0].position_amt == Decimal(position_amt)


@pytest.mark.asyncio
async def test_require_position_side_fails_closed_when_absent(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """A caller that needs the side gets an exception, not a guess."""
    httpx_mock.add_response(
        method="GET",
        url=_POSITION_RISK_URL,
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "7.4",
                "entryPrice": "0.5000",
                "leverage": "1",
            }
        ],
    )
    result = await client.get_position(symbol="XRPUSDT")

    with pytest.raises(BinanceFuturesDemoPositionSideUnavailable) as excinfo:
        result.require_position_side()
    # The message has to say why there is no fallback, or the next reader
    # "fixes" it by adding one.
    assert "Refusing to infer" in str(excinfo.value)


@pytest.mark.asyncio
async def test_blank_position_side_is_treated_as_absent_not_as_a_value(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``"  "`` is not a side. Whitespace must not become a usable value."""
    httpx_mock.add_response(
        method="GET",
        url=_POSITION_RISK_URL,
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "7.4",
                "entryPrice": "0.5000",
                "leverage": "1",
                "positionSide": "   ",
            }
        ],
    )
    result = await client.get_position(symbol="XRPUSDT")
    assert result.position_side is None
    with pytest.raises(BinanceFuturesDemoPositionSideUnavailable):
        result.require_position_side()


def test_the_adapter_source_contains_no_sign_based_position_side_derivation() -> None:
    """No quantity-sign fallback exists anywhere on the module's surface.

    The behavioural tests above cover the paths they drive; this one covers
    the module. It reads the source and asserts that ``position_side`` is
    only ever assigned from ``_extract_position_side`` or from the caller's
    own argument — never from a conditional on an amount. A future
    ``position_side = "LONG" if amt > 0 else "SHORT"`` fails here even if it
    is added on a path no test drives yet.
    """
    import ast
    import inspect as _inspect

    from app.services.brokers.binance.futures_demo import execution_client

    source = _inspect.getsource(execution_client)
    tree = ast.parse(source)

    allowed_calls = {"_extract_position_side", "_verify_position_side_echo"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "position_side":
            continue
        value = node.value
        if isinstance(value, ast.Name):
            # Passing the caller's own argument through is the whole point.
            assert value.id in {"position_side", "echoed_position_side"}
            continue
        if isinstance(value, ast.Call):
            func = value.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            assert name in allowed_calls, f"position_side= built by {name!r}"
            continue
        if isinstance(value, ast.Constant):
            continue
        raise AssertionError(
            f"position_side= assigned from a {type(value).__name__} node — "
            "only a pass-through or _extract_position_side is allowed"
        )

    # Non-vacuity: the walk above must actually have found the assignments.
    assert source.count("position_side=") >= 5


# ===========================================================================
# AC2 — SUBMIT_ARG: the submit surface accepts it, sends it, verifies it
# ===========================================================================
@pytest.mark.asyncio
async def test_submit_order_sends_explicit_position_side_and_records_the_echo(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """A stated ``position_side`` reaches the signed payload verbatim."""
    httpx_mock.add_response(
        method="POST", url=_ORDER_URL, status_code=200, json=_submit_body()
    )
    result = await client.submit_order(
        symbol="XRPUSDT",
        side="SELL",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="rob1288-cid",
        reduce_only=True,
        position_side="BOTH",
        confirm=True,
    )
    assert isinstance(result, FuturesDemoOrderSubmitResult)
    assert result.position_side == "BOTH"

    last = httpx_mock.get_request()
    assert last is not None
    url_str = str(last.url)
    assert "positionSide=BOTH" in url_str
    # The close-order pairing v2 asks for: reduceOnly AND an exact side.
    assert "reduceOnly=true" in url_str
    assert "signature=" in url_str


@pytest.mark.asyncio
async def test_submit_order_without_position_side_omits_the_param(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Unstated stays unstated — the param is absent, not defaulted.

    This is both the additive-compatibility check for every caller that
    predates ROB-1288 and a NO_INFER check: an omitted argument must not
    acquire a value on the way to the broker.
    """
    httpx_mock.add_response(
        method="POST",
        url=_ORDER_URL,
        status_code=200,
        json=_submit_body(positionSide=_ABSENT, side="BUY", reduceOnly=False),
    )
    result = await client.submit_order(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="rob1288-cid",
        confirm=True,
    )
    assert isinstance(result, FuturesDemoOrderSubmitResult)
    assert result.position_side is None

    last = httpx_mock.get_request()
    assert last is not None
    assert "positionSide" not in str(last.url)


@pytest.mark.asyncio
async def test_order_test_sends_explicit_position_side(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """The pre-submit ``/order/test`` probe carries the same side.

    If the probe and the submit disagreed on ``positionSide``, the probe
    would be validating a different order than the one that gets placed.
    """
    httpx_mock.add_response(
        method="POST", url=_ORDER_TEST_URL, status_code=200, json={}
    )
    await client.order_test(
        symbol="XRPUSDT",
        side="SELL",
        order_type="MARKET",
        qty=Decimal("10"),
        reduce_only=True,
        position_side="BOTH",
    )
    last = httpx_mock.get_request()
    assert last is not None
    assert "positionSide=BOTH" in str(last.url)


@pytest.mark.asyncio
async def test_preview_submit_carries_position_side_with_zero_http(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """The dry-run surfaces the stated side and still dispatches no HTTP."""
    result = client.preview_submit(
        symbol="XRPUSDT",
        side="SELL",
        order_type="MARKET",
        qty=Decimal("10"),
        reduce_only=True,
        position_side="BOTH",
    )
    assert isinstance(result, FuturesDemoDryRunResult)
    assert result.position_side == "BOTH"
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_unconfirmed_submit_with_position_side_is_still_a_dry_run(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Stating a side does not arm the order — ``confirm`` still gates."""
    result = await client.submit_order(
        symbol="XRPUSDT",
        side="SELL",
        order_type="MARKET",
        qty=Decimal("10"),
        reduce_only=True,
        position_side="BOTH",
    )
    assert isinstance(result, FuturesDemoDryRunResult)
    assert result.position_side == "BOTH"
    assert httpx_mock.get_requests() == []


# ===========================================================================
# AC4 — NEGATIVE: wrong / missing / mode-inappropriate all fail closed
# ===========================================================================
@pytest.mark.asyncio
async def test_submit_order_fails_closed_when_the_echo_disagrees(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """NEGATIVE 1 — a different echoed side is a mismatch, not a success.

    The order may well have been placed by the time this raises; that is the
    point. A broker that placed something other than what was asked for is an
    anomaly the caller must see, not a result to hand back as if it matched.
    """
    httpx_mock.add_response(
        method="POST",
        url=_ORDER_URL,
        status_code=200,
        json=_submit_body(positionSide="LONG"),
    )
    with pytest.raises(BinanceFuturesDemoPositionSideMismatch) as excinfo:
        await client.submit_order(
            symbol="XRPUSDT",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("10"),
            client_order_id="rob1288-cid",
            reduce_only=True,
            position_side="BOTH",
            confirm=True,
        )
    message = str(excinfo.value)
    assert "'BOTH'" in message
    assert "'LONG'" in message


@pytest.mark.asyncio
async def test_submit_order_fails_closed_when_the_echo_is_missing(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """NEGATIVE 2 — 🔴 an absent echo is not agreement.

    Silence is the failure mode that would slip through most easily: the
    response is a normal 200 FILLED and everything else about it matches.
    With no ``positionSide`` in it there is no evidence the broker honoured
    the requested side, and the only way to produce one would be to infer it
    — which contract v2 §4.3 forbids. So absence raises.
    """
    httpx_mock.add_response(
        method="POST",
        url=_ORDER_URL,
        status_code=200,
        json=_submit_body(positionSide=_ABSENT),
    )
    with pytest.raises(BinanceFuturesDemoPositionSideMismatch) as excinfo:
        await client.submit_order(
            symbol="XRPUSDT",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("10"),
            client_order_id="rob1288-cid",
            reduce_only=True,
            position_side="BOTH",
            confirm=True,
        )
    message = str(excinfo.value)
    assert "no positionSide" in message
    assert "refusing to infer the side from quantity/side" in message


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("hedge_value", ["LONG", "SHORT"])
async def test_submit_order_refuses_hedge_position_side_before_any_http(
    client: BinanceFuturesDemoExecutionClient, httpx_mock, hedge_value: str
) -> None:
    """NEGATIVE 3 — a value that is wrong *for this account's mode*.

    ``LONG``/``SHORT`` are real Binance values, so they would not be caught
    by a vocabulary check alone. They are hedge values, and this adapter is
    One-way only (ROB-298 PR 2), so they are refused at the boundary — with
    ``confirm=True`` already passed, and with zero HTTP dispatched.
    """
    _arm_catch_all(httpx_mock)
    with pytest.raises(BinanceFuturesDemoHedgeModeBlocked) as excinfo:
        await client.submit_order(
            symbol="XRPUSDT",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("10"),
            reduce_only=True,
            position_side=hedge_value,
            confirm=True,
        )
    assert "One-way mode only" in str(excinfo.value)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("bad_value", ["both", "Both", "BOTH ", "", "LONGSHORT", "1"])
async def test_submit_order_rejects_unknown_position_side_without_normalising(
    client: BinanceFuturesDemoExecutionClient, httpx_mock, bad_value: str
) -> None:
    """A malformed side is a caller bug, and case is not quietly repaired.

    ``"both"`` is included deliberately: upper-casing it would be a kindness
    that defeats the echo check, because the value compared against the
    broker's reply would no longer be the value the caller wrote.
    """
    _arm_catch_all(httpx_mock)
    with pytest.raises(ValueError):
        await client.submit_order(
            symbol="XRPUSDT",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("10"),
            reduce_only=True,
            position_side=bad_value,
            confirm=True,
        )
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("hedge_value", ["LONG", "SHORT"])
async def test_order_test_and_preview_refuse_hedge_position_side_too(
    client: BinanceFuturesDemoExecutionClient, httpx_mock, hedge_value: str
) -> None:
    """The other two order-shaped surfaces enforce the same rule.

    Otherwise the probe would happily validate a hedge order that the submit
    then refuses, which is a worse failure than refusing both.
    """
    _arm_catch_all(httpx_mock)
    with pytest.raises(BinanceFuturesDemoHedgeModeBlocked):
        await client.order_test(
            symbol="XRPUSDT",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("10"),
            position_side=hedge_value,
        )
    with pytest.raises(BinanceFuturesDemoHedgeModeBlocked):
        client.preview_submit(
            symbol="XRPUSDT",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("10"),
            position_side=hedge_value,
        )
    assert httpx_mock.get_requests() == []


# ===========================================================================
# AC6 — the neighbouring safety boundaries are untouched
# ===========================================================================
@pytest.mark.asyncio
async def test_position_side_does_not_weaken_reduce_only_or_the_confirm_gate(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """The new parameter changes neither default and arms nothing."""
    import inspect

    parameters = inspect.signature(
        BinanceFuturesDemoExecutionClient.submit_order
    ).parameters
    assert parameters["position_side"].default is None
    assert parameters["reduce_only"].default is False
    assert parameters["confirm"].default is False

    # An open order stating BOTH still carries no reduceOnly.
    httpx_mock.add_response(
        method="POST",
        url=_ORDER_URL,
        status_code=200,
        json=_submit_body(side="BUY", reduceOnly=False),
    )
    await client.submit_order(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="rob1288-cid",
        position_side="BOTH",
        confirm=True,
    )
    url_str = str(httpx_mock.get_request().url)
    assert "reduceOnly" not in url_str
    assert "positionSide=BOTH" in url_str
    assert url_str.startswith("https://demo-fapi.binance.com/fapi/v1/order?")
