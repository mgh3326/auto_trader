"""I14 reader and the account reference, without a database (hk 3795 T4c)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.mandate import loss_guard
from app.services.mandate.account_ref import derive_account_ref

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


EVENT_AT = datetime(2026, 11, 20, 12, tzinfo=UTC)
RETRO_AT = datetime(2026, 11, 20, 11, tzinfo=UTC)


def _ev(event: str, seq: int, evidence=None, thesis_seq=None):
    return SimpleNamespace(
        event=event,
        seq=seq,
        evidence=evidence or {},
        thesis_seq=thesis_seq,
        created_at=EVENT_AT,
    )


EVIDENCE = {"retrospective_id": 42, "approval_issue_id": "hk:9"}


async def test_i14_a_plan_without_thesis_broken_has_no_matching_evidence():
    events = [_ev("proposed", 1), _ev("approved", 2), _ev("retired", 3)]
    assert not loss_guard.evidence_matches(
        events, retrospective_id=42, approval_issue_id="hk:9"
    )


async def test_i14_both_ids_must_come_from_the_thesis_broken_event():
    events = [_ev("proposed", 1), _ev("thesis_broken", 2, EVIDENCE)]
    ok = loss_guard.evidence_matches(
        events, retrospective_id="42", approval_issue_id="hk:9"
    )
    assert ok  # ids compare as text: the proposal carries an int and a str
    assert not loss_guard.evidence_matches(
        events, retrospective_id=43, approval_issue_id="hk:9"
    )
    assert not loss_guard.evidence_matches(
        events, retrospective_id=42, approval_issue_id="hk:other"
    )
    assert not loss_guard.evidence_matches(
        events, retrospective_id=None, approval_issue_id=None
    )


async def test_i14_ids_must_be_from_one_event_not_mixed_across_events():
    broken = _ev("thesis_broken", 2, {"retrospective_id": 42, "approval_issue_id": "A"})
    refresh = _ev(
        "thesis_evidence_refreshed",
        3,
        {"retrospective_id": 77, "approval_issue_id": "B"},
        thesis_seq=2,
    )
    events = [broken, refresh]
    assert loss_guard.evidence_matches(
        events, retrospective_id=77, approval_issue_id="B"
    )
    assert not loss_guard.evidence_matches(
        events, retrospective_id=42, approval_issue_id="B"
    )


async def test_i14_f_a_refresh_counts_only_for_the_thesis_event_it_points_at():
    broken = _ev("thesis_broken", 2, EVIDENCE)
    stray = _ev(
        "thesis_evidence_refreshed",
        4,
        {"retrospective_id": 99, "approval_issue_id": "hk:new"},
        thesis_seq=3,  # not the thesis_broken event
    )
    assert not loss_guard.evidence_matches(
        [broken, stray], retrospective_id=99, approval_issue_id="hk:new"
    )
    pointing = _ev(
        "thesis_evidence_refreshed",
        4,
        {"retrospective_id": 99, "approval_issue_id": "hk:new"},
        thesis_seq=2,
    )
    assert loss_guard.evidence_matches(
        [broken, pointing], retrospective_id=99, approval_issue_id="hk:new"
    )


async def test_i14_h_a_retired_plan_is_judged_by_the_same_evidence(monkeypatch):
    events = [
        _ev("proposed", 1),
        _ev("thesis_broken", 2, EVIDENCE),
        _ev("retired", 3),
        _ev(
            "thesis_evidence_refreshed",
            4,
            {"retrospective_id": 100, "approval_issue_id": "hk:r"},
            thesis_seq=2,
        ),
    ]

    async def plans(session, *, market, symbol):
        return [11]

    async def history(session, plan_id):
        return events

    async def created(session, retrospective_id):
        return RETRO_AT

    monkeypatch.setattr(loss_guard, "unreleased_live_plan_ids", plans)
    monkeypatch.setattr(loss_guard.store, "list_plan_events", history)
    monkeypatch.setattr(loss_guard, "_retrospective_created_at", created)

    refused = await loss_guard.loss_cut_refusal(
        None,
        market="equity_kr",
        symbol="005930",
        retrospective_id=42,
        approval_issue_id="x",
    )
    allowed = await loss_guard.loss_cut_refusal(
        None,
        market="equity_kr",
        symbol="005930",
        retrospective_id=100,
        approval_issue_id="hk:r",
    )
    assert refused == "mandate_thesis_not_broken"
    assert allowed is None


async def test_i14_g_no_unreleased_live_plan_means_no_effect_and_no_event_read(
    monkeypatch,
):
    async def plans(session, *, market, symbol):
        return []

    async def history(session, plan_id):
        raise AssertionError("no plan, no events")

    monkeypatch.setattr(loss_guard, "unreleased_live_plan_ids", plans)
    monkeypatch.setattr(loss_guard.store, "list_plan_events", history)

    assert (
        await loss_guard.loss_cut_refusal(
            None,
            market="equity_kr",
            symbol="005930",
            retrospective_id=1,
            approval_issue_id="x",
        )
        is None
    )


async def test_i14_e_creation_takes_the_symbol_lock_of_the_live_realm_first(
    monkeypatch,
):
    order: list[str] = []

    async def lock(session, realm, market, symbol):
        order.append(f"lock:{realm}:{market}:{symbol}")

    async def plans(session, *, market, symbol):
        order.append("read")
        return []

    monkeypatch.setattr(loss_guard, "acquire_symbol_lock", lock)
    monkeypatch.setattr(loss_guard, "unreleased_live_plan_ids", plans)

    await loss_guard.loss_cut_refusal(
        None,
        market="equity_kr",
        symbol="005930",
        retrospective_id=1,
        approval_issue_id="x",
        lock_symbol=True,
    )
    assert order == ["lock:live:equity_kr:005930", "read"]


async def test_i14_the_guard_does_not_catch_a_read_failure_the_caller_fails_closed(
    monkeypatch,
):
    async def plans(session, *, market, symbol):
        raise RuntimeError("db down")

    monkeypatch.setattr(loss_guard, "unreleased_live_plan_ids", plans)
    with pytest.raises(RuntimeError):
        await loss_guard.loss_cut_refusal(
            None,
            market="equity_kr",
            symbol="005930",
            retrospective_id=1,
            approval_issue_id="x",
        )


async def test_the_service_turns_an_unreadable_plan_into_a_refusal(monkeypatch):
    from app.services.order_proposals.service import OrderProposalsService

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(loss_guard, "loss_cut_refusal", boom)
    service = OrderProposalsService(session=SimpleNamespace())  # type: ignore[arg-type]
    group = SimpleNamespace(
        exit_intent="loss_cut",
        market="equity_kr",
        symbol="005930",
        retrospective_id=1,
        approval_issue_id="x",
    )
    assert await service.mandate_loss_cut_refusal(group) == "mandate_unreadable"
    plain = SimpleNamespace(exit_intent=None)
    assert await service.mandate_loss_cut_refusal(plain) is None  # never looked at


async def test_account_ref_is_opaque_stable_and_mode_bound():
    a = derive_account_ref("kis_live", "12345678-01", secret="s")
    assert a is not None and a.startswith("acct_") and "12345678" not in a
    assert a == derive_account_ref("kis_live", " 12345678-01 ", secret="s")
    assert a != derive_account_ref("toss_live", "12345678-01", secret="s")
    assert a != derive_account_ref("kis_live", "12345678-02", secret="s")
    assert a != derive_account_ref("kis_live", "12345678-01", secret="other")
    assert derive_account_ref("kis_live", "", secret="s") is None
    assert derive_account_ref("kis_live", "x", secret="") is None


async def test_i14_i_the_cited_retrospective_must_still_exist_when_read(monkeypatch):
    """Ordering is enforced when the event is written (store); the reader needs the row."""
    events = [_ev("proposed", 1), _ev("thesis_broken", 2, EVIDENCE)]

    async def plans(session, *, market, symbol):
        return [11]

    async def history(session, plan_id):
        return events

    monkeypatch.setattr(loss_guard, "unreleased_live_plan_ids", plans)
    monkeypatch.setattr(loss_guard.store, "list_plan_events", history)
    args = {
        "market": "equity_kr",
        "symbol": "005930",
        "retrospective_id": 42,
        "approval_issue_id": "hk:9",
    }
    for lookup, expected in (
        (_lookup(None), "mandate_thesis_not_broken"),
        (_lookup(RETRO_AT), None),
    ):
        monkeypatch.setattr(loss_guard, "_retrospective_created_at", lookup)
        assert await loss_guard.loss_cut_refusal(None, **args) == expected


def _lookup(value):
    async def lookup(session, retrospective_id):
        return value

    return lookup


async def test_i14_i_the_event_writer_refuses_a_retrospective_that_does_not_exist_yet():
    """The write-time check (a visibility check, not a transaction-start timestamp)."""
    import inspect

    from app.services.mandate import store

    source = inspect.getsource(store.append_plan_event)
    assert '"thesis_broken", "thesis_evidence_refreshed"' in source
    assert source.index("_require_cited_retrospective(") < source.index("apply_event(")
