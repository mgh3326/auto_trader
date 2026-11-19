"""hk 3795 T3a: the ``mpb`` callback without a database.

The checks that run before anything is written (chat, data, principal) and the
mapping from a button to a plan event are exercised with fakes. The same path against
the real triggers is ``test_batches_db.py`` (CI).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.routers import telegram_callback as router
from app.services.mandate import batch_callback as cb
from app.services.mandate import batch_rules as rules
from app.services.mandate import batches as bt
from app.services.mandate import hashing
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.order_proposals.approval_message import parse_callback_data

pytestmark = pytest.mark.unit

CHAT = "-1001"
OPERATOR = 424242
OTHER = 99
NONCE = "N" * 22
NOW = datetime(2026, 11, 23, 0, 10, tzinfo=UTC)  # 09:10 KST Monday


def update(
    *,
    data: object = None,
    user: object = OPERATOR,
    chat: object = CHAT,
    action: str = "approve",
    member_id: int = 7,
) -> dict[str, Any]:
    data = (
        data
        if data is not None
        else rules.build_callback_data(action, member_id, NONCE)
    )
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cbq-1",
            "from": {"id": user},
            "message": {"message_id": 55, "chat": {"id": chat}},
            "data": data,
        },
    }


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT)
    monkeypatch.setattr(settings, "MANDATE_APPROVER_TELEGRAM_USER_IDS", str(OPERATOR))


class Notifier:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str | None]] = []
        self.edits: list[tuple[str, int, str, dict | None]] = []

    async def answer_callback(self, callback_query_id, text=None):
        self.answers.append((callback_query_id, text))
        return True

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))


class NoSession:
    """A session factory that must never be opened."""

    def __call__(self):
        raise AssertionError("the database was opened before the principal check")


# ------------------------------------------------------------- principal list (§3.5)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("  ", frozenset()),
        ("424242", frozenset({424242})),
        ("424242, 7", frozenset({424242, 7})),
        ("424242,abc", frozenset()),  # one bad entry closes the whole list
        ("0", frozenset()),
        ("-5", frozenset()),
        ("007", frozenset()),
        ("4503599627370496", frozenset()),  # 2**52
        ("4503599627370495", frozenset({4503599627370495})),
        ("424242,,7", frozenset()),
    ],
)
def test_the_approver_list_is_closed_and_fails_closed(
    monkeypatch, raw, expected
) -> None:
    monkeypatch.setattr(settings, "MANDATE_APPROVER_TELEGRAM_USER_IDS", raw)
    assert settings.mandate_approver_telegram_user_ids == expected


def test_the_approver_list_defaults_to_nobody() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["MANDATE_APPROVER_TELEGRAM_USER_IDS"].default == ""


# --------------------------------------------------------------------- authorize()
def test_a_listed_sender_in_an_allowed_chat_is_authorized() -> None:
    auth = cb.authorize(update())
    assert isinstance(auth, cb.Authorized)
    assert auth.user_id == OPERATOR and auth.parsed.member_id == 7
    assert auth.message_id == 55 and auth.callback_query_id == "cbq-1"


def test_i21_an_empty_approver_list_means_nobody_can_approve(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MANDATE_APPROVER_TELEGRAM_USER_IDS", "")
    got = cb.authorize(update())
    assert got == cb.Rejected("mandate_approver_not_allowed", "cbq-1")


def test_i21_a_sender_who_is_not_listed_is_refused_in_an_allowed_chat() -> None:
    assert cb.authorize(update(user=OTHER)).reason == "mandate_approver_not_allowed"


def test_i21_a_listed_sender_in_a_chat_that_is_not_allowed_is_refused_first() -> None:
    assert cb.authorize(update(chat="-999")).reason == "chat_not_allowed"


@pytest.mark.parametrize("bad", [True, "424242", 424242.0, None, 0, -1, 2**52])
def test_the_sender_id_must_be_an_exact_bounded_int(bad) -> None:
    assert cb.authorize(update(user=bad)).reason == "invalid_telegram_identifier"


def test_the_sender_object_must_be_an_exact_dict() -> None:
    raw = update()
    raw["callback_query"]["from"] = [("id", OPERATOR)]
    assert cb.authorize(raw).reason == "invalid_telegram_identifier"


@pytest.mark.parametrize("data", ["mpb:garbage", "mpb:a:1:short", "mpb:q:1:" + NONCE])
def test_malformed_mpb_data_is_refused(data) -> None:
    assert cb.authorize(update(data=data)).reason == "malformed_callback_data"


def test_an_update_without_a_callback_query_is_refused() -> None:
    assert cb.authorize({"message": {}}).reason == "malformed_callback_data"


# ------------------------------------------------- no row, no nonce for an unlisted id
@pytest.mark.asyncio
async def test_i21_an_unlisted_sender_never_opens_the_database_and_gets_a_reply() -> (
    None
):
    notifier = Notifier()
    result = await cb.handle_mandate_callback(
        update(user=OTHER), now=NOW, session_factory=NoSession(), notifier=notifier
    )
    assert result == {"handled": False, "reason": "mandate_approver_not_allowed"}
    assert notifier.answers == [
        ("cbq-1", cb.ANSWER_TEXT["mandate_approver_not_allowed"])
    ]
    assert notifier.edits == []


@pytest.mark.asyncio
async def test_i21_an_empty_list_never_opens_the_database(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MANDATE_APPROVER_TELEGRAM_USER_IDS", "")
    result = await cb.handle_mandate_callback(
        update(), now=NOW, session_factory=NoSession(), notifier=Notifier()
    )
    assert result["reason"] == "mandate_approver_not_allowed"


@pytest.mark.asyncio
async def test_i21_a_disallowed_chat_never_opens_the_database() -> None:
    result = await cb.handle_mandate_callback(
        update(chat="-999"), now=NOW, session_factory=NoSession(), notifier=Notifier()
    )
    assert result["reason"] == "chat_not_allowed"


# ------------------------------------------------------------------ the old path
def test_the_order_approval_callback_does_not_parse_an_mpb_button() -> None:
    data = rules.build_callback_data("approve", 7, NONCE)
    with pytest.raises(ValueError):
        parse_callback_data(data)


@pytest.mark.asyncio
async def test_the_router_sends_mpb_buttons_here_and_everything_else_unchanged(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED", False
    )
    seen: list[str] = []

    async def mandate(body, now):
        seen.append("mandate")

    async def order(body, now, now_fn):
        seen.append("order")

    monkeypatch.setattr(router, "handle_mandate_callback", mandate)
    monkeypatch.setattr(router, "handle_callback_update", order)

    assert await router.telegram_callback(update()) == {"ok": True}
    order_update = update(data="op:12345678:" + "A" * 22 + ":1:" + "B" * 12 + ":n")
    assert await router.telegram_callback(order_update) == {"ok": True}
    assert await router.telegram_callback({"message": {}}) == {"ok": True}
    assert seen == ["mandate", "order", "order"]


@pytest.mark.asyncio
async def test_an_mpb_button_never_reaches_the_durable_inbox(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED", True
    )

    async def boom(body):
        raise AssertionError("the durable inbox received an mpb button")

    async def mandate(body, now):
        return {"handled": False}

    monkeypatch.setattr(router, "_ingest_durably", boom)
    monkeypatch.setattr(router, "handle_mandate_callback", mandate)
    assert await router.telegram_callback(update()) == {"ok": True}


# ------------------------------------------------ the answer: button -> plan event
class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.locks: list[tuple[str, str]] = []


@pytest.fixture
def world(monkeypatch):
    rec = Recorder()
    batch = SimpleNamespace(
        id=3,
        realm="mock",
        basket_key="basket-1",
        market="equity_kr",
        target_session=date(2026, 11, 23),
        approval_deadline_at=datetime(2026, 11, 23, 0, 0, tzinfo=UTC),  # 09:00 KST
        expires_at=datetime(2026, 11, 23, 6, 30, tzinfo=UTC),  # 15:30 KST
    )
    plan = SimpleNamespace(
        id=11,
        symbol="005930",
        planned_amount=Decimal("2350000"),
        adopted_amount=Decimal("0"),
        sector_key="chips",
        thesis="thesis",
        invalidation_conditions=["x falls"],
    )
    terms = SimpleNamespace(id=21)

    def member(kind: str):
        return SimpleNamespace(
            id=7,
            batch_id=3,
            plan_id=11,
            card_no=1,
            kind=kind,
            terms_id=21,
            terms_sha256="a" * 64,
            plan_sha256="b" * 64,
            nonce_digest=rules.nonce_digest(NONCE),
        )

    class Session:
        async def get(self, model, key):
            return terms

        async def commit(self):
            rec.commits += 1

        async def rollback(self):
            rec.rollbacks += 1

    state = {"member": member("new_plan"), "refusal": None, "orders": 2}

    async def get_member(session, member_id):
        return state["member"]

    async def get_batch(session, batch_id):
        return batch

    async def lock(session, realm, basket_key):
        rec.locks.append((realm, basket_key))

    async def refusal(session, mem, bat, moment):
        return state["refusal"]

    async def get_plan(session, plan_id):
        return plan

    async def open_orders(session, plan_id):
        return state["orders"]

    async def append(session, **kwargs):
        rec.events.append(kwargs)

    monkeypatch.setattr(bt, "get_member", get_member)
    monkeypatch.setattr(bt, "get_batch", get_batch)
    monkeypatch.setattr(bt, "open_order_count", open_orders)
    monkeypatch.setattr(ms, "get_plan", get_plan)
    monkeypatch.setattr(ms, "append_plan_event", append)
    monkeypatch.setattr(cb, "acquire_basket_lock", lock)
    monkeypatch.setattr(cb, "_refusal", refusal)
    return SimpleNamespace(
        rec=rec, state=state, member=member, session=Session(), batch=batch
    )


class FixedCalendar:
    def bounds(self, market: str, day: date):
        if day.weekday() >= 5:
            return None
        zone = rules.MARKET_ZONE[market]
        return (
            datetime(day.year, day.month, day.day, 9, 0, tzinfo=zone),
            datetime(day.year, day.month, day.day, 15, 30, tzinfo=zone),
        )


def auth_for(action: str) -> cb.Authorized:
    got = cb.authorize(update(action=action))
    assert isinstance(got, cb.Authorized)
    return got


BEFORE_OPEN = datetime(2026, 11, 22, 23, 40, tzinfo=UTC)  # Mon 08:40 KST
AFTER_OPEN = NOW  # Mon 09:10 KST


@pytest.mark.asyncio
async def test_new_plan_approve_before_the_deadline_is_approved_effective_on_the_target(
    world,
) -> None:
    out = await cb.answer(
        world.session, auth_for("approve"), now=BEFORE_OPEN, calendar=FixedCalendar()
    )
    assert out.handled and out.reason == "approved"
    (event,) = world.rec.events
    assert event["event"] == "approved" and event["plan_id"] == 11
    assert event["effective_session"] == date(2026, 11, 23)
    assert event["batch_member_id"] == 7
    assert (event["terms_id"], event["terms_sha256"]) == (21, "a" * 64)
    assert event["actor"] == f"operator:telegram:{OPERATOR}"
    assert world.rec.commits == 1 and world.rec.rollbacks == 0
    assert world.rec.locks == [("mock", "basket-1")]


@pytest.mark.asyncio
async def test_new_plan_approve_after_the_deadline_takes_effect_the_next_session(
    world,
) -> None:
    out = await cb.answer(
        world.session, auth_for("approve"), now=AFTER_OPEN, calendar=FixedCalendar()
    )
    assert out.handled
    assert world.rec.events[0]["effective_session"] == date(2026, 11, 24)


@pytest.mark.asyncio
async def test_new_plan_reject_is_rejected_with_the_card_clause_and_no_effective_session(
    world,
) -> None:
    out = await cb.answer(
        world.session, auth_for("reject"), now=AFTER_OPEN, calendar=FixedCalendar()
    )
    assert out.handled and out.reason == "rejected"
    (event,) = world.rec.events
    assert event["event"] == "rejected"
    assert "effective_session" not in event
    assert event["terms_id"] == 21 and event["batch_member_id"] == 7
    assert event["evidence"] == {"reason_code": "batch_rejected"}


@pytest.mark.asyncio
async def test_reapproval_approve_is_terms_rebound(world) -> None:
    world.state["member"] = world.member("reapproval")
    out = await cb.answer(
        world.session, auth_for("approve"), now=BEFORE_OPEN, calendar=FixedCalendar()
    )
    assert out.handled
    (event,) = world.rec.events
    assert event["event"] == "terms_rebound"
    assert event["effective_session"] == date(2026, 11, 23)


@pytest.mark.asyncio
async def test_reapproval_reject_retires_the_plan_and_records_the_open_order_count(
    world,
) -> None:
    world.state["member"] = world.member("reapproval")
    out = await cb.answer(
        world.session, auth_for("reject"), now=AFTER_OPEN, calendar=FixedCalendar()
    )
    assert out.handled and out.facts is not None
    assert out.facts.open_order_count == 2
    (event,) = world.rec.events
    assert event["event"] == "retired"
    assert event["evidence"] == {
        "reason_code": "reapproval_rejected",
        "open_order_count": 2,
    }


@pytest.mark.asyncio
async def test_the_events_a_button_may_write_are_exactly_the_four_of_the_design(
    world,
) -> None:
    written: set[str] = set()
    for kind in v.BATCH_KINDS:
        for action in ("approve", "reject"):
            world.state["member"] = world.member(kind)
            world.rec.events.clear()
            await cb.answer(
                world.session,
                auth_for(action),
                now=AFTER_OPEN,
                calendar=FixedCalendar(),
            )
            written.add(world.rec.events[0]["event"])
    assert written == {"approved", "rejected", "terms_rebound", "retired"}


@pytest.mark.asyncio
async def test_a_wrong_nonce_writes_nothing(world) -> None:
    other = rules.build_callback_data("approve", 7, "M" * 22)
    auth = cb.authorize(update(data=other))
    out = await cb.answer(world.session, auth, now=AFTER_OPEN, calendar=FixedCalendar())
    assert out == cb.Outcome(False, "mandate_nonce_mismatch")
    assert world.rec.events == [] and world.rec.commits == 0
    assert world.rec.locks == []  # refused before the basket lock


@pytest.mark.asyncio
async def test_an_unknown_card_writes_nothing(world) -> None:
    world.state["member"] = None
    out = await cb.answer(
        world.session, auth_for("approve"), now=AFTER_OPEN, calendar=FixedCalendar()
    )
    assert out == cb.Outcome(False, "card_not_found")
    assert world.rec.events == []


@pytest.mark.parametrize(
    "refusal",
    [
        "mandate_card_superseded",
        "mandate_batch_answered",
        "mandate_batch_expired",
        "mandate_binding_broken",
        "mandate_terms_not_current",
        "mandate_expired",
    ],
)
@pytest.mark.asyncio
async def test_every_refusal_writes_nothing_and_rolls_back(world, refusal) -> None:
    world.state["refusal"] = refusal
    out = await cb.answer(
        world.session, auth_for("approve"), now=AFTER_OPEN, calendar=FixedCalendar()
    )
    assert out == cb.Outcome(False, refusal)
    assert world.rec.events == [] and world.rec.commits == 0
    assert world.rec.rollbacks == 1


@pytest.mark.asyncio
async def test_a_refused_event_from_the_store_rolls_back_with_a_closed_reason(
    world, monkeypatch
) -> None:
    async def refuse(session, **kwargs):
        raise ms.MandateError(v.MandateRefusal.INVALID_TRANSITION, "state changed")

    monkeypatch.setattr(ms, "append_plan_event", refuse)
    out = await cb.answer(
        world.session, auth_for("approve"), now=AFTER_OPEN, calendar=FixedCalendar()
    )
    assert out == cb.Outcome(False, "mandate_invalid_transition")
    assert world.rec.commits == 0 and world.rec.rollbacks == 1


@pytest.mark.asyncio
async def test_a_calendar_without_the_next_session_refuses_the_approval(world) -> None:
    class Closed:
        def bounds(self, market, day):
            return None

    out = await cb.answer(
        world.session, auth_for("approve"), now=AFTER_OPEN, calendar=Closed()
    )
    assert out == cb.Outcome(False, "mandate_calendar_unknown")
    assert world.rec.events == []


@pytest.mark.asyncio
async def test_handle_replies_and_edits_the_card_after_the_commit(world) -> None:
    notifier = Notifier()

    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return world.session

        async def __aexit__(self, *exc):
            return False

    result = await cb.handle_mandate_callback(
        update(),
        now=AFTER_OPEN,
        session_factory=Factory(),
        notifier=notifier,
        calendar=FixedCalendar(),
    )
    assert result == {"handled": True, "reason": "approved"}
    assert notifier.answers == [("cbq-1", cb.ANSWER_TEXT["approved"])]
    (chat, message_id, text, markup) = notifier.edits[0]
    assert (chat, message_id, markup) == (CHAT, 55, {"inline_keyboard": []})
    assert "결과: 승인했습니다." in text


@pytest.mark.asyncio
async def test_a_lost_reply_does_not_change_the_result(world) -> None:
    class Broken(Notifier):
        async def answer_callback(self, *a, **k):
            raise RuntimeError("telegram down")

        async def edit_message(self, *a, **k):
            raise RuntimeError("telegram down")

    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return world.session

        async def __aexit__(self, *exc):
            return False

    result = await cb.handle_mandate_callback(
        update(),
        now=AFTER_OPEN,
        session_factory=Factory(),
        notifier=Broken(),
        calendar=FixedCalendar(),
    )
    assert result["handled"] is True
    assert world.rec.commits == 1


# ----------------------------------------------------------- the refusal ordering
@pytest.fixture
def checks(monkeypatch):
    """``_refusal`` with every input in its passing state; a test breaks one at a time."""
    member = SimpleNamespace(
        id=7,
        batch_id=3,
        plan_id=11,
        card_no=1,
        terms_id=21,
        terms_sha256="",
        plan_sha256="p" * 64,
    )
    batch = SimpleNamespace(expires_at=NOW + timedelta(hours=1))
    plan = SimpleNamespace(sleeve_id=5)
    version = SimpleNamespace(state="live")
    terms = SimpleNamespace(
        id=21,
        content_sha256="",
        expires_at=NOW + timedelta(days=30),
        sleeve_version_id=9,
    )
    content = {"k": "v"}
    digest = hashing.content_sha256(content)
    member.terms_sha256 = digest
    terms.content_sha256 = digest
    state = {
        "latest": member,
        "answer": None,
        "plan_hash": member.plan_sha256,
        "live": terms,
        "version": version,
        "content": content,
    }

    async def latest(session, batch_id, plan_id):
        return state["latest"]

    async def answer_event(session, batch_id, plan_id):
        return state["answer"]

    async def get_plan(session, plan_id):
        return plan

    async def current_terms(session, sleeve_id):
        return state["version"], state["live"]

    class Session:
        async def get(self, model, key):
            return terms if model.__name__ == "MandateTerms" else version

    monkeypatch.setattr(bt, "latest_card", latest)
    monkeypatch.setattr(bt, "answer_event", answer_event)
    monkeypatch.setattr(bt, "plan_sha256", lambda p: state["plan_hash"])
    monkeypatch.setattr(ms, "get_plan", get_plan)
    monkeypatch.setattr(ms, "current_terms", current_terms)
    monkeypatch.setattr(ms, "terms_content_from_rows", lambda t, v_: state["content"])
    return SimpleNamespace(
        member=member, batch=batch, terms=terms, state=state, session=Session()
    )


@pytest.mark.asyncio
async def test_refusal_none_when_everything_holds(checks) -> None:
    assert await cb._refusal(checks.session, checks.member, checks.batch, NOW) is None


@pytest.mark.asyncio
async def test_refusal_superseded_when_a_newer_card_exists(checks) -> None:
    checks.state["latest"] = SimpleNamespace(id=8)
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_card_superseded"


@pytest.mark.asyncio
async def test_refusal_answered_comes_before_expiry(checks) -> None:
    checks.state["answer"] = object()
    checks.batch.expires_at = NOW - timedelta(hours=1)
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_batch_answered"


@pytest.mark.asyncio
async def test_refusal_expired_at_and_after_the_expiry(checks) -> None:
    checks.batch.expires_at = NOW
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_batch_expired"


@pytest.mark.asyncio
async def test_refusal_binding_broken_when_the_plan_hash_differs(checks) -> None:
    checks.state["plan_hash"] = "c" * 64
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_binding_broken"


@pytest.mark.asyncio
async def test_refusal_binding_broken_when_the_clause_rows_no_longer_hash_to_the_card(
    checks,
) -> None:
    checks.state["content"] = {"k": "tampered"}
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_binding_broken"


@pytest.mark.asyncio
async def test_refusal_terms_not_current_when_the_live_clause_moved_on(checks) -> None:
    checks.state["live"] = SimpleNamespace(id=22)
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_terms_not_current"


@pytest.mark.asyncio
async def test_refusal_terms_not_current_when_the_sleeve_is_not_live(checks) -> None:
    checks.state["version"] = SimpleNamespace(state="stopped")
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_terms_not_current"


@pytest.mark.asyncio
async def test_refusal_terms_expired(checks) -> None:
    checks.terms.expires_at = NOW
    got = await cb._refusal(checks.session, checks.member, checks.batch, NOW)
    assert got == "mandate_expired"
