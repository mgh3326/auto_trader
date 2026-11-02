"""ROB-1461 item 3: gateway-origin kis_mock follow-ups mint; every other order cannot.

These tests run the real capability issuer and the real ``cancel_order_impl`` and
``modify_order_impl`` guards. Only the KIS client, the ledger and the writer lease
are replaced. See the amendment in
``docs/contracts/rob-1263-kis-coordination-adapter.md`` §7.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.mcp_server.tooling import kis_mock_ledger, orders_modify_cancel
from app.schemas.execution_intent import AmendChild, CancelChild
from app.services.brokers import edge_client
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
)
from app.services.execution_intent_gateway.kis_mock_forwarder import (
    FollowupRefusal,
    KisMockForwarder,
    claim_scope,
    followup_refusal,
    order_correlation_id,
)
from app.services.execution_intent_gateway.store import IntentJournal
from app.services.kis_mock_runner import singleton
from tests.services.execution_intent_gateway.support import (
    MemoryGatewayStore,
    intent_snapshot,
)

ORDER_ID = "0001001"
ROOT_KEY = "key-00001"


class _Lease:
    acquired = True

    async def __aenter__(self) -> _Lease:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Kis:
    """The wire. Records the capability that was live when the call arrived."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.receipts: list[Any] = []

    def _record(self, action: str, order_id: str) -> None:
        self.calls.append((action, order_id))
        self.receipts.append(singleton.active_followup_capability())

    async def cancel_korea_order(self, **kwargs: Any) -> dict[str, Any]:
        self._record("cancel", kwargs["order_number"])
        return {"ord_tmd": "100000"}

    async def modify_korea_order(
        self, order_id: str, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        self._record("modify", order_id)
        return {"odno": "0001002"}


def _child(
    key: str = ROOT_KEY,
    *,
    child_id: int = 1,
    action: str = "submit",
    target: str | None = None,
    state: ChildState = ChildState.ACCEPTED,
    order_id: str | None = ORDER_ID,
) -> ChildSnapshot:
    return ChildSnapshot(
        child_id=child_id,
        idempotency_key=key,
        request_digest=f"digest-{key}",
        action=action,
        target_idempotency_key=target,
        quantity=None if action == "cancel" else 10,
        limit_price_krw=None if action == "cancel" else 70_000,
        state=state,
        refusal_reason="x" if state is ChildState.REFUSED else None,
        broker_order_id=order_id,
        result=None,
    )


def _ledger_order(**overrides: Any) -> dict[str, Any]:
    return {
        "ledger_id": 7,
        "symbol": "005930",
        "side": "buy",
        "quantity": 10.0,
        "remaining_quantity": 10.0,
        "price": 70_000.0,
        "krx_fwdg_ord_orgno": "00950",
        "instrument_type": "equity_kr",
        "lifecycle_state": "accepted",
        "correlation_id": order_correlation_id(ROOT_KEY),
        "edge_command_id": None,
        **overrides,
    }


def _seeded(intent: IntentSnapshot, *children: ChildSnapshot) -> MemoryGatewayStore:
    store = MemoryGatewayStore(intent)
    for child in children:
        store.children.append(child)
        store._intent_of_child[child.child_id] = intent.intent_id
    return store


def _cancel(intent: IntentSnapshot, target: str = ROOT_KEY) -> CancelChild:
    return CancelChild(
        action="cancel",
        intent_id=intent.intent_id,
        idempotency_key="key-00002",
        target_idempotency_key=target,
    )


def _amend(intent: IntentSnapshot, target: str = ROOT_KEY) -> AmendChild:
    return AmendChild(
        action="amend",
        intent_id=intent.intent_id,
        idempotency_key="key-00002",
        target_idempotency_key=target,
        quantity=6,
        limit_price_krw=70_100,
    )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Kis:
    kis = _Kis()
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda *, is_mock: kis
    )
    monkeypatch.setattr(edge_client, "get_kis_mock_edge_url", lambda: None)

    async def security_type(symbol: str) -> None:
        return None

    monkeypatch.setattr(
        orders_modify_cancel, "_kr_security_type_or_none", security_type
    )
    return kis


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The mock ledger: one order, plus what the real paths write to it."""
    state: dict[str, Any] = {"order": _ledger_order(), "writes": []}

    async def resolve(order_no: str) -> dict[str, Any] | None:
        return state["order"]

    async def cancelled(**kwargs: Any) -> None:
        state["writes"].append(("cancelled", kwargs))

    async def terms(**kwargs: Any) -> None:
        state["writes"].append(("terms", kwargs))

    monkeypatch.setattr(kis_mock_ledger, "resolve_mock_order_for_cancel", resolve)
    monkeypatch.setattr(kis_mock_ledger, "mark_kis_mock_order_cancelled", cancelled)
    monkeypatch.setattr(kis_mock_ledger, "update_kis_mock_order_terms", terms)
    return state


# The gate this PR leaves closed: no receipt means zero broker calls.


@pytest.mark.asyncio
async def test_without_a_minted_capability_the_guards_still_refuse(
    wire: _Kis, ledger: dict[str, Any]
) -> None:
    result = await orders_modify_cancel.cancel_order_impl(
        order_id=ORDER_ID, symbol="005930", market="kr", is_mock=True
    )
    assert result["success"] is False
    assert wire.calls == []


# The positive path, through the real guards.


@pytest.mark.asyncio
async def test_gateway_cancel_mints_and_reaches_the_broker_once(
    wire: _Kis, ledger: dict[str, Any]
) -> None:
    intent = intent_snapshot()
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child()), lease_factory=_Lease
    )

    forwarded = await forwarder.cancel(
        intent, _cancel(intent), broker_order_id=ORDER_ID
    )

    assert forwarded.accepted is True
    assert forwarded.broker_order_id == ORDER_ID
    assert wire.calls == [("cancel", ORDER_ID)]


@pytest.mark.asyncio
async def test_gateway_amend_mints_and_returns_the_replacement_order(
    wire: _Kis, ledger: dict[str, Any]
) -> None:
    intent = intent_snapshot()
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child()), lease_factory=_Lease
    )

    forwarded = await forwarder.amend(intent, _amend(intent), broker_order_id=ORDER_ID)

    assert (forwarded.accepted, forwarded.broker_order_id) == (True, "0001002")
    assert wire.calls == [("modify", ORDER_ID)]


@pytest.mark.asyncio
async def test_amend_repoints_the_ledger_row_so_the_next_follow_up_finds_it(
    wire: _Kis, ledger: dict[str, Any]
) -> None:
    intent = intent_snapshot()
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child()), lease_factory=_Lease
    )
    await forwarder.amend(intent, _amend(intent), broker_order_id=ORDER_ID)
    kind, written = ledger["writes"][0]
    assert kind == "terms"
    assert written["order_no"] == "0001002"
    assert written["detail"]["modified_from_order_no"] == ORDER_ID


@pytest.mark.asyncio
async def test_cancel_of_an_amended_order_binds_to_the_amend_and_the_root(
    wire: _Kis, ledger: dict[str, Any]
) -> None:
    intent = intent_snapshot()
    amend = _child(
        "key-00002",
        child_id=2,
        action="amend",
        target=ROOT_KEY,
        order_id="0001002",
    )
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child(), amend), lease_factory=_Lease
    )
    request = CancelChild(
        action="cancel",
        intent_id=intent.intent_id,
        idempotency_key="key-00003",
        target_idempotency_key="key-00002",
    )
    forwarded = await forwarder.cancel(intent, request, broker_order_id="0001002")
    assert forwarded.accepted is True
    assert wire.calls == [("cancel", "0001002")]


@pytest.mark.asyncio
async def test_the_receipt_names_the_journal_row_and_dies_with_the_call(
    wire: _Kis, ledger: dict[str, Any]
) -> None:
    intent = intent_snapshot()
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child()), lease_factory=_Lease
    )
    await forwarder.cancel(intent, _cancel(intent), broker_order_id=ORDER_ID)

    receipt = wire.receipts[0]
    assert receipt.operation is singleton.KISMockOperation.FOLLOWUP_CANCEL
    assert receipt.claim_account_scope == claim_scope(intent.intent_id)
    assert receipt.claim_idempotency_key == ROOT_KEY
    assert receipt.claim_row_id == 1
    assert receipt.claim_side == "buy"
    assert receipt.attributed_broker_order_id == ORDER_ID
    assert receipt.alive is False
    assert singleton.active_followup_capability() is None


@pytest.mark.asyncio
async def test_soft_cancel_is_not_accepted(
    wire: _Kis, ledger: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unsupported(**kwargs: Any) -> dict[str, Any]:
        wire.calls.append(("cancel", kwargs["order_number"]))
        raise RuntimeError("모의투자 unsupported")

    monkeypatch.setattr(wire, "cancel_korea_order", unsupported)
    monkeypatch.setattr(
        orders_modify_cancel, "_is_kis_mock_unsupported", lambda message: True
    )
    intent = intent_snapshot()
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child()), lease_factory=_Lease
    )

    forwarded = await forwarder.cancel(
        intent, _cancel(intent), broker_order_id=ORDER_ID
    )

    assert forwarded.result["soft_cancelled"] is True
    assert forwarded.accepted is False


@pytest.mark.asyncio
async def test_a_claim_that_vanishes_inside_the_lease_sends_nothing(
    wire: _Kis, ledger: dict[str, Any]
) -> None:
    intent = intent_snapshot()
    store = _seeded(intent, _child())
    calls = 0
    read = store.read_intent_journal

    async def journal_gone_on_second_read(intent_id: Any) -> IntentJournal | None:
        nonlocal calls
        calls += 1
        return await read(intent_id) if calls == 1 else None

    store.read_intent_journal = journal_gone_on_second_read  # type: ignore[method-assign]
    forwarder = KisMockForwarder(journal=store, lease_factory=_Lease)

    with pytest.raises(singleton.KISMockFollowupNotAuthorized):
        await forwarder.cancel(intent, _cancel(intent), broker_order_id=ORDER_ID)
    assert wire.calls == []


# The amendment's limit: J2B-origin and non-mock orders cannot mint.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "correlation",
    [
        None,
        "",
        "mock-idempotency-v1:abc",
        "kis-mock-signal:abc",
        order_correlation_id("key-99999"),
        ROOT_KEY,
    ],
)
async def test_j2b_origin_order_cannot_mint(
    wire: _Kis,
    ledger: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    correlation: str | None,
) -> None:
    def must_not_issue(**kwargs: Any) -> None:
        raise AssertionError("no capability may be issued for this order")

    monkeypatch.setattr(singleton, "issue_kis_mock_followup_capability", must_not_issue)
    ledger["order"] = _ledger_order(correlation_id=correlation)
    intent = intent_snapshot()
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child()), lease_factory=_Lease
    )

    for forwarded in (
        await forwarder.cancel(intent, _cancel(intent), broker_order_id=ORDER_ID),
        await forwarder.amend(intent, _amend(intent), broker_order_id=ORDER_ID),
    ):
        assert forwarded.accepted is False
        assert forwarded.result["reason_code"] == (
            FollowupRefusal.NOT_GATEWAY_ORIGIN.value
        )
    assert wire.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["kis_live", "toss_live", "kiwoom_mock", ""])
async def test_non_mock_order_cannot_mint(
    wire: _Kis,
    ledger: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    account: str,
) -> None:
    def must_not_issue(**kwargs: Any) -> None:
        raise AssertionError("no capability may be issued for this order")

    monkeypatch.setattr(singleton, "issue_kis_mock_followup_capability", must_not_issue)
    intent = intent_snapshot(account=account)
    forwarder = KisMockForwarder(
        journal=_seeded(intent, _child()), lease_factory=_Lease
    )

    for forwarded in (
        await forwarder.cancel(intent, _cancel(intent), broker_order_id=ORDER_ID),
        await forwarder.amend(intent, _amend(intent), broker_order_id=ORDER_ID),
    ):
        assert forwarded.accepted is False
        assert forwarded.result["reason_code"] == FollowupRefusal.ACCOUNT_NOT_MOCK.value
    assert wire.calls == []


# followup_refusal: one case per refusal, and every refusal has a case.

_INTENT = intent_snapshot()


def _journal(*children: ChildSnapshot) -> IntentJournal:
    return IntentJournal(intent=_INTENT, children=children)


_REFUSALS: dict[FollowupRefusal, dict[str, Any]] = {
    FollowupRefusal.ACCOUNT_NOT_MOCK: {
        "intent": intent_snapshot(intent_id=_INTENT.intent_id, account="kis_live")
    },
    FollowupRefusal.INTENT_NOT_IN_JOURNAL: {"journal": None},
    FollowupRefusal.TARGET_NOT_IN_JOURNAL: {"target_key": "key-99999"},
    FollowupRefusal.ORDER_ID_MISMATCH: {"broker_order_id": "0009999"},
    FollowupRefusal.ORDER_NOT_IN_LEDGER: {"ledger_order": None},
    FollowupRefusal.NOT_GATEWAY_ORIGIN: {
        "ledger_order": _ledger_order(correlation_id="mock-idempotency-v1:abc")
    },
    FollowupRefusal.LEDGER_TERMS_MISMATCH: {
        "ledger_order": _ledger_order(symbol="000660")
    },
    FollowupRefusal.ORDER_NOT_OPEN: {
        "ledger_order": _ledger_order(lifecycle_state="cancelled")
    },
    FollowupRefusal.REMAINDER_UNKNOWN: {
        "ledger_order": _ledger_order(remaining_quantity=0.0)
    },
}


def _arguments(**overrides: Any) -> dict[str, Any]:
    return {
        "intent": _INTENT,
        "journal": _journal(_child()),
        "target_key": ROOT_KEY,
        "broker_order_id": ORDER_ID,
        "ledger_order": _ledger_order(),
        **overrides,
    }


def test_every_refusal_has_a_case() -> None:
    assert set(_REFUSALS) == set(FollowupRefusal)


def test_the_baseline_case_is_allowed() -> None:
    arguments = _arguments()
    assert followup_refusal(arguments.pop("intent"), **arguments) is None


@pytest.mark.parametrize("refusal", list(FollowupRefusal), ids=lambda r: r.value)
def test_each_condition_refuses_alone(refusal: FollowupRefusal) -> None:
    arguments = _arguments(**_REFUSALS[refusal])
    assert followup_refusal(arguments.pop("intent"), **arguments) is refusal


def test_another_intents_journal_is_refused() -> None:
    arguments = _arguments(
        journal=IntentJournal(
            intent=intent_snapshot(intent_id=uuid4()), children=(_child(),)
        )
    )
    assert (
        followup_refusal(arguments.pop("intent"), **arguments)
        is FollowupRefusal.INTENT_NOT_IN_JOURNAL
    )


@pytest.mark.parametrize(
    "ledger_overrides",
    [
        {"side": "sell"},
        {"instrument_type": "equity_us"},
        {"symbol": "000660"},
    ],
)
def test_ledger_terms_must_match_the_intent(ledger_overrides: dict[str, Any]) -> None:
    arguments = _arguments(ledger_order=_ledger_order(**ledger_overrides))
    assert (
        followup_refusal(arguments.pop("intent"), **arguments)
        is FollowupRefusal.LEDGER_TERMS_MISMATCH
    )


@pytest.mark.parametrize(
    "state",
    ["planned", "previewed", "reconciled", "stale", "failed", "anomaly", "expired"],
)
def test_only_an_open_order_can_mint(state: str) -> None:
    arguments = _arguments(ledger_order=_ledger_order(lifecycle_state=state))
    assert (
        followup_refusal(arguments.pop("intent"), **arguments)
        is FollowupRefusal.ORDER_NOT_OPEN
    )


@pytest.mark.parametrize("remaining", [None, 0, 0.0, -1.0, "10", True, float("nan")])
def test_an_unknown_remainder_cannot_mint(remaining: Any) -> None:
    arguments = _arguments(ledger_order=_ledger_order(remaining_quantity=remaining))
    assert (
        followup_refusal(arguments.pop("intent"), **arguments)
        is FollowupRefusal.REMAINDER_UNKNOWN
    )


@pytest.mark.parametrize(
    "target",
    [
        _child(state=ChildState.REFUSED, order_id=ORDER_ID),
        _child(action="cancel", target="key-00000"),
        _child(order_id=None),
        _child(order_id="0009999"),
    ],
    ids=["refused", "cancel_row", "no_order_id", "other_order_id"],
)
def test_the_target_row_must_be_a_sent_order_with_this_id(
    target: ChildSnapshot,
) -> None:
    arguments = _arguments(journal=_journal(target))
    assert (
        followup_refusal(arguments.pop("intent"), **arguments)
        is FollowupRefusal.ORDER_ID_MISMATCH
    )


def test_uncertain_target_with_a_journaled_order_id_may_still_be_cancelled() -> None:
    arguments = _arguments(journal=_journal(_child(state=ChildState.UNCERTAIN)))
    assert followup_refusal(arguments.pop("intent"), **arguments) is None
