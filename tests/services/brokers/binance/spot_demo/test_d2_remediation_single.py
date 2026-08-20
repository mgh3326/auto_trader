"""``d2_remediation_single`` — the closed three-order D2 writer.

The interesting question about this writer is not "does it place an order"
but "what can it be talked into placing". These tests answer that by mutation:
each of the nine cases below takes the real sealed r7 attempt-2 payload, makes
one change a mistaken or hostile caller might make, and proves the writer
refuses. Every mutant is paired with the unmutated original so a refusal that
came from something unrelated would show up as the control failing too.

Checks are closed equality against the frozen constant, never a name match:
``0.00016`` is refused because it is not ``0.00015``, not because a string
did or did not contain a substring.
"""

from __future__ import annotations

import asyncio
import copy
import re
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import pytest

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_ALLOWED_OPERATION_IDS,
    D2_BOUND_ORDERS,
    D2_PRE_SNAPSHOT_HASH,
    D2_REMEDIATION_ENABLED_ENV,
    D2_REMEDIATION_ID,
    D2_VENUE_HOST,
    WRITER_NAME,
    D2BlindRetryRefused,
    D2LeaseNotHeld,
    D2ReasonCode,
    D2RemediationDisabled,
    D2RemediationSingleWriter,
    D2SealBindingMismatch,
    D2UnauthorizedOperation,
    bind_sealed_orders,
    d2_advisory_keyset,
)
from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoAssetBalance,
    SpotDemoOpenOrder,
    SpotDemoOpenOrdersResult,
    SpotDemoOrderSubmitResult,
    SpotDemoOrderTestResult,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.mock_integration.coordination import AdvisoryLeaseGrant

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# The sealed payload, transcribed from r7 attempt-2
# --------------------------------------------------------------------------


def _actionable(symbol: str, asset: str, quantity: str, price: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "asset": asset,
        "product": "spot",
        "disposition": "REVIEWED_SCOPE_LIMIT_CANDIDATE",
        "mutation_authorized": False,
        "proposed_one_step": {
            "side": "SELL",
            "order_type": "LIMIT",
            "time_in_force": None,
            "proposed_quantity_floor": quantity,
            "proposed_limit_price_floor": price,
            "rounding_direction": "floor_only_no_uplift",
        },
    }


def _dust(symbol: str, asset: str) -> dict[str, Any]:
    """A floor-zero attestation row. It authorizes no order and must never
    be picked up as one."""

    return {
        "symbol": symbol,
        "asset": asset,
        "product": "spot",
        "disposition": "LOT_SIZE_FLOOR_ZERO_V1_2",
        "mutation_authorized": False,
        "planned_quantity": "0",
    }


def sealed_payload() -> dict[str, Any]:
    """The exact bound object: three actionable rows, three dust rows, one
    quote-cash row."""

    return {
        "remediation_id": D2_REMEDIATION_ID,
        "product_domain": "both",
        "pre_snapshot_hash": D2_PRE_SNAPSHOT_HASH,
        "operator_authorization": None,
        "authorized_symbols": {
            "spot": {
                "BTCUSDT": _actionable(
                    "BTCUSDT", "BTC", "0.00015000", "69266.01000000"
                ),
                "ETHUSDT": _actionable("ETHUSDT", "ETH", "0.00520000", "2248.56000000"),
                "USDCUSDT": _actionable(
                    "USDCUSDT", "USDC", "5000.00000000", "1.00072000"
                ),
                "SOLUSDT": _dust("SOLUSDT", "SOL"),
                "XRPUSDT": _dust("XRPUSDT", "XRP"),
                "DOGEUSDT": _dust("DOGEUSDT", "DOGE"),
                "USDT": {
                    "asset": "USDT",
                    "disposition": "QUOTE_CASH_ATTESTATION_ONLY_KEEP",
                },
            }
        },
    }


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeExecutionClient:
    """Counts everything that crosses the transport boundary.

    ``submit_calls`` is the number that matters: a duplicate order is the
    worst available failure of this writer, so the tests assert on the count,
    not on whether a retry loop is visible in the source.
    """

    def __init__(
        self,
        *,
        base_url: str = f"https://{D2_VENUE_HOST}",
        submit_error: Exception | None = None,
        status_body: dict[str, Any] | None = None,
        status_error: Exception | None = None,
        submit_status: str = "NEW",
    ) -> None:
        self._base_url = base_url
        self.submit_calls: list[dict[str, Any]] = []
        self.order_test_calls: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.open_order_reads = 0
        self.balance_reads = 0
        self._submit_error = submit_error
        self._status_body = status_body
        self._status_error = status_error
        self._submit_status = submit_status

    async def order_test(self, **kwargs: Any) -> SpotDemoOrderTestResult:
        self.order_test_calls.append(kwargs)
        return SpotDemoOrderTestResult(
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            qty=kwargs["qty"],
        )

    async def submit_order(self, **kwargs: Any) -> SpotDemoOrderSubmitResult:
        self.submit_calls.append(kwargs)
        if self._submit_error is not None:
            raise self._submit_error
        return SpotDemoOrderSubmitResult(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=f"bid-{len(self.submit_calls)}",
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            qty=kwargs["qty"],
            executed_qty=Decimal("0"),
            cummulative_quote_qty=Decimal("0"),
            status=self._submit_status,
        )

    async def get_order_status(self, **kwargs: Any) -> dict[str, Any]:
        self.status_calls.append(kwargs)
        if self._status_error is not None:
            raise self._status_error
        if self._status_body is not None:
            return dict(self._status_body)
        # Default: the venue faithfully echoes the order that was authorized.
        order = next(o for o in D2_BOUND_ORDERS if o.symbol == kwargs["symbol"])
        return {
            "symbol": order.symbol,
            "side": order.side,
            "type": order.order_type,
            "origQty": format(order.quantity, "f"),
            "executedQty": "0",
            "status": "NEW",
            "orderId": f"readback-{len(self.status_calls)}",
            "clientOrderId": kwargs["client_order_id"],
        }

    async def get_all_open_orders(self) -> SpotDemoOpenOrdersResult:
        self.open_order_reads += 1
        return SpotDemoOpenOrdersResult(
            orders=[
                SpotDemoOpenOrder(
                    client_order_id="cid",
                    broker_order_id="bid",
                    symbol="BTCUSDT",
                    side="SELL",
                    qty=Decimal("0.00015"),
                    status="NEW",
                )
            ]
        )

    async def get_asset_balance(self, *, asset: str) -> SpotDemoAssetBalance:
        self.balance_reads += 1
        return SpotDemoAssetBalance(asset=asset, free=Decimal("0"), locked=Decimal("0"))


class FakeLease:
    """A stand-in for ``PostgresAdvisoryKeysetLease`` with the same use-time
    contract: ownership is re-proved on demand and never cached."""

    def __init__(
        self,
        grant: AdvisoryLeaseGrant,
        *,
        released: bool = False,
        attest_error: Exception | None = None,
    ) -> None:
        self._grant = grant
        self.released = released
        self.attest_calls = 0
        self._attest_error = attest_error

    async def assert_owned(self, expected_grant: AdvisoryLeaseGrant) -> None:
        self.attest_calls += 1
        if self._attest_error is not None:
            raise self._attest_error
        if expected_grant is not self._grant:
            raise RuntimeError("lease_lost")


#: A grant carries the loop its session is bound to. Sync tests have no running
#: loop, and ``asyncio.get_event_loop()`` outside one is order-dependent — it
#: succeeds or raises depending on whether an earlier async test happened to
#: leave a loop set on the thread. One inert loop keeps these tests independent
#: of collection order.
_SENTINEL_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="session", autouse=True)
def _close_sentinel_loop() -> Any:
    yield
    _SENTINEL_LOOP.close()


def make_grant(keys: tuple[int, ...] | None = None) -> AdvisoryLeaseGrant:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = _SENTINEL_LOOP
    return AdvisoryLeaseGrant(
        keys=keys if keys is not None else d2_advisory_keyset(),
        backend_pid=4242,
        database_oid=99,
        connection_token="lockconn:test",
        event_loop=loop,
    )


@pytest.fixture(autouse=True)
def _arm_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env gate is default-off; every test that expects the writer to
    construct has to arm it explicitly, and one test below removes it again."""

    monkeypatch.setenv(D2_REMEDIATION_ENABLED_ENV, "true")


def build_writer(
    *,
    payload: dict[str, Any] | None = None,
    client: FakeExecutionClient | None = None,
    lease: FakeLease | None = None,
    grant: AdvisoryLeaseGrant | None = None,
    ledger: Any = None,
) -> D2RemediationSingleWriter:
    the_grant = grant if grant is not None else make_grant()
    return D2RemediationSingleWriter(
        execution_client=client or FakeExecutionClient(),  # type: ignore[arg-type]
        sealed_payload=payload if payload is not None else sealed_payload(),
        lease=lease if lease is not None else FakeLease(the_grant),  # type: ignore[arg-type]
        lease_grant=the_grant,
        ledger=ledger,
    )


# --------------------------------------------------------------------------
# Control — the unmutated seal binds, and binds to the frozen constant
# --------------------------------------------------------------------------


def test_unmutated_seal_binds_to_the_frozen_constant() -> None:
    bound = bind_sealed_orders(sealed_payload())
    assert bound is D2_BOUND_ORDERS
    assert [order.symbol for order in bound] == ["BTCUSDT", "ETHUSDT", "USDCUSDT"]


def test_dust_and_quote_cash_rows_never_become_orders() -> None:
    bound = bind_sealed_orders(sealed_payload())
    symbols = {order.symbol for order in bound}
    assert symbols.isdisjoint({"SOLUSDT", "XRPUSDT", "DOGEUSDT", "USDT"})


def test_bound_set_is_exactly_three_sell_limits() -> None:
    assert len(D2_BOUND_ORDERS) == 3
    assert {order.side for order in D2_BOUND_ORDERS} == {"SELL"}
    assert {order.order_type for order in D2_BOUND_ORDERS} == {"LIMIT"}
    assert len(D2_ALLOWED_OPERATION_IDS) == 3


def test_request_params_equal_the_sealed_values_exactly() -> None:
    writer = build_writer()
    params = [op.request_params for op in writer.plan()]
    assert params == [
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "0.00015000",
            "price": "69266.01000000",
            "timeInForce": "GTC",
        },
        {
            "symbol": "ETHUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "0.00520000",
            "price": "2248.56000000",
            "timeInForce": "GTC",
        },
        {
            "symbol": "USDCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "5000.00000000",
            "price": "1.00072000",
            "timeInForce": "GTC",
        },
    ]


# --------------------------------------------------------------------------
# M1 — a different symbol
# --------------------------------------------------------------------------


def test_m1_foreign_symbol_is_refused() -> None:
    control = sealed_payload()
    assert bind_sealed_orders(control) is D2_BOUND_ORDERS  # control passes

    mutant = copy.deepcopy(control)
    mutant["authorized_symbols"]["spot"]["SOLUSDT"] = _actionable(
        "SOLUSDT", "SOL", "1.00000000", "100.00000000"
    )
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


# --------------------------------------------------------------------------
# M2 — side flipped to BUY
# --------------------------------------------------------------------------


def test_m2_buy_side_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]["side"] = "BUY"
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


# --------------------------------------------------------------------------
# M3 — one digit of quantity
# --------------------------------------------------------------------------


def test_m3_quantity_one_digit_changed_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    step = mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]
    assert step["proposed_quantity_floor"] == "0.00015000"
    step["proposed_quantity_floor"] = "0.00016000"
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m3_trailing_zero_difference_is_not_a_mutation() -> None:
    """The check is decimal value equality, not string equality: a differently
    spelled but identical quantity must still bind, or the guard would be
    rejecting for the wrong reason."""

    same_value = copy.deepcopy(sealed_payload())
    step = same_value["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]
    step["proposed_quantity_floor"] = "0.00015"
    assert bind_sealed_orders(same_value) is D2_BOUND_ORDERS


# --------------------------------------------------------------------------
# M4 — one tick of price
# --------------------------------------------------------------------------


def test_m4_price_one_tick_changed_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    step = mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]
    assert step["proposed_limit_price_floor"] == "69266.01000000"
    # PRICE_FILTER tickSize for BTCUSDT is 0.01.
    step["proposed_limit_price_floor"] = "69266.02000000"
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


# --------------------------------------------------------------------------
# M5 — a different seal
# --------------------------------------------------------------------------


def test_m5_foreign_pre_snapshot_hash_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    # The superseded attempt-1 hash: a real, adjacent, wrong seal.
    mutant["pre_snapshot_hash"] = (
        "sha256:df50c7c0acfaf059e9f97ee27826520cf7bfa9b71d9f0c2316c16d8dcadecdfe"
    )
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_HASH_MISMATCH


def test_m5_hash_mismatch_is_caught_before_the_writer_constructs() -> None:
    client = FakeExecutionClient()
    mutant = copy.deepcopy(sealed_payload())
    mutant["pre_snapshot_hash"] = "sha256:" + "0" * 64
    with pytest.raises(D2SealBindingMismatch):
        build_writer(payload=mutant, client=client)
    assert client.submit_calls == []
    assert client.order_test_calls == []


# --------------------------------------------------------------------------
# M6 — no lease
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m6_released_lease_blocks_the_order_path() -> None:
    client = FakeExecutionClient()
    grant = make_grant()
    writer = build_writer(
        client=client, lease=FakeLease(grant, released=True), grant=grant
    )
    with pytest.raises(D2LeaseNotHeld) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.LEASE_NOT_HELD
    assert client.submit_calls == []
    assert client.order_test_calls == []


@pytest.mark.asyncio
async def test_m6_lease_over_the_wrong_keyset_blocks_the_order_path() -> None:
    client = FakeExecutionClient()
    wrong = make_grant(keys=(1234567890,))
    writer = build_writer(client=client, lease=FakeLease(wrong), grant=wrong)
    with pytest.raises(D2LeaseNotHeld) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.LEASE_SCOPE_MISMATCH
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_m6_unattestable_lease_blocks_the_order_path() -> None:
    client = FakeExecutionClient()
    grant = make_grant()
    lease = FakeLease(grant, attest_error=RuntimeError("lock_authority_unavailable"))
    writer = build_writer(client=client, lease=lease, grant=grant)
    with pytest.raises(D2LeaseNotHeld):
        await writer.execute(confirm=True)
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_lease_is_reattested_immediately_before_each_submit() -> None:
    """Acquisition-time success is not carried forward: there is one
    attestation for the run and one more per order, plus one per proof epoch."""

    client = FakeExecutionClient()
    grant = make_grant()
    lease = FakeLease(grant)
    writer = build_writer(client=client, lease=lease, grant=grant)
    await writer.execute(confirm=True)
    assert len(client.submit_calls) == 3
    # 1 run entry + 3 pre-submit + 2 proof epochs
    assert lease.attest_calls == 6


# --------------------------------------------------------------------------
# M7 — MARKET
# --------------------------------------------------------------------------


def test_m7_market_order_type_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    step = mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]
    step["order_type"] = "MARKET"
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m7_no_bound_order_is_a_market_order() -> None:
    assert all(order.order_type == "LIMIT" for order in D2_BOUND_ORDERS)


# --------------------------------------------------------------------------
# M8 — a fourth order
# --------------------------------------------------------------------------


def test_m8_fourth_order_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    mutant["authorized_symbols"]["spot"]["XRPUSDT"] = _actionable(
        "XRPUSDT", "XRP", "10.00000000", "0.50000000"
    )
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH
    assert "XRPUSDT" in str(exc.value)


def test_m8_a_missing_third_order_is_also_refused() -> None:
    """The set is closed in both directions: silently shrinking it would let a
    truncated payload look like a clean partial run."""

    mutant = copy.deepcopy(sealed_payload())
    del mutant["authorized_symbols"]["spot"]["USDCUSDT"]
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


# --------------------------------------------------------------------------
# M9 — re-sending after a failure
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m9_ambiguous_submit_is_resolved_by_readback_not_resend() -> None:
    """Every submit fails locally after the POST, so every outcome is
    ambiguous. Each one is settled by asking the venue exactly once; no symbol
    is ever POSTed twice."""

    client = FakeExecutionClient(
        submit_error=TimeoutError("connection reset after POST")
    )
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    report = await writer.execute(confirm=True)

    assert [call["symbol"] for call in client.submit_calls] == [
        "BTCUSDT",
        "ETHUSDT",
        "USDCUSDT",
    ]  # each exactly once
    assert len(client.status_calls) == 3
    assert all(outcome.readback_used for outcome in report.outcomes)
    assert report.broker_submit_count == 3
    assert report.halted_reason is None


@pytest.mark.asyncio
async def test_m9_unresolvable_outcome_halts_the_run_as_an_anomaly() -> None:
    client = FakeExecutionClient(
        submit_error=TimeoutError("connection reset after POST"),
        status_error=BinanceDemoOrderNotFound("not found"),
    )
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    report = await writer.execute(confirm=True)

    assert len(client.submit_calls) == 1
    assert report.halted_reason is not None
    assert report.outcomes[0].status == "anomaly"
    assert len(report.outcomes) == 1  # ETH and USDC were never dispatched
    assert report.broker_submit_count == 1


@pytest.mark.asyncio
async def test_m9_second_dispatch_of_one_client_order_id_is_refused() -> None:
    """The no-retry rule is a claim set, not the absence of a loop: even a
    direct second dispatch of the same id fails before the transport."""

    client = FakeExecutionClient()
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    op = writer.plan()[0]
    await writer._dispatch_one(op, include_order_test=False)
    assert len(client.submit_calls) == 1
    with pytest.raises(D2BlindRetryRefused) as exc:
        await writer._dispatch_one(op, include_order_test=False)
    assert exc.value.reason_code is D2ReasonCode.BLIND_RETRY_REFUSED
    assert len(client.submit_calls) == 1  # no second POST


# --------------------------------------------------------------------------
# Dry run is the default and reaches no mutation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_submits_nothing() -> None:
    client = FakeExecutionClient()
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    report = await writer.execute()  # no confirm= at all
    assert client.submit_calls == []
    assert report.broker_mutation_count == 0
    assert report.lease_attested is True
    assert len(report.operations) == 3
    assert len(client.order_test_calls) == 3


@pytest.mark.asyncio
async def test_dry_run_can_skip_even_the_non_mutating_order_test() -> None:
    client = FakeExecutionClient()
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    await writer.execute(confirm=False, include_order_test=False)
    assert client.order_test_calls == []
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_dry_run_evidence_prints_the_sealed_values() -> None:
    client = FakeExecutionClient()
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    evidence = (await writer.execute(confirm=False)).as_evidence()
    assert evidence["writer"] == WRITER_NAME
    assert evidence["pre_snapshot_hash"] == D2_PRE_SNAPSHOT_HASH
    assert evidence["broker_mutation_count"] == 0
    assert [op["request_params"] for op in evidence["operations"]] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]


# --------------------------------------------------------------------------
# Inherited boundaries are still boundaries
# --------------------------------------------------------------------------


def test_env_gate_is_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(D2_REMEDIATION_ENABLED_ENV, raising=False)
    with pytest.raises(D2RemediationDisabled) as exc:
        build_writer()
    assert exc.value.reason_code is D2ReasonCode.DISABLED


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.binance.com",
        "https://testnet.binance.vision",
        "https://demo-api.binance.com.evil.example",
    ],
)
def test_non_spot_demo_host_is_refused(base_url: str) -> None:
    with pytest.raises(D2UnauthorizedOperation) as exc:
        build_writer(client=FakeExecutionClient(base_url=base_url))
    assert exc.value.reason_code is D2ReasonCode.HOST_NOT_SPOT_DEMO


@pytest.mark.asyncio
async def test_broker_echo_mismatch_is_not_treated_as_success() -> None:
    class DriftingClient(FakeExecutionClient):
        async def submit_order(self, **kwargs: Any) -> SpotDemoOrderSubmitResult:
            self.submit_calls.append(kwargs)
            return SpotDemoOrderSubmitResult(
                client_order_id=kwargs["client_order_id"],
                broker_order_id="bid",
                symbol=kwargs["symbol"],
                side=kwargs["side"],
                order_type=kwargs["order_type"],
                qty=Decimal("0.00099"),  # not what was authorized
                executed_qty=Decimal("0"),
                cummulative_quote_qty=Decimal("0"),
                status="NEW",
            )

    client = DriftingClient()
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    with pytest.raises(Exception) as exc:
        await writer.execute(confirm=True)
    assert getattr(exc.value, "reason_code", None) is D2ReasonCode.BROKER_ECHO_MISMATCH


@pytest.mark.asyncio
async def test_two_independent_proof_epochs_are_produced() -> None:
    client = FakeExecutionClient()
    grant = make_grant()
    writer = build_writer(client=client, lease=FakeLease(grant), grant=grant)
    report = await writer.execute(confirm=True)
    assert [epoch.epoch_index for epoch in report.proof_epochs] == [1, 2]
    # Each epoch issued its own reads; neither reused the other's bytes.
    assert client.open_order_reads == 2
    assert client.balance_reads == 8  # (3 assets + USDT) x 2 epochs


def test_writer_name_matches_the_operator_contract() -> None:
    assert WRITER_NAME == "d2_remediation_single"
    assert D2RemediationSingleWriter.writer_name == "d2_remediation_single"


# --------------------------------------------------------------------------
# Wire-level proof: what would actually be sent
# --------------------------------------------------------------------------
#
# The tests above use a fake client, so they prove the writer's decisions but
# not the bytes. These use the real ``BinanceSpotDemoExecutionClient`` over a
# mocked transport, so the assertions are on the query string that Binance
# would receive — signing, param building, and host pinning all included.


_BASE = f"https://{D2_VENUE_HOST}"
_ORDER_RE = re.compile(r"^https://demo-api\.binance\.com/api/v3/order\?.*$")
_ORDER_TEST_RE = re.compile(r"^https://demo-api\.binance\.com/api/v3/order/test\?.*$")
_OPEN_ORDERS_RE = re.compile(r"^https://demo-api\.binance\.com/api/v3/openOrders\?.*$")
_ACCOUNT_RE = re.compile(r"^https://demo-api\.binance\.com/api/v3/account\?.*$")


@pytest.fixture
def real_client(monkeypatch: pytest.MonkeyPatch) -> BinanceSpotDemoExecutionClient:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SECRET")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _BASE)
    return BinanceSpotDemoExecutionClient.from_env()


def _sent_order_fields(request: Any) -> dict[str, str]:
    params = dict(parse_qsl(str(request.url).split("?", 1)[1]))
    return {
        key: params[key]
        for key in ("symbol", "side", "type", "quantity", "price", "timeInForce")
        if key in params
    }


@pytest.mark.asyncio
async def test_dry_run_sends_only_order_test_with_the_sealed_fields(
    real_client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    for _ in D2_BOUND_ORDERS:
        httpx_mock.add_response(method="POST", url=_ORDER_TEST_RE, json={})
    grant = make_grant()
    writer = D2RemediationSingleWriter(
        execution_client=real_client,
        sealed_payload=sealed_payload(),
        lease=FakeLease(grant),  # type: ignore[arg-type]
        lease_grant=grant,
    )

    await writer.execute(confirm=False)

    requests = httpx_mock.get_requests()
    assert [str(r.url).split("?", 1)[0] for r in requests] == [
        f"{_BASE}/api/v3/order/test"
    ] * 3
    assert [_sent_order_fields(r) for r in requests] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]


@pytest.mark.asyncio
async def test_confirm_posts_exactly_the_sealed_fields(
    real_client: BinanceSpotDemoExecutionClient, httpx_mock: Any
) -> None:
    for _ in D2_BOUND_ORDERS:
        httpx_mock.add_response(method="POST", url=_ORDER_TEST_RE, json={})
    for index, order in enumerate(D2_BOUND_ORDERS):
        httpx_mock.add_response(
            method="POST",
            url=_ORDER_RE,
            json={
                "symbol": order.symbol,
                "orderId": 1000 + index,
                "clientOrderId": "echoed",
                "side": order.side,
                "type": order.order_type,
                "origQty": format(order.quantity, "f"),
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
                "status": "NEW",
            },
        )
    # Proof-epoch reads: 2 account-wide open-order reads and 4 balance reads
    # per epoch.
    for _ in range(2):
        httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])
    for _ in range(8):
        httpx_mock.add_response(method="GET", url=_ACCOUNT_RE, json={"balances": []})

    grant = make_grant()
    writer = D2RemediationSingleWriter(
        execution_client=real_client,
        sealed_payload=sealed_payload(),
        lease=FakeLease(grant),  # type: ignore[arg-type]
        lease_grant=grant,
    )
    await writer.execute(confirm=True)

    posts = [
        r
        for r in httpx_mock.get_requests()
        if str(r.url).split("?", 1)[0] == f"{_BASE}/api/v3/order"
    ]
    assert [_sent_order_fields(r) for r in posts] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]
    # Every request went to the Spot Demo host and nowhere else.
    assert {r.url.host for r in httpx_mock.get_requests()} == {D2_VENUE_HOST}
