"""``d2_remediation_single`` — the closed three-order D2 writer.

The interesting question about this writer is not "does it place an order" but
"what can it be talked into placing". These tests answer that by mutation: each
case takes a valid sealed payload, makes one change a mistaken or hostile
caller might make, and proves the writer refuses.

Round 2 exists because an adversarial verifier got five of these past the first
version by injecting mutants directly. Those mutants are reproduced here by
name, so a regression that reopens any of them fails CI rather than waiting for
another review:

    B1  unregistered / altered payload bytes, null operator_authorization,
        absent expiry, wrong credential fingerprint, mutation_authorized=false
    B2  replay under a fresh process (new client_order_id)
    B3  a ledger-less dispatch path
    B4  wrong credential, unfrozen writer lane, no pre-dispatch account truth
    B5  a broker echo whose price or timeInForce is not the sealed one
    B6  a fabricated lease object, and an injected ``environ`` that arms the
        gate the operator left off

Checks are closed equality against the frozen constant, never a name match:
``0.00016`` is refused because it is not ``0.00015``, not because a string did
or did not contain a substring.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import pytest

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.spot_demo import d2_remediation_single as d2
from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_ALLOWED_OPERATION_IDS,
    D2_BOUND_ORDERS,
    D2_CREDENTIAL_FINGERPRINT,
    D2_PRE_SNAPSHOT_HASH,
    D2_REMEDIATION_ENABLED_ENV,
    D2_REMEDIATION_ID,
    D2_VENUE_HOST,
    WRITER_NAME,
    D2AccountTruthDrift,
    D2BlindRetryRefused,
    D2DispatchNotAuthorized,
    D2LeaseNotHeld,
    D2LedgerRequired,
    D2PriorAttemptUnresolved,
    D2ReasonCode,
    D2RemediationDisabled,
    D2RemediationError,
    D2RemediationSingleWriter,
    D2SealBindingMismatch,
    D2UnauthorizedOperation,
    SealedPayloadRecord,
    bind_sealed_orders,
    d2_advisory_keyset,
    load_sealed_authority,
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
from app.services.mock_integration.coordination import (
    AdvisoryLeaseGrant,
    PostgresAdvisoryKeysetLease,
    split_advisory_key,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# The sealed payload, transcribed from r7 attempt-2
# --------------------------------------------------------------------------


def _actionable(
    symbol: str,
    asset: str,
    quantity: str,
    price: str,
    free: str,
    *,
    mutation_authorized: bool = False,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "asset": asset,
        "product": "spot",
        "disposition": "REVIEWED_SCOPE_LIMIT_CANDIDATE",
        "mutation_authorized": mutation_authorized,
        "proposed_one_step": {
            "side": "SELL",
            "order_type": "LIMIT",
            "time_in_force": None,
            "proposed_quantity_floor": quantity,
            "proposed_limit_price_floor": price,
            "raw_free_quantity": free,
            "raw_locked_quantity": "0E-8",
            "rounding_direction": "floor_only_no_uplift",
        },
    }


def _dust(symbol: str, asset: str) -> dict[str, Any]:
    """A floor-zero attestation row. It authorizes no order and must never be
    picked up as one."""

    return {
        "symbol": symbol,
        "asset": asset,
        "product": "spot",
        "disposition": "LOT_SIZE_FLOOR_ZERO_V1_2",
        "mutation_authorized": False,
        "planned_quantity": "0",
    }


def sealed_payload(*, authorized: bool = False) -> dict[str, Any]:
    """The bound object: three actionable rows, three dust rows, one quote row.

    ``authorized=False`` mirrors the real r7 attempt-2 file — it binds, but
    authorizes nothing. ``authorized=True`` is the shape a re-signed payload
    would have, and exists so the tests can show the dispatch gate is
    discriminating rather than a blanket refusal.
    """

    payload: dict[str, Any] = {
        "remediation_id": D2_REMEDIATION_ID,
        "product_domain": "both",
        "pre_snapshot_hash": D2_PRE_SNAPSHOT_HASH,
        "operator_authorization": None,
        "expiry": None,
        "physical_account_identity": {
            "credential_fingerprint": D2_CREDENTIAL_FINGERPRINT,
            "spot_host": D2_VENUE_HOST,
        },
        "authorized_symbols": {
            "spot": {
                "BTCUSDT": _actionable(
                    "BTCUSDT",
                    "BTC",
                    "0.00015000",
                    "69266.01000000",
                    "0.00015957",
                    mutation_authorized=authorized,
                ),
                "ETHUSDT": _actionable(
                    "ETHUSDT",
                    "ETH",
                    "0.00520000",
                    "2248.56000000",
                    "0.00529470",
                    mutation_authorized=authorized,
                ),
                "USDCUSDT": _actionable(
                    "USDCUSDT",
                    "USDC",
                    "5000.00000000",
                    "1.00072000",
                    "5000.00000000",
                    mutation_authorized=authorized,
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
    if authorized:
        payload["operator_authorization"] = {
            "section": "§125차",
            "signature": "operator-resign-fixture",
        }
        payload["expiry"] = "2099-01-01T00:00:00Z"
    return payload


def write_sealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    register: bool = True,
    dispatch_authorized: bool = False,
    name: str = "binding-payload-proposed.json",
) -> Path:
    """Write a payload and (optionally) register its exact-byte digest.

    Registration is monkeypatched module state, not a production seam: nothing
    in ``app/`` or ``scripts/`` can add an entry at runtime, and the shipped map
    has exactly one entry with ``dispatch_authorized=False``.
    """

    path = tmp_path / name
    raw = json.dumps(payload).encode("utf-8")
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    registry = dict(d2.D2_KNOWN_SEALED_PAYLOADS)
    if register:
        registry[digest] = SealedPayloadRecord(
            sha256=digest,
            pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
            dispatch_authorized=dispatch_authorized,
            note="test fixture",
        )
    monkeypatch.setattr(d2, "D2_KNOWN_SEALED_PAYLOADS", registry)
    return path


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeExecutionClient:
    """Counts everything that crosses the transport boundary.

    ``submit_calls`` is the number that matters: a duplicate order is the worst
    available failure of this writer, so the tests assert on the count, not on
    whether a retry loop is visible in the source.
    """

    def __init__(
        self,
        *,
        base_url: str = f"https://{D2_VENUE_HOST}",
        credential_fingerprint: str = D2_CREDENTIAL_FINGERPRINT,
        submit_error: Exception | None = None,
        status_body: dict[str, Any] | None = None,
        status_error: Exception | None = None,
        submit_status: str = "NEW",
        echo_overrides: dict[str, Any] | None = None,
        open_orders: list[SpotDemoOpenOrder] | None = None,
        balances: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._base_url = base_url
        self.credential_fingerprint = credential_fingerprint
        self.submit_calls: list[dict[str, Any]] = []
        self.order_test_calls: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.open_order_reads = 0
        self.balance_reads = 0
        self._submit_error = submit_error
        self._status_body = status_body
        self._status_error = status_error
        self._submit_status = submit_status
        self._echo_overrides = echo_overrides or {}
        self._open_orders = open_orders if open_orders is not None else []
        # Default: the account is exactly what the seal observed.
        self._balances = balances or {
            order.asset: (
                format(order.sealed_free_quantity, "f"),
                format(order.sealed_locked_quantity, "f"),
            )
            for order in D2_BOUND_ORDERS
        }

    def _echo(self, order: Any, cid: str, index: int) -> dict[str, Any]:
        body = {
            "symbol": order.symbol,
            "side": order.side,
            "type": order.order_type,
            "origQty": format(order.quantity, "f"),
            "executedQty": "0",
            "price": format(order.price, "f"),
            "timeInForce": order.time_in_force,
            "status": self._submit_status,
            "orderId": f"bid-{index}",
            "clientOrderId": cid,
        }
        body.update(self._echo_overrides)
        return body

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
        order = next(o for o in D2_BOUND_ORDERS if o.symbol == kwargs["symbol"])
        body = self._echo(order, kwargs["client_order_id"], len(self.submit_calls))
        return SpotDemoOrderSubmitResult(
            client_order_id=str(body["clientOrderId"]),
            broker_order_id=str(body["orderId"]),
            symbol=str(body["symbol"]),
            side=str(body["side"]),
            order_type=str(body["type"]),
            qty=Decimal(str(body["origQty"])),
            executed_qty=Decimal(str(body["executedQty"])),
            cummulative_quote_qty=Decimal("0"),
            status=str(body["status"]),
            raw_response_redacted=body,
        )

    async def get_order_status(self, **kwargs: Any) -> dict[str, Any]:
        self.status_calls.append(kwargs)
        if self._status_error is not None:
            raise self._status_error
        if self._status_body is not None:
            return dict(self._status_body)
        order = next(o for o in D2_BOUND_ORDERS if o.symbol == kwargs["symbol"])
        return self._echo(order, kwargs["client_order_id"], len(self.status_calls))

    async def get_all_open_orders(self) -> SpotDemoOpenOrdersResult:
        self.open_order_reads += 1
        return SpotDemoOpenOrdersResult(orders=list(self._open_orders))

    async def get_asset_balance(self, *, asset: str) -> SpotDemoAssetBalance:
        self.balance_reads += 1
        free, locked = self._balances.get(asset, ("0", "0"))
        return SpotDemoAssetBalance(
            asset=asset, free=Decimal(free), locked=Decimal(locked)
        )


class FakeLedger(BinanceDemoLedgerService):
    """A real ``BinanceDemoLedgerService`` subclass with in-memory rows.

    A subclass, not a look-alike: the writer's ``isinstance`` guard exists to
    reject an *unrelated* object being passed as a ledger, not to reject a test
    double of the right type.
    """

    def __init__(self, *, existing: dict[str, str] | None = None) -> None:
        self.states: dict[str, str] = dict(existing or {})
        self.calls: list[tuple[str, str]] = []

    async def get_by_client_order_id(self, client_order_id: str) -> Any:
        state = self.states.get(client_order_id)
        if state is None:
            return None
        return type("Row", (), {"lifecycle_state": state})()

    async def resolve_or_create_instrument(self, **kwargs: Any) -> int:
        return 1

    async def record_planned(self, **kwargs: Any) -> Any:
        cid = kwargs["client_order_id"]
        self.states[cid] = "planned"
        self.calls.append(("planned", cid))
        return None

    async def record_previewed(self, **kwargs: Any) -> Any:
        cid = kwargs["client_order_id"]
        self.states[cid] = "previewed"
        self.calls.append(("previewed", cid))
        return None

    async def record_validated(self, **kwargs: Any) -> Any:
        cid = kwargs["client_order_id"]
        self.states[cid] = "validated"
        self.calls.append(("validated", cid))
        return None

    async def record_submitted(self, **kwargs: Any) -> Any:
        cid = kwargs["client_order_id"]
        self.states[cid] = "submitted"
        self.calls.append(("submitted", cid))
        return None

    async def record_anomaly(self, **kwargs: Any) -> Any:
        cid = kwargs["client_order_id"]
        self.states[cid] = "anomaly"
        self.calls.append(("anomaly", cid))
        return None


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def one(self) -> dict[str, Any]:
        return self._rows[0]

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeLockAuthority:
    """A ``LockAuthorityConnection`` that answers the real attestation SQL.

    Built so the tests can construct a **real** ``PostgresAdvisoryKeysetLease``
    rather than a duck-typed stand-in — which the writer now rejects — and so
    the genuine ``pg_locks`` ownership logic runs instead of being stubbed out.
    """

    def __init__(
        self, *, keys: tuple[int, ...], backend_pid: int = 4242, database_oid: int = 99
    ) -> None:
        self.keys = keys
        self.backend_pid = backend_pid
        self.database_oid = database_oid
        self.unlocked: set[int] = set()

    async def execute(self, statement: Any, parameters: Any = None, /) -> _Rows:
        sql = str(statement)
        if "pg_backend_pid" in sql:
            return _Rows(
                [
                    {
                        "backend_pid": self.backend_pid,
                        "database_oid": self.database_oid,
                    }
                ]
            )
        if "pg_locks" in sql:
            rows = []
            for key in self.keys:
                if key in self.unlocked:
                    continue
                classid, objid = split_advisory_key(key)
                rows.append(
                    {
                        "locktype": "advisory",
                        "mode": "ExclusiveLock",
                        "granted": True,
                        "database_oid": self.database_oid,
                        "pid": self.backend_pid,
                        "objsubid": 1,
                        "classid": classid,
                        "objid": objid,
                    }
                )
            return _Rows(rows)
        if "pg_advisory_unlock" in sql:
            self.unlocked.add(int((parameters or {})["key"]))
            return _Rows([{"released": True}])
        raise AssertionError(f"unexpected SQL: {sql}")

    async def commit(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def can_prove_backend_session_termination(self) -> bool:
        return True

    async def terminate_backend_session(
        self, *, expected_pid: int, owner_token: str
    ) -> Any:  # pragma: no cover - not reached by these tests
        raise AssertionError("termination must not be needed in these tests")


def make_lease(
    keys: tuple[int, ...] | None = None,
) -> tuple[PostgresAdvisoryKeysetLease, AdvisoryLeaseGrant]:
    """A real lease over a fake authority, bound to the running loop."""

    resolved = keys if keys is not None else d2_advisory_keyset()
    authority = FakeLockAuthority(keys=resolved)
    grant = AdvisoryLeaseGrant(
        keys=resolved,
        backend_pid=authority.backend_pid,
        database_oid=authority.database_oid,
        connection_token="lockconn:test",
        event_loop=asyncio.get_event_loop(),
    )
    lease = PostgresAdvisoryKeysetLease(connection=authority, grant=grant)  # type: ignore[arg-type]
    return lease, grant


@pytest.fixture(autouse=True)
def _arm_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env gate is default-off; every test that expects the writer to
    construct has to arm it, and one test below removes it again."""

    monkeypatch.setenv(D2_REMEDIATION_ENABLED_ENV, "true")


def build_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any] | None = None,
    client: FakeExecutionClient | None = None,
    ledger: Any = None,
    authorized: bool = False,
    lease: Any = None,
    grant: Any = None,
) -> D2RemediationSingleWriter:
    path = write_sealed(
        tmp_path,
        monkeypatch,
        payload if payload is not None else sealed_payload(authorized=authorized),
        dispatch_authorized=authorized,
    )
    authority = load_sealed_authority(path)
    if lease is None:
        lease, grant = make_lease()
    return D2RemediationSingleWriter(
        execution_client=client or FakeExecutionClient(),  # type: ignore[arg-type]
        authority=authority,
        lease=lease,
        lease_grant=grant,
        ledger=ledger if ledger is not None else FakeLedger(),
    )


# --------------------------------------------------------------------------
# Control — the unmutated seal binds, and binds to the frozen constant
# --------------------------------------------------------------------------


def test_unmutated_seal_binds_to_the_frozen_constant() -> None:
    bound = bind_sealed_orders(sealed_payload())
    assert bound is D2_BOUND_ORDERS
    assert [order.symbol for order in bound] == ["BTCUSDT", "ETHUSDT", "USDCUSDT"]


def test_dust_and_quote_cash_rows_never_become_orders() -> None:
    symbols = {order.symbol for order in bind_sealed_orders(sealed_payload())}
    assert symbols.isdisjoint({"SOLUSDT", "XRPUSDT", "DOGEUSDT", "USDT"})


def test_bound_set_is_exactly_three_sell_limits() -> None:
    assert len(D2_BOUND_ORDERS) == 3
    assert {order.side for order in D2_BOUND_ORDERS} == {"SELL"}
    assert {order.order_type for order in D2_BOUND_ORDERS} == {"LIMIT"}
    assert len(D2_ALLOWED_OPERATION_IDS) == 3


def test_request_params_equal_the_sealed_values_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = build_writer(tmp_path, monkeypatch)
    assert [op.request_params for op in writer.plan()] == [
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
# Round-1 mutants M1-M8 — payload shape
# --------------------------------------------------------------------------


def test_m1_foreign_symbol_is_refused() -> None:
    control = sealed_payload()
    assert bind_sealed_orders(control) is D2_BOUND_ORDERS  # control passes

    mutant = copy.deepcopy(control)
    mutant["authorized_symbols"]["spot"]["SOLUSDT"] = _actionable(
        "SOLUSDT", "SOL", "1.00000000", "100.00000000", "1.00000000"
    )
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m2_buy_side_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]["side"] = "BUY"
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


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


def test_m4_price_one_tick_changed_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    step = mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]
    assert step["proposed_limit_price_floor"] == "69266.01000000"
    step["proposed_limit_price_floor"] = "69266.02000000"  # tickSize is 0.01
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m5_foreign_pre_snapshot_hash_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    # The superseded attempt-1 hash: a real, adjacent, wrong seal.
    mutant["pre_snapshot_hash"] = (
        "sha256:df50c7c0acfaf059e9f97ee27826520cf7bfa9b71d9f0c2316c16d8dcadecdfe"
    )
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_HASH_MISMATCH


def test_m7_market_order_type_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"][
        "order_type"
    ] = "MARKET"
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m7_no_bound_order_is_a_market_order() -> None:
    assert all(order.order_type == "LIMIT" for order in D2_BOUND_ORDERS)


def test_m8_fourth_order_is_refused() -> None:
    mutant = copy.deepcopy(sealed_payload())
    mutant["authorized_symbols"]["spot"]["XRPUSDT"] = _actionable(
        "XRPUSDT", "XRP", "10.00000000", "0.50000000", "10.00000000"
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


def test_sealed_observed_balance_is_part_of_the_bound_identity() -> None:
    """Rewriting only the observed free balance keeps every order field intact,
    and must still be refused — the pre-dispatch drift check compares against
    it, so a payload that lies about it is a different object."""

    mutant = copy.deepcopy(sealed_payload())
    mutant["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"][
        "raw_free_quantity"
    ] = "9.99999999"
    with pytest.raises(D2SealBindingMismatch) as exc:
        bind_sealed_orders(mutant)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


# --------------------------------------------------------------------------
# B1 — sealed bytes, operator authorization, expiry, mutation_authorized
# --------------------------------------------------------------------------


def test_b1_unregistered_payload_digest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_sealed(tmp_path, monkeypatch, sealed_payload(), register=False)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_UNKNOWN_DIGEST


def test_b1_altering_the_file_changes_its_digest_and_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier's ``MUTANT_BINDING_PAYLOAD_SHA256_MISMATCH``.

    Round 1 carried the expected digest as metadata and never compared it to
    anything, so any file whose *contents* parsed was accepted. Now the digest
    is computed over the bytes before the JSON is parsed, so editing the file at
    all — even in a field the parser ignores — changes the digest and fails.
    """

    path = write_sealed(tmp_path, monkeypatch, sealed_payload())
    assert load_sealed_authority(path) is not None  # control

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["a_field_the_parser_never_reads"] = "tampered"
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_UNKNOWN_DIGEST


@pytest.mark.asyncio
async def test_b1_null_operator_authorization_blocks_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient()
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=False)
    reasons = writer.dispatch_block_reasons()
    assert any("operator_authorization is null" in r for r in reasons)
    with pytest.raises(D2DispatchNotAuthorized) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.DISPATCH_NOT_AUTHORIZED
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_b1_absent_and_past_expiry_both_block_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = sealed_payload(authorized=True)
    absent["expiry"] = None
    writer = build_writer(
        tmp_path,
        monkeypatch,
        payload=absent,
        authorized=True,
        client=FakeExecutionClient(),
    )
    assert any("expiry is absent" in r for r in writer.dispatch_block_reasons())

    expired = sealed_payload(authorized=True)
    expired["expiry"] = "2020-01-01T00:00:00Z"
    writer2 = build_writer(
        tmp_path,
        monkeypatch,
        payload=expired,
        authorized=True,
        client=FakeExecutionClient(),
    )
    assert any("expired at" in r for r in writer2.dispatch_block_reasons())
    with pytest.raises(D2DispatchNotAuthorized):
        await writer2.execute(confirm=True)


@pytest.mark.asyncio
async def test_b1_mutation_authorized_false_blocks_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = sealed_payload(authorized=True)
    payload["authorized_symbols"]["spot"]["ETHUSDT"]["mutation_authorized"] = False
    client = FakeExecutionClient()
    writer = build_writer(
        tmp_path, monkeypatch, payload=payload, authorized=True, client=client
    )
    reasons = writer.dispatch_block_reasons()
    assert any("mutation_authorized is not true for ['ETHUSDT']" in r for r in reasons)
    with pytest.raises(D2DispatchNotAuthorized):
        await writer.execute(confirm=True)
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_b1_registered_dispatch_authorized_false_blocks_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a fully signed payload cannot dispatch until a reviewed change
    registers its digest as dispatch-authorized."""

    path = write_sealed(
        tmp_path,
        monkeypatch,
        sealed_payload(authorized=True),
        dispatch_authorized=False,
    )
    authority = load_sealed_authority(path)
    lease, grant = make_lease()
    writer = D2RemediationSingleWriter(
        execution_client=FakeExecutionClient(),  # type: ignore[arg-type]
        authority=authority,
        lease=lease,
        lease_grant=grant,
        ledger=FakeLedger(),
    )
    reasons = writer.dispatch_block_reasons()
    assert any("dispatch_authorized=false" in r for r in reasons)
    with pytest.raises(D2DispatchNotAuthorized):
        await writer.execute(confirm=True)


def test_b1_every_shipped_sealed_payload_is_dispatch_blocked() -> None:
    """The structural claim: this repository grants no dispatch authority.

    Not "we checked once" — the shipped map is asserted empty of authorized
    entries here and again by an import-time tripwire in the module.
    """

    assert d2.D2_KNOWN_SEALED_PAYLOADS
    assert not any(
        record.dispatch_authorized for record in d2.D2_KNOWN_SEALED_PAYLOADS.values()
    )


@pytest.mark.asyncio
async def test_b1_a_fully_authorized_seal_does_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate discriminates; it is not a blanket refusal.

    Without this the five B1 refusals above would be satisfied by a writer that
    simply never runs, which would prove nothing about the gate.
    """

    client = FakeExecutionClient()
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    assert writer.dispatch_block_reasons() == ()
    report = await writer.execute(confirm=True)
    assert len(client.submit_calls) == 3
    assert report.halted_reason is None


# --------------------------------------------------------------------------
# B2 — durable replay fence
# --------------------------------------------------------------------------


def test_b2_client_order_ids_are_deterministic_across_processes() -> None:
    """The verifier's ``REPLAY_WITH_NEW_CLIENT_ORDER_ID``.

    Round 1 minted a UUID per ``plan()`` call, so a restarted process believed
    it was making a first attempt and would re-send. The id is now derived from
    the seal and the order, so it is the same everywhere.
    """

    first = [order.client_order_id for order in D2_BOUND_ORDERS]
    second = [order.client_order_id for order in D2_BOUND_ORDERS]
    assert first == second
    assert not any(re.fullmatch(r"[0-9a-f]{32}", cid) for cid in first)
    assert all(cid.startswith("d2rem-") and len(cid) <= 36 for cid in first)
    assert len(set(first)) == 3


def test_b2_ids_change_when_the_order_changes() -> None:
    """Derivation, not a constant: a different price is a different id."""

    order = D2_BOUND_ORDERS[0]
    other = replace(order, price=order.price + Decimal("0.01"))
    assert other.client_order_id != order.client_order_id


@pytest.mark.asyncio
async def test_b2_a_prior_ledger_row_prevents_a_second_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the restart the process-local claim set cannot see: the ledger
    already knows this exact bound order was attempted."""

    prior_cid = D2_BOUND_ORDERS[0].client_order_id
    client = FakeExecutionClient()
    ledger = FakeLedger(existing={prior_cid: "submitted"})
    writer = build_writer(
        tmp_path, monkeypatch, client=client, ledger=ledger, authorized=True
    )
    report = await writer.execute(confirm=True)

    submitted_symbols = [call["symbol"] for call in client.submit_calls]
    assert "BTCUSDT" not in submitted_symbols  # never re-sent
    assert submitted_symbols == ["ETHUSDT", "USDCUSDT"]
    assert report.outcomes[0].readback_used is True
    assert report.outcomes[0].ledger_state == "submitted"


@pytest.mark.asyncio
async def test_b2_prior_row_with_no_broker_record_halts_instead_of_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The genuinely ambiguous case: a ledger row exists, the broker denies it.

    "Never arrived" and "arrived then vanished" are indistinguishable from
    here, and only one of them is safe to act on — so neither is acted on.
    """

    prior_cid = D2_BOUND_ORDERS[0].client_order_id
    client = FakeExecutionClient(status_error=BinanceDemoOrderNotFound("nope"))
    ledger = FakeLedger(existing={prior_cid: "validated"})
    writer = build_writer(
        tmp_path, monkeypatch, client=client, ledger=ledger, authorized=True
    )
    with pytest.raises(D2PriorAttemptUnresolved) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.PRIOR_ATTEMPT_UNRESOLVED
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_b2_in_process_second_dispatch_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inner fence still holds even with the ledger check bypassed."""

    client = FakeExecutionClient()
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    op = writer.plan()[0]
    writer._claims.claim(op.client_order_id)
    with pytest.raises(D2BlindRetryRefused) as exc:
        writer._claims.claim(op.client_order_id)
    assert exc.value.reason_code is D2ReasonCode.BLIND_RETRY_REFUSED
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_b2_ambiguous_submit_is_resolved_by_readback_not_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient(
        submit_error=TimeoutError("connection reset after POST")
    )
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    report = await writer.execute(confirm=True)

    assert [call["symbol"] for call in client.submit_calls] == [
        "BTCUSDT",
        "ETHUSDT",
        "USDCUSDT",
    ]  # each exactly once
    assert len(client.status_calls) == 3
    assert all(outcome.readback_used for outcome in report.outcomes)
    assert report.broker_submit_count == 3


@pytest.mark.asyncio
async def test_b2_unresolvable_outcome_halts_the_run_as_an_anomaly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient(
        submit_error=TimeoutError("connection reset after POST"),
        status_error=BinanceDemoOrderNotFound("not found"),
    )
    ledger = FakeLedger()
    writer = build_writer(
        tmp_path, monkeypatch, client=client, ledger=ledger, authorized=True
    )
    report = await writer.execute(confirm=True)

    assert len(client.submit_calls) == 1
    assert report.halted_reason is not None
    assert report.outcomes[0].status == "anomaly"
    assert len(report.outcomes) == 1  # ETH and USDC were never dispatched
    assert ("anomaly", D2_BOUND_ORDERS[0].client_order_id) in ledger.calls


# --------------------------------------------------------------------------
# B3 — the ledger is mandatory
# --------------------------------------------------------------------------


def test_b3_ledger_has_no_default_and_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signature = inspect.signature(D2RemediationSingleWriter.__init__)
    assert signature.parameters["ledger"].default is inspect.Parameter.empty

    path = write_sealed(tmp_path, monkeypatch, sealed_payload())
    authority = load_sealed_authority(path)
    lease, grant = make_lease()
    with pytest.raises(TypeError):
        D2RemediationSingleWriter(  # type: ignore[call-arg]
            execution_client=FakeExecutionClient(),  # type: ignore[arg-type]
            authority=authority,
            lease=lease,
            lease_grant=grant,
        )


def test_b3_a_non_ledger_object_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_sealed(tmp_path, monkeypatch, sealed_payload())
    authority = load_sealed_authority(path)
    lease, grant = make_lease()
    with pytest.raises(D2LedgerRequired) as exc:
        D2RemediationSingleWriter(
            execution_client=FakeExecutionClient(),  # type: ignore[arg-type]
            authority=authority,
            lease=lease,
            lease_grant=grant,
            ledger=object(),  # type: ignore[arg-type]
        )
    assert exc.value.reason_code is D2ReasonCode.LEDGER_REQUIRED


@pytest.mark.asyncio
async def test_b3_every_dispatch_writes_the_full_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = FakeLedger()
    writer = build_writer(tmp_path, monkeypatch, ledger=ledger, authorized=True)
    await writer.execute(confirm=True)
    for order in D2_BOUND_ORDERS:
        cid = order.client_order_id
        assert [state for state, c in ledger.calls if c == cid] == [
            "planned",
            "previewed",
            "validated",
            "submitted",
        ]


def test_b3_the_cli_constructs_a_ledger_service() -> None:
    source = Path("scripts/binance_spot_demo_d2_remediation.py").read_text(
        encoding="utf-8"
    )
    assert "BinanceDemoLedgerService(session)" in source
    assert "ledger=ledger" in source


# --------------------------------------------------------------------------
# B4 — physical account, writer freeze, pre-dispatch account truth
# --------------------------------------------------------------------------


def test_b4_a_foreign_credential_fingerprint_in_the_seal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = sealed_payload()
    mutant["physical_account_identity"]["credential_fingerprint"] = "sha256:" + "a" * 64
    path = write_sealed(tmp_path, monkeypatch, mutant)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ACCOUNT_MISMATCH


def test_b4_a_client_on_a_different_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Demo host is shared: the right host proves nothing about whose
    balances are about to be sold."""

    path = write_sealed(tmp_path, monkeypatch, sealed_payload())
    authority = load_sealed_authority(path)
    lease, grant = make_lease()
    with pytest.raises(D2SealBindingMismatch) as exc:
        D2RemediationSingleWriter(
            execution_client=FakeExecutionClient(  # type: ignore[arg-type]
                credential_fingerprint="sha256:" + "b" * 64
            ),
            authority=authority,
            lease=lease,
            lease_grant=grant,
            ledger=FakeLedger(),
        )
    assert exc.value.reason_code is D2ReasonCode.SEAL_ACCOUNT_MISMATCH


def test_b4_an_unfrozen_writer_lane_blocks_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.mock_lane_registry import CANONICAL_LANE_REGISTRY

    account = d2.d2_physical_account_id()
    unfrozen = tuple(
        replace(entry, writer=True) if entry.physical_account_id == account else entry
        for entry in CANONICAL_LANE_REGISTRY
    )
    monkeypatch.setattr(d2, "CANONICAL_LANE_REGISTRY", unfrozen)
    with pytest.raises(D2RemediationError) as exc:
        build_writer(tmp_path, monkeypatch)
    assert exc.value.reason_code is D2ReasonCode.WRITER_FREEZE_VIOLATED


def test_b4_the_shipped_registry_is_frozen_for_this_account() -> None:
    d2.assert_writer_freeze()
    d2.assert_registry_credential_fingerprint()


@pytest.mark.asyncio
async def test_b4_a_foreign_resting_order_stops_the_run_before_any_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 read the open-order book only in the post-dispatch proof epochs,
    so a foreign resting order was discovered after the sells were on the
    book."""

    client = FakeExecutionClient(
        open_orders=[
            SpotDemoOpenOrder(
                client_order_id="someone-elses-bot",
                broker_order_id="9",
                symbol="XRPUSDT",
                side="BUY",
                qty=Decimal("10"),
                status="NEW",
            )
        ]
    )
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    with pytest.raises(D2AccountTruthDrift) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.ACCOUNT_TRUTH_DRIFT
    assert client.submit_calls == []
    assert client.order_test_calls == []


@pytest.mark.asyncio
async def test_b4_balance_drift_stops_the_run_before_any_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drifted = {
        order.asset: (format(order.sealed_free_quantity, "f"), "0")
        for order in D2_BOUND_ORDERS
    }
    drifted["BTC"] = ("0.00009000", "0")  # someone spent it since the snapshot
    client = FakeExecutionClient(balances=drifted)
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    with pytest.raises(D2AccountTruthDrift) as exc:
        await writer.execute(confirm=True)
    assert "BTC free" in str(exc.value)
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_b4_account_truth_is_read_before_the_first_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient()
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    report = await writer.execute(confirm=True)
    assert report.account_truth is not None
    # 1 pre-dispatch read + 2 proof epochs.
    assert client.open_order_reads == 3
    # 3 sealed assets pre-dispatch + (3 + USDT) x 2 epochs.
    assert client.balance_reads == 11


# --------------------------------------------------------------------------
# B5 — the broker echo must prove the sealed price and TIF
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"price": "69266.02000000"}, "price"),
        ({"timeInForce": "IOC"}, "timeInForce"),
        ({"price": None}, "carries no price"),
        ({"timeInForce": None}, "carries no timeInForce"),
        ({"origQty": "0.00099000"}, "qty"),
        ({"side": "BUY"}, "side"),
    ],
)
async def test_b5_broker_echo_mismatch_is_not_treated_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, Any],
    needle: str,
) -> None:
    """The verifier's ``MUTANT_BROKER_PRICE_ECHO``.

    Round 1 compared symbol, side, type, and quantity, so a response that
    echoed a different *price* was accepted as ``submitted``. Absence is a
    failure too: a response that cannot prove the sealed price has not proved
    it.
    """

    client = FakeExecutionClient(echo_overrides=override)
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    with pytest.raises(D2RemediationError) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.BROKER_ECHO_MISMATCH
    assert needle in str(exc.value)


@pytest.mark.asyncio
async def test_b5_a_faithful_echo_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the echo check is discriminating, not always-fail."""

    client = FakeExecutionClient()
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    report = await writer.execute(confirm=True)
    assert report.halted_reason is None
    assert len(report.outcomes) == 3


@pytest.mark.asyncio
async def test_b5_readback_echo_is_checked_just_as_hard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drifted price is not laundered by arriving through the readback path."""

    client = FakeExecutionClient(
        submit_error=TimeoutError("reset"),
        echo_overrides={"price": "1.00000000"},
    )
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    with pytest.raises(D2RemediationError) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.BROKER_ECHO_MISMATCH


# --------------------------------------------------------------------------
# B6 — lease capability and env bypass
# --------------------------------------------------------------------------


def test_b6_a_fabricated_lease_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier injected an object with ``.released`` and ``.assert_owned``
    and it satisfied every later check while proving nothing."""

    class FabricatedLease:
        released = False

        async def assert_owned(self, grant: Any) -> None:
            return None

    path = write_sealed(tmp_path, monkeypatch, sealed_payload())
    authority = load_sealed_authority(path)
    _, grant = make_lease()
    with pytest.raises(D2LeaseNotHeld) as exc:
        D2RemediationSingleWriter(
            execution_client=FakeExecutionClient(),  # type: ignore[arg-type]
            authority=authority,
            lease=FabricatedLease(),  # type: ignore[arg-type]
            lease_grant=grant,
            ledger=FakeLedger(),
        )
    assert exc.value.reason_code is D2ReasonCode.LEASE_NOT_A_CAPABILITY


def test_b6_there_is_no_environ_seam_to_arm_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier's ``INJECTED_ENV_BYPASSES_OS_D2_GATE``.

    Round 1 accepted an ``environ`` mapping "for testability", which let an
    in-process caller arm the gate the operator had deliberately left off.
    """

    assert (
        "environ"
        not in inspect.signature(D2RemediationSingleWriter.__init__).parameters
    )
    assert inspect.signature(d2.d2_remediation_enabled).parameters == {}

    monkeypatch.delenv(D2_REMEDIATION_ENABLED_ENV, raising=False)
    path = write_sealed(tmp_path, monkeypatch, sealed_payload())
    authority = load_sealed_authority(path)
    lease, grant = make_lease()
    with pytest.raises(D2RemediationDisabled) as exc:
        D2RemediationSingleWriter(
            execution_client=FakeExecutionClient(),  # type: ignore[arg-type]
            authority=authority,
            lease=lease,
            lease_grant=grant,
            ledger=FakeLedger(),
        )
    assert exc.value.reason_code is D2ReasonCode.DISABLED


def test_b6_a_raw_payload_cannot_be_passed_instead_of_an_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, grant = make_lease()
    with pytest.raises(D2SealBindingMismatch):
        D2RemediationSingleWriter(
            execution_client=FakeExecutionClient(),  # type: ignore[arg-type]
            authority=sealed_payload(),  # type: ignore[arg-type]
            lease=lease,
            lease_grant=grant,
            ledger=FakeLedger(),
        )


def test_b6_a_sealed_authority_cannot_be_hand_built() -> None:
    """Only ``load_sealed_authority`` may mint one, so the digest check cannot
    be skipped by constructing the result object directly."""

    with pytest.raises(D2SealBindingMismatch):
        d2.SealedAuthority(
            source_path="/dev/null",
            payload_sha256="0" * 64,
            record=SealedPayloadRecord(
                sha256="0" * 64,
                pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
                dispatch_authorized=True,
                note="forged",
            ),
            orders=D2_BOUND_ORDERS,
            credential_fingerprint=D2_CREDENTIAL_FINGERPRINT,
            operator_authorization={"forged": True},
            expiry=None,
            mutation_authorized_symbols=frozenset(
                order.symbol for order in D2_BOUND_ORDERS
            ),
            _token=object(),
        )


@pytest.mark.asyncio
async def test_b6_a_released_lease_blocks_the_order_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient()
    lease, grant = make_lease()
    writer = build_writer(
        tmp_path,
        monkeypatch,
        client=client,
        authorized=True,
        lease=lease,
        grant=grant,
    )
    lease._released = True
    with pytest.raises(D2LeaseNotHeld) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.LEASE_NOT_HELD
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_b6_a_lease_over_the_wrong_keyset_blocks_the_order_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient()
    lease, grant = make_lease(keys=(1234567890,))
    writer = build_writer(
        tmp_path,
        monkeypatch,
        client=client,
        authorized=True,
        lease=lease,
        grant=grant,
    )
    with pytest.raises(D2LeaseNotHeld) as exc:
        await writer.execute(confirm=True)
    assert exc.value.reason_code is D2ReasonCode.LEASE_SCOPE_MISMATCH
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_b6_lease_ownership_is_reproved_before_each_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease that stops attesting mid-run stops the run.

    Uses the real ``pg_locks`` attestation: dropping the lock rows is what a
    lost session looks like to PostgreSQL.
    """

    client = FakeExecutionClient()
    lease, grant = make_lease()
    writer = build_writer(
        tmp_path,
        monkeypatch,
        client=client,
        authorized=True,
        lease=lease,
        grant=grant,
    )
    authority: FakeLockAuthority = lease._connection  # type: ignore[assignment]
    authority.unlocked.update(grant.keys)  # the backend no longer holds them
    with pytest.raises(D2LeaseNotHeld):
        await writer.execute(confirm=True)
    assert client.submit_calls == []


# --------------------------------------------------------------------------
# Dry run is the default and reaches no mutation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_submits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient()
    writer = build_writer(tmp_path, monkeypatch, client=client)
    report = await writer.execute()  # no confirm= at all
    assert client.submit_calls == []
    assert report.broker_mutation_count == 0
    assert report.lease_attested is True
    assert len(report.operations) == 3
    assert len(client.order_test_calls) == 3


@pytest.mark.asyncio
async def test_dry_run_reports_every_dispatch_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rehearsal lists all the blockers at once, rather than making the
    operator discover them one run at a time."""

    writer = build_writer(tmp_path, monkeypatch, client=FakeExecutionClient())
    evidence = (await writer.execute(confirm=False)).as_evidence()
    assert evidence["dispatch_authorized"] is False
    reasons = " | ".join(evidence["dispatch_block_reasons"])
    assert "dispatch_authorized=false" in reasons
    assert "operator_authorization is null" in reasons
    assert "expiry is absent" in reasons
    assert "mutation_authorized is not true" in reasons


@pytest.mark.asyncio
async def test_dry_run_evidence_prints_the_sealed_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = build_writer(tmp_path, monkeypatch, client=FakeExecutionClient())
    evidence = (await writer.execute(confirm=False)).as_evidence()
    assert evidence["writer"] == WRITER_NAME
    assert evidence["pre_snapshot_hash"] == D2_PRE_SNAPSHOT_HASH
    assert evidence["broker_mutation_count"] == 0
    assert [op["request_params"] for op in evidence["operations"]] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.binance.com",
        "https://testnet.binance.vision",
        "https://demo-api.binance.com.evil.example",
    ],
)
def test_non_spot_demo_host_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    with pytest.raises(D2UnauthorizedOperation) as exc:
        build_writer(
            tmp_path, monkeypatch, client=FakeExecutionClient(base_url=base_url)
        )
    assert exc.value.reason_code is D2ReasonCode.HOST_NOT_SPOT_DEMO


@pytest.mark.asyncio
async def test_two_independent_proof_epochs_are_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeExecutionClient()
    writer = build_writer(tmp_path, monkeypatch, client=client, authorized=True)
    report = await writer.execute(confirm=True)
    assert [epoch.epoch_index for epoch in report.proof_epochs] == [1, 2]
    assert all(epoch.ledger_states for epoch in report.proof_epochs)


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


def _sealed_payload_for(client: BinanceSpotDemoExecutionClient) -> dict[str, Any]:
    """The seal has to name the account the live client actually holds."""

    payload = sealed_payload(authorized=True)
    payload["physical_account_identity"]["credential_fingerprint"] = (
        client.credential_fingerprint
    )
    return payload


def _account_balances_json() -> dict[str, Any]:
    return {
        "balances": [
            {
                "asset": order.asset,
                "free": format(order.sealed_free_quantity, "f"),
                "locked": format(order.sealed_locked_quantity, "f"),
            }
            for order in D2_BOUND_ORDERS
        ]
    }


def _sent_order_fields(request: Any) -> dict[str, str]:
    params = dict(parse_qsl(str(request.url).split("?", 1)[1]))
    return {
        key: params[key]
        for key in ("symbol", "side", "type", "quantity", "price", "timeInForce")
        if key in params
    }


def _wire_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: BinanceSpotDemoExecutionClient,
) -> D2RemediationSingleWriter:
    monkeypatch.setattr(d2, "D2_CREDENTIAL_FINGERPRINT", client.credential_fingerprint)
    monkeypatch.setattr(
        d2,
        "assert_registry_credential_fingerprint",
        lambda: None,
    )
    path = write_sealed(
        tmp_path, monkeypatch, _sealed_payload_for(client), dispatch_authorized=True
    )
    authority = load_sealed_authority(path)
    lease, grant = make_lease()
    return D2RemediationSingleWriter(
        execution_client=client,
        authority=authority,
        lease=lease,
        lease_grant=grant,
        ledger=FakeLedger(),
    )


@pytest.mark.asyncio
async def test_dry_run_sends_only_reads_and_order_test_with_the_sealed_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_client: BinanceSpotDemoExecutionClient,
    httpx_mock: Any,
) -> None:
    httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])
    for _ in D2_BOUND_ORDERS:
        httpx_mock.add_response(
            method="GET", url=_ACCOUNT_RE, json=_account_balances_json()
        )
        httpx_mock.add_response(method="POST", url=_ORDER_TEST_RE, json={})

    writer = _wire_writer(tmp_path, monkeypatch, real_client)
    await writer.execute(confirm=False)

    requests = httpx_mock.get_requests()
    order_tests = [
        r
        for r in requests
        if str(r.url).split("?", 1)[0] == f"{_BASE}/api/v3/order/test"
    ]
    assert [_sent_order_fields(r) for r in order_tests] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]
    assert not [
        r for r in requests if str(r.url).split("?", 1)[0] == f"{_BASE}/api/v3/order"
    ]
    assert {r.url.host for r in requests} == {D2_VENUE_HOST}


@pytest.mark.asyncio
async def test_confirm_posts_exactly_the_sealed_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_client: BinanceSpotDemoExecutionClient,
    httpx_mock: Any,
) -> None:
    httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])
    for _ in D2_BOUND_ORDERS:
        httpx_mock.add_response(
            method="GET", url=_ACCOUNT_RE, json=_account_balances_json()
        )
    for index, order in enumerate(D2_BOUND_ORDERS):
        httpx_mock.add_response(method="POST", url=_ORDER_TEST_RE, json={})
        httpx_mock.add_response(
            method="POST",
            url=_ORDER_RE,
            json={
                "symbol": order.symbol,
                "orderId": 1000 + index,
                "clientOrderId": order.client_order_id,
                "side": order.side,
                "type": order.order_type,
                "origQty": format(order.quantity, "f"),
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
                "price": format(order.price, "f"),
                "timeInForce": order.time_in_force,
                "status": "NEW",
            },
        )
    for _ in range(2):
        httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])
    for _ in range(8):
        httpx_mock.add_response(
            method="GET", url=_ACCOUNT_RE, json=_account_balances_json()
        )

    writer = _wire_writer(tmp_path, monkeypatch, real_client)
    await writer.execute(confirm=True)

    posts = [
        r
        for r in httpx_mock.get_requests()
        if str(r.url).split("?", 1)[0] == f"{_BASE}/api/v3/order"
    ]
    assert [_sent_order_fields(r) for r in posts] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]
    # The deterministic ids go on the wire, not a UUID.
    sent_ids = [
        dict(parse_qsl(str(r.url).split("?", 1)[1]))["newClientOrderId"] for r in posts
    ]
    assert sent_ids == [order.client_order_id for order in D2_BOUND_ORDERS]
    assert {r.url.host for r in httpx_mock.get_requests()} == {D2_VENUE_HOST}


# --------------------------------------------------------------------------
# Bypass enumeration — the seams I went looking for myself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_backdated_now_fn_cannot_revive_an_expired_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``now_fn`` exists for reproducible evidence timestamps.

    Wiring it into the expiry comparison would have made it a bypass: a caller
    passing a clock fixed before the expiry could run an authority that has
    already lapsed. The expiry check reads the real clock.
    """

    import datetime as real_dt

    expired = sealed_payload(authorized=True)
    expired["expiry"] = "2020-01-01T00:00:00Z"
    path = write_sealed(tmp_path, monkeypatch, expired, dispatch_authorized=True)
    authority = load_sealed_authority(path)
    lease, grant = make_lease()
    client = FakeExecutionClient()
    writer = D2RemediationSingleWriter(
        execution_client=client,  # type: ignore[arg-type]
        authority=authority,
        lease=lease,
        lease_grant=grant,
        ledger=FakeLedger(),
        now_fn=lambda: real_dt.datetime(2019, 1, 1, tzinfo=real_dt.UTC),
    )
    assert any("expired at" in r for r in writer.dispatch_block_reasons())
    with pytest.raises(D2DispatchNotAuthorized):
        await writer.execute(confirm=True)
    assert client.submit_calls == []
