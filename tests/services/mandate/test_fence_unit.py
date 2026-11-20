"""The send-fence transaction and its lock set, without a database (hk 3795 T4c).

A fake session records what the fence does. The lock order, the event kinds the module
may write, the absence of caller-side locks and the never-raises / never-sends contract
are pinned here; the racing-session behaviour (X5..X13, X15, X16) is in
``test_fence_db.py`` on the CI database.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.mandate import fence, fence_locks
from app.services.mandate import vocabulary as v

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NOW = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self, *, commit_error: Exception | None = None) -> None:
        self.commit_error = commit_error
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True


def _request(**over) -> fence.FenceRequest:
    base = {
        "plan_id": 1,
        "tranche_index": 1,
        "proposal_id": uuid.uuid4(),
        "approval_mode": "human",
        "notional": Decimal("300"),
        "actual_account_ref": "acct-1",
        "now": NOW,
    }
    base.update(over)
    return fence.FenceRequest(**base)


def _factory(*sessions: FakeSession):
    queue = list(sessions)
    return lambda: queue.pop(0)


async def test_x11_a_transaction_that_dies_before_commit_writes_no_fence(monkeypatch):
    async def dies(session, req, freeze_fn):
        raise RuntimeError("connection lost mid transaction")

    monkeypatch.setattr(fence, "_fence_once", dies)
    session = FakeSession()

    outcome = await fence.commit_send_fence(
        _request(), session_factory=_factory(session)
    )

    assert outcome.fenced is False
    assert outcome.refusal == v.MandateRefusal.FENCE_UNAVAILABLE.value
    assert session.commits == 0 and session.rollbacks == 1 and session.closed


async def test_x12_an_unconfirmed_commit_sends_nothing(monkeypatch):
    async def writes(session, req, freeze_fn):
        return fence.FenceOutcome(True, seq=5, attempt_id=req.attempt_id)

    monkeypatch.setattr(fence, "_fence_once", writes)
    session = FakeSession(commit_error=ConnectionError("commit response lost"))

    outcome = await fence.commit_send_fence(
        _request(), session_factory=_factory(session)
    )

    assert outcome.fenced is False  # the caller must not start the broker call
    assert outcome.refusal == v.MandateRefusal.FENCE_UNAVAILABLE.value
    assert outcome.detail == {"why": "commit_unconfirmed"}


async def test_a_committed_fence_is_the_only_thing_that_reports_fenced(monkeypatch):
    async def writes(session, req, freeze_fn):
        return fence.FenceOutcome(True, seq=5, attempt_id=req.attempt_id)

    monkeypatch.setattr(fence, "_fence_once", writes)
    session = FakeSession()

    outcome = await fence.commit_send_fence(
        _request(), session_factory=_factory(session)
    )

    assert outcome.fenced is True and outcome.seq == 5
    assert session.commits == 1


async def test_a_refusal_rolls_back_and_carries_its_closed_code(monkeypatch):
    async def refuses(session, req, freeze_fn):
        raise fence._Refuse(v.MandateRefusal.DRAWDOWN_FROZEN.value)

    monkeypatch.setattr(fence, "_fence_once", refuses)
    session = FakeSession()

    outcome = await fence.commit_send_fence(
        _request(), session_factory=_factory(session)
    )

    assert (outcome.fenced, outcome.refusal) == (False, "mandate_drawdown_frozen")
    assert outcome.blocking is True
    assert session.commits == 0 and session.rollbacks == 1


async def test_a_lost_seq_race_is_judged_once_more_then_gives_up(monkeypatch):
    calls = {"n": 0}

    async def races(session, req, freeze_fn):
        calls["n"] += 1
        raise fence._SeqRace

    monkeypatch.setattr(fence, "_fence_once", races)
    sessions = [FakeSession(), FakeSession()]

    outcome = await fence.commit_send_fence(
        _request(), session_factory=_factory(*sessions)
    )

    assert calls["n"] == fence.MAX_ATTEMPTS == 2
    assert outcome.refusal == v.MandateRefusal.FENCE_UNAVAILABLE.value
    assert all(s.commits == 0 and s.closed for s in sessions)


async def test_a_seq_race_that_clears_on_the_second_read_writes_one_fence(monkeypatch):
    calls = {"n": 0}

    async def second_time_lucky(session, req, freeze_fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fence._SeqRace
        return fence.FenceOutcome(True, seq=9, attempt_id=req.attempt_id)

    monkeypatch.setattr(fence, "_fence_once", second_time_lucky)
    sessions = [FakeSession(), FakeSession()]

    outcome = await fence.commit_send_fence(
        _request(), session_factory=_factory(*sessions)
    )

    assert outcome.fenced is True
    assert sum(s.commits for s in sessions) == 1


async def test_an_unknown_approval_mode_is_refused_before_any_session(monkeypatch):
    def no_session():
        raise AssertionError("no session may be opened")

    outcome = await fence.commit_send_fence(
        _request(approval_mode="semi"), session_factory=no_session
    )
    assert outcome.refusal == v.MandateRefusal.INVALID_FIELDS.value


async def test_release_is_only_for_a_proven_unsent_or_an_explicit_rejection():
    assert set(v.RELEASE_REASONS) == {
        "pre_send_refused",
        "explicit_rejection",
        "pre_post_failure",
    }

    def no_session():
        raise AssertionError("a refused reason opens no session")

    for reason in ("timeout", "ambiguous", "unverified", "process_death", ""):
        out = await fence.release_send_fence(
            1, 2, reason_code=reason, session_factory=no_session
        )
        assert (out.released, out.reason) == (False, "reason_not_allowed")


async def test_a_recheck_that_cannot_read_refuses(monkeypatch):
    async def unreadable(session, plan_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(fence, "_load_identity", unreadable)
    session = FakeSessionWithExecute()

    code = await fence.send_boundary_refusal(
        _request(), session_factory=lambda: session
    )

    assert code == v.MandateRefusal.UNREADABLE.value


class FakeSessionWithExecute(FakeSession):
    async def execute(self, *_a, **_k):
        return None


# --------------------------------------------------------------- structure
def _calls_in_order(func) -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            names.append((node.lineno, node.col_offset, name))
    return [n for _l, _c, n in sorted(names)]


async def test_the_fence_takes_f1_then_f2_then_f3s_in_that_order():
    order = _calls_in_order(fence._take_fence_locks)
    assert (
        order.index("acquire_freeze_shared")
        < order.index("lock_authority_shared")
        < order.index("lock_mutex")
    )
    assert fence_locks.FENCE_LOCK_ORDER == (
        fence_locks.FenceLock.FREEZE,
        fence_locks.FenceLock.AUTHORITY,
        fence_locks.FenceLock.SLEEVE,
        fence_locks.FenceLock.BASKET_DAY,
        fence_locks.FenceLock.GLOBAL_DAY,
    )
    assert list(fence_locks.FENCE_LOCK_ORDER) == sorted(fence_locks.FENCE_LOCK_ORDER)


async def test_a_fence_reads_facts_only_after_every_lock_it_takes():
    order = _calls_in_order(fence._fence_once)
    last_lock = max(
        i
        for i, name in enumerate(order)
        if name in {"_take_fence_locks", "lock_mutex", "basket_day_mutex_key"}
    )
    assert last_lock < order.index("gather_send_facts")
    assert order.index("_take_fence_locks") < order.index("lock_mutex")
    # F3 (basket day) before F4 (global day), both before the first read
    keys = [n for n in order if n.endswith("_day_mutex_key")]
    assert keys == ["basket_day_mutex_key", "global_day_mutex_key"]


async def test_the_release_writer_takes_f3s_before_the_day_locks():
    order = _calls_in_order(fence._release_once)
    mutex_keys = [n for n in order if n.endswith("mutex_key")]
    assert mutex_keys == [
        "sleeve_mutex_key",
        "basket_day_mutex_key",
        "global_day_mutex_key",
    ]


async def test_the_freeze_lock_keys_are_global_then_basket():
    assert fence_locks.freeze_lock_keys("live", "b1") == (
        "mandate_freeze:global",
        "mandate_freeze:live:b1",
    )
    with pytest.raises(ValueError):
        fence_locks.freeze_lock_keys("paper", "b1")


async def test_the_fence_holds_no_caller_side_lock_and_writes_two_event_kinds_only():
    """§4.6: F1..F4 and nothing else; the fence never goes through the event writer
    that takes L3, and the only events it inserts are the fence and its release."""
    source = inspect.getsource(fence)
    for forbidden in (
        "acquire_sleeve_lock",
        "acquire_in_order",
        "acquire_basket_lock",
        "acquire_symbol_lock",
        "append_plan_event",
        "pg_advisory_xact_lock(",
    ):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    written: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "MandatePlanEvent"
        ):
            for kw in node.keywords:
                if kw.arg == "event" and isinstance(kw.value, ast.Constant):
                    written.add(kw.value.value)
    assert written == {"tranche_send_fenced", "tranche_send_released"}


async def test_the_mutex_and_authority_keys_are_stable_and_validated():
    from datetime import date

    day = date(2026, 11, 23)
    assert fence_locks.sleeve_mutex_key(7) == "sleeve:7"
    assert (
        fence_locks.basket_day_mutex_key("live", "b", day) == "basket:live:b:2026-11-23"
    )
    assert (
        fence_locks.global_day_mutex_key("kis_live", "equity_kr", day)
        == "global:kis_live:equity_kr:2026-11-23"
    )
    with pytest.raises(ValueError):
        fence_locks.basket_day_mutex_key("nope", "b", day)


async def test_the_fence_modules_import_no_order_proposal_or_broker_module():
    """The decision and writer side (§4.6) stay below the send path: only
    ``order_proposals/mandate_send.py`` connects them, and it lives on the other side."""
    from pathlib import Path

    import app.services.mandate as pkg

    root = Path(pkg.__file__).parent
    for name in (
        "fence",
        "fence_rules",
        "fence_locks",
        "loss_guard",
        "account_ref",
        "buy_freeze",
    ):
        tree = ast.parse((root / f"{name}.py").read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            for module in modules:
                assert not module.startswith(
                    (
                        "app.services.order_proposals",
                        "app.services.brokers",
                        "app.mcp_server",
                    )
                ), (name, module)


async def test_a_release_that_loses_a_seq_race_is_tried_again(monkeypatch):
    """A fence known never to have sent is closed on a retry, not left open (review r1)."""
    calls = {"n": 0}

    async def flaky(plan_id, fenced_seq, *, reason_code, session_factory=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return fence.ReleaseOutcome(False, "release_failed")
        return fence.ReleaseOutcome(True, reason_code)

    monkeypatch.setattr(fence, "_release_once", flaky)
    out = await fence.release_send_fence(1, 2, reason_code="pre_send_refused")
    assert out.released is True and calls["n"] == 3

    async def always(plan_id, fenced_seq, *, reason_code, session_factory=None):
        calls["n"] += 1
        return fence.ReleaseOutcome(False, "release_failed")

    calls["n"] = 0
    monkeypatch.setattr(fence, "_release_once", always)
    out = await fence.release_send_fence(1, 2, reason_code="pre_send_refused")
    assert out.released is False and calls["n"] == fence.MAX_RELEASE_ATTEMPTS


async def test_i11_the_freeze_is_judged_on_a_clock_sampled_after_the_locks(monkeypatch):
    """A measurement that expired while the fence waited cannot authorize it."""
    from datetime import timedelta
    from types import SimpleNamespace

    from app.services.mandate.buy_freeze import FreezeVerdict

    valid_until = NOW + timedelta(minutes=1)

    async def verdict(session, **_kw):
        return FreezeVerdict(False, "fixture", {}, valid_until=valid_until)

    sleeve = SimpleNamespace(account_mode="kis_live", market="equity_kr", id=1)
    early = await fence._read_freeze(None, sleeve, lambda: NOW, verdict)  # type: ignore[arg-type]
    late = await fence._read_freeze(
        None,
        sleeve,
        lambda: NOW + timedelta(minutes=2),
        verdict,  # type: ignore[arg-type]
    )
    assert (early, late) == (False, True)
    # a measurement that expires while the reader runs: the clock is sampled again
    ticks = iter([NOW, NOW + timedelta(minutes=2)])  # before the reader, after it
    during = await fence._read_freeze(None, sleeve, lambda: next(ticks), verdict)  # type: ignore[arg-type]
    assert during is True
    req = _request(clock=lambda: NOW + timedelta(minutes=2))
    assert req.clock() > valid_until  # type: ignore[misc]
    source = inspect.getsource(fence.gather_send_facts)
    assert (
        "req.clock or _real_now" in source
    )  # the clock itself is handed to the reader


async def test_a_clause_that_moves_a_sleeve_takes_every_f2_once_in_order():
    """Two sleeves swapping baskets must not hold one F2 each (review r1)."""
    from app.services.mandate import store

    source = inspect.getsource(store._write_terms)
    assert "extra_baskets=(destination,)" in source
    assert "_bump_basket_authority(" not in source  # no second, out-of-order bump
    lock_src = inspect.getsource(fence_locks.bump_for_sleeve_version)
    assert "sorted(" in lock_src
    assert lock_src.index("bump_authority(") < lock_src.index("lock_mutex(")


async def test_x26_the_projection_closes_fences_under_f3s():
    """The closing writer takes the fence's sleeve mutex before it appends (review r2)."""
    from app.services.mandate import projection

    source = inspect.getsource(projection.project_plan)
    assert source.index("lock_mutex(") < source.index("append_plan_event(")
    assert source.index("acquire_in_order(") < source.index("lock_mutex(")


async def test_a_plan_bound_order_is_sent_in_the_regular_session_only():
    """§4.4: NXT / after-market / Toss US pre-post never carry a plan-bound order."""
    from types import SimpleNamespace

    from app.services.order_proposals import approval_window as aw

    def allowed(at):
        return aw.ApprovalWindowDecision(
            code=aw.ApprovalWindowCode.ALLOW,
            observed_at=at,
            valid_until=None,
            policy_stamp="s",
            market="equity_kr",
            account_mode="kis_live",
            action="place",
            order_type="limit",
        )

    bound = SimpleNamespace(mandate_plan_id=7, market="equity_kr")
    plain = SimpleNamespace(mandate_plan_id=None, market="equity_kr")
    regular = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)  # 10:00 KST Monday
    nxt_after = datetime(2026, 11, 23, 9, 30, tzinfo=UTC)  # 18:30 KST

    assert aw._plan_bound_regular_only(bound, allowed(regular)).allowed is True
    out = aw._plan_bound_regular_only(bound, allowed(nxt_after))
    assert out.allowed is False
    assert out.code is aw.ApprovalWindowCode.DEFER_SESSION_CLOSED
    # an ordinary proposal is untouched
    assert aw._plan_bound_regular_only(plain, allowed(nxt_after)).allowed is True
