"""hk 5441 — the send boundary of an exit handoff card (design S5 d, O1, O3).

``exit_handoff_place_fn`` wraps only the live call of a card that was claimed
from a handoff. These tests drive it with a stand-in order function that calls
the pre-send hook the way the order layer does: last, immediately before the
request.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.order_proposals import exit_handoff_card
from app.services.order_proposals.exit_handoff_card import (
    CardProvenUnsent,
    card_unsent_reason,
    exit_handoff_place_fn,
)
from app.services.strategy_sleeves.exit_handoffs import ExitHandoffError

pytestmark = pytest.mark.unit

#: The claim the fake fence counts an attempt on.
CLAIM = 3


class _Order:
    def __init__(self, *, reach_hook: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.posted = False
        self.reach_hook = reach_hook

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs.get("dry_run") is not False:
            return {"success": True, "dry_run": True}
        if not self.reach_hook:
            return {"success": False, "error": "refused by a guard below"}
        hook = kwargs.get("pre_send_hook")
        if hook is not None:
            try:
                await hook()
            except PreSendFreshnessError as exc:
                return {
                    "success": False,
                    "pre_send_blocked": True,
                    "reason_codes": list(exc.reason_codes),
                }
        self.posted = True
        return {"success": True, "order_id": "uuid-1"}


class _Handoff:
    """Records what the guard asked of the handoff service."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fences = 0
        self.proven_unsent = 0
        self.fence_error: Exception | None = None
        self.refusal_before_post: str | None = None
        #: Whether the proven-unsent record commits.
        self.record_commits = True
        self.recorded_claims: list[int] = []
        self.events: list[str] = []

        async def fence_card_send(_factory, **_kwargs):
            self.events.append("fence")
            if self.fence_error is not None:
                raise self.fence_error
            self.fences += 1
            return CLAIM

        async def record_card_proven_unsent(_factory, **kwargs):
            self.events.append("proven_unsent")
            self.recorded_claims.append(kwargs["claim_count"])
            if not self.record_commits:
                return False
            self.proven_unsent += 1
            return True

        async def card_refusal(_session, **_kwargs):
            self.events.append("pre_post_check")
            return self.refusal_before_post

        from app.services.strategy_sleeves import exit_handoffs

        monkeypatch.setattr(exit_handoffs, "fence_card_send", fence_card_send)
        monkeypatch.setattr(
            exit_handoffs, "record_card_proven_unsent", record_card_proven_unsent
        )
        monkeypatch.setattr(exit_handoffs, "card_refusal", card_refusal)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _card(handoff_id: int | None = 5) -> Any:
    return SimpleNamespace(exit_handoff_id=handoff_id, proposal_id=uuid4())


def _wrapped(order: _Order, group: Any) -> Any:
    return exit_handoff_place_fn(
        group, order, session_factory=_Session, account_ref=lambda: "upbit:upbit:x"
    )


def test_a_proposal_that_is_not_a_handoff_card_keeps_its_place_fn() -> None:
    order = _Order()
    assert exit_handoff_place_fn(_card(None), order) is order
    assert exit_handoff_place_fn(SimpleNamespace(proposal_id=uuid4()), order) is order


@pytest.mark.asyncio
async def test_the_preview_is_not_fenced(monkeypatch: pytest.MonkeyPatch) -> None:
    handoff, order = _Handoff(monkeypatch), _Order()
    result = await _wrapped(order, _card())(dry_run=True, symbol="KRW-XRP")
    assert result == {"success": True, "dry_run": True}
    assert handoff.events == [] and order.calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_the_fence_commits_before_the_send_and_the_account_is_rechecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, order = _Handoff(monkeypatch), _Order()
    inner_calls: list[str] = []

    async def window_gate() -> None:
        inner_calls.append("window")

    result = await _wrapped(order, _card())(dry_run=False, pre_send_hook=window_gate)

    assert result == {"success": True, "order_id": "uuid-1"}
    assert handoff.events == ["fence", "pre_post_check"]
    assert inner_calls == ["window"] and order.posted is True
    assert handoff.proven_unsent == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ExitHandoffError("exit_handoff_claim_lost"),
        ExitHandoffError("exit_handoff_account_mismatch"),
        RuntimeError("database unavailable"),
    ],
)
async def test_without_a_committed_fence_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.fence_error = error

    result = await _wrapped(order, _card())(dry_run=False)

    assert order.calls == [] and order.posted is False
    # No proof: the refusal did not say that the card is provably unsent.
    assert not isinstance(result, CardProvenUnsent)
    assert result["success"] is False and result["pre_send_blocked"] is True
    # Nothing was counted, so there is nothing to mark as proven unsent.
    assert handoff.proven_unsent == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["exit_handoff_buy_in_flight", "exit_handoff_account_mismatch"]
)
async def test_a_fence_that_refused_with_nothing_unexplained_is_the_proof(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """PR 2486 review (65wJD0): the fence saw, under its locks, that this card
    holds the claim and that none of its attempts is unexplained, and refused
    without counting. The card has provably not posted, and the send says so
    with the claim it was bound to instead of an ordinary failed result."""
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.fence_error = ExitHandoffError(code, unsent_claim_count=7)
    card = _card()

    result = await _wrapped(order, card)(dry_run=False)

    assert order.calls == [] and order.posted is False
    assert result == CardProvenUnsent(
        handoff_id=5, proposal_id=card.proposal_id, claim_count=7, reason=code
    )
    assert handoff.events == ["fence"]
    assert card_unsent_reason(card, result) == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["exit_handoff_prior_attempt_unresolved", "exit_handoff_claim_lost"]
)
async def test_a_fence_refusal_that_proves_nothing_is_an_ordinary_result(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """An unexplained earlier attempt or a lost claim says nothing about whether
    the card posted: the approval classifies it as before, by looking."""
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.fence_error = ExitHandoffError(code)
    card = _card()

    result = await _wrapped(order, card)(dry_run=False)

    assert order.posted is False
    assert result["reason_codes"] == [code]
    assert card_unsent_reason(card, result) is None


@pytest.mark.asyncio
async def test_a_changed_account_right_before_the_post_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.refusal_before_post = "exit_handoff_account_mismatch"
    card = _card()

    result = await _wrapped(order, card)(dry_run=False)

    assert order.posted is False
    assert handoff.events == ["fence", "pre_post_check", "proven_unsent"]
    # The counted attempt was recorded as unsent on the claim it was counted on.
    assert handoff.recorded_claims == [CLAIM]
    assert result == CardProvenUnsent(
        handoff_id=5,
        proposal_id=card.proposal_id,
        claim_count=CLAIM,
        reason="exit_handoff_account_mismatch",
    )


@pytest.mark.asyncio
async def test_an_attempt_whose_non_send_could_not_be_recorded_is_no_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook stopped the send, but the record did not commit: the attempt
    stays counted as possibly sent, so nothing may be concluded from it."""
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.refusal_before_post = "exit_handoff_account_mismatch"
    handoff.record_commits = False
    card = _card()

    result = await _wrapped(order, card)(dry_run=False)

    assert order.posted is False and handoff.proven_unsent == 0
    assert not isinstance(result, CardProvenUnsent)
    assert result["reason_codes"] == ["exit_handoff_account_mismatch"]
    assert card_unsent_reason(card, result) is None


@pytest.mark.asyncio
async def test_an_error_below_the_hook_is_the_proof_only_once_it_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = _Handoff(monkeypatch)

    async def fails_before_the_hook(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("balance read failed")

    card = _card()
    result = await _wrapped(fails_before_the_hook, card)(dry_run=False)  # type: ignore[arg-type]
    assert result == CardProvenUnsent(
        handoff_id=5,
        proposal_id=card.proposal_id,
        claim_count=CLAIM,
        reason="balance read failed",
    )

    handoff.record_commits = False
    with pytest.raises(RuntimeError, match="balance read failed"):
        await _wrapped(fails_before_the_hook, _card())(dry_run=False)  # type: ignore[arg-type]


def test_a_proof_for_another_card_is_not_this_cards_proof() -> None:
    card = _card()
    other_proposal = CardProvenUnsent(5, uuid4(), CLAIM, "exit_handoff_buy_in_flight")
    other_handoff = CardProvenUnsent(
        6, card.proposal_id, CLAIM, "exit_handoff_buy_in_flight"
    )

    assert card_unsent_reason(card, other_proposal) is None
    assert card_unsent_reason(card, other_handoff) is None
    # A result that merely looks like one is not one either.
    assert card_unsent_reason(card, {"exit_handoff_proven_unsent": True}) is None


@pytest.mark.asyncio
async def test_a_window_block_in_the_inner_hook_is_recorded_as_proven_unsent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, order = _Handoff(monkeypatch), _Order()

    async def closed_window() -> None:
        raise PreSendFreshnessError(("approval_window:defer_session_closed",))

    await _wrapped(order, _card())(dry_run=False, pre_send_hook=closed_window)

    assert order.posted is False
    assert handoff.events == ["fence", "proven_unsent"]


@pytest.mark.asyncio
async def test_a_guard_below_that_refuses_before_the_hook_is_proven_unsent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, order = _Handoff(monkeypatch), _Order(reach_hook=False)
    await _wrapped(order, _card())(dry_run=False)
    assert handoff.events == ["fence", "proven_unsent"]


@pytest.mark.asyncio
async def test_an_error_after_the_post_leaves_the_attempt_counted_as_possibly_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = _Handoff(monkeypatch)

    async def posts_then_fails(**kwargs: Any) -> dict[str, Any]:
        await kwargs["pre_send_hook"]()
        raise RuntimeError("connection reset after the request was written")

    with pytest.raises(RuntimeError):
        await _wrapped(posts_then_fails, _card())(dry_run=False)  # type: ignore[arg-type]

    # O1: the count stays above the proven-unsent count, so the handoff holds.
    assert handoff.fences == 1 and handoff.proven_unsent == 0


@pytest.mark.asyncio
async def test_an_unreadable_pre_post_check_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, order = _Handoff(monkeypatch), _Order()

    async def broken(_session, **_kwargs):
        raise RuntimeError("read failed")

    from app.services.strategy_sleeves import exit_handoffs

    monkeypatch.setattr(exit_handoffs, "card_refusal", broken)
    result = await _wrapped(order, _card())(dry_run=False)

    assert order.posted is False
    assert isinstance(result, CardProvenUnsent)
    assert result.reason == "exit_handoff_check_unavailable"
    assert handoff.proven_unsent == 1


def test_the_refused_result_has_the_shape_the_order_layer_uses() -> None:
    result = exit_handoff_card._refused_result("exit_handoff_claim_lost")
    assert result["success"] is False and result["pre_send_blocked"] is True
    assert result["dry_run"] is False


def test_revalidation_wraps_every_place_rung_through_this_module() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3]
        / "app/services/order_proposals/revalidation.py"
    ).read_text("utf-8")
    body = source[source.index("async def _revalidate_place_rung(") :]
    wrap = body.index("place_order_fn = exit_handoff_place_fn(group, place_order_fn)")
    assert wrap < body.index('new_state="revalidating"')
    assert wrap < body.index("dry_run=False")


class _RungService:
    """Records the rung writes ``_revalidate_place_rung`` makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def transition_rung(self, _proposal_id, _rung_index, *, new_state):
        self.calls.append(("transition", {"new_state": new_state}))

    async def record_rejected(self, _proposal_id, _rung_index, **kwargs):
        self.calls.append(("rejected", kwargs))

    async def record_unverified(self, _proposal_id, _rung_index, **kwargs):
        self.calls.append(("unverified", kwargs))


async def _approve_card_rung(
    monkeypatch: pytest.MonkeyPatch, order: _Order, lookups: list[str]
) -> tuple[Any, _RungService]:
    """Run the real ``_revalidate_place_rung`` for a handoff card's one rung.

    The window and lifecycle gates are passed (they have their own tests); the
    place function is the real wrapper around ``order``; the identifier lookup
    is a broker that cannot be reached.
    """
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.services.order_proposals import revalidation
    from app.services.order_proposals.broker_gateway import SubmitEvidence
    from app.services.strategy_sleeves import account_binding

    async def passed(**_kwargs: Any) -> None:
        return None

    class _WindowGate:
        blocked_decision = None

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __call__(self) -> None:
            return None

    monkeypatch.setattr(revalidation, "_pre_mutation_window_gate", passed)
    monkeypatch.setattr(revalidation, "_pre_submit_lifecycle_gate", passed)
    monkeypatch.setattr(revalidation, "ApprovalWindowPreSendGate", _WindowGate)
    # The wrapper's default session factory, which it imports when it is built.
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _Session)
    monkeypatch.setattr(
        account_binding, "configured_upbit_account_ref", lambda: "upbit:upbit:x"
    )

    async def unreachable(**kwargs: Any) -> SubmitEvidence:
        lookups.append(kwargs["identifier"])
        return SubmitEvidence("unknown", reason="broker unreachable")

    group = SimpleNamespace(
        exit_handoff_id=5,
        proposal_id=uuid4(),
        account_mode="upbit",
        market="crypto",
        symbol="KRW-XRP",
        order_type="limit",
        thesis="card",
        strategy=None,
        exit_intent=None,
        exit_reason=None,
        retrospective_id=None,
        approval_issue_id=None,
        decision_id=None,
        source_asof={},
    )
    rung = SimpleNamespace(
        rung_index=0, side="sell", quantity=Decimal("40"), limit_price=Decimal("3000")
    )
    service = _RungService()
    now = datetime(2026, 11, 20, 5, 20, tzinfo=UTC)

    async def place(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("dry_run") is not False:
            return {
                "success": True,
                "price": kwargs["price"],
                "quantity": kwargs["quantity"],
            }
        return await order(**kwargs)

    outcome = await revalidation._revalidate_place_rung(
        service=service,  # type: ignore[arg-type]
        group=group,  # type: ignore[arg-type]
        rung=rung,  # type: ignore[arg-type]
        now=now,
        place_order_fn=place,
        correlation_mint=lambda **_kwargs: "corr-1",
        fetch_submit_evidence_fn=unreachable,
        buying_power_claimer=None,  # type: ignore[arg-type]
        buying_power_releaser=None,  # type: ignore[arg-type]
        eligibility_gate=None,
        window_evaluator=None,  # type: ignore[arg-type]
        expected_policy_stamp="stamp",
        now_fn=lambda: now,
        parking_sell_auto_enabled=False,
    )
    return outcome, service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["exit_handoff_buy_in_flight", "exit_handoff_account_mismatch"]
)
async def test_the_approval_ends_a_provably_unsent_card_without_asking_the_broker(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """PR 2486 review (65wJD0, 65t2wx): the fence refused with nothing counted
    and nothing unexplained, and the broker cannot be reached. The rung ends
    ``rejected`` with no idempotency key -- the look of a card that was never
    sent, which the handoff's settle step releases -- and no lookup is made.
    Before, the refusal was classified by looking: an unreachable broker left
    the rung ``unverified`` with a key, and the handoff claimed for good."""
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.fence_error = ExitHandoffError(code, unsent_claim_count=1)
    lookups: list[str] = []

    outcome, service = await _approve_card_rung(monkeypatch, order, lookups)

    assert order.posted is False
    assert lookups == []
    assert [name for name, _ in service.calls if name != "transition"] == ["rejected"]
    rejected = dict(service.calls)["rejected"]
    assert rejected["reason"] == code and "idempotency_key" not in rejected
    assert outcome.result == "error" and outcome.detail["error"] == code


@pytest.mark.asyncio
async def test_the_approval_still_looks_when_the_refusal_proves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier attempt of this card is unexplained: it may be at the broker.
    The refusal is classified as before, by the identifier lookup, and with the
    broker unreachable the rung is ``unverified``, never ended."""
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.fence_error = ExitHandoffError("exit_handoff_prior_attempt_unresolved")
    lookups: list[str] = []

    outcome, service = await _approve_card_rung(monkeypatch, order, lookups)

    assert order.posted is False
    assert len(lookups) == 1
    assert [name for name, _ in service.calls if name != "transition"] == ["unverified"]
    assert outcome.result == "unverified"


@pytest.mark.asyncio
async def test_a_card_whose_request_went_out_is_classified_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook returned and the order layer then failed: the request may have
    been made. No proof exists, so the classifier looks."""
    _Handoff(monkeypatch)

    async def posts_then_fails(**kwargs: Any) -> dict[str, Any]:
        await kwargs["pre_send_hook"]()
        return {"success": False, "error": "timeout after the request"}

    lookups: list[str] = []
    outcome, service = await _approve_card_rung(
        monkeypatch,
        posts_then_fails,  # type: ignore[arg-type]
        lookups,
    )

    assert len(lookups) == 1
    assert outcome.result == "unverified"
    assert "rejected" not in dict(service.calls)


def test_revalidation_ends_the_rung_on_the_proof_before_it_asks_the_broker() -> None:
    """The proof is consumed after the window-block check, which restores the
    card rather than ending it, and before ``_classify_submit``, whose
    identifier lookup turns an unavailable broker into an ``unverified`` rung.
    The behaviour itself is exercised through the real approval callback in
    ``callback_inbox/test_exit_handoff_card_dead_letter.py`` (CI)."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3]
        / "app/services/order_proposals/revalidation.py"
    ).read_text("utf-8")
    body = source[source.index("async def _revalidate_place_rung(") :]
    body = body[: body.index("\nasync def _record_exit_plan_after_accepted_buy(")]
    live = body.index("dry_run=False")
    consumed = body.index("unsent_reason = card_unsent_reason(group, submit)")
    window_blocks = [
        index
        for index in range(len(body))
        if body.startswith("if transport_gate.blocked_decision is not None:", index)
    ]
    assert len(window_blocks) == 2
    assert live < window_blocks[-1] < consumed
    after = body[consumed:]
    assert after.index("await service.record_rejected(") < after.index(
        "outcome = await _classify_submit("
    )
    # It is the last ``_classify_submit`` of the function: nothing classifies
    # this card's submit result without passing the proof check first.
    assert body.count("_classify_submit(") == 2
    assert body.index("_classify_submit(") < window_blocks[-1]


def test_dispatch_never_auto_approves_a_handoff_card() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "app/services/order_proposals/dispatch.py"
    ).read_text("utf-8")
    gate = source.index("SleeveGateReason.EXIT_HANDOFF_CARD")
    assert source.rindex('getattr(group, "exit_handoff_id", None) is not None', 0, gate)
    assert gate < source.index(
        "daily_notional = await service.auto_approved_daily_notional"
    )
    # The card is not published when its claim or its account no longer holds.
    assert (
        "handoff_refusal = await _exit_handoff_card_refusal(session, group)" in source
    )
