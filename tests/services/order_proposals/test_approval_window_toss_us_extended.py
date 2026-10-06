"""#1116 Toss US extended-session capability behind a default-off policy key.

The key is ``order_proposals.approval_window.toss_live_us_sessions``. Its
default ``[regular]`` must reproduce the pre-#1116 decisions exactly; when the
operator adds ``pre``/``post`` only Toss US integer-quantity LIMIT place
proposals may use them, every other shape is refused before any broker call,
and a pre/post submission carries an unmeasured DAY-expiry record.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.brokers.toss.market_calendar import (
    TossSessionWindow,
    TossUsMarketDay,
    parse_us_market_calendar,
    us_toss_session_for,
)
from app.services.order_proposals import approval_window as policy
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    SubmissionSessionEvidence,
    apply_toss_us_extended_order_shape,
    evaluate_approval_window,
)
from app.services.order_proposals.revalidation import (
    ApprovalWindowPreSendGate,
    PreSendFreshnessError,
    revalidate_and_submit,
)
from app.services.trading_policy_service import toss_live_us_approval_sessions
from tests.services.order_proposals.test_approval_window import (
    _FakeRevalidationService,
)

_KST = policy._KST


def _w(start: str, end: str) -> dict[str, str]:
    return {"startTime": start, "endTime": end}


# US 2026-09-30 (EDT) with its next business day. Toss KST windows:
# day 09:00-17:00, pre 17:00-22:30, regular 22:30-05:00, post 05:00-08:00.
_SUMMER = parse_us_market_calendar(
    {
        "today": {
            "date": "2026-09-30",
            "dayMarket": _w("2026-09-30T09:00:00+09:00", "2026-09-30T17:00:00+09:00"),
            "preMarket": _w("2026-09-30T17:00:00+09:00", "2026-09-30T22:30:00+09:00"),
            "regularMarket": _w(
                "2026-09-30T22:30:00+09:00", "2026-10-01T05:00:00+09:00"
            ),
            "afterMarket": _w("2026-10-01T05:00:00+09:00", "2026-10-01T08:00:00+09:00"),
        },
        "nextBusinessDay": {
            "date": "2026-10-01",
            "dayMarket": _w("2026-10-01T09:00:00+09:00", "2026-10-01T17:00:00+09:00"),
            "preMarket": _w("2026-10-01T17:00:00+09:00", "2026-10-01T22:30:00+09:00"),
            "regularMarket": _w(
                "2026-10-01T22:30:00+09:00", "2026-10-02T05:00:00+09:00"
            ),
            "afterMarket": _w("2026-10-02T05:00:00+09:00", "2026-10-02T08:00:00+09:00"),
        },
    }
)

# DST shift: US DST ends Sunday 2026-11-01. Friday 10-30 is the last EDT
# session; Monday 11-02 is the first EST session, one hour later in KST.
_DST_SHIFT = parse_us_market_calendar(
    {
        "today": {
            "date": "2026-10-30",
            "dayMarket": _w("2026-10-30T09:00:00+09:00", "2026-10-30T17:00:00+09:00"),
            "preMarket": _w("2026-10-30T17:00:00+09:00", "2026-10-30T22:30:00+09:00"),
            "regularMarket": _w(
                "2026-10-30T22:30:00+09:00", "2026-10-31T05:00:00+09:00"
            ),
            "afterMarket": _w("2026-10-31T05:00:00+09:00", "2026-10-31T08:00:00+09:00"),
        },
        "nextBusinessDay": {
            "date": "2026-11-02",
            "dayMarket": _w("2026-11-02T10:00:00+09:00", "2026-11-02T18:00:00+09:00"),
            "preMarket": _w("2026-11-02T18:00:00+09:00", "2026-11-02T23:30:00+09:00"),
            "regularMarket": _w(
                "2026-11-02T23:30:00+09:00", "2026-11-03T06:00:00+09:00"
            ),
            "afterMarket": _w("2026-11-03T06:00:00+09:00", "2026-11-03T09:00:00+09:00"),
        },
    }
)


def _kst(*args: int) -> datetime:
    return datetime(*args, tzinfo=_KST)


def _use_calendar(monkeypatch, calendar) -> dict[str, int]:
    calls = {"calendar": 0}

    async def calendar_reader(market, query_date):
        assert market == "us"
        calls["calendar"] += 1
        return calendar

    monkeypatch.setattr(policy, "get_toss_market_calendar", calendar_reader)
    return calls


def _use_key(monkeypatch, sessions: tuple[str, ...]) -> None:
    monkeypatch.setattr(policy, "toss_live_us_approval_sessions", lambda: sessions)


def _group(
    *,
    account_mode: str = "toss_live",
    market: str = "equity_us",
    order_type: str = "limit",
    action: str = "place",
    valid_until: datetime | None = None,
):
    return SimpleNamespace(
        proposal_id=uuid.uuid4(),
        market=market,
        account_mode=account_mode,
        symbol="SGOV" if market == "equity_us" else "005930",
        valid_until=valid_until or _kst(2026, 12, 31),
        action=action,
        order_type=order_type,
        exit_intent=None,
        exit_reason=None,
        approval_nonce=None,
        approval_dispatch_membership_revision=None,
    )


def _rung(quantity="1", limit_price="100", notional=None):
    return SimpleNamespace(
        rung_index=0,
        state="pending_approval",
        side="buy",
        quantity=None if quantity is None else Decimal(quantity),
        limit_price=None if limit_price is None else Decimal(limit_price),
        notional=None if notional is None else Decimal(notional),
    )


# --- verbatim copy of origin/main f111be804 _resolve_toss_us_session --------
# (and the three helpers it used), kept here as the A1 equality oracle.


def _main_containing_window(windows, now):
    return next((window for window in windows if window.contains(now)), None)


def _main_next_window_start(windows, *, after):
    candidates = [window.start for window in windows if window.start >= after]
    return min(candidates) if candidates else None


def _main_toss_us_regular_windows(calendar):
    return sorted(
        (
            day.regular_market
            for day in calendar.days
            if isinstance(day, TossUsMarketDay) and day.regular_market is not None
        ),
        key=lambda window: window.start,
    )


def _main_resolver(calendar):
    async def _main_resolve_toss_us_session(group, *, now):
        local = now.astimezone(_KST)
        if calendar is None:
            return SubmissionSessionEvidence(
                known=False,
                source="toss_market_calendar:us",
                current_session="unknown",
                allowed_sessions=("regular",),
                allowed_now=False,
                detail="calendar_unavailable",
            )
        regular_windows = _main_toss_us_regular_windows(calendar)
        session = us_toss_session_for(local, calendar=calendar) or "closed"
        current = _main_containing_window(regular_windows, local)
        next_open = _main_next_window_start(
            regular_windows,
            after=current.end if current is not None else local,
        )
        if not regular_windows or (session != "regular" and next_open is None):
            return SubmissionSessionEvidence(
                known=False,
                source="toss_market_calendar:us",
                current_session=session,
                allowed_sessions=("regular",),
                allowed_now=False,
                detail="regular_window_unavailable",
            )
        return SubmissionSessionEvidence(
            known=True,
            source="toss_market_calendar:us",
            current_session=session,
            allowed_sessions=("regular",),
            allowed_now=session == "regular",
            allowed_until=current.end
            if session == "regular" and current is not None
            else None,
            next_allowed_at=next_open,
        )

    return _main_resolve_toss_us_session


_SUMMER_INSTANTS = [
    _kst(2026, 9, 30, 8, 59, 59),  # closed before the day market
    _kst(2026, 9, 30, 9, 0, 0),  # day market
    _kst(2026, 9, 30, 16, 59, 59),  # day market last second
    _kst(2026, 9, 30, 17, 0, 0),  # pre start
    _kst(2026, 9, 30, 20, 0, 0),  # pre
    _kst(2026, 9, 30, 22, 29, 59),  # pre last second
    _kst(2026, 9, 30, 22, 30, 0),  # regular start
    _kst(2026, 10, 1, 4, 59, 59),  # regular last second
    _kst(2026, 10, 1, 5, 0, 0),  # post start
    _kst(2026, 10, 1, 7, 59, 59),  # post last second
    _kst(2026, 10, 1, 8, 0, 0),  # closed after post
]


# --- A1: the default key is byte-identical to main ---------------------------


def test_shipped_policy_key_defaults_to_regular_only():
    assert toss_live_us_approval_sessions() == ("regular",)


@pytest.mark.asyncio
@pytest.mark.parametrize("now", _SUMMER_INSTANTS)
@pytest.mark.parametrize(
    ("order_type", "action"),
    [("limit", "place"), ("market", "place"), ("limit", "cancel")],
)
async def test_default_key_decisions_equal_main_for_every_session(
    monkeypatch, now, order_type, action
):
    """A1: the shipped key (no monkeypatch) decides exactly like main."""
    _use_calendar(monkeypatch, _SUMMER)
    group = _group(order_type=order_type, action=action)

    new = await evaluate_approval_window(group, now=now)
    main = await evaluate_approval_window(
        group, now=now, session_resolver=_main_resolver(_SUMMER)
    )

    assert new.to_dict() == main.to_dict()
    assert new.policy_stamp == main.policy_stamp
    # The rung-shape gate is inert under the default key, even for a
    # fractional amount-based rung in pre/post.
    shaped = apply_toss_us_extended_order_shape(
        new, group=group, rungs=[_rung("0.5", notional="10")]
    )
    assert shaped == new


@pytest.mark.asyncio
async def test_default_key_calendar_unavailable_equals_main(monkeypatch):
    _use_calendar(monkeypatch, None)
    group = _group()
    now = _kst(2026, 9, 30, 17, 0, 0)
    new = await evaluate_approval_window(group, now=now)
    main = await evaluate_approval_window(
        group, now=now, session_resolver=_main_resolver(None)
    )
    assert new.to_dict() == main.to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("now", _SUMMER_INSTANTS)
async def test_unreadable_policy_falls_back_to_main_regular_only(monkeypatch, now):
    _use_calendar(monkeypatch, _SUMMER)

    def broken_policy():
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(policy, "toss_live_us_approval_sessions", broken_policy)
    group = _group()
    new = await evaluate_approval_window(group, now=now)
    main = await evaluate_approval_window(
        group, now=now, session_resolver=_main_resolver(_SUMMER)
    )
    assert new.to_dict() == main.to_dict()


def test_policy_key_schema_is_closed_and_canonical():
    from pydantic import ValidationError

    from app.schemas.trading_policy import OrderProposalApprovalWindowPolicy

    assert OrderProposalApprovalWindowPolicy().toss_live_us_sessions == ("regular",)
    assert OrderProposalApprovalWindowPolicy(
        toss_live_us_sessions=["post", "regular", "pre"]
    ).toss_live_us_sessions == ("pre", "regular", "post")
    for bad in (["pre"], ["day", "regular"], ["regular", "regular"], []):
        with pytest.raises(ValidationError):
            OrderProposalApprovalWindowPolicy(toss_live_us_sessions=bad)
    with pytest.raises(ValidationError):
        OrderProposalApprovalWindowPolicy(toss_live_us_sessions=["regular"], x=1)


# --- A2: key with pre --------------------------------------------------------


@pytest.mark.asyncio
async def test_key_with_pre_allows_toss_integer_limit_in_pre_only(monkeypatch):
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular"))
    group = _group()

    pre = await evaluate_approval_window(group, now=_kst(2026, 9, 30, 17, 30))
    day = await evaluate_approval_window(group, now=_kst(2026, 9, 30, 12, 0))
    post = await evaluate_approval_window(group, now=_kst(2026, 10, 1, 6, 0))

    assert pre.code is ApprovalWindowCode.ALLOW
    assert pre.evidence.current_session == "pre"
    assert pre.evidence.allowed_sessions == ("pre", "regular")
    assert pre.evidence.allowed_until == _kst(2026, 9, 30, 22, 30)
    assert apply_toss_us_extended_order_shape(pre, group=group, rungs=[_rung()]) == pre
    assert day.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert day.evidence.current_session == "day"
    assert day.evidence.next_allowed_at == _kst(2026, 9, 30, 17, 0)
    assert post.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert post.evidence.current_session == "post"
    assert post.evidence.next_allowed_at == _kst(2026, 10, 1, 17, 0)
    # The allowed session set is part of the stamp: a card dispatched under
    # the default key cannot be approved after the key flips (and back).
    default_stamp = policy._policy_stamp(
        group,
        SubmissionSessionEvidence(
            known=True,
            source="x",
            current_session="regular",
            allowed_sessions=("regular",),
            allowed_now=True,
        ),
    )
    assert pre.policy_stamp != default_stamp


@pytest.mark.asyncio
async def test_key_with_pre_leaves_kis_us_deferred_and_never_reads_key(monkeypatch):
    def key_must_not_be_read():
        raise AssertionError("KIS US / KR / crypto must not read the Toss key")

    monkeypatch.setattr(policy, "toss_live_us_approval_sessions", key_must_not_be_read)

    async def calendar_must_not_be_read(market, query_date):
        raise AssertionError("KIS US does not use the Toss calendar")

    monkeypatch.setattr(policy, "get_toss_market_calendar", calendar_must_not_be_read)
    kis = await evaluate_approval_window(
        _group(account_mode="kis_live"), now=_kst(2026, 9, 30, 17, 30)
    )
    assert kis.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert kis.evidence.source == "exchange_calendars:XNYS"
    assert kis.evidence.allowed_sessions == ("regular",)

    crypto = await evaluate_approval_window(
        _group(account_mode="upbit", market="crypto"),
        now=_kst(2026, 9, 30, 17, 30),
    )
    assert crypto.code is ApprovalWindowCode.ALLOW


@pytest.mark.asyncio
async def test_key_never_reaches_kr_toss_or_kis(monkeypatch):
    def key_must_not_be_read():
        raise AssertionError("KR must not read the Toss US key")

    monkeypatch.setattr(policy, "toss_live_us_approval_sessions", key_must_not_be_read)

    async def kr_resolver(group, now):
        return SubmissionSessionEvidence(
            known=True,
            source="test:kr",
            current_session="regular",
            allowed_sessions=("regular",),
            allowed_now=True,
            allowed_until=now + timedelta(hours=1),
        )

    monkeypatch.setattr(policy, "_resolve_kr_session", kr_resolver)
    for account_mode in ("toss_live", "kis_live"):
        decision = await evaluate_approval_window(
            _group(account_mode=account_mode, market="equity_kr"),
            now=_kst(2026, 9, 30, 10, 0),
        )
        assert decision.code is ApprovalWindowCode.ALLOW
        assert decision.evidence.source == "test:kr"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_type", "action", "reason"),
    [
        ("market", "place", "market_order"),
        ("limit", "cancel", "action_not_place:cancel"),
        ("limit", "replace", "action_not_place:replace"),
    ],
)
async def test_key_keeps_non_limit_place_groups_regular_only(
    monkeypatch, order_type, action, reason
):
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    group = _group(order_type=order_type, action=action)
    for now in (_kst(2026, 9, 30, 17, 0), _kst(2026, 10, 1, 5, 0)):
        decision = await evaluate_approval_window(group, now=now)
        assert decision.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
        assert decision.evidence.allowed_sessions == ("regular",)
        assert decision.evidence.detail == (
            f"toss_us_extended_session_refused:{reason}"
        )
    regular = await evaluate_approval_window(group, now=_kst(2026, 9, 30, 22, 30))
    assert regular.code is ApprovalWindowCode.ALLOW
    assert regular.evidence.detail is None


# --- A3: each refusal, with zero broker calls --------------------------------


def _toss_revalidation_service(
    *, order_type="limit", quantity="1", notional=None
) -> _FakeRevalidationService:
    service = _FakeRevalidationService(valid_until=_kst(2026, 10, 9))
    service.group.account_mode = "toss_live"
    service.group.market = "equity_us"
    service.group.symbol = "SGOV"
    service.group.side = "buy"
    service.group.order_type = order_type
    service.rung.side = "buy"
    service.rung.quantity = Decimal(quantity)
    service.rung.notional = None if notional is None else Decimal(notional)
    return service


def _broker_trap():
    calls = {"broker": 0}

    async def place_must_not_run(**kwargs):
        calls["broker"] += 1
        raise AssertionError("refusal must precede any broker preview/submit")

    async def fetch_must_not_run(**kwargs):
        calls["broker"] += 1
        raise AssertionError("refusal must precede any broker lookup")

    async def cancel_must_not_run(**kwargs):
        calls["broker"] += 1
        raise AssertionError("refusal must precede any broker cancel")

    async def opposite_must_not_run(**kwargs):
        calls["broker"] += 1
        raise AssertionError("refusal must precede any broker read")

    return calls, {
        "place_order_fn": place_must_not_run,
        "fetch_target_fn": fetch_must_not_run,
        "cancel_target_fn": cancel_must_not_run,
        "opposite_pending_check_fn": opposite_must_not_run,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ({"order_type": "market"}, "market_order"),
        ({"notional": "10"}, "amount_based_order"),
        ({"quantity": "0.5"}, "fractional_quantity"),
    ],
    ids=["market", "order_amount", "fractional_qty"],
)
@pytest.mark.parametrize(
    "now",
    [_kst(2026, 9, 30, 17, 0, 0), _kst(2026, 10, 1, 5, 0, 0)],
    ids=["pre", "post"],
)
async def test_extended_session_refusal_happens_before_any_broker_call(
    monkeypatch, shape, reason, now
):
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    service = _toss_revalidation_service(**shape)
    stamp = (await evaluate_approval_window(service.group, now=now)).policy_stamp
    calls, trap = _broker_trap()

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=now,
        window_evaluator=evaluate_approval_window,
        expected_policy_stamp=stamp,
        **trap,
    )

    assert [outcome.result for outcome in outcomes] == ["defer_session_closed"]
    window = outcomes[0].detail["approval_window"]
    assert window["session_evidence"]["detail"] == (
        f"toss_us_extended_session_refused:{reason}"
    )
    assert "day_expiry" not in window["session_evidence"]
    assert calls == {"broker": 0}
    assert service.rung.state == "pending_approval"


@pytest.mark.asyncio
async def test_integer_limit_in_pre_reaches_the_broker_preview(monkeypatch):
    """Positive control for the trap above: the eligible shape is not refused."""
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular"))
    now = _kst(2026, 9, 30, 17, 0, 0)
    service = _toss_revalidation_service()
    stamp = (await evaluate_approval_window(service.group, now=now)).policy_stamp
    previews: list[dict] = []

    async def preview_then_stop(**kwargs):
        previews.append(kwargs)
        return {"success": False, "error": "stop after preview"}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=service.group.proposal_id,
        now=now,
        place_order_fn=preview_then_stop,
        window_evaluator=evaluate_approval_window,
        expected_policy_stamp=stamp,
    )

    assert len(previews) == 1
    assert previews[0]["dry_run"] is True
    assert previews[0]["order_type"] == "limit"
    assert previews[0]["quantity"] == Decimal("1")
    assert [outcome.result for outcome in outcomes] == ["error"]


@pytest.mark.asyncio
async def test_transport_hook_refuses_fractional_after_regular_rolls_into_post(
    monkeypatch,
):
    """The last gate before HTTP re-applies the shape when the session moved."""
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    service = _toss_revalidation_service(quantity="0.5")
    regular_now = _kst(2026, 10, 1, 4, 59, 59)
    stamp = (
        await evaluate_approval_window(service.group, now=regular_now)
    ).policy_stamp
    post_now = _kst(2026, 10, 1, 5, 0, 0)
    gate = ApprovalWindowPreSendGate(
        group=service.group,
        rung=service.rung,
        window_evaluator=evaluate_approval_window,
        expected_policy_stamp=stamp,
        now_fn=lambda: post_now,
    )
    with pytest.raises(PreSendFreshnessError):
        await gate()
    assert gate.blocked_decision is not None
    assert gate.blocked_decision.detail == (
        "toss_us_extended_session_refused:fractional_quantity"
    )

    integer = _toss_revalidation_service()
    open_gate = ApprovalWindowPreSendGate(
        group=integer.group,
        rung=integer.rung,
        window_evaluator=evaluate_approval_window,
        expected_policy_stamp=stamp,
        now_fn=lambda: post_now,
    )
    await open_gate()
    assert open_gate.blocked_decision is None


@pytest.mark.parametrize(
    ("rung", "reason"),
    [
        (_rung("1.5"), "fractional_quantity"),
        (_rung("0"), "quantity_invalid"),
        (_rung("-1"), "quantity_invalid"),
        (_rung("NaN"), "quantity_invalid"),
        (_rung(None), "quantity_invalid"),
        (_rung("1", limit_price=None), "limit_price_missing"),
        (_rung("1", notional="100"), "amount_based_order"),
    ],
)
def test_rung_shape_refusals_are_closed(rung, reason):
    assert policy.toss_us_extended_order_refusal(_group(), [rung]) == reason
    assert policy.toss_us_extended_order_refusal(_group(), []) == "rungs_missing"
    assert policy.toss_us_extended_order_refusal(_group(), [_rung("3")]) is None


@pytest.mark.asyncio
async def test_dispatch_publishes_no_card_for_fractional_order_in_pre(monkeypatch):
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular"))
    now = _kst(2026, 9, 30, 17, 0, 0)
    group = _group()
    nonce_mints = 0
    recorded: list[str] = []

    class FakeDispatchService:
        async def get_proposal(self, proposal_id):
            return group, [_rung("0.25")]

        async def set_approval_nonce(self, proposal_id, nonce):
            nonlocal nonce_mints
            nonce_mints += 1

        async def start_approval_dispatch(self, *args, **kwargs):
            return None

        async def finish_approval_dispatch(self, *args, publication, **kwargs):
            recorded.append(publication.failure_code)

    class FakeSession:
        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def service_factory():
        yield FakeSession()

    class NoSendNotifier:
        async def send_approval_message(self, *args, **kwargs):
            raise AssertionError("refused shape must not publish a card")

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "test-chat"
    )
    monkeypatch.setattr(
        dispatch_module, "OrderProposalsService", lambda ignored: FakeDispatchService()
    )

    result = await dispatch_module.send_proposal_for_approval(
        group.proposal_id,
        notifier=NoSendNotifier(),
        now=now,
        service_factory=service_factory,
    )

    assert result.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert result.evidence.next_allowed_at == _kst(2026, 9, 30, 22, 30)
    assert nonce_mints == 0
    assert recorded == [
        "DEFER_SESSION_CLOSED/toss_us_extended_session_refused:fractional_quantity"
    ]


# --- A4: exact KST boundaries, including a DST-shift week --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calendar", "now", "session", "allowed"),
    [
        (_SUMMER, _kst(2026, 9, 30, 16, 59, 59), "day", False),
        (_SUMMER, _kst(2026, 9, 30, 17, 0, 0), "pre", True),
        (_SUMMER, _kst(2026, 9, 30, 22, 29, 59), "pre", True),
        (_SUMMER, _kst(2026, 9, 30, 22, 30, 0), "regular", True),
        (_SUMMER, _kst(2026, 10, 1, 4, 59, 59), "regular", True),
        (_SUMMER, _kst(2026, 10, 1, 5, 0, 0), "post", True),
        (_SUMMER, _kst(2026, 10, 1, 7, 59, 59), "post", True),
        (_SUMMER, _kst(2026, 10, 1, 8, 0, 0), "closed", False),
        # Last EDT session, then the first EST session one hour later.
        (_DST_SHIFT, _kst(2026, 10, 31, 7, 59, 59), "post", True),
        (_DST_SHIFT, _kst(2026, 10, 31, 8, 0, 0), "closed", False),
        (_DST_SHIFT, _kst(2026, 11, 2, 17, 0, 0), "day", False),
        (_DST_SHIFT, _kst(2026, 11, 2, 17, 59, 59), "day", False),
        (_DST_SHIFT, _kst(2026, 11, 2, 18, 0, 0), "pre", True),
        (_DST_SHIFT, _kst(2026, 11, 2, 22, 30, 0), "pre", True),
        (_DST_SHIFT, _kst(2026, 11, 2, 23, 29, 59), "pre", True),
        (_DST_SHIFT, _kst(2026, 11, 2, 23, 30, 0), "regular", True),
        (_DST_SHIFT, _kst(2026, 11, 3, 5, 0, 0), "regular", True),
        (_DST_SHIFT, _kst(2026, 11, 3, 5, 59, 59), "regular", True),
        (_DST_SHIFT, _kst(2026, 11, 3, 6, 0, 0), "post", True),
    ],
)
async def test_session_boundaries_at_the_exact_second(
    monkeypatch, calendar, now, session, allowed
):
    _use_calendar(monkeypatch, calendar)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    decision = await evaluate_approval_window(_group(), now=now)
    assert decision.evidence.current_session == session
    assert decision.allowed is allowed
    if allowed:
        assert decision.evidence.allowed_until > now
    else:
        assert decision.evidence.next_allowed_at > now

    # The default key at the same second: regular only, as on main.
    monkeypatch.setattr(policy, "toss_live_us_approval_sessions", lambda: ("regular",))
    default = await evaluate_approval_window(_group(), now=now)
    assert default.allowed is (session == "regular")


@pytest.mark.asyncio
async def test_closed_weekend_across_dst_points_to_the_est_pre_open(monkeypatch):
    _use_calendar(monkeypatch, _DST_SHIFT)
    _use_key(monkeypatch, ("pre", "regular"))
    decision = await evaluate_approval_window(_group(), now=_kst(2026, 10, 31, 8, 0, 0))
    assert decision.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert decision.evidence.next_allowed_at == _kst(2026, 11, 2, 18, 0, 0)


# --- A5: DAY expiry of a pre/post submission is recorded, unmeasured ---------


@pytest.mark.asyncio
async def test_pre_submission_records_documented_but_unmeasured_day_expiry(
    monkeypatch,
):
    _use_calendar(monkeypatch, _DST_SHIFT)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    pre = await evaluate_approval_window(_group(), now=_kst(2026, 11, 2, 18, 0, 0))
    assert pre.to_dict()["session_evidence"]["day_expiry"] == {
        "time_in_force": "DAY",
        "submission_session": "pre",
        "expected_expiry_at": "2026-11-03T06:00:00+09:00",
        "basis": "toss_openapi_doc:day_cancels_unfilled_at_regular_close",
        "measured": False,
    }

    post = await evaluate_approval_window(_group(), now=_kst(2026, 11, 3, 6, 0, 0))
    assert post.to_dict()["session_evidence"]["day_expiry"] == {
        "time_in_force": "DAY",
        "submission_session": "post",
        "expected_expiry_at": None,
        "basis": "unmeasured:toss_day_order_submitted_after_regular_close",
        "measured": False,
    }

    regular = await evaluate_approval_window(_group(), now=_kst(2026, 11, 2, 23, 30, 0))
    assert "day_expiry" not in regular.to_dict()["session_evidence"]


def test_day_expiry_never_reads_kr_day_expiry_key():
    window = TossSessionWindow(
        start=_kst(2026, 9, 30, 17, 0), end=_kst(2026, 9, 30, 22, 30)
    )
    regular = TossSessionWindow(
        start=_kst(2026, 9, 30, 22, 30), end=_kst(2026, 10, 1, 5, 0)
    )
    expectation = policy._toss_us_day_expiry("pre", window, [regular])
    assert expectation.expected_expiry_at == _kst(2026, 10, 1, 5, 0)
    assert expectation.measured is False
    # KR toss_live DAY expiry is "15:30"; nothing here may resemble it.
    assert "15:30" not in repr(expectation)


@pytest.mark.asyncio
async def test_card_in_pre_persists_day_expiry_on_the_approval_record(
    monkeypatch, db_session
):
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput
    from tests.services.order_proposals.test_dispatch import (
        _FakeNotifier,
        _session_factory,
    )

    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular"))
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"chat-{uuid.uuid4().hex}",
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="SGOV",
        market="equity_us",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        thesis="t1116 measurement shape",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        valid_until=_kst(2026, 10, 1, 9, 0),
        now=_kst(2026, 9, 30, 16, 0),
    )
    await db_session.commit()
    now = _kst(2026, 9, 30, 17, 5)

    dispatch = await dispatch_module.send_proposal_for_approval(
        group.proposal_id,
        notifier=_FakeNotifier(message_id=7001),
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert dispatch.ok is True
    refreshed, _ = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.source_asof["approval_window_day_expiry"] == {
        "time_in_force": "DAY",
        "submission_session": "pre",
        "expected_expiry_at": "2026-10-01T05:00:00+09:00",
        "basis": "toss_openapi_doc:day_cancels_unfilled_at_regular_close",
        "measured": False,
    }


@pytest.mark.asyncio
async def test_default_key_card_writes_no_day_expiry(monkeypatch, db_session):
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput
    from tests.services.order_proposals.test_dispatch import (
        _FakeNotifier,
        _session_factory,
    )

    _use_calendar(monkeypatch, _SUMMER)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"chat-{uuid.uuid4().hex}",
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="SGOV",
        market="equity_us",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        thesis="t1116 default",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        valid_until=_kst(2026, 10, 1, 9, 0),
        now=_kst(2026, 9, 30, 16, 0),
    )
    await db_session.commit()

    dispatch = await dispatch_module.send_proposal_for_approval(
        group.proposal_id,
        notifier=_FakeNotifier(message_id=7101),
        now=_kst(2026, 9, 30, 23, 0),
        service_factory=_session_factory(db_session),
    )

    assert dispatch.ok is True
    refreshed, _ = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert "approval_window_day_expiry" not in refreshed.source_asof
    assert refreshed.source_asof["approval_window_policy_stamp"]


# --- r1 tester findings: protective exits and batch paths --------------------
# Ported from the round-1 tester reproductions (hk job
# 1116-toss-us-ext-session-20260930-1719, tester-r1-repro.py).


def _fake_valid_toss_preview(kwargs):
    quantity, price = str(kwargs["quantity"]), str(kwargs["price"])
    return {
        "success": True,
        "approval_hash": "t1116-token",
        "quantity": quantity,
        "price": price,
        "payload_preview": {
            "clientOrderId": kwargs["proposal_client_order_id"],
            "quantity": quantity,
            "price": price,
        },
    }


def _loss_cut_service(quantity: str) -> _FakeRevalidationService:
    item = _toss_revalidation_service(quantity=quantity)
    item.group.side = item.rung.side = "sell"
    item.group.exit_intent = "loss_cut"
    item.group.exit_reason = "stop_loss"
    item.group.retrospective_id = 1
    item.group.approval_issue_id = "T1116"
    return item


def _counting_place(calls: dict[str, int]):
    async def place(**kwargs):
        if kwargs["dry_run"]:
            calls["preview"] += 1
            return _fake_valid_toss_preview(kwargs)
        await kwargs["pre_send_hook"]()
        calls["submit"] += 1
        return {"success": True, "status": "resting", "broker_order_id": "t1116-order"}

    return place


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now",
    [_kst(2026, 9, 30, 17, 0, 0), _kst(2026, 10, 1, 5, 0, 0)],
    ids=["pre", "post"],
)
async def test_enabled_key_refuses_fractional_loss_cut_before_any_broker_call(
    monkeypatch, now
):
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    item = _loss_cut_service("0.5")
    calls = {"preview": 0, "submit": 0}
    stamp = (await evaluate_approval_window(item.group, now=now)).policy_stamp

    outcomes = await revalidate_and_submit(
        service=item,
        proposal_id=item.group.proposal_id,
        now=now,
        now_fn=lambda: now,
        place_order_fn=_counting_place(calls),
        correlation_mint=lambda **kwargs: "t1116-corr",
        expected_policy_stamp=stamp,
    )

    assert calls == {"preview": 0, "submit": 0}
    assert outcomes[0].result == "defer_session_closed"
    assert outcomes[0].detail["approval_window"]["detail"] == (
        "toss_us_extended_session_refused:fractional_quantity"
    )


@pytest.mark.asyncio
async def test_enabled_key_loss_cut_integer_limit_and_regular_stay_exempt(monkeypatch):
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    for quantity, now in (
        ("1", _kst(2026, 9, 30, 17, 0, 0)),
        ("0.5", _kst(2026, 9, 30, 22, 30, 0)),
        ("0.5", _kst(2026, 9, 30, 12, 0, 0)),
    ):
        item = _loss_cut_service(quantity)
        decision = await policy.evaluate_approval_window_boundary(
            item.group,
            window_evaluator=evaluate_approval_window,
            now_fn=lambda now=now: now,
            rungs=[item.rung],
        )
        assert decision.code is ApprovalWindowCode.ALLOW
        assert decision.evidence.current_session == "exempt"


@pytest.mark.asyncio
async def test_enabled_key_loss_cut_with_unknown_calendar_stays_exempt(monkeypatch):
    _use_calendar(monkeypatch, None)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    item = _loss_cut_service("0.5")
    now = _kst(2026, 9, 30, 17, 0, 0)
    decision = await policy.evaluate_approval_window_boundary(
        item.group,
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: now,
        rungs=[item.rung],
    )
    assert decision.code is ApprovalWindowCode.ALLOW
    assert decision.evidence.current_session == "exempt"


@pytest.mark.asyncio
async def test_default_key_loss_cut_exemption_is_unchanged_and_io_free(monkeypatch):
    """A1 for protective exits: the default key adds no calendar I/O."""

    # Counted, not raised: the exit session lookup is fail-open and would
    # swallow an exception, hiding the read.
    calendar_calls = _use_calendar(monkeypatch, _SUMMER)
    item = _loss_cut_service("0.5")
    now = _kst(2026, 9, 30, 17, 0, 0)
    with_rungs = await policy.evaluate_approval_window_boundary(
        item.group,
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: now,
        rungs=[item.rung],
    )
    without_rungs = await policy.evaluate_approval_window_boundary(
        item.group, window_evaluator=evaluate_approval_window, now_fn=lambda: now
    )
    assert with_rungs == without_rungs
    assert with_rungs.code is ApprovalWindowCode.ALLOW
    assert calendar_calls == {"calendar": 0}


@pytest.mark.asyncio
async def test_enabled_key_loss_cut_first_click_refuses_before_confirmation_preview(
    monkeypatch,
):
    from app.services.order_proposals import telegram_callback as callback_module
    from tests.services.order_proposals.test_approval_window import (
        _callback_for_group,
    )

    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    now = _kst(2026, 9, 30, 17, 0, 0)
    item = _loss_cut_service("0.5")
    previews: list[object] = []

    async def preview(**kwargs):
        previews.append(kwargs["proposal_id"])
        raise AssertionError("refusal must precede the confirmation preview")

    class FakeSession:
        async def commit(self):
            return None

    class Notifier:
        async def edit_message(self, *args, **kwargs):
            return None

    outcome = await callback_module._handle_loss_cut_first_click(
        session=FakeSession(),
        service=item,
        proposal_id=item.group.proposal_id,
        callback=_callback_for_group(item.group),
        now=now,
        notifier=Notifier(),
        chat_id=42,
        message_id=None,
        telegram_user_id="777",
        loss_cut_preview_fn=preview,
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: now,
    )
    assert previews == []
    assert outcome["reason"] == "DEFER_SESSION_CLOSED"
    assert outcome["approval_window"]["detail"] == (
        "toss_us_extended_session_refused:fractional_quantity"
    )


async def _seed_batch_pair(monkeypatch, db_session, first_quantity):
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        "t1116-" + uuid.uuid4().hex,
    )

    async def advisory(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatch_module, "build_create_advisory", advisory)
    service = OrderProposalsService(db_session)
    groups = []
    for symbol, quantity in (("SGOV", first_quantity), ("BIL", "1")):
        groups.append(
            await service.create_proposal(
                symbol=symbol,
                market="equity_us",
                account_mode="toss_live",
                side="sell",
                order_type="limit",
                proposer="t1116",
                thesis="t1116 batch",
                rungs=[RungInput(0, "sell", Decimal(quantity), Decimal("100"), None)],
                valid_until=_kst(2026, 10, 1, 8),
                now=_kst(2026, 10, 1, 4, 50),
            )
        )
    await db_session.commit()
    return groups


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_quantity", "expected_summaries"), [("1", 1), ("0.5", 0)]
)
async def test_batch_summary_in_post_checks_every_member_rung(
    monkeypatch, db_session, first_quantity, expected_summaries
):
    from tests.services.order_proposals.test_dispatch import (
        _FakeNotifier,
        _session_factory,
    )

    groups = await _seed_batch_pair(monkeypatch, db_session, first_quantity)
    notifier = _FakeNotifier(message_id=8001)
    for group, now in zip(
        groups, (_kst(2026, 10, 1, 4, 59), _kst(2026, 10, 1, 5, 1)), strict=True
    ):
        result = await dispatch_module.send_proposal_for_approval(
            group.proposal_id,
            notifier=notifier,
            now=now,
            service_factory=_session_factory(db_session),
        )
        assert result.ok
    summaries = sum("*일괄 승인 대기*" in text for text, _, _ in notifier.sent_messages)
    assert summaries == expected_summaries


@pytest.mark.asyncio
async def test_batch_callback_with_fractional_member_blocks_before_nonce_or_sibling(
    monkeypatch, db_session
):
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals import telegram_callback as callback_module
    from tests.services.order_proposals.test_approval_window import (
        _callback_for_batch,
    )
    from tests.services.order_proposals.test_dispatch import (
        _FakeNotifier,
        _session_factory,
    )

    groups = await _seed_batch_pair(monkeypatch, db_session, "0.5")
    notifier = _FakeNotifier(message_id=9001)
    chat = settings.ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR
    for group, now in zip(
        groups, (_kst(2026, 10, 1, 4, 58), _kst(2026, 10, 1, 4, 59)), strict=True
    ):
        result = await dispatch_module.send_proposal_for_approval(
            group.proposal_id,
            notifier=notifier,
            now=now,
            service_factory=_session_factory(db_session),
        )
        assert result.ok
    service = OrderProposalsService(db_session)
    _text, keyboard, _chat = next(
        message
        for message in notifier.sent_messages
        if "*일괄 승인 대기*" in message[0]
    )
    envelope = callback_module.parse_callback_data(
        keyboard["inline_keyboard"][0][0]["callback_data"]
    )
    assert envelope is not None
    batch_id = await service.resolve_approval_batch_id_prefix(envelope.subject_short)
    batch = await service._repo.get_approval_batch_by_id(batch_id)
    assert batch is not None
    now = _kst(2026, 10, 1, 5, 0, 0)
    calls = {"preview": 0, "submit": 0}

    async def revalidate(**kwargs):
        return await revalidate_and_submit(
            **kwargs,
            now_fn=lambda: now,
            place_order_fn=_counting_place(calls),
            correlation_mint=lambda **ignored: "t1116-batch-corr",
        )

    outcome = await callback_module._handle_batch_approve(
        service_factory=_session_factory(db_session),
        batch_short=str(batch.batch_id)[:8],
        callback=_callback_for_batch(batch),
        now=now,
        notifier=notifier,
        chat_id=chat,
        message_id=None,
        telegram_user_id="777",
        revalidate_fn=revalidate,
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: now,
    )
    assert outcome["reason"] == "BATCH_WINDOW_BLOCKED"
    assert calls == {"preview": 0, "submit": 0}
    assert batch.approval_nonce_used_at is None


def test_every_production_window_boundary_call_passes_rungs():
    """Static guard: no production gate may evaluate the window without rungs.

    The Toss US pre/post order-shape rule lives inside
    evaluate_approval_window_boundary and only sees the rungs its caller
    passes; a caller that forgets them would silently skip the rule.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app"
    missing: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in {
                "evaluate_approval_window_boundary",
                "_evaluate_bound_window",
            }:
                continue
            if path.name == "telegram_callback.py" and name == (
                "evaluate_approval_window_boundary"
            ):
                # The wrapper forwards its required rungs argument.
                assert any(k.arg == "rungs" for k in node.keywords)
                continue
            if not any(k.arg == "rungs" for k in node.keywords):
                missing.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    assert missing == []


# --- r2 tester finding: a session roll during/after the exit calendar await --
# Ported from the round-2 tester reproductions (tester-r2-repro.py).

_ROLLS = [
    (_SUMMER, _kst(2026, 10, 1, 4, 59, 59), _kst(2026, 10, 1, 5, 0, 0), "post"),
    (_SUMMER, _kst(2026, 9, 30, 16, 59, 59), _kst(2026, 9, 30, 17, 0, 0), "pre"),
    (_DST_SHIFT, _kst(2026, 11, 3, 5, 59, 59), _kst(2026, 11, 3, 6, 0, 0), "post"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("calendar", "start", "end", "session"), _ROLLS)
async def test_exit_classifies_the_session_after_the_calendar_await(
    monkeypatch, calendar, start, end, session
):
    _use_key(monkeypatch, ("pre", "regular", "post"))
    item = _loss_cut_service("0.5")
    item.group.valid_until = end + timedelta(days=1)
    current = start

    async def crossing_calendar(market, query_date):
        nonlocal current
        current = end
        return calendar

    monkeypatch.setattr(policy, "get_toss_market_calendar", crossing_calendar)
    decision = await policy.evaluate_approval_window_boundary(
        item.group,
        rungs=[item.rung],
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: current,
    )
    assert decision.observed_at == end
    assert decision.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
    assert decision.evidence.current_session == session
    assert decision.detail == "toss_us_extended_session_refused:fractional_quantity"


@pytest.mark.asyncio
@pytest.mark.parametrize(("calendar", "start", "end", "session"), _ROLLS)
async def test_exit_interval_ends_with_its_session_so_a_later_roll_fails_closed(
    monkeypatch, calendar, start, end, session
):
    """A roll after the classification sample is caught by the recheck."""
    _use_calendar(monkeypatch, calendar)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    item = _loss_cut_service("0.5")
    item.group.valid_until = end + timedelta(days=1)
    clock = iter((start, start, end))

    decision = await policy.evaluate_approval_window_boundary(
        item.group,
        rungs=[item.rung],
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: next(clock),
    )
    assert decision.allowed is False
    assert decision.observed_at == end


@pytest.mark.asyncio
async def test_exit_integer_limit_keeps_exemption_bounded_by_its_session(monkeypatch):
    _use_calendar(monkeypatch, _SUMMER)
    _use_key(monkeypatch, ("pre", "regular", "post"))
    item = _loss_cut_service("1")
    now = _kst(2026, 9, 30, 17, 30)
    decision = await policy.evaluate_approval_window_boundary(
        item.group,
        rungs=[item.rung],
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: now,
    )
    assert decision.code is ApprovalWindowCode.ALLOW
    assert decision.evidence.current_session == "exempt"
    assert decision.evidence.allowed_until == _kst(2026, 9, 30, 22, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calendar", "start", "end"),
    [(c, s, e) for c, s, e, _session in _ROLLS if e.hour in (5, 6)],
)
async def test_loss_cut_transport_calendar_roll_blocks_the_wire(
    monkeypatch, calendar, start, end
):
    _use_key(monkeypatch, ("pre", "regular", "post"))
    item = _loss_cut_service("0.5")
    item.group.valid_until = end + timedelta(days=1)
    current, armed = start, False
    calls = {"preview": 0, "wire": 0}

    async def crossing_calendar(market, query_date):
        nonlocal current
        if armed:
            current = end
        return calendar

    async def place(**kwargs):
        nonlocal armed
        if kwargs["dry_run"]:
            calls["preview"] += 1
            return _fake_valid_toss_preview(kwargs)
        armed = True
        await kwargs["pre_send_hook"]()
        calls["wire"] += 1
        return {"success": True, "status": "resting", "broker_order_id": "t1116-roll"}

    monkeypatch.setattr(policy, "get_toss_market_calendar", crossing_calendar)
    stamp = (await evaluate_approval_window(item.group, now=start)).policy_stamp
    outcomes = await revalidate_and_submit(
        service=item,
        proposal_id=item.group.proposal_id,
        now=start,
        now_fn=lambda: current,
        place_order_fn=place,
        expected_policy_stamp=stamp,
        correlation_mint=lambda **ignored: "t1116-roll-corr",
    )
    assert calls == {"preview": 1, "wire": 0}
    assert outcomes[0].result == "defer_session_closed"
    assert item.rung.state == "pending_approval"


@pytest.mark.asyncio
async def test_loss_cut_first_click_second_calendar_roll_blocks_preview(monkeypatch):
    from app.services.order_proposals import telegram_callback as callback_module
    from tests.services.order_proposals.test_approval_window import (
        _callback_for_group,
    )

    _use_key(monkeypatch, ("pre", "regular", "post"))
    item = _loss_cut_service("0.5")
    start, end = _kst(2026, 10, 1, 4, 59, 59), _kst(2026, 10, 1, 5, 0, 0)
    current, lookups = start, 0
    previews: list[object] = []

    async def crossing_calendar(market, query_date):
        nonlocal current, lookups
        lookups += 1
        if lookups == 2:
            current = end
        return _SUMMER

    async def preview(**kwargs):
        previews.append(kwargs["now"])
        raise AssertionError("refusal must precede the confirmation preview")

    class FakeSession:
        async def commit(self):
            return None

    class Notifier:
        async def edit_message(self, *args, **kwargs):
            return None

    monkeypatch.setattr(policy, "get_toss_market_calendar", crossing_calendar)
    outcome = await callback_module._handle_loss_cut_first_click(
        session=FakeSession(),
        service=item,
        proposal_id=item.group.proposal_id,
        callback=_callback_for_group(item.group),
        now=start,
        notifier=Notifier(),
        chat_id=42,
        message_id=None,
        telegram_user_id="777",
        loss_cut_preview_fn=preview,
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: current,
    )
    assert previews == []
    assert outcome["reason"] == "DEFER_SESSION_CLOSED"


# --- r3 tester finding: a session roll during the awaited revalidating --------
# transition must not reach the broker preview or the target read.
# Ported from the round-3 tester reproductions (tester-r3-repro.py).

_TRANSITION_ROLLS = [
    pytest.param(
        _SUMMER, _kst(2026, 9, 30, 16, 59, 59), _kst(2026, 9, 30, 17), id="edt-day-pre"
    ),
    pytest.param(
        _SUMMER, _kst(2026, 10, 1, 4, 59, 59), _kst(2026, 10, 1, 5), id="edt-reg-post"
    ),
    pytest.param(
        _DST_SHIFT,
        _kst(2026, 11, 2, 17, 59, 59),
        _kst(2026, 11, 2, 18),
        id="est-day-pre",
    ),
    pytest.param(
        _DST_SHIFT,
        _kst(2026, 11, 3, 5, 59, 59),
        _kst(2026, 11, 3, 6),
        id="est-reg-post",
    ),
]


def _rolling_transition(item, *, end):
    """Wrap transition_rung so the clock rolls when a rung enters revalidating."""
    state = {"current": None}
    transition = item.transition_rung

    async def crossing_transition(*args, **kwargs):
        result = await transition(*args, **kwargs)
        if kwargs.get("new_state") == "revalidating":
            state["current"] = end
        return result

    item.transition_rung = crossing_transition
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(("calendar", "start", "end"), _TRANSITION_ROLLS)
@pytest.mark.parametrize("shape", ["fractional", "amount", "integer"])
async def test_exit_roll_during_revalidating_transition_precedes_preview(
    monkeypatch, calendar, start, end, shape
):
    _use_key(monkeypatch, ("pre", "regular", "post"))
    _use_calendar(monkeypatch, calendar)
    item = _loss_cut_service("0.5" if shape == "fractional" else "1")
    if shape == "amount":
        item.rung.notional = Decimal("100")
    item.group.valid_until = end + timedelta(days=2)
    clock = _rolling_transition(item, end=end)
    clock["current"] = start
    calls = {"preview": 0, "submit": 0}

    stamp = (await evaluate_approval_window(item.group, now=start)).policy_stamp
    outcomes = await revalidate_and_submit(
        service=item,
        proposal_id=item.group.proposal_id,
        now=start,
        now_fn=lambda: clock["current"],
        place_order_fn=_counting_place(calls),
        expected_policy_stamp=stamp,
        correlation_mint=lambda **ignored: "t1116-r3-corr",
    )
    if shape == "integer":
        assert calls == {"preview": 1, "submit": 1}
        assert outcomes[0].result == "submitted_resting"
    else:
        assert calls == {"preview": 0, "submit": 0}
        assert outcomes[0].result == "defer_session_closed"
        assert item.rung.state == "pending_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calendar", "start", "end", "expected"),
    [
        (
            _SUMMER,
            _kst(2026, 10, 1, 4, 59, 59),
            _kst(2026, 10, 1, 5),
            "defer_session_closed",
        ),
        # The DST fixture has no regular window after this post session, so
        # the regular-only action fails closed as unknown.
        (
            _DST_SHIFT,
            _kst(2026, 11, 3, 5, 59, 59),
            _kst(2026, 11, 3, 6),
            "calendar_unknown",
        ),
    ],
)
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_action_roll_during_revalidating_transition_precedes_target_read(
    monkeypatch, calendar, start, end, expected, action
):
    from dataclasses import replace as dc_replace

    from app.services.order_proposals.target_order import TargetOrderSnapshot

    _use_key(monkeypatch, ("pre", "regular", "post"))
    _use_calendar(monkeypatch, calendar)
    item = _toss_revalidation_service()
    item.group.action = action
    item.group.side = item.rung.side = "sell"
    item.group.target_broker_order_id = "r3-target"
    item.group.valid_until = end + timedelta(days=2)
    approved = TargetOrderSnapshot(
        broker_order_id="r3-target",
        symbol="SGOV",
        side="sell",
        order_type="limit",
        limit_price="100",
        remaining_quantity="1",
        status="open",
        observed_at=start.isoformat(),
    )
    item.group.source_asof = {"target_order_snapshot": approved.to_payload()}
    clock = _rolling_transition(item, end=end)
    clock["current"] = start
    calls = {"preview": 0, "fetch": 0, "cancel": 0, "opposite": 0}

    async def fetch_target(**kwargs):
        calls["fetch"] += 1
        return dc_replace(approved, observed_at=clock["current"].isoformat())

    async def place(**kwargs):
        calls["preview"] += 1
        raise AssertionError("place trap")

    async def cancel(**kwargs):
        calls["cancel"] += 1
        raise AssertionError("cancel trap")

    async def opposite(**kwargs):
        calls["opposite"] += 1
        raise AssertionError("opposite trap")

    stamp = (await evaluate_approval_window(item.group, now=start)).policy_stamp
    outcomes = await revalidate_and_submit(
        service=item,
        proposal_id=item.group.proposal_id,
        now=start,
        now_fn=lambda: clock["current"],
        place_order_fn=place,
        expected_policy_stamp=stamp,
        fetch_target_fn=fetch_target,
        cancel_target_fn=cancel,
        opposite_pending_check_fn=opposite,
    )
    assert calls == {"preview": 0, "fetch": 0, "cancel": 0, "opposite": 0}
    assert outcomes[0].result == expected
    assert item.rung.state == "pending_approval"


@pytest.mark.asyncio
async def test_real_service_transition_roll_blocks_before_fractional_preview(
    monkeypatch, db_session
):
    from types import SimpleNamespace as NS

    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals import service as service_module
    from app.services.order_proposals.service import RungInput

    _use_key(monkeypatch, ("pre", "regular", "post"))
    _use_calendar(monkeypatch, _SUMMER)
    start, end = _kst(2026, 10, 1, 4, 59, 59), _kst(2026, 10, 1, 5)
    calls = {"preview": 0, "submit": 0}

    async def retrospective(session, retro_id):
        return NS(symbol="SGOV", trigger_type="stop_loss", created_at=start)

    monkeypatch.setattr(service_module, "get_retrospective_by_id", retrospective)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="SGOV",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="t1116-r3",
        thesis="t1116 transition roll",
        rungs=[RungInput(0, "sell", Decimal("0.5"), Decimal("100"), None)],
        valid_until=end + timedelta(days=2),
        now=start,
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=1,
        approval_issue_id="T1116-R3",
    )
    await db_session.commit()
    clock = _rolling_transition(service, end=end)
    clock["current"] = start

    stamp = (await evaluate_approval_window(group, now=start)).policy_stamp
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=start,
        now_fn=lambda: clock["current"],
        place_order_fn=_counting_place(calls),
        expected_policy_stamp=stamp,
    )
    await db_session.commit()
    stored_group, rungs = await service.get_proposal(group.proposal_id)
    assert calls == {"preview": 0, "submit": 0}
    assert outcomes[0].result == "defer_session_closed"
    assert rungs[0].state == "pending_approval"
    assert stored_group.exit_intent == "loss_cut"
