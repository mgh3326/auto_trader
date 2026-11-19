"""hk 5441 — the live forwarder's send boundary and the proven non-send (design
§8.2-4, B2, B12, B28, B29).

The forwarder is run against the real ``_execute_crypto_order`` wire code for
the price and volume it posts, and against a recording stand-in for
``_place_order_impl`` for what it is called with.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.mcp_server.tooling import order_execution
from app.mcp_server.tooling.crypto_prevalidated_wire import PrevalidatedCryptoWire
from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.execution_intent_gateway.decisions import ChildState, Refusal
from app.services.execution_intent_gateway.forward_result import ForwardResult
from app.services.execution_intent_gateway.upbit_decisions import (
    NOT_SENT_REASONS,
    SleeveView,
)
from app.services.execution_intent_gateway.upbit_gateway import UpbitGatewayPath
from app.services.execution_intent_gateway.upbit_live_forwarder import (
    UPBIT_LIVE_ENABLED_ENV,
    UpbitLiveForwarder,
)
from app.services.kis_mock_runner.control import KillMode
from tests.services.execution_intent_gateway.support import FakeKillSwitch
from tests.services.execution_intent_gateway.upbit_support import (
    ACCOUNT_REF,
    LIVE_ENV,
    NOW,
    FakeShadowForwarder,
    MemoryUpbitStore,
    book,
    facts,
    mandate,
    submit,
    upbit_intent,
    version,
)
from tests.services.execution_intent_gateway.upbit_support import (
    cancel as cancel_child,
)

pytestmark = pytest.mark.unit


class _RecordingPlace:
    """Stands in for ``_place_order_impl``.

    ``reach_post`` says whether the order layer gets as far as its pre-send
    hook, which is the last thing it does before the HTTP request.
    """

    def __init__(self, *, reach_post: bool = True, result: dict | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reach_post = reach_post
        self.result = result or {
            "success": True,
            "broker_status": "accepted",
            "order_id": "uuid-9",
        }
        self.posted = False

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.reach_post:
            return {"success": False, "error": "Buy price exceeds current price"}
        try:
            await kwargs["pre_send_hook"]()
        except PreSendFreshnessError as exc:
            return {
                "success": False,
                "pre_send_blocked": True,
                "reason_codes": list(exc.reason_codes),
            }
        self.posted = True
        return self.result


def _forwarder(
    store: MemoryUpbitStore,
    *,
    environment: dict[str, str] | None = None,
    kill: KillMode = KillMode.ACTIVE,
    account_ref: str | None = ACCOUNT_REF,
) -> UpbitLiveForwarder:
    return UpbitLiveForwarder(
        facts=store,
        kill_switch=FakeKillSwitch(kill),
        environment=LIVE_ENV if environment is None else environment,
        clock=lambda: NOW,
        account_ref=lambda: account_ref,
    )


@pytest.fixture
def place(monkeypatch: pytest.MonkeyPatch) -> _RecordingPlace:
    recorder = _RecordingPlace()
    monkeypatch.setattr(order_execution, "_place_order_impl", recorder)
    return recorder


@pytest.mark.asyncio
async def test_the_child_key_is_the_upbit_identifier(place: _RecordingPlace) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    request = submit(intent, "3f2b6c1e-8a4d-4e5f-9a7b-1c2d3e4f5a6b:1")

    result = await _forwarder(store).submit(intent, request, child_id=1)

    [call] = place.calls
    assert call["client_order_id"] == request.idempotency_key
    assert call["market"] == "crypto" and call["order_type"] == "limit"
    assert call["dry_run"] is False and "is_mock" not in call
    assert call["prevalidated_wire"] == PrevalidatedCryptoWire(
        price_krw=3_000, volume=Decimal("10")
    )
    assert result.accepted is True and result.broker_order_id == "uuid-9"
    assert place.posted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "false", "TRUE", "1"])
async def test_without_the_live_switch_the_order_service_is_not_called(
    place: _RecordingPlace, value: str | None
) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    environment = {} if value is None else {UPBIT_LIVE_ENABLED_ENV: value}
    forwarder = _forwarder(store, environment=environment)

    sent = await forwarder.submit(intent, submit(intent), child_id=1)
    cancelled = await forwarder.cancel(
        intent, cancel_child(intent, "child-0001"), broker_order_id="uuid-1"
    )

    assert place.calls == []
    assert sent.not_sent_reason == cancelled.not_sent_reason == "upbit_live_disabled"


@pytest.mark.asyncio
async def test_a_shadow_intent_given_to_the_live_forwarder_sends_nothing(
    place: _RecordingPlace,
) -> None:
    intent = upbit_intent(execution_mode="shadow")
    store = MemoryUpbitStore(intent, facts=facts())
    result = await _forwarder(store).submit(intent, submit(intent), child_id=1)
    assert place.calls == [] and result.not_sent_reason == "upbit_live_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "kill", "account_ref", "reason"),
    [
        (
            {"mandate": mandate(revoked=True)},
            KillMode.ACTIVE,
            ACCOUNT_REF,
            "mandate_revoked",
        ),
        ({}, KillMode.GLOBAL_FREEZE, ACCOUNT_REF, "submissions_killed"),
        ({}, KillMode.ACTIVE, "upbit:upbit:" + "c" * 64, "account_mismatch"),
        ({}, KillMode.ACTIVE, None, "account_mismatch"),
        (
            {"mandate": mandate(account_ref=None)},
            KillMode.ACTIVE,
            ACCOUNT_REF,
            "account_mismatch",
        ),
        (
            {"sleeve": SleeveView(version=version("stopped"), book=book())},
            KillMode.ACTIVE,
            ACCOUNT_REF,
            "sleeve_not_live",
        ),
    ],
)
async def test_the_pre_send_hook_refuses_and_nothing_is_posted(
    place: _RecordingPlace, changed, kill, account_ref, reason
) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts(**changed))
    forwarder = _forwarder(store, kill=kill, account_ref=account_ref)

    result = await forwarder.submit(intent, submit(intent), child_id=1)

    assert place.posted is False
    assert result.accepted is False and result.not_sent_reason == reason
    assert reason in NOT_SENT_REASONS


@pytest.mark.asyncio
async def test_a_guard_below_that_refuses_before_the_hook_is_a_proven_non_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingPlace(reach_post=False)
    monkeypatch.setattr(order_execution, "_place_order_impl", recorder)
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())

    result = await _forwarder(store).submit(intent, submit(intent), child_id=1)

    assert result.not_sent_reason == Refusal.LOWER_GUARD_REFUSED.value


@pytest.mark.asyncio
async def test_an_exception_after_the_hook_passed_is_not_a_proven_non_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raising(**kwargs: Any) -> dict[str, Any]:
        await kwargs["pre_send_hook"]()
        raise RuntimeError("connection reset after the request was written")

    monkeypatch.setattr(order_execution, "_place_order_impl", raising)
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())

    with pytest.raises(RuntimeError):
        await _forwarder(store).submit(intent, submit(intent), child_id=1)


@pytest.mark.asyncio
async def test_an_exception_before_the_hook_is_a_proven_non_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raising(**_kwargs: Any) -> dict[str, Any]:
        raise ValueError("No holdings found")

    monkeypatch.setattr(order_execution, "_place_order_impl", raising)
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())

    result = await _forwarder(store).submit(intent, submit(intent), child_id=1)
    assert result.not_sent_reason == Refusal.LOWER_GUARD_REFUSED.value


@pytest.mark.asyncio
async def test_a_broker_rejection_reported_as_success_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live ledger writer answers success=True even when Upbit rejected.
    recorder = _RecordingPlace(
        result={"success": True, "broker_status": "rejected", "order_id": None}
    )
    monkeypatch.setattr(order_execution, "_place_order_impl", recorder)
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())

    result = await _forwarder(store).submit(intent, submit(intent), child_id=1)
    assert result.accepted is False and result.not_sent_reason is None


# ------------------------------------------------------------ B29 in the journal


def _path(store: MemoryUpbitStore, live: Any) -> UpbitGatewayPath:
    async def avg(_symbol: str) -> Decimal | None:
        return Decimal("2500")

    async def no_cooldown(_symbol: str) -> bool:
        return False

    return UpbitGatewayPath(
        store=store,
        kill_switch=FakeKillSwitch(),
        shadow_forwarder=FakeShadowForwarder(),
        live_forwarder=live,
        avg_price_reader=avg,
        cooldown_reader=no_cooldown,
        environment=LIVE_ENV,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_b29_a_proven_non_send_is_journaled_not_sent_and_answers_refused(
    place: _RecordingPlace,
) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    forwarder = _forwarder(store, account_ref="upbit:upbit:" + "c" * 64)

    first = await _path(store, forwarder).handle(submit(intent))
    again = await _path(store, forwarder).handle(submit(intent))

    row = store.child("child-0001")
    assert row.state is ChildState.NOT_SENT and row.broker_order_id is None
    assert row.refusal_reason == "account_mismatch"
    assert (first.status, first.reason) == ("refused", "account_mismatch")
    assert (again.status, again.reason, again.replayed) == (
        "refused",
        "account_mismatch",
        True,
    )
    assert place.posted is False and len(place.calls) == 1


@pytest.mark.asyncio
async def test_b29_a_not_sent_child_holds_no_exposure(place: _RecordingPlace) -> None:
    intent = upbit_intent(max_quantity_decimal=Decimal("10"))
    store = MemoryUpbitStore(intent, facts=facts())
    bad = _forwarder(store, account_ref=None)
    good = _forwarder(store)

    assert (await _path(store, bad).handle(submit(intent, "child-0001"))).status == (
        "refused"
    )
    # The whole intent quantity is still free for a child with a new key.
    assert (await _path(store, good).handle(submit(intent, "child-0002"))).status == (
        "accepted"
    )


@pytest.mark.asyncio
async def test_b29_a_not_sent_order_cannot_be_cancelled(place: _RecordingPlace) -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    await _path(store, _forwarder(store, account_ref=None)).handle(submit(intent))

    outcome = await _path(store, _forwarder(store)).handle(
        cancel_child(intent, "child-0001")
    )
    assert outcome.reason == Refusal.TARGET_NOT_WORKING.value


@pytest.mark.asyncio
async def test_a_live_intent_with_no_live_forwarder_is_refused_without_a_row() -> None:
    intent = upbit_intent()
    store = MemoryUpbitStore(intent, facts=facts())
    outcome = await _path(store, None).handle(submit(intent))
    assert outcome.reason == Refusal.FORWARDER_UNAVAILABLE.value
    assert store.children == []


# ------------------------------------------------------------ B2, B28 on the wire


class _FakeUpbit:
    def __init__(self) -> None:
        self.buys: list[tuple[str, Any, str]] = []
        self.sells: list[tuple[str, str, str]] = []

    def adjust_price_to_upbit_unit(self, price: float) -> float:
        # The legacy table: 5 KRW steps between 1,000 and 10,000 KRW.
        return round(price / 5) * 5

    async def place_buy_order(self, symbol, price, volume, ord_type, **_kw):
        self.buys.append((symbol, price, volume))
        return {"uuid": "uuid-1"}

    async def place_sell_order(self, symbol, volume, price, **_kw):
        self.sells.append((symbol, volume, price))
        return {"uuid": "uuid-2"}


@pytest.fixture
def upbit(monkeypatch: pytest.MonkeyPatch) -> _FakeUpbit:
    fake = _FakeUpbit()
    monkeypatch.setattr(order_execution, "upbit_service", fake)

    async def holdings(_symbol: str, _market: str) -> dict[str, Any]:
        return {"quantity": 100.0}

    monkeypatch.setattr(order_execution, "_get_holdings_for_order", holdings)
    return fake


@pytest.mark.asyncio
async def test_b28_a_prevalidated_price_is_posted_unadjusted(upbit: _FakeUpbit) -> None:
    # 3,001 KRW is on the published 1 KRW unit; the legacy table would send 3,000.
    wire = PrevalidatedCryptoWire(price_krw=3_001, volume=Decimal("10"))
    await order_execution._execute_crypto_order(
        "KRW-XRP", "buy", "limit", 10.0, 3001.0, prevalidated_wire=wire
    )
    await order_execution._execute_crypto_order(
        "KRW-XRP", "sell", "limit", 10.0, 3001.0, prevalidated_wire=wire
    )
    assert upbit.buys == [("KRW-XRP", "3001", "10.00000000")]
    assert upbit.sells == [("KRW-XRP", "10.00000000", "3001")]


@pytest.mark.asyncio
async def test_every_other_caller_still_gets_the_legacy_adjustment(
    upbit: _FakeUpbit,
) -> None:
    await order_execution._execute_crypto_order("KRW-XRP", "buy", "limit", 10.0, 3001.0)
    assert upbit.buys == [("KRW-XRP", 3000, "10.00000000")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("price", "quantity"),
    [(3002.0, 10.0), (3001.0, 9.99999999), (3001.0, 10.00000001), (None, 10.0)],
)
async def test_b2_a_price_or_volume_that_differs_from_the_approved_one_is_not_posted(
    upbit: _FakeUpbit, price, quantity
) -> None:
    wire = PrevalidatedCryptoWire(price_krw=3_001, volume=Decimal("10"))
    for side in ("buy", "sell"):
        with pytest.raises(PreSendFreshnessError) as raised:
            await order_execution._execute_crypto_order(
                "KRW-XRP", side, "limit", quantity, price, prevalidated_wire=wire
            )
        assert raised.value.reason_codes == ("price_normalization_mismatch",)
    assert upbit.buys == [] and upbit.sells == []


@pytest.mark.asyncio
async def test_a_prevalidated_wire_is_never_a_market_order(upbit: _FakeUpbit) -> None:
    wire = PrevalidatedCryptoWire(price_krw=3_001, volume=Decimal("10"))
    with pytest.raises(ValueError):
        await order_execution._execute_crypto_order(
            "KRW-XRP", "buy", "market", None, 30000.0, prevalidated_wire=wire
        )


def test_the_mismatch_reason_is_one_the_journal_accepts() -> None:
    assert "price_normalization_mismatch" in NOT_SENT_REASONS
    assert isinstance(
        ForwardResult(accepted=False, broker_order_id=None, result={}).shadowed, bool
    )
