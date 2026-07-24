from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.brokers.toss.market_calendar import (
    parse_kr_market_calendar,
    parse_us_market_calendar,
)
from app.services.nxt_preflight import NxtTradability
from app.services.order_proposals import approval_window as policy
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals import telegram_callback as callback_module
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    SubmissionSessionEvidence,
    evaluate_approval_window,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalDispatchState,
    CallbackEnvelope,
    DispatchBinding,
    build_membership_digest,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.revalidation import RungOutcome, revalidate_and_submit
from app.services.order_proposals.service import OrderProposalsService
from app.services.order_proposals.target_order import TargetOrderSnapshot


def _group(
    *,
    market: str = "equity_us",
    account_mode: str = "kis_live",
    symbol: str = "VOO",
    valid_until: object,
):
    return SimpleNamespace(
        market=market,
        account_mode=account_mode,
        symbol=symbol,
        valid_until=valid_until,
        action="place",
        order_type="limit",
    )


async def _allowed_session(group, *, now):
    return SubmissionSessionEvidence(
        known=True,
        source="test",
        current_session="regular",
        allowed_sessions=("regular",),
        allowed_now=True,
        allowed_until=now + timedelta(hours=8),
        next_allowed_at=now + timedelta(days=1),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(microseconds=-1), ApprovalWindowCode.ALLOW),
        (timedelta(0), ApprovalWindowCode.EXPIRED),
        (timedelta(microseconds=1), ApprovalWindowCode.EXPIRED),
    ],
)
async def test_valid_until_exact_microsecond_boundaries(offset, expected):
    deadline = datetime(2026, 7, 23, 4, 55, tzinfo=policy._KST)
    decision = await evaluate_approval_window(
        _group(valid_until=deadline),
        now=deadline + offset,
        session_resolver=_allowed_session,
    )
    assert decision.code is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid_until", "detail"),
    [
        (None, "valid_until_missing"),
        (datetime(2026, 7, 23, 4, 55), "valid_until_naive"),
        ("2026-07-23T04:55:00+09:00", "valid_until_invalid_type"),
    ],
)
async def test_invalid_valid_until_fails_closed_without_session_lookup(
    valid_until, detail
):
    calls = 0

    async def must_not_resolve(group, *, now):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid validity must short-circuit session I/O")

    decision = await evaluate_approval_window(
        _group(valid_until=valid_until),
        now=datetime(2026, 7, 23, 1, tzinfo=UTC),
        session_resolver=must_not_resolve,
    )
    assert decision.code is ApprovalWindowCode.INVALID_VALID_UNTIL
    assert decision.detail == detail
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "valid_until",
    [None, datetime(2026, 7, 23, 4, 55), "invalid"],
)
async def test_invalid_valid_until_cannot_consume_approval_nonce(valid_until):
    now = datetime(2026, 7, 23, 3, tzinfo=UTC)
    group = SimpleNamespace(
        proposal_id=uuid.uuid4(),
        superseded_by_proposal_id=None,
        lifecycle_state="proposed",
        valid_until=valid_until,
        source_asof={},
        approval_nonce="nonce",
        approval_nonce_used_at=None,
    )

    class FakeRepo:
        def __init__(self):
            self.updates = 0

        async def get_group_by_proposal_id(self, proposal_id, *, for_update):
            assert proposal_id == group.proposal_id
            assert for_update is True
            return group

        async def update_group(self, group, **kwargs):
            self.updates += 1
            raise AssertionError("invalid validity must not consume a nonce")

    service = object.__new__(OrderProposalsService)
    service._repo = FakeRepo()

    with pytest.raises(OrderProposalError, match="approval_window:INVALID_VALID_UNTIL"):
        await service.consume_approval_nonce(group.proposal_id, "nonce", now=now)

    assert service._repo.updates == 0
    assert group.approval_nonce_used_at is None


@pytest.mark.asyncio
async def test_locked_callback_reads_refresh_identity_map_state() -> None:
    """A FOR UPDATE waiter must not reuse its pre-lock nonce/rung snapshot."""
    from app.services.order_proposals.repository import OrderProposalRepository

    statements = []

    class FakeScalars:
        def all(self):
            return []

    class FakeResult:
        def scalar_one_or_none(self):
            return None

        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def execute(self, statement):
            statements.append(statement)
            return FakeResult()

    repo = OrderProposalRepository(FakeSession())

    await repo.get_group_by_proposal_id(uuid.uuid4(), for_update=True)
    assert statements[-1].get_execution_options()["populate_existing"] is True

    await repo.get_group_by_pk(1, for_update=True)
    assert statements[-1].get_execution_options()["populate_existing"] is True

    await repo.get_approval_batch_by_id(uuid.uuid4(), for_update=True)
    assert statements[-1].get_execution_options()["populate_existing"] is True

    await repo.list_rungs(1)
    assert statements[-1].get_execution_options()["populate_existing"] is True

    await repo.list_approval_batch_members(1)
    assert statements[-1].get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("known", "expected"),
    [
        (True, ApprovalWindowCode.DEFER_SESSION_CLOSED),
        (False, ApprovalWindowCode.CALENDAR_UNKNOWN),
    ],
)
async def test_dispatch_session_block_publishes_no_nonce_or_card(
    monkeypatch, known, expected
):
    now = datetime(2026, 7, 23, 8, 50, tzinfo=policy._KST)
    group = _group(
        account_mode="toss_live",
        valid_until=now + timedelta(days=1),
    )
    group.proposal_id = uuid.uuid4()
    nonce_mints = 0
    telegram_sends = 0

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            return SubmissionSessionEvidence(
                known=known,
                source="test:toss-us-calendar",
                current_session="closed" if known else "unknown",
                allowed_sessions=("regular",),
                allowed_now=False,
                next_allowed_at=now + timedelta(hours=12) if known else None,
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    class FakeDispatchService:
        async def get_proposal(self, proposal_id):
            return group, []

        async def set_approval_nonce(self, proposal_id, nonce):
            nonlocal nonce_mints
            nonce_mints += 1

    class FakeSession:
        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def service_factory():
        yield FakeSession()

    class NoSendNotifier:
        async def send_approval_message(self, *args, **kwargs):
            nonlocal telegram_sends
            telegram_sends += 1
            raise AssertionError("blocked dispatch must not publish Telegram")

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "test-chat"
    )
    monkeypatch.setattr(
        dispatch_module,
        "OrderProposalsService",
        lambda ignored: FakeDispatchService(),
    )

    result = await dispatch_module.send_proposal_for_approval(
        group.proposal_id,
        notifier=NoSendNotifier(),
        now=now,
        service_factory=service_factory,
        window_evaluator=evaluator,
    )

    assert result.code is expected
    assert nonce_mints == 0
    assert telegram_sends == 0


@pytest.mark.asyncio
async def test_dispatch_rechecks_boundary_before_nonce_or_card(monkeypatch):
    started = datetime(2026, 7, 23, 4, 54, 59, 999999, tzinfo=policy._KST)
    deadline = started + timedelta(microseconds=1)
    group = _group(account_mode="toss_live", valid_until=deadline)
    group.proposal_id = uuid.uuid4()
    nonce_mints = 0
    telegram_sends = 0
    expiry_transitions = 0
    clock_values = iter((started, deadline))

    async def evaluator(group, *, now):
        return await evaluate_approval_window(
            group, now=now, session_resolver=_allowed_session
        )

    class FakeDispatchService:
        async def get_proposal(self, proposal_id):
            return group, []

        async def set_approval_nonce(self, proposal_id, nonce):
            nonlocal nonce_mints
            nonce_mints += 1

        async def expire_mutable_rungs_if_needed(self, proposal_id, *, now):
            nonlocal expiry_transitions
            expiry_transitions += 1
            return True

    class FakeSession:
        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def service_factory():
        yield FakeSession()

    class NoSendNotifier:
        async def send_approval_message(self, *args, **kwargs):
            nonlocal telegram_sends
            telegram_sends += 1
            raise AssertionError("boundary-crossed dispatch must not publish")

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "test-chat"
    )
    monkeypatch.setattr(
        dispatch_module,
        "OrderProposalsService",
        lambda ignored: FakeDispatchService(),
    )

    result = await dispatch_module.send_proposal_for_approval(
        group.proposal_id,
        notifier=NoSendNotifier(),
        now=started,
        now_fn=lambda: next(clock_values),
        service_factory=service_factory,
        window_evaluator=evaluator,
    )

    assert result.code is ApprovalWindowCode.EXPIRED
    assert nonce_mints == 0
    assert telegram_sends == 0
    assert expiry_transitions == 1


@pytest.mark.asyncio
async def test_batch_summary_expiry_preserves_broker_backed_sibling() -> None:
    now = datetime(2026, 7, 23, 4, 55, tzinfo=policy._KST)
    group = _group(account_mode="toss_live", valid_until=now)
    group.proposal_id = uuid.uuid4()
    published = await evaluate_approval_window(
        group,
        now=now - timedelta(hours=1),
        session_resolver=_allowed_session,
    )
    group.source_asof = {
        "approval_window_policy_stamp": published.policy_stamp,
    }
    rungs = [
        SimpleNamespace(rung_index=0, state="resting", broker_order_id="broker-1"),
        SimpleNamespace(
            rung_index=1,
            state="pending_approval",
            broker_order_id=None,
        ),
    ]
    batch = SimpleNamespace(batch_id=uuid.uuid4(), summary_message_id=None)
    registration = SimpleNamespace(
        batch=batch,
        summary_action="send",
        binding=DispatchBinding(
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.BATCH,
            membership_revision=1,
            membership_digest="AbCdEf0123_-",
        ),
    )
    expired_ids: list[uuid.UUID] = []
    released_ids: list[uuid.UUID] = []
    telegram_sends = 0

    class FakeService:
        async def register_approval_batch_member(self, *args, **kwargs):
            return registration

        async def get_approval_batch_display(self, *args, **kwargs):
            return batch, [(group, rungs)]

        async def expire_if_needed(self, *args, **kwargs):
            raise AssertionError("whole-group expiry must not run for mixed rungs")

        async def expire_mutable_rungs_if_needed(self, proposal_id, *, now):
            expired_ids.append(proposal_id)
            return 1

        async def release_approval_batch_summary_claim(self, batch_id, *, now):
            released_ids.append(batch_id)

    class FakeSession:
        async def commit(self):
            return None

    class NoSendNotifier:
        async def send_approval_message(self, *args, **kwargs):
            nonlocal telegram_sends
            telegram_sends += 1
            raise AssertionError("expired batch summary must not publish")

    async def evaluator(candidate, *, now):
        return await evaluate_approval_window(
            candidate,
            now=now,
            session_resolver=_allowed_session,
        )

    await dispatch_module._register_and_publish_batch_summary(
        session=FakeSession(),
        service=FakeService(),
        proposal_id=group.proposal_id,
        message_id=123,
        chat_id="42",
        now=now,
        notifier=NoSendNotifier(),
        window_evaluator=evaluator,
        now_fn=lambda: now,
    )

    assert expired_ids == [group.proposal_id]
    assert released_ids == [batch.batch_id]
    assert rungs[0].state == "resting"
    assert telegram_sends == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 7, 22, 13, 29, 59, 999999, tzinfo=UTC), False),
        (datetime(2026, 7, 22, 13, 30, tzinfo=UTC), True),
        (datetime(2026, 7, 22, 19, 59, 59, 999999, tzinfo=UTC), True),
        (datetime(2026, 7, 22, 20, 0, tzinfo=UTC), False),
    ],
)
async def test_xnys_regular_open_close_exact_boundaries(at, expected):
    evidence = policy._resolve_xnys_session(
        _group(valid_until=at + timedelta(days=7)), at
    )
    assert evidence.allowed_now is expected
    assert evidence.source == "exchange_calendars:XNYS"


@pytest.mark.asyncio
async def test_xnys_premarket_after_hours_and_holiday_defer():
    group = _group(valid_until=datetime(2026, 7, 10, tzinfo=UTC))
    pre = policy._resolve_xnys_session(group, datetime(2026, 7, 2, 12, 0, tzinfo=UTC))
    post = policy._resolve_xnys_session(group, datetime(2026, 7, 2, 21, 0, tzinfo=UTC))
    holiday = policy._resolve_xnys_session(
        group, datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    )
    assert (pre.allowed_now, post.allowed_now, holiday.allowed_now) == (
        False,
        False,
        False,
    )
    assert pre.next_allowed_at == datetime(2026, 7, 2, 13, 30, tzinfo=UTC)
    assert holiday.next_allowed_at == datetime(2026, 7, 6, 13, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_xnys_half_day_close_and_dst_are_calendar_derived():
    group = _group(valid_until=datetime(2027, 1, 10, tzinfo=UTC))
    half_day_before_close = policy._resolve_xnys_session(
        group, datetime(2026, 11, 27, 17, 59, 59, 999999, tzinfo=UTC)
    )
    half_day_at_close = policy._resolve_xnys_session(
        group, datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    )
    winter_open = policy._resolve_xnys_session(
        group, datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    )
    summer_open = policy._resolve_xnys_session(
        group, datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
    )
    assert half_day_before_close.allowed_now is True
    assert half_day_at_close.allowed_now is False
    assert winter_open.allowed_now is True
    assert summer_open.allowed_now is True


def _window(start: str, end: str) -> dict[str, str]:
    return {"startTime": start, "endTime": end}


@pytest.mark.asyncio
async def test_toss_us_regular_only_uses_broker_calendar(monkeypatch):
    calendar = parse_us_market_calendar(
        {
            "today": {
                "date": "2026-07-22",
                "dayMarket": _window(
                    "2026-07-22T09:00:00+09:00", "2026-07-22T17:00:00+09:00"
                ),
                "preMarket": _window(
                    "2026-07-22T17:00:00+09:00", "2026-07-22T22:30:00+09:00"
                ),
                "regularMarket": _window(
                    "2026-07-22T22:30:00+09:00", "2026-07-23T05:00:00+09:00"
                ),
                "afterMarket": _window(
                    "2026-07-23T05:00:00+09:00", "2026-07-23T09:00:00+09:00"
                ),
            },
            "nextBusinessDay": {
                "date": "2026-07-23",
                "regularMarket": _window(
                    "2026-07-23T22:30:00+09:00", "2026-07-24T05:00:00+09:00"
                ),
            },
        }
    )

    async def calendar_reader(market, query_date):
        assert market == "us"
        return calendar

    monkeypatch.setattr(policy, "get_toss_market_calendar", calendar_reader)
    group = _group(
        account_mode="toss_live",
        valid_until=datetime(2026, 7, 25, tzinfo=policy._KST),
    )
    pre = await policy.resolve_submission_session(
        group, now=datetime(2026, 7, 22, 22, 29, 59, tzinfo=policy._KST)
    )
    opened = await policy.resolve_submission_session(
        group, now=datetime(2026, 7, 22, 22, 30, tzinfo=policy._KST)
    )
    closed = await policy.resolve_submission_session(
        group, now=datetime(2026, 7, 23, 5, 0, tzinfo=policy._KST)
    )
    assert pre.allowed_now is False
    assert opened.allowed_now is True
    assert closed.allowed_now is False
    assert closed.next_allowed_at == datetime(2026, 7, 23, 22, 30, tzinfo=policy._KST)


def _kr_calendar():
    return parse_kr_market_calendar(
        {
            "today": {
                "date": "2026-07-23",
                "integrated": {
                    "preMarket": _window(
                        "2026-07-23T08:00:00+09:00",
                        "2026-07-23T08:50:00+09:00",
                    ),
                    "regularMarket": _window(
                        "2026-07-23T09:00:00+09:00",
                        "2026-07-23T15:30:00+09:00",
                    ),
                    "afterMarket": _window(
                        "2026-07-23T16:00:00+09:00",
                        "2026-07-23T20:00:00+09:00",
                    ),
                },
            },
            "nextBusinessDay": {
                "date": "2026-07-24",
                "integrated": {
                    "preMarket": _window(
                        "2026-07-24T08:00:00+09:00",
                        "2026-07-24T08:50:00+09:00",
                    ),
                    "regularMarket": _window(
                        "2026-07-24T09:00:00+09:00",
                        "2026-07-24T15:30:00+09:00",
                    ),
                    "afterMarket": _window(
                        "2026-07-24T16:00:00+09:00",
                        "2026-07-24T20:00:00+09:00",
                    ),
                },
            },
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "toss_live"])
async def test_kr_regular_and_nxt_carry_preserved(monkeypatch, account_mode):
    async def calendar_reader(market, query_date):
        return _kr_calendar()

    async def tradability_reader(symbols):
        return {
            "005930": NxtTradability(
                nxt_eligible=True,
                nxt_trading_suspended=False,
                asof=datetime(2026, 7, 23, 7, tzinfo=policy._KST),
            )
        }

    monkeypatch.setattr(policy, "get_toss_market_calendar", calendar_reader)
    monkeypatch.setattr(policy, "get_kr_nxt_tradability", tradability_reader)
    group = _group(
        market="equity_kr",
        account_mode=account_mode,
        symbol="005930",
        valid_until=datetime(2026, 7, 24, 20, tzinfo=policy._KST),
    )
    for at in (
        datetime(2026, 7, 23, 8, 0, tzinfo=policy._KST),
        datetime(2026, 7, 23, 9, 0, tzinfo=policy._KST),
        datetime(2026, 7, 23, 16, 0, tzinfo=policy._KST),
    ):
        evidence = await policy.resolve_submission_session(group, now=at)
        assert evidence.allowed_now is True
        assert evidence.allowed_sessions == (
            "nxt_premarket",
            "regular",
            "nxt_after",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "toss_live"])
async def test_krx_regular_remains_authoritative_when_nxt_calendar_is_unavailable(
    monkeypatch, account_mode
):
    async def unavailable_calendar(market, query_date):
        return None

    async def unavailable_tradability(symbols):
        raise RuntimeError("symbol universe unavailable")

    monkeypatch.setattr(policy, "get_toss_market_calendar", unavailable_calendar)
    monkeypatch.setattr(policy, "get_kr_nxt_tradability", unavailable_tradability)
    now = datetime(2026, 7, 23, 9, 0, tzinfo=policy._KST)
    decision = await evaluate_approval_window(
        _group(
            market="equity_kr",
            account_mode=account_mode,
            symbol="005930",
            valid_until=now + timedelta(hours=1),
        ),
        now=now,
    )

    assert decision.code is ApprovalWindowCode.ALLOW
    assert decision.evidence.source.startswith("exchange_calendars:XKRX")
    assert decision.evidence.allowed_sessions == ("regular",)


@pytest.mark.asyncio
async def test_kr_nxt_unknown_or_not_tradable_fails_closed_or_defers(monkeypatch):
    async def calendar_reader(market, query_date):
        return _kr_calendar()

    monkeypatch.setattr(policy, "get_toss_market_calendar", calendar_reader)
    group = _group(
        market="equity_kr",
        account_mode="toss_live",
        symbol="005930",
        valid_until=datetime(2026, 7, 24, 20, tzinfo=policy._KST),
    )

    async def stale_reader(symbols):
        return {
            "005930": NxtTradability(
                nxt_eligible=True,
                nxt_trading_suspended=False,
                asof=datetime(2026, 7, 20, tzinfo=policy._KST),
            )
        }

    monkeypatch.setattr(policy, "get_kr_nxt_tradability", stale_reader)
    unknown = await evaluate_approval_window(
        group, now=datetime(2026, 7, 23, 8, 10, tzinfo=policy._KST)
    )
    assert unknown.code is ApprovalWindowCode.CALENDAR_UNKNOWN

    async def ineligible_reader(symbols):
        return {
            "005930": NxtTradability(
                nxt_eligible=False,
                nxt_trading_suspended=False,
                asof=datetime(2026, 7, 23, 7, tzinfo=policy._KST),
            )
        }

    monkeypatch.setattr(policy, "get_kr_nxt_tradability", ineligible_reader)
    deferred = await evaluate_approval_window(
        group, now=datetime(2026, 7, 23, 8, 10, tzinfo=policy._KST)
    )
    assert deferred.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert deferred.evidence.next_allowed_at == datetime(
        2026, 7, 23, 9, 0, tzinfo=policy._KST
    )


@pytest.mark.asyncio
async def test_crypto_is_24x7_and_does_not_use_market_calendar(monkeypatch):
    async def must_not_read(*args, **kwargs):
        raise AssertionError("crypto must not read equity calendars")

    monkeypatch.setattr(policy, "get_toss_market_calendar", must_not_read)
    now = datetime(2026, 7, 23, 3, tzinfo=UTC)
    decision = await evaluate_approval_window(
        _group(
            market="crypto",
            account_mode="upbit",
            symbol="KRW-BTC",
            valid_until=now + timedelta(hours=1),
        ),
        now=now,
    )
    assert decision.code is ApprovalWindowCode.ALLOW
    assert decision.evidence.current_session == "24x7"


@pytest.mark.asyncio
async def test_unknown_calendar_and_no_executable_next_window_are_distinct():
    now = datetime(2026, 7, 23, 1, tzinfo=UTC)

    async def unknown(group, *, now):
        return SubmissionSessionEvidence(
            known=False,
            source="test",
            current_session="unknown",
            allowed_sessions=("regular",),
            allowed_now=False,
        )

    unknown_decision = await evaluate_approval_window(
        _group(valid_until=now + timedelta(days=1)),
        now=now,
        session_resolver=unknown,
    )
    assert unknown_decision.code is ApprovalWindowCode.CALENDAR_UNKNOWN

    async def next_after_expiry(group, *, now):
        return SubmissionSessionEvidence(
            known=True,
            source="test",
            current_session="closed",
            allowed_sessions=("regular",),
            allowed_now=False,
            next_allowed_at=now + timedelta(hours=2),
        )

    no_window = await evaluate_approval_window(
        _group(valid_until=now + timedelta(hours=1)),
        now=now,
        session_resolver=next_after_expiry,
    )
    assert no_window.code is ApprovalWindowCode.NO_EXECUTABLE_WINDOW
    next_allowed_at = (now + timedelta(hours=2)).isoformat()
    assert next_allowed_at in policy.approval_window_operator_text(no_window)
    outcome = RungOutcome(
        0,
        "no_executable_window",
        {"approval_window": no_window.to_dict()},
    )
    assert next_allowed_at in callback_module._window_outcome_text(outcome)


class _FakeRevalidationService:
    def __init__(self, *, action: str = "place", valid_until: datetime):
        proposal_id = uuid.uuid4()
        dispatch_binding = build_proposal_dispatch_binding(
            proposal_id=proposal_id,
            nonce="published-nonce",
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.MANUAL,
            current_membership_revision=None,
        )
        self.group = SimpleNamespace(
            proposal_id=proposal_id,
            superseded_by_proposal_id=None,
            lifecycle_state="proposed",
            source_asof={},
            action=action,
            order_type="limit",
            account_mode="kis_live",
            market="equity_us",
            symbol="VOO",
            side="sell",
            thesis=None,
            strategy=None,
            exit_intent=None,
            exit_reason=None,
            retrospective_id=None,
            approval_issue_id=None,
            broker_account_id=None,
            target_broker_order_id="target-1" if action != "place" else None,
            valid_until=valid_until,
            approval_nonce="published-nonce",
            approval_nonce_used_at=None,
            approval_dispatch_state=ApprovalDispatchState.SENT_CURRENT.value,
            approval_dispatch_attempt_id=dispatch_binding.attempt_id,
            approval_dispatch_card_kind=dispatch_binding.card_kind.value,
            approval_dispatch_membership_revision=(
                dispatch_binding.membership_revision
            ),
            approval_dispatch_membership_digest=dispatch_binding.membership_digest,
            approved_by_telegram_user_id=None,
            approved_at=None,
            commit_lease_until=None,
        )
        self.rung = SimpleNamespace(
            rung_index=0,
            state="pending_approval",
            side="sell",
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
        )
        self.transitions: list[str] = []
        self.expire_calls = 0
        self.resting_calls = 0
        self.cancelled_calls = 0
        self.nonce_consumes = 0
        self.allow_nonce_consume = False

    async def get_proposal(self, proposal_id):
        assert proposal_id == self.group.proposal_id
        return self.group, [self.rung]

    async def transition_rung(self, proposal_id, rung_index, *, new_state, **kwargs):
        assert proposal_id == self.group.proposal_id
        assert rung_index == 0
        self.rung.state = new_state
        self.transitions.append(new_state)
        return self.rung

    async def expire_if_needed(self, proposal_id, *, now):
        assert proposal_id == self.group.proposal_id
        self.expire_calls += 1
        self.rung.state = "expired"
        self.group.lifecycle_state = "expired"
        return True

    async def expire_rung_if_needed(self, proposal_id, rung_index, *, now):
        assert rung_index == 0
        return await self.expire_if_needed(proposal_id, now=now)

    async def expire_mutable_rungs_if_needed(self, proposal_id, *, now):
        await self.expire_if_needed(proposal_id, now=now)
        return 1

    async def restore_rung_after_pre_send_block(
        self, proposal_id, rung_index, *, now, expired
    ):
        assert proposal_id == self.group.proposal_id
        assert rung_index == 0
        self.rung.state = "pending_approval"
        if expired:
            await self.expire_if_needed(proposal_id, now=now)
        return self.rung

    async def record_resting(self, proposal_id, rung_index, **kwargs):
        self.resting_calls += 1
        self.rung.state = "resting"
        return self.rung

    async def record_cancelled(self, proposal_id, rung_index, **kwargs):
        self.cancelled_calls += 1
        self.rung.state = "cancelled"
        return self.rung

    async def acquire_target_mutation_lock(self, group):
        assert group is self.group

    async def preflight_published_proposal_callback(self, proposal_id, *, callback):
        assert proposal_id == self.group.proposal_id
        OrderProposalsService._assert_published_proposal_binding(
            self.group,
            callback=callback,
        )
        return self.group

    async def consume_published_proposal_callback(
        self,
        proposal_id,
        *,
        callback,
        now,
        telegram_user_id=None,
    ):
        assert proposal_id == self.group.proposal_id
        self.nonce_consumes += 1
        if not self.allow_nonce_consume:
            raise AssertionError("blocked callback must not consume its nonce")
        OrderProposalsService._assert_published_proposal_binding(
            self.group,
            callback=callback,
        )
        self.group.approval_nonce_used_at = now
        return self.group

    async def consume_approval_nonce(self, proposal_id, nonce, *, now):
        self.nonce_consumes += 1
        if not self.allow_nonce_consume:
            raise AssertionError("blocked callback must not consume its nonce")
        self.group.approval_nonce_used_at = now
        return self.group

    async def acquire_commit_lease(self, proposal_id, *, now):
        self.group.commit_lease_until = now + timedelta(seconds=10)
        return True

    async def record_approval(self, proposal_id, *, telegram_user_id, now):
        self.group.approved_by_telegram_user_id = telegram_user_id
        self.group.approved_at = now
        return self.group

    async def restore_approval_after_window_block(self, proposal_id, *, nonce, expired):
        self.group.approval_nonce = None if expired else nonce
        self.group.approval_nonce_used_at = None
        self.group.approved_by_telegram_user_id = None
        self.group.approved_at = None
        self.group.commit_lease_until = None
        return self.group


def _callback_for_group(
    group: SimpleNamespace,
    *,
    action: str = "op",
) -> CallbackEnvelope:
    return CallbackEnvelope(
        action=action,
        subject_short=str(group.proposal_id)[:8],
        attempt_id=group.approval_dispatch_attempt_id,
        membership_revision=group.approval_dispatch_membership_revision,
        membership_digest=group.approval_dispatch_membership_digest,
        nonce=group.approval_nonce,
    )


def _callback_for_batch(batch: SimpleNamespace) -> CallbackEnvelope:
    return CallbackEnvelope(
        action="ba",
        subject_short=str(batch.batch_id)[:8],
        attempt_id=batch.approval_dispatch_attempt_id,
        membership_revision=batch.membership_revision,
        membership_digest=batch.membership_digest,
        nonce=batch.approval_nonce,
    )


def _bind_batch_snapshot(
    batch: SimpleNamespace,
    groups: list[SimpleNamespace],
    members: list[SimpleNamespace],
) -> CallbackEnvelope:
    batch.approval_dispatch_state = ApprovalDispatchState.SENT_CURRENT.value
    batch.approval_dispatch_attempt_id = uuid.uuid4()
    batch.membership_revision = 1
    digest_members = []
    for group, member in zip(groups, members, strict=True):
        binding = build_proposal_dispatch_binding(
            proposal_id=group.proposal_id,
            nonce=group.approval_nonce,
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.MANUAL,
            current_membership_revision=None,
        )
        group.approval_dispatch_state = ApprovalDispatchState.SENT_CURRENT.value
        group.approval_dispatch_attempt_id = binding.attempt_id
        group.approval_dispatch_card_kind = binding.card_kind.value
        group.approval_dispatch_membership_revision = binding.membership_revision
        group.approval_dispatch_membership_digest = binding.membership_digest
        member.membership_revision = batch.membership_revision
        member.approval_dispatch_attempt_id_snapshot = binding.attempt_id
        member.approval_membership_revision_snapshot = binding.membership_revision
        member.approval_membership_digest_snapshot = binding.membership_digest
        member.approval_card_kind_snapshot = binding.card_kind.value
        digest_members.append(
            {
                "proposal_id": str(group.proposal_id),
                "approval_nonce": member.approval_nonce_snapshot,
                "approval_message_id": member.approval_message_id,
                "approval_dispatch_attempt_id": str(binding.attempt_id),
                "approval_membership_revision": binding.membership_revision,
                "approval_membership_digest": binding.membership_digest,
            }
        )
    batch.membership_digest = build_membership_digest(
        card_kind=ApprovalCardKind.BATCH,
        membership_revision=batch.membership_revision,
        members=digest_members,
    )
    return _callback_for_batch(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["place", "replace", "cancel"])
@pytest.mark.parametrize(
    "block_kind",
    ["expired", "session_closed", "calendar_unknown"],
)
async def test_revalidation_initial_window_block_has_zero_provider_or_broker_calls(
    action, block_kind
):
    now = datetime(2026, 7, 23, 8, 50, tzinfo=policy._KST)
    service = _FakeRevalidationService(
        action=action,
        valid_until=now if block_kind == "expired" else now + timedelta(days=2),
    )
    calls = {"preview_or_submit": 0, "fetch": 0, "cancel": 0}

    async def place_must_not_run(**kwargs):
        calls["preview_or_submit"] += 1
        raise AssertionError("window block must precede provider preview/live submit")

    async def fetch_must_not_run(**kwargs):
        calls["fetch"] += 1
        raise AssertionError("window block must precede target provider lookup")

    async def cancel_must_not_run(**kwargs):
        calls["cancel"] += 1
        raise AssertionError("window block must precede broker cancel")

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            return SubmissionSessionEvidence(
                known=block_kind != "calendar_unknown",
                source="test:us-calendar",
                current_session=(
                    "unknown" if block_kind == "calendar_unknown" else "closed"
                ),
                allowed_sessions=("regular",),
                allowed_now=False,
                next_allowed_at=(
                    now + timedelta(hours=12)
                    if block_kind == "session_closed"
                    else None
                ),
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    expected_stamp = (await evaluator(service.group, now=now)).policy_stamp
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=now,
        place_order_fn=place_must_not_run,
        fetch_target_fn=fetch_must_not_run,
        cancel_target_fn=cancel_must_not_run,
        window_evaluator=evaluator,
        expected_policy_stamp=expected_stamp,
    )

    expected = {
        "expired": "expired",
        "session_closed": "defer_session_closed",
        "calendar_unknown": "calendar_unknown",
    }[block_kind]
    assert [outcome.result for outcome in outcomes] == [expected]
    assert calls == {"preview_or_submit": 0, "fetch": 0, "cancel": 0}
    assert service.expire_calls == (1 if block_kind == "expired" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_kind", "expected"),
    [
        ("expiry", "expired"),
        ("session", "defer_session_closed"),
    ],
)
async def test_revalidation_toctou_after_preview_blocks_live_submit(
    close_kind, expected
):
    started = datetime(2026, 7, 22, 19, 54, 59, 999999, tzinfo=UTC)
    boundary = started + timedelta(microseconds=1)
    valid_until = boundary if close_kind == "expiry" else started + timedelta(days=2)
    service = _FakeRevalidationService(valid_until=valid_until)
    calls = {"preview": 0, "submit": 0}

    async def place(**kwargs):
        if kwargs["dry_run"]:
            calls["preview"] += 1
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "100",
                "quantity": "1",
            }
        calls["submit"] += 1
        raise AssertionError("TOCTOU window close must block live submit")

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            session_open = close_kind != "session" or now < boundary
            return SubmissionSessionEvidence(
                known=True,
                source="test:us-calendar",
                current_session="regular" if session_open else "closed",
                allowed_sessions=("regular",),
                allowed_now=session_open,
                allowed_until=boundary if session_open else None,
                next_allowed_at=boundary + timedelta(hours=17),
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    initial = await evaluator(service.group, now=started)
    clock_values = iter((*([started] * 5), boundary))
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=started,
        place_order_fn=place,
        correlation_mint=lambda **kwargs: "must-not-run",
        window_evaluator=evaluator,
        expected_policy_stamp=initial.policy_stamp,
        now_fn=lambda: next(clock_values),
    )

    assert [outcome.result for outcome in outcomes] == [expected]
    assert calls == {"preview": 1, "submit": 0}
    assert service.resting_calls == 0


@pytest.mark.asyncio
async def test_replace_rechecks_after_cancel_before_replacement_submit():
    started = datetime(2026, 7, 22, 19, 54, 59, 999999, tzinfo=UTC)
    boundary = started + timedelta(microseconds=1)
    service = _FakeRevalidationService(
        action="replace", valid_until=started + timedelta(days=2)
    )
    approved_target = TargetOrderSnapshot(
        broker_order_id="target-1",
        symbol="VOO",
        side="sell",
        order_type="limit",
        limit_price="100",
        remaining_quantity="1",
        status="open",
        observed_at=started.isoformat(),
    )
    service.group.source_asof = {"target_order_snapshot": approved_target.to_payload()}
    target_states = iter(
        (
            approved_target,
            TargetOrderSnapshot(
                broker_order_id="target-1",
                symbol="VOO",
                side="sell",
                order_type="limit",
                limit_price="100",
                remaining_quantity="1",
                status="cancelled",
                observed_at=boundary.isoformat(),
            ),
        )
    )
    calls = {"preview": 0, "cancel": 0, "submit": 0}

    async def place(**kwargs):
        if kwargs["dry_run"]:
            calls["preview"] += 1
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "100",
                "quantity": "1",
            }
        calls["submit"] += 1
        raise AssertionError("closed-window replacement must not be submitted")

    async def fetch_target(**kwargs):
        return next(target_states)

    async def cancel_target(**kwargs):
        calls["cancel"] += 1
        return {"success": True}

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            session_open = now < boundary
            return SubmissionSessionEvidence(
                known=True,
                source="test:us-calendar",
                current_session="regular" if session_open else "closed",
                allowed_sessions=("regular",),
                allowed_now=session_open,
                allowed_until=boundary if session_open else None,
                next_allowed_at=boundary + timedelta(hours=17),
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    initial = await evaluator(service.group, now=started)
    service.group.source_asof["approval_window_policy_stamp"] = initial.policy_stamp
    clock_values = iter((*([started] * 11), boundary))
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=started,
        place_order_fn=place,
        fetch_target_fn=fetch_target,
        cancel_target_fn=cancel_target,
        correlation_mint=lambda **kwargs: "corr-1",
        opposite_pending_check_fn=lambda **kwargs: None,
        window_evaluator=evaluator,
        expected_policy_stamp=initial.policy_stamp,
        now_fn=lambda: next(clock_values),
    )

    assert [outcome.result for outcome in outcomes] == ["defer_session_closed"]
    assert outcomes[0].detail["target_cancelled"] is True
    assert outcomes[0].detail["replacement_submitted"] is False
    assert calls == {"preview": 1, "cancel": 1, "submit": 0}
    assert service.cancelled_calls == 1
    assert service.rung.state == "cancelled"


@pytest.mark.asyncio
async def test_card_can_expire_before_callback_without_nonce_consumption():
    deadline = datetime(2026, 7, 23, 4, 55, tzinfo=policy._KST)
    item = _FakeRevalidationService(valid_until=deadline)
    dispatched = await _allowed_window_evaluator(
        item.group, now=deadline - timedelta(microseconds=1)
    )
    item.group.source_asof = {"approval_window_policy_stamp": dispatched.policy_stamp}
    revalidate_calls = 0

    async def must_not_revalidate(**kwargs):
        nonlocal revalidate_calls
        revalidate_calls += 1
        raise AssertionError("expired callback must not revalidate")

    class FakeSession:
        async def commit(self):
            return None

    result = await callback_module._handle_approve(
        session=FakeSession(),
        service=item,
        proposal_id=item.group.proposal_id,
        callback=_callback_for_group(item.group),
        now=deadline,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        callback_query_id=None,
        telegram_user_id="777",
        revalidate_fn=must_not_revalidate,
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: deadline,
    )

    assert result["reason"] == "EXPIRED"
    assert item.nonce_consumes == 0
    assert item.expire_calls == 1
    assert revalidate_calls == 0


@pytest.mark.asyncio
async def test_card_session_can_close_before_callback_without_nonce_consumption():
    dispatched_at = datetime(2026, 7, 22, 19, 54, 59, 999999, tzinfo=UTC)
    closed_at = dispatched_at + timedelta(microseconds=1)
    item = _FakeRevalidationService(valid_until=closed_at + timedelta(days=2))

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            session_open = now < closed_at
            return SubmissionSessionEvidence(
                known=True,
                source="test:us-calendar",
                current_session="regular" if session_open else "closed",
                allowed_sessions=("regular",),
                allowed_now=session_open,
                next_allowed_at=(
                    None if session_open else closed_at + timedelta(hours=17)
                ),
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    dispatched = await evaluator(item.group, now=dispatched_at)
    item.group.source_asof = {"approval_window_policy_stamp": dispatched.policy_stamp}
    revalidate_calls = 0

    async def must_not_revalidate(**kwargs):
        nonlocal revalidate_calls
        revalidate_calls += 1
        raise AssertionError("session-closed callback must not revalidate")

    class FakeSession:
        async def commit(self):
            return None

    result = await callback_module._handle_approve(
        session=FakeSession(),
        service=item,
        proposal_id=item.group.proposal_id,
        callback=_callback_for_group(item.group),
        now=closed_at,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        callback_query_id=None,
        telegram_user_id="777",
        revalidate_fn=must_not_revalidate,
        window_evaluator=evaluator,
        now_fn=lambda: closed_at,
    )

    assert result["reason"] == "DEFER_SESSION_CLOSED"
    assert item.nonce_consumes == 0
    assert item.expire_calls == 0
    assert revalidate_calls == 0


@pytest.mark.asyncio
async def test_callback_rechecks_boundary_immediately_before_nonce_consumption():
    started = datetime(2026, 7, 23, 4, 54, 59, 999999, tzinfo=policy._KST)
    deadline = started + timedelta(microseconds=1)
    item = _FakeRevalidationService(valid_until=deadline)
    dispatched = await _allowed_window_evaluator(item.group, now=started)
    item.group.source_asof = {"approval_window_policy_stamp": dispatched.policy_stamp}
    clock_values = iter((started, deadline))
    revalidate_calls = 0

    async def must_not_revalidate(**kwargs):
        nonlocal revalidate_calls
        revalidate_calls += 1
        raise AssertionError("nonce-boundary expiry must not revalidate")

    class FakeSession:
        async def commit(self):
            return None

    result = await callback_module._handle_approve(
        session=FakeSession(),
        service=item,
        proposal_id=item.group.proposal_id,
        callback=_callback_for_group(item.group),
        now=started,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        callback_query_id=None,
        telegram_user_id="777",
        revalidate_fn=must_not_revalidate,
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: next(clock_values),
    )

    assert result["reason"] == "EXPIRED"
    assert item.nonce_consumes == 0
    assert item.expire_calls == 1
    assert revalidate_calls == 0


@pytest.mark.asyncio
async def test_callback_missing_dispatch_policy_stamp_fails_closed():
    now = datetime(2026, 7, 22, 14, tzinfo=UTC)
    item = _FakeRevalidationService(valid_until=now + timedelta(hours=1))
    item.group.source_asof = {}

    decision = await callback_module._evaluate_bound_window(
        item.group,
        now=now,
        window_evaluator=_allowed_window_evaluator,
    )

    assert decision.code is ApprovalWindowCode.CALENDAR_UNKNOWN
    assert decision.detail == "approval_window_policy_stamp_missing"


@pytest.mark.asyncio
async def test_valid_us_regular_session_preserves_preview_and_submit_path():
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    service = _FakeRevalidationService(valid_until=now + timedelta(hours=1))
    calls = {"preview": 0, "submit": 0}

    async def place(**kwargs):
        if kwargs["dry_run"]:
            calls["preview"] += 1
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "100",
                "quantity": "1",
            }
        calls["submit"] += 1
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "broker-1",
            "correlation_id": "corr-1",
            "idempotency_key": "idem-1",
            "approval_hash_digest": "digest-1",
        }

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=now,
        place_order_fn=place,
        correlation_mint=lambda **kwargs: "corr-1",
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: now,
        expected_policy_stamp=(
            await _allowed_window_evaluator(service.group, now=now)
        ).policy_stamp,
    )

    assert [outcome.result for outcome in outcomes] == ["submitted_resting"]
    assert calls == {"preview": 1, "submit": 1}
    assert service.resting_calls == 1


async def _allowed_window_evaluator(group, *, now):
    return await evaluate_approval_window(
        group, now=now, session_resolver=_allowed_session
    )


@pytest.mark.asyncio
async def test_mixed_batch_window_block_preserves_exact_membership_and_all_nonces(
    monkeypatch,
):
    now = datetime(2026, 7, 23, 8, 50, tzinfo=policy._KST)
    groups = []
    for symbol, valid_until in (
        ("VALID", now + timedelta(days=2)),
        ("EXPIRED", now),
        ("CLOSED", now + timedelta(days=2)),
    ):
        item = _FakeRevalidationService(valid_until=valid_until)
        item.group.symbol = symbol
        item.group.exit_intent = None
        item.group.approval_nonce = f"nonce-{symbol.lower()}"
        item.group.approval_nonce_used_at = None
        groups.append(item)

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            allowed = group.symbol != "CLOSED"
            return SubmissionSessionEvidence(
                known=True,
                source="test:us-calendar",
                current_session="regular" if allowed else "closed",
                allowed_sessions=("regular",),
                allowed_now=allowed,
                allowed_until=now + timedelta(hours=8) if allowed else None,
                next_allowed_at=None if allowed else now + timedelta(hours=12),
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    for item in groups:
        # Published cards carry the same capability policy stamp. Current
        # session state is evidence, not part of the stable stamp.
        stamp_now = now - timedelta(hours=1)

        async def dispatch_resolver(group, *, now):
            return SubmissionSessionEvidence(
                known=True,
                source="test:us-calendar",
                current_session="regular",
                allowed_sessions=("regular",),
                allowed_now=True,
            )

        decision = await evaluate_approval_window(
            item.group, now=stamp_now, session_resolver=dispatch_resolver
        )
        item.group.source_asof = {"approval_window_policy_stamp": decision.policy_stamp}

    batch_id = uuid.uuid4()
    batch = SimpleNamespace(
        batch_id=batch_id,
        chat_id="42",
        approval_nonce="batch-nonce",
        approval_nonce_used_at=None,
        # Real registration bounds the batch by the earliest member
        # valid_until. The expired member therefore expires the batch exactly
        # at this callback boundary; member-level typed convergence must still
        # run before the generic batch-TTL return.
        expires_at=now,
        approval_dispatch_state=ApprovalDispatchState.SENT_CURRENT.value,
        approval_dispatch_attempt_id=uuid.uuid4(),
        membership_revision=1,
        membership_digest="AbCdEf0123_-",
    )
    proposals = [(item.group, [item.rung]) for item in groups]

    class FakeBatchService:
        def __init__(self):
            self.batch_nonce_consumes = 0
            self.expired_ids: list[uuid.UUID] = []

        async def acquire_approval_batch_chat_lock(self, chat_id):
            assert chat_id == "42"

        async def resolve_approval_batch_id_prefix(self, batch_short):
            return batch_id

        async def preflight_published_batch_callback(
            self,
            requested_batch_id,
            *,
            callback,
            chat_id,
            now,
            allow_expired=False,
        ):
            assert requested_batch_id == batch_id
            OrderProposalsService._assert_published_batch_binding(
                batch,
                callback=callback,
                chat_id=chat_id,
                now=now,
                allow_expired=allow_expired,
            )
            return batch

        async def get_approval_batch_display(
            self, requested_batch_id, *, for_update=False
        ):
            assert requested_batch_id == batch_id
            return batch, proposals

        async def expire_mutable_rungs_if_needed(self, proposal_id, *, now):
            self.expired_ids.append(proposal_id)
            return True

        async def consume_approval_batch_nonce(self, *args, **kwargs):
            self.batch_nonce_consumes += 1
            raise AssertionError("mixed blocked batch must not consume its nonce")

    fake_service = FakeBatchService()

    class FakeSession:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    session = FakeSession()

    @contextlib.asynccontextmanager
    async def service_factory():
        yield session

    monkeypatch.setattr(
        callback_module, "OrderProposalsService", lambda ignored: fake_service
    )

    result = await callback_module._handle_batch_approve(
        service_factory=service_factory,
        batch_short=str(batch_id)[:8],
        callback=_callback_for_batch(batch),
        now=now,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        telegram_user_id="777",
        revalidate_fn=lambda **kwargs: None,
        window_evaluator=evaluator,
        now_fn=lambda: now,
    )

    assert result["reason"] == "BATCH_WINDOW_BLOCKED"
    assert [item["proposal_id"] for item in result["results"]] == [
        str(item.group.proposal_id) for item in groups
    ]
    assert [item["reason"] for item in result["results"]] == [
        "batch_atomic_window_block",
        "approval_window:EXPIRED:now_at_or_after_valid_until",
        "🌙 주문 가능 세션 아님 — 다음 허용 세션: 2026-07-23T20:50:00+09:00",
    ]
    assert fake_service.batch_nonce_consumes == 0
    assert batch.approval_nonce_used_at is None
    assert [item.group.approval_nonce_used_at for item in groups] == [None, None, None]
    assert fake_service.expired_ids == [groups[1].group.proposal_id]


@pytest.mark.asyncio
async def test_batch_rechecks_frozen_members_before_global_nonce(monkeypatch):
    started = datetime(2026, 7, 23, 4, 54, 59, 999999, tzinfo=policy._KST)
    boundary = started + timedelta(microseconds=1)
    groups = [
        _FakeRevalidationService(valid_until=started + timedelta(days=1)),
        _FakeRevalidationService(valid_until=boundary),
    ]
    for index, item in enumerate(groups):
        item.group.exit_intent = None
        item.group.approval_nonce = f"member-{index}"
        item.group.approval_nonce_used_at = None
        dispatched = await _allowed_window_evaluator(item.group, now=started)
        item.group.source_asof = {
            "approval_window_policy_stamp": dispatched.policy_stamp
        }

    batch_id = uuid.uuid4()
    batch = SimpleNamespace(
        batch_id=batch_id,
        chat_id="42",
        approval_nonce="batch-nonce",
        approval_nonce_used_at=None,
        expires_at=started + timedelta(minutes=5),
        approval_dispatch_state=ApprovalDispatchState.SENT_CURRENT.value,
        approval_dispatch_attempt_id=uuid.uuid4(),
        membership_revision=1,
        membership_digest="AbCdEf0123_-",
    )
    proposals = [(item.group, [item.rung]) for item in groups]

    class FakeBatchService:
        def __init__(self):
            self.batch_nonce_consumes = 0
            self.expired_ids = []

        async def acquire_approval_batch_chat_lock(self, chat_id):
            assert chat_id == "42"

        async def resolve_approval_batch_id_prefix(self, batch_short):
            return batch_id

        async def preflight_published_batch_callback(
            self,
            requested_batch_id,
            *,
            callback,
            chat_id,
            now,
            allow_expired=False,
        ):
            assert requested_batch_id == batch_id
            OrderProposalsService._assert_published_batch_binding(
                batch,
                callback=callback,
                chat_id=chat_id,
                now=now,
                allow_expired=allow_expired,
            )
            return batch

        async def get_approval_batch_display(
            self, requested_batch_id, *, for_update=False
        ):
            return batch, proposals

        async def expire_mutable_rungs_if_needed(self, proposal_id, *, now):
            self.expired_ids.append(proposal_id)
            return True

        async def consume_approval_batch_nonce(self, *args, **kwargs):
            self.batch_nonce_consumes += 1
            raise AssertionError("second batch preflight must precede nonce")

    fake_service = FakeBatchService()

    class FakeSession:
        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def service_factory():
        yield FakeSession()

    monkeypatch.setattr(
        callback_module, "OrderProposalsService", lambda ignored: fake_service
    )
    result = await callback_module._handle_batch_approve(
        service_factory=service_factory,
        batch_short=str(batch_id)[:8],
        callback=_callback_for_batch(batch),
        now=started,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        telegram_user_id="777",
        revalidate_fn=lambda **kwargs: None,
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: boundary,
    )

    assert result["reason"] == "BATCH_WINDOW_BLOCKED"
    assert [row["proposal_id"] for row in result["results"]] == [
        str(item.group.proposal_id) for item in groups
    ]
    assert fake_service.batch_nonce_consumes == 0
    assert batch.approval_nonce_used_at is None
    assert fake_service.expired_ids == [groups[1].group.proposal_id]


@pytest.mark.asyncio
async def test_loss_cut_first_click_session_block_precedes_external_preview():
    now = datetime(2026, 7, 23, 8, 50, tzinfo=policy._KST)
    item = _FakeRevalidationService(valid_until=now + timedelta(days=1))

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            return SubmissionSessionEvidence(
                known=True,
                source="test:us-calendar",
                current_session="closed",
                allowed_sessions=("regular",),
                allowed_now=False,
                next_allowed_at=now + timedelta(hours=12),
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    # Bind the card to the same stable capability contract while it was open.
    async def dispatch_resolver(group, *, now):
        return SubmissionSessionEvidence(
            known=True,
            source="test:us-calendar",
            current_session="regular",
            allowed_sessions=("regular",),
            allowed_now=True,
        )

    dispatched = await evaluate_approval_window(
        item.group,
        now=now - timedelta(hours=1),
        session_resolver=dispatch_resolver,
    )
    item.group.source_asof = {"approval_window_policy_stamp": dispatched.policy_stamp}
    preview_calls = 0

    async def preview_must_not_run(**kwargs):
        nonlocal preview_calls
        preview_calls += 1
        raise AssertionError("closed session must block before loss-cut preview")

    class FakeSession:
        async def commit(self):
            return None

    result = await callback_module._handle_loss_cut_first_click(
        session=FakeSession(),
        service=item,
        proposal_id=item.group.proposal_id,
        callback=_callback_for_group(item.group),
        now=now,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        telegram_user_id="777",
        loss_cut_preview_fn=preview_must_not_run,
        window_evaluator=evaluator,
        now_fn=lambda: now,
    )

    assert result["reason"] == "DEFER_SESSION_CLOSED"
    assert preview_calls == 0
    assert item.expire_calls == 0


_JULY_23_INCIDENT = (
    ("f1e40819-e643-4abb-929c-5e049b675375", "VOO", "695.98"),
    ("5e9e8ecc-10af-4db4-bd59-3551e473eb71", "QQQM", "295.24"),
    ("4917e582-b57d-400e-9249-f3274e0adaf7", "ORCL", "117.21"),
    ("f290064a-9d42-43e7-b06a-56a4047c9da2", "AVGO", "357.18"),
    ("c8024f7e-d41a-42d3-9670-58b2606ba1f6", "QQQ", "717.15"),
    ("bfbff3ad-e3a8-4b8c-9ff6-b216381433d9", "SPYM", "89.13"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("proposal_id", "symbol", "price"), _JULY_23_INCIDENT)
async def test_july_23_incident_fixture_is_typed_expired_without_calendar_lookup(
    monkeypatch, proposal_id, symbol, price
):
    group = _group(
        account_mode="toss_live",
        symbol=symbol,
        valid_until=datetime(2026, 7, 23, 4, 55, tzinfo=policy._KST),
    )
    group.proposal_id = proposal_id
    group.limit_price = price

    calls = 0
    nonce_mints = 0
    telegram_sends = 0
    expiry_transitions = 0

    async def must_not_resolve(group, *, now):
        nonlocal calls
        calls += 1
        raise AssertionError("expired incident fixture must not query calendar")

    async def evaluator(group, *, now):
        return await evaluate_approval_window(
            group, now=now, session_resolver=must_not_resolve
        )

    class FakeDispatchService:
        async def get_proposal(self, requested_id):
            assert str(requested_id) == proposal_id
            return group, []

        async def set_approval_nonce(self, requested_id, nonce):
            nonlocal nonce_mints
            nonce_mints += 1

        async def expire_mutable_rungs_if_needed(self, requested_id, *, now):
            nonlocal expiry_transitions
            expiry_transitions += 1
            return True

    class FakeSession:
        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def service_factory():
        yield FakeSession()

    class NoSendNotifier:
        async def send_approval_message(self, *args, **kwargs):
            nonlocal telegram_sends
            telegram_sends += 1
            raise AssertionError("expired incident fixture must not publish a card")

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "test-chat"
    )
    monkeypatch.setattr(
        dispatch_module,
        "OrderProposalsService",
        lambda ignored: FakeDispatchService(),
    )

    decision = await dispatch_module.send_proposal_for_approval(
        uuid.UUID(proposal_id),
        notifier=NoSendNotifier(),
        now=datetime(2026, 7, 23, 8, 50, tzinfo=policy._KST),
        service_factory=service_factory,
        window_evaluator=evaluator,
    )

    assert decision.code is ApprovalWindowCode.EXPIRED
    assert calls == 0
    assert nonce_mints == 0
    assert telegram_sends == 0
    assert expiry_transitions == 1


@pytest.mark.asyncio
async def test_transport_hook_rechecks_after_all_preview_work_before_http():
    started = datetime(2026, 7, 22, 19, 59, 59, 999999, tzinfo=UTC)
    boundary = started + timedelta(microseconds=1)
    service = _FakeRevalidationService(
        valid_until=started + timedelta(days=2),
    )
    broker_http_calls = 0
    preview_calls = 0

    async def evaluator(group, *, now):
        session_open = now < boundary

        async def resolver(group, *, now):
            return SubmissionSessionEvidence(
                known=True,
                source="test:transport-calendar",
                current_session="regular" if session_open else "closed",
                allowed_sessions=("regular",),
                allowed_now=session_open,
                allowed_until=boundary if session_open else None,
                next_allowed_at=boundary + timedelta(hours=17),
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    async def place(**kwargs):
        nonlocal broker_http_calls, preview_calls
        if kwargs["dry_run"]:
            preview_calls += 1
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "100",
                "quantity": "1",
            }
        await kwargs["pre_send_hook"]()
        broker_http_calls += 1
        raise AssertionError("transport hook must abort before HTTP")

    initial = await evaluator(service.group, now=started)
    service.group.source_asof = {"approval_window_policy_stamp": initial.policy_stamp}
    clock_values = iter((*([started] * 9), boundary))
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=started,
        place_order_fn=place,
        correlation_mint=lambda **kwargs: "corr-never-sent",
        window_evaluator=evaluator,
        expected_policy_stamp=initial.policy_stamp,
        now_fn=lambda: next(clock_values),
    )

    assert [outcome.result for outcome in outcomes] == ["defer_session_closed"]
    assert preview_calls == 1
    assert broker_http_calls == 0
    assert service.rung.state == "pending_approval"
    assert service.resting_calls == 0


@pytest.mark.asyncio
async def test_callback_pre_send_block_leaves_nonce_unconsumed_durably():
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    service = _FakeRevalidationService(valid_until=now + timedelta(days=1))
    service.allow_nonce_consume = True
    dispatched = await _allowed_window_evaluator(service.group, now=now)
    service.group.source_asof = {
        "approval_window_policy_stamp": dispatched.policy_stamp
    }
    blocked = await evaluate_approval_window(
        service.group,
        now=now,
        session_resolver=lambda group, *, now: _closed_session(now),
    )

    async def revalidate_block(**kwargs):
        return [
            RungOutcome(
                0,
                "defer_session_closed",
                {
                    "approval_window": blocked.to_dict(),
                    "error": blocked.code.value,
                },
            )
        ]

    class FakeSession:
        commits = 0

        async def commit(self):
            self.commits += 1

    result = await callback_module._handle_approve(
        session=FakeSession(),
        service=service,
        proposal_id=service.group.proposal_id,
        callback=_callback_for_group(service.group),
        now=now,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        callback_query_id=None,
        telegram_user_id="777",
        revalidate_fn=revalidate_block,
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: now,
    )

    assert result["handled"] is False
    assert result["results"] == ["defer_session_closed"]
    assert service.nonce_consumes == 1
    assert service.group.approval_nonce == "published-nonce"
    assert service.group.approval_nonce_used_at is None
    assert service.group.approved_at is None
    assert service.group.commit_lease_until is None


@pytest.mark.asyncio
async def test_callback_mixed_zero_send_outcomes_restore_nonce_on_window_block():
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    service = _FakeRevalidationService(valid_until=now + timedelta(days=1))
    service.allow_nonce_consume = True
    dispatched = await _allowed_window_evaluator(service.group, now=now)
    service.group.source_asof = {
        "approval_window_policy_stamp": dispatched.policy_stamp
    }
    blocked = await evaluate_approval_window(
        service.group,
        now=now,
        session_resolver=lambda group, *, now: _closed_session(now),
    )

    async def revalidate_block(**kwargs):
        return [
            RungOutcome(0, "guard_blocked", {"error": "preview_guard"}),
            RungOutcome(
                1,
                "defer_session_closed",
                {
                    "approval_window": blocked.to_dict(),
                    "error": blocked.code.value,
                },
            ),
        ]

    class FakeSession:
        async def commit(self):
            return None

    result = await callback_module._handle_approve(
        session=FakeSession(),
        service=service,
        proposal_id=service.group.proposal_id,
        callback=_callback_for_group(service.group),
        now=now,
        notifier=SimpleNamespace(),
        chat_id=42,
        message_id=None,
        callback_query_id=None,
        telegram_user_id="777",
        revalidate_fn=revalidate_block,
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: now,
    )

    assert result["handled"] is False
    assert result["results"] == ["guard_blocked", "defer_session_closed"]
    assert service.group.approval_nonce == "published-nonce"
    assert service.group.approval_nonce_used_at is None
    assert service.group.approved_at is None
    assert service.group.commit_lease_until is None


async def _closed_session(now):
    return SubmissionSessionEvidence(
        known=True,
        source="test:closed",
        current_session="closed",
        allowed_sessions=("regular",),
        allowed_now=False,
        next_allowed_at=now + timedelta(hours=12),
    )


@pytest.mark.asyncio
async def test_batch_nonce_rejects_membership_added_after_preflight():
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    batch = SimpleNamespace(
        id=7,
        batch_id=uuid.uuid4(),
        chat_id="42",
        approval_nonce="batch-nonce",
        approval_nonce_used_at=None,
        expires_at=now + timedelta(minutes=5),
    )
    groups = [
        SimpleNamespace(
            id=index,
            proposal_id=uuid.uuid4(),
            valid_until=now + timedelta(days=1),
            superseded_by_proposal_id=None,
            lifecycle_state="proposed",
            exit_intent=None,
            source_asof={},
            approval_nonce=f"member-{index}",
            approval_nonce_used_at=None,
        )
        for index in (1, 2)
    ]
    members = [
        SimpleNamespace(
            id=index,
            proposal_pk=group.id,
            approval_nonce_snapshot=f"member-{index}",
            approval_message_id=100 + index,
        )
        for index, group in enumerate(groups, start=1)
    ]
    callback = _bind_batch_snapshot(batch, groups, members)

    class FakeRepo:
        updates = 0

        async def get_approval_batch_by_id(self, batch_id, *, for_update):
            assert batch_id == batch.batch_id
            assert for_update is True
            return batch

        async def list_approval_batch_members(self, batch_pk):
            assert batch_pk == batch.id
            return members

        async def get_group_by_pk(self, proposal_pk, *, for_update=False):
            assert for_update is True
            return next(group for group in groups if group.id == proposal_pk)

        async def list_rungs(self, proposal_pk):
            return [
                SimpleNamespace(
                    state="pending_approval",
                )
            ]

        async def update_approval_batch(self, *args, **kwargs):
            self.updates += 1
            raise AssertionError("changed membership must not consume batch nonce")

    service = object.__new__(OrderProposalsService)
    service._repo = FakeRepo()

    with pytest.raises(OrderProposalError, match="approval_batch_membership_changed"):
        await service.consume_approval_batch_nonce(
            batch.batch_id,
            callback=callback,
            chat_id="42",
            telegram_user_id="777",
            now=now,
            expected_members=((groups[0].proposal_id, "member-1"),),
        )

    assert service._repo.updates == 0
    assert batch.approval_nonce_used_at is None


@pytest.mark.asyncio
async def test_batch_nonce_rejects_member_consumed_after_preflight():
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    batch = SimpleNamespace(
        id=7,
        batch_id=uuid.uuid4(),
        chat_id="42",
        approval_nonce="batch-nonce",
        approval_nonce_used_at=None,
        expires_at=now + timedelta(minutes=5),
    )
    groups = [
        SimpleNamespace(
            id=index,
            proposal_id=uuid.uuid4(),
            valid_until=now + timedelta(days=1),
            superseded_by_proposal_id=None,
            lifecycle_state="proposed",
            exit_intent=None,
            source_asof={},
            approval_nonce=f"member-{index}",
            approval_nonce_used_at=now if index == 2 else None,
        )
        for index in (1, 2)
    ]
    members = [
        SimpleNamespace(
            id=index,
            proposal_pk=group.id,
            approval_nonce_snapshot=f"member-{index}",
            approval_message_id=100 + index,
        )
        for index, group in enumerate(groups, start=1)
    ]
    callback = _bind_batch_snapshot(batch, groups, members)

    class FakeRepo:
        updates = 0

        async def get_approval_batch_by_id(self, batch_id, *, for_update):
            assert for_update is True
            return batch

        async def list_approval_batch_members(self, batch_pk):
            return members

        async def get_group_by_pk(self, proposal_pk, *, for_update=False):
            assert for_update is True
            return next(group for group in groups if group.id == proposal_pk)

        async def list_rungs(self, proposal_pk):
            return [SimpleNamespace(state="pending_approval")]

        async def update_approval_batch(self, *args, **kwargs):
            self.updates += 1
            raise AssertionError("stale member must not consume batch nonce")

    service = object.__new__(OrderProposalsService)
    service._repo = FakeRepo()

    with pytest.raises(OrderProposalError, match="approval_batch_member_stale"):
        await service.consume_approval_batch_nonce(
            batch.batch_id,
            callback=callback,
            chat_id="42",
            telegram_user_id="777",
            now=now,
            expected_members=tuple(
                (group.proposal_id, str(group.approval_nonce)) for group in groups
            ),
        )

    assert service._repo.updates == 0
    assert batch.approval_nonce_used_at is None


@pytest.mark.asyncio
async def test_expired_later_rung_preserves_prior_broker_backed_rung():
    now = datetime(2026, 7, 23, 4, 55, tzinfo=policy._KST)
    group = _group(valid_until=now)
    group.proposal_id = uuid.uuid4()
    group.lifecycle_state = "partially_submitted"
    group.superseded_by_proposal_id = None
    group.source_asof = {}
    rungs = [
        SimpleNamespace(rung_index=0, state="resting"),
        SimpleNamespace(rung_index=1, state="pending_approval"),
    ]

    class MixedRungService:
        expired_indexes: list[int] = []

        async def get_proposal(self, proposal_id):
            return group, rungs

        async def expire_rung_if_needed(self, proposal_id, rung_index, *, now):
            assert rung_index == 1
            self.expired_indexes.append(rung_index)
            rungs[1].state = "expired"
            return True

    service = MixedRungService()
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=now,
        window_evaluator=_allowed_window_evaluator,
        now_fn=lambda: now,
    )

    assert [outcome.result for outcome in outcomes] == ["expired"]
    assert rungs[0].state == "resting"
    assert rungs[1].state == "expired"
    assert service.expired_indexes == [1]


@pytest.mark.asyncio
async def test_upbit_place_adapter_threads_transport_hook(monkeypatch):
    from app.services.brokers.kis.pre_send import PreSendFreshnessError
    from app.services.brokers.upbit import orders as upbit_orders

    wrapper_calls = 0
    provider_calls = 0

    async def request_with_boundary_hook(*args, pre_send_hook=None, **kwargs):
        nonlocal wrapper_calls, provider_calls
        wrapper_calls += 1
        assert pre_send_hook is not None
        await pre_send_hook()
        provider_calls += 1
        raise AssertionError("blocked hook must precede Upbit POST")

    async def block():
        raise PreSendFreshnessError(("approval_window:EXPIRED",))

    monkeypatch.setattr(
        upbit_orders._client,
        "_request_with_auth",
        request_with_boundary_hook,
    )

    with pytest.raises(PreSendFreshnessError):
        await upbit_orders.place_sell_order(
            "KRW-BTC",
            "0.1",
            "100000000",
            identifier="proposal-id",
            pre_send_hook=block,
        )

    assert wrapper_calls == 1
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_upbit_cancel_adapter_threads_transport_hook(monkeypatch):
    from app.services.brokers.kis.pre_send import PreSendFreshnessError
    from app.services.brokers.upbit import orders as upbit_orders

    wrapper_calls = 0
    provider_calls = 0

    async def request_with_boundary_hook(*args, pre_send_hook=None, **kwargs):
        nonlocal wrapper_calls, provider_calls
        wrapper_calls += 1
        assert pre_send_hook is not None
        await pre_send_hook()
        provider_calls += 1
        raise AssertionError("blocked hook must precede Upbit DELETE")

    async def block():
        raise PreSendFreshnessError(("approval_window:DEFER_SESSION_CLOSED",))

    monkeypatch.setattr(
        upbit_orders._client,
        "_request_with_auth",
        request_with_boundary_hook,
    )

    with pytest.raises(PreSendFreshnessError):
        await upbit_orders.cancel_orders(
            ["broker-order-id"],
            pre_send_hook=block,
        )

    assert wrapper_calls == 1
    assert provider_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["equity_kr", "equity_us"])
async def test_kis_cancel_adapter_threads_transport_hook(monkeypatch, market):
    from unittest.mock import AsyncMock

    from app.services.brokers.kis import domestic_orders, overseas_orders
    from app.services.brokers.kis.pre_send import PreSendFreshnessError

    provider_calls = 0

    async def request(*args, pre_send_hook=None, **kwargs):
        nonlocal provider_calls
        assert pre_send_hook is not None
        await pre_send_hook()
        provider_calls += 1
        raise AssertionError("blocked hook must precede KIS cancel POST")

    request_mock = AsyncMock(side_effect=request)
    parent = SimpleNamespace(
        _settings=SimpleNamespace(
            kis_account_no="12345678-01",
            kis_access_token="dummy-token",
        ),
        _hdr_base={},
        _ensure_token=AsyncMock(),
        _request_with_rate_limit=request_mock,
        _kis_url=lambda path: f"https://kis.invalid{path}",
    )

    async def block():
        raise PreSendFreshnessError(("approval_window:DEFER_SESSION_CLOSED",))

    if market == "equity_kr":
        monkeypatch.setattr(
            domestic_orders,
            "is_nxt_eligible",
            AsyncMock(return_value=False),
        )
        client = domestic_orders.DomesticOrderClient(parent)
        call = client.cancel_korea_order(
            "broker-order-id",
            "005930",
            1,
            70000,
            "sell",
            krx_fwdg_ord_orgno="06010",
            pre_send_hook=block,
        )
    else:
        client = overseas_orders.OverseasOrderClient(parent)
        call = client.cancel_overseas_order(
            "broker-order-id",
            "VOO",
            "NYSE",
            1,
            pre_send_hook=block,
        )

    with pytest.raises(PreSendFreshnessError):
        await call

    request_mock.assert_awaited_once()
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_toss_place_adapter_binds_hook_at_transport_context(monkeypatch):
    from app.mcp_server.tooling import orders_toss_variants as toss
    from app.services.brokers.kis.pre_send import PreSendFreshnessError
    from app.services.order_proposals.revalidation import _default_place_order_fn

    transport_calls = 0

    async def fake_toss_place(**kwargs):
        nonlocal transport_calls
        hook = toss._toss_pre_send_hook.get()
        assert hook is not None
        await hook()
        transport_calls += 1
        raise AssertionError("blocked hook must precede Toss POST")

    async def block():
        raise PreSendFreshnessError(("approval_window:DEFER_SESSION_CLOSED",))

    monkeypatch.setattr(toss, "toss_place_order", fake_toss_place)

    with pytest.raises(PreSendFreshnessError):
        await _default_place_order_fn(
            account_mode="toss_live",
            proposal_client_order_id="tosprop-123456789012345678901234",
            dry_run=False,
            symbol="VOO",
            side="buy",
            market="equity_us",
            order_type="limit",
            quantity=Decimal("1"),
            price=Decimal("100"),
            approval_hash="hash",
            pre_send_hook=block,
        )

    assert transport_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_mode,market", [("kis_live", "equity_us"), ("upbit", "crypto")]
)
async def test_default_execution_adapter_threads_transport_hook(
    monkeypatch,
    account_mode,
    market,
):
    from app.mcp_server.tooling import order_execution
    from app.services.brokers.kis.pre_send import PreSendFreshnessError
    from app.services.order_proposals.revalidation import _default_place_order_fn

    transport_calls = 0

    async def fake_place_impl(**kwargs):
        nonlocal transport_calls
        await kwargs["pre_send_hook"]()
        transport_calls += 1
        raise AssertionError("blocked hook must precede provider HTTP")

    async def block():
        raise PreSendFreshnessError(("approval_window:CALENDAR_UNKNOWN",))

    monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_impl)

    with pytest.raises(PreSendFreshnessError):
        await _default_place_order_fn(
            account_mode=account_mode,
            proposal_client_order_id="proposal-client-id",
            dry_run=False,
            symbol="VOO" if market == "equity_us" else "KRW-BTC",
            side="buy",
            market=market,
            order_type="limit",
            quantity=Decimal("1"),
            price=Decimal("100"),
            approval_hash="hash",
            pre_send_hook=block,
        )

    assert transport_calls == 0
