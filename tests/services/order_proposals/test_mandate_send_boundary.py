"""hk 3795 T4c: the plan-bound send boundary on the proposal send path.

The send port is faked, so these run without a database and count real broker calls
(``sent``): no committed fence, no POST (I24); a fence allows one POST; only a proven
non-send or an explicit rejection closes a fence; an ambiguous outcome never does.
Test names carry the invariant or the §4.6 case they pin.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.mandate import fence as mf
from app.services.order_proposals import mandate_send, revalidation
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NOW = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)


class Broker:
    """A fake broker edge: the pre-send hook runs, then one 'HTTP request' is counted."""

    def __init__(self, reply=None, *, retry: bool = False, raise_before_hook=None):
        self.sent = 0
        self.preview_calls = 0
        self.reply = reply or {
            "success": True,
            "status": "acked",
            "broker_order_id": "B-1",
        }
        self.retry = retry
        self.raise_before_hook = raise_before_hook

    async def __call__(self, **kwargs):
        if kwargs.get("dry_run") is True:
            self.preview_calls += 1
            return {
                "success": True,
                "price": str(kwargs["price"]),
                "quantity": str(kwargs["quantity"]),
                "approval_hash": "h",
            }
        if self.raise_before_hook is not None:
            raise self.raise_before_hook
        hook = kwargs["pre_send_hook"]
        await hook()
        self.sent += 1
        if self.retry:  # a lower layer retries the same request
            await hook()
            self.sent += 1
        return self.reply


class Port(mandate_send.MandateSendPort):
    def __init__(self, *, commit=None, boundary=None, account="acct-1"):
        self.committed: list[mf.FenceRequest] = []
        self.released: list[tuple[int, int, str]] = []
        self.boundary_calls: list[int | None] = []
        self._commit = commit
        self._boundary = boundary

        async def commit_fence(request):
            self.committed.append(request)
            if self._commit is not None:
                return self._commit
            return mf.FenceOutcome(True, seq=4, attempt_id=request.attempt_id)

        async def release_fence(plan_id, seq, *, reason_code):
            self.released.append((plan_id, seq, reason_code))
            return mf.ReleaseOutcome(True, reason_code)

        async def boundary_refusal(request, *, fenced_seq=None, **_):
            self.boundary_calls.append(fenced_seq)
            return self._boundary

        async def resolver(_group):
            return account

        super().__init__(
            commit_fence=commit_fence,
            release_fence=release_fence,
            boundary_refusal=boundary_refusal,
            account_ref_resolver=resolver,
        )


def _group(**over):
    base = {
        "proposal_id": uuid.uuid4(),
        "account_mode": "kiwoom_mock",
        "broker_account_id": "acct-1",
        "symbol": "005930",
        "market": "equity_kr",
        "order_type": "limit",
        "action": "place",
        "side": "buy",
        "thesis": "t",
        "strategy": "s",
        "exit_intent": None,
        "exit_reason": None,
        "retrospective_id": None,
        "approval_issue_id": None,
        "source_asof": {"approval_window_policy_stamp": "stamp"},
        "superseded_by_proposal_id": None,
        "lifecycle_state": "proposed",
        "approval_nonce": "n",
        "decision_id": None,
        "mandate_plan_id": 7,
        "mandate_tranche_index": 1,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _rung(**over):
    base = {
        "rung_index": 0,
        "side": "buy",
        "limit_price": Decimal("100"),
        "quantity": Decimal("3"),
        "notional": None,
        "state": "pending_approval",
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def harness(monkeypatch):
    async def allowed(*_a, **_k):
        return SimpleNamespace(allowed=True, observed_at=NOW)

    monkeypatch.setattr(revalidation, "evaluate_approval_window_boundary", allowed)

    def build(group, rung):
        service = AsyncMock()
        service.get_proposal.return_value = (group, [rung])
        service.mandate_loss_cut_refusal = AsyncMock(return_value=None)
        service.assert_dispatch_still_authorized = AsyncMock(return_value=None)
        return service

    async def claimer(**_k):
        return None

    async def run(service, broker, port, **kw):
        return await revalidate_and_submit(
            service=service,
            proposal_id=uuid.uuid4(),
            now=NOW,
            place_order_fn=broker,
            correlation_mint=lambda **_k: "corr",
            buying_power_claimer=claimer,
            buying_power_releaser=AsyncMock(),
            expected_policy_stamp="stamp",
            now_fn=lambda: NOW,
            mandate_port=port,
            **kw,
        )

    return SimpleNamespace(build=build, run=run)


async def test_i24_no_committed_fence_means_no_broker_call(harness):
    """Fence commit failure injected on the human path: zero POSTs, rung restored."""
    group, rung = _group(), _rung()
    service = harness.build(group, rung)
    broker = Broker()
    port = Port(commit=mf.FenceOutcome(False, refusal="mandate_fence_unavailable"))

    outcomes = await harness.run(service, broker, port)

    assert broker.sent == 0
    assert outcomes[0].result == "guard_blocked"
    assert outcomes[0].detail["reason"] == "mandate_fence_unavailable"
    service.transition_rung.assert_any_await(
        group.proposal_id, 0, new_state="pending_approval"
    )
    assert port.released == []  # nothing was fenced, nothing to release


async def test_i24_a_committed_fence_allows_exactly_one_post(harness):
    group, rung = _group(), _rung()
    broker = Broker(retry=True)  # a lower layer tries the same request twice
    port = Port()

    outcomes = await harness.run(harness.build(group, rung), broker, port)

    assert broker.sent == 1  # the second hook call raised: the retry sent nothing
    assert outcomes[0].result == "unverified"  # ...and the fence stays open
    assert port.released == []
    assert len(port.committed) == 1


async def test_i24_a_retry_of_the_same_fence_is_refused_at_the_hook(harness):
    send = mandate_send.SendFence(
        port=Port(),
        plan_id=7,
        seq=4,
        request=mf.FenceRequest(
            plan_id=7,
            tranche_index=1,
            proposal_id=uuid.uuid4(),
            approval_mode="human",
            notional=Decimal("300"),
            actual_account_ref="acct-1",
            now=NOW,
        ),
    )
    hook = send.hook(None)
    await hook()
    assert send.started is True
    with pytest.raises(PreSendFreshnessError, match="mandate_fence_single_post"):
        await hook()


async def test_a_clean_submit_keeps_the_fence_and_records_one_ack(harness):
    group, rung = _group(), _rung()
    service = harness.build(group, rung)
    broker = Broker()
    port = Port()

    outcomes = await harness.run(service, broker, port)

    assert broker.sent == 1
    assert outcomes[0].result == "submitted_acked"
    assert port.released == []
    assert port.boundary_calls == [4]  # the recheck at the edge named this fence


async def test_pre_send_recheck_refusal_sends_nothing_and_closes_the_fence(harness):
    """§4.6 POST 직전 재확인: a freeze seen at the edge -> no request, fence released."""
    group, rung = _group(), _rung()
    service = harness.build(group, rung)

    class Edge(Broker):
        async def __call__(self, **kwargs):
            if kwargs.get("dry_run") is True:
                return await super().__call__(**kwargs)
            await kwargs["pre_send_hook"]()  # raises: nothing is sent after this
            self.sent += 1
            return self.reply

    broker = Edge()
    port = Port(boundary="mandate_drawdown_frozen")

    outcomes = await harness.run(service, broker, port)

    assert broker.sent == 0
    assert outcomes[0] == RungOutcome(
        0, "guard_blocked", {"reason": "mandate_drawdown_frozen"}
    )
    assert port.released == [(7, 4, "pre_send_refused")]
    service.restore_rung_after_pre_send_block.assert_awaited_once()


async def test_an_explicit_rejection_closes_the_fence(harness):
    broker = Broker({"success": False, "error": "price out of band"})
    port = Port()

    outcomes = await harness.run(harness.build(_group(), _rung()), broker, port)

    assert broker.sent == 1
    assert outcomes[0].result == "error"
    assert port.released == [(7, 4, "explicit_rejection")]


@pytest.mark.parametrize(
    "reply",
    [
        {"success": True, "status": "weird"},  # ambiguous: no order id
        {"success": True},  # ambiguous: nothing
    ],
    ids=["unknown-status", "no-status"],
)
async def test_x12_an_ambiguous_reply_keeps_the_fence_open(harness, reply):
    broker = Broker(reply)
    port = Port()

    outcomes = await harness.run(harness.build(_group(), _rung()), broker, port)

    assert outcomes[0].result == "unverified"
    assert port.released == []  # only a proof closes a fence


async def test_an_exception_after_the_request_started_keeps_the_fence_open(harness):
    class Dies(Broker):
        async def __call__(self, **kwargs):
            if kwargs.get("dry_run") is True:
                return await super().__call__(**kwargs)
            await kwargs["pre_send_hook"]()
            self.sent += 1
            raise TimeoutError("response lost")

    broker = Dies()
    port = Port()

    outcomes = await harness.run(harness.build(_group(), _rung()), broker, port)

    assert broker.sent == 1
    assert outcomes[0].result == "unverified"
    assert port.released == []


async def test_an_exception_before_the_hook_is_not_a_proof_of_non_send(harness):
    """We cannot prove nothing was sent when the adapter never reached the hook."""
    broker = Broker(raise_before_hook=RuntimeError("adapter blew up"))
    port = Port()

    outcomes = await harness.run(harness.build(_group(), _rung()), broker, port)

    assert outcomes[0].result == "unverified"
    assert port.released == []


async def test_i15_the_callers_broker_account_id_is_not_an_input(harness):
    group = _group(broker_account_id="attacker-chosen")
    port = Port(account="acct-from-the-request")

    await harness.run(harness.build(group, _rung()), Broker(), port)

    assert port.committed[0].actual_account_ref == "acct-from-the-request"


async def test_a_plan_bound_proposal_must_be_a_single_buy_rung(harness):
    for group, rung in (
        (_group(action="cancel"), _rung()),
        (_group(), _rung(side="sell")),
    ):
        port = Port()
        broker = Broker()
        outcomes = await harness.run(harness.build(group, rung), broker, port)
        assert outcomes[0].result == "guard_blocked"
        assert broker.sent == 0 and port.committed == []


async def test_t4c_the_automatic_path_stays_closed_until_t5(harness):
    port = Port()
    broker = Broker()
    gate = AsyncMock()

    outcomes = await harness.run(
        harness.build(_group(), _rung()), broker, port, eligibility_gate=gate
    )

    assert outcomes == [
        RungOutcome(0, "approval_required", {"reason": "mandate_switch_off"})
    ]
    assert broker.sent == 0 and port.committed == []
    gate.assert_not_called()


async def test_an_untagged_proposal_never_touches_the_mandate_port(harness):
    """Acceptance 4: no plan, no fence, no extra reads — the old path exactly."""
    group = _group(mandate_plan_id=None, mandate_tranche_index=None)
    broker = Broker()
    port = Port()

    outcomes = await harness.run(harness.build(group, _rung()), broker, port)

    assert outcomes[0].result == "submitted_acked"
    assert broker.sent == 1
    assert port.committed == [] and port.released == [] and port.boundary_calls == []


async def test_i14_a_loss_sale_without_thesis_evidence_is_refused_at_the_edge(harness):
    group = _group(
        mandate_plan_id=None,
        mandate_tranche_index=None,
        exit_intent="loss_cut",
        side="sell",
    )
    rung = _rung(side="sell")
    service = harness.build(group, rung)
    service.mandate_loss_cut_refusal = AsyncMock(
        return_value="mandate_thesis_not_broken"
    )

    class Edge(Broker):
        async def __call__(self, **kwargs):
            if kwargs.get("dry_run") is True:
                return await super().__call__(**kwargs)
            await kwargs["pre_send_hook"]()
            self.sent += 1
            return self.reply

    broker = Edge()
    outcomes = await harness.run(service, broker, Port())

    assert broker.sent == 0
    assert outcomes[0] == RungOutcome(
        0, "guard_blocked", {"reason": "mandate_thesis_not_broken"}
    )


async def test_i14_a_loss_sale_with_no_plan_in_the_way_goes_through(harness):
    group = _group(
        mandate_plan_id=None, mandate_tranche_index=None, exit_intent="loss_cut"
    )
    rung = _rung(side="sell")
    broker = Broker()
    outcomes = await harness.run(harness.build(group, rung), broker, Port())
    assert broker.sent == 1
    assert outcomes[0].result == "submitted_acked"


# ------------------------------------------------------------- the pre-nonce check
async def test_callback_check_refuses_before_the_nonce_and_never_for_an_untagged_group():
    port = Port(boundary="mandate_tranche_uncertain")
    plan_bound = _group()
    assert (
        await mandate_send.pre_nonce_refusal(plan_bound, [_rung()], now=NOW, port=port)
        == "mandate_tranche_uncertain"
    )
    untagged = _group(mandate_plan_id=None, mandate_tranche_index=None)
    assert (
        await mandate_send.pre_nonce_refusal(untagged, [_rung()], now=NOW, port=port)
        is None
    )
    assert len(port.boundary_calls) == 1  # only the plan-bound group was asked


async def test_callback_check_cannot_be_overridden_by_a_failing_account_lookup():
    port = Port()

    async def broken(_group):
        raise RuntimeError("no account")

    port.account_ref_resolver = broken
    seen: dict[str, object] = {}

    async def boundary(request, **_):
        seen["account"] = request.actual_account_ref
        return "mandate_account_mismatch"

    port.boundary_refusal = boundary
    result = await mandate_send.pre_nonce_refusal(
        _group(), [_rung()], now=NOW, port=port
    )
    assert result == "mandate_account_mismatch"
    assert seen["account"] is None  # cannot compute -> the rules block it


async def test_fence_notional_is_the_larger_of_limit_and_current_price():
    rung = _rung()
    assert mandate_send.fence_notional(rung) == Decimal("300")
    assert mandate_send.fence_notional(rung, {"current_price": "110"}) == Decimal("330")
    assert mandate_send.fence_notional(rung, {"current_price": "90"}) == Decimal("300")
    assert mandate_send.fence_notional(_rung(limit_price=None)) is None
    assert mandate_send.fence_notional(_rung(quantity=Decimal("0"))) is None


async def test_the_send_port_defaults_are_the_real_fence_functions():
    port = mandate_send.MandateSendPort()
    assert port.commit_fence is mf.commit_send_fence
    assert port.release_fence is mf.release_send_fence
    assert port.boundary_refusal is mf.send_boundary_refusal


async def test_the_batch_and_single_approval_handlers_check_before_any_nonce_use():
    """T4a's denial lived in both handlers; its replacement must too, ahead of the nonce."""
    import inspect

    from app.services.order_proposals import telegram_callback as cb

    single = inspect.getsource(cb._handle_approve)
    batch = inspect.getsource(cb._handle_batch_approve)
    assert single.index("_mandate_pre_nonce_block(") < single.index("consume_")
    assert batch.index("_mandate_pre_nonce_block(") < batch.index("consume_")
