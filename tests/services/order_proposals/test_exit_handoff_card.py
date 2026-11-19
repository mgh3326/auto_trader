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
from app.services.order_proposals.exit_handoff_card import exit_handoff_place_fn
from app.services.strategy_sleeves.exit_handoffs import ExitHandoffError

pytestmark = pytest.mark.unit


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
        self.events: list[str] = []

        async def fence(self_, _factory=None, **_kw):  # noqa: ANN001
            raise AssertionError("unused")

        async def fence_card_send(_factory, **_kwargs):
            self.events.append("fence")
            if self.fence_error is not None:
                raise self.fence_error
            self.fences += 1

        async def record_card_proven_unsent(_factory, **_kwargs):
            self.events.append("proven_unsent")
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
    assert result["success"] is False and result["pre_send_blocked"] is True
    # Nothing was counted, so there is nothing to mark as proven unsent.
    assert handoff.proven_unsent == 0


@pytest.mark.asyncio
async def test_a_changed_account_right_before_the_post_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, order = _Handoff(monkeypatch), _Order()
    handoff.refusal_before_post = "exit_handoff_account_mismatch"

    result = await _wrapped(order, _card())(dry_run=False)

    assert order.posted is False
    assert result["reason_codes"] == ["exit_handoff_account_mismatch"]
    assert handoff.events == ["fence", "pre_post_check", "proven_unsent"]


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
    assert result["reason_codes"] == ["exit_handoff_check_unavailable"]
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
