"""R37 — Telegram numeric identifiers are exact, bounded wire values.

Telegram's ``from.id`` and ``update_id`` arrive at an order-adjacent trust
boundary.  They are not Python numbers to be coerced: a valid value is an
exact built-in ``int`` in the documented range, and its decimal text is made
only after that validation.  This module independently pins that contract at
the inline normalizer, durable ingress, digest helpers, worker reconstruction
and the real inbox row.

The protected object deliberately records every rendering/coercion hook.  No
assertion or parameter id interpolates it, so a causal RED failure cannot
become a disclosure surface itself.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import IntEnum
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import FakeNotifier, make_update

INVALID_IDENTIFIER_REASON = "invalid_telegram_identifier"
TELEGRAM_USER_ID_MAX = 2**52 - 1
TELEGRAM_UPDATE_ID_MAX = 2_147_483_647


class _IdentifierIntSubclass(int):
    """An integer-shaped value that is not an exact Telegram wire integer."""


class _IdentifierIntEnum(IntEnum):
    ONE = 1


class _IdentifierPoison:
    """Records every prohibited coercion or rendering operation."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __str__(self) -> str:
        self.calls.append("str")
        return "r37-identifier-private-marker"

    def __repr__(self) -> str:
        self.calls.append("repr")
        return "r37-identifier-private-marker"

    def __int__(self) -> int:
        self.calls.append("int")
        return 1

    def __index__(self) -> int:
        self.calls.append("index")
        return 1

    def __hash__(self) -> int:
        self.calls.append("hash")
        return 1

    def __eq__(self, other: object) -> bool:
        self.calls.append("eq")
        return False


def _callback_data() -> str:
    """A valid envelope; each test changes only one identifier boundary."""
    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )

    proposal_id = uuid.UUID("0123abcd-1111-4222-8333-444444444444")
    nonce = "nonce123456"
    return build_callback_data(
        action="op",
        proposal_id=proposal_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=uuid.UUID("11111111-2222-4333-8444-555555555555"),
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            membership_digest=build_membership_digest(
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=1,
                members=[{"proposal_id": str(proposal_id), "approval_nonce": nonce}],
            ),
        ),
    )


def _invalid_values(limit: int) -> tuple[tuple[str, object], ...]:
    """Independent hostile matrix shared by the two numeric contracts."""
    return (
        ("none", None),
        ("bool", True),
        ("int_subclass", _IdentifierIntSubclass(1)),
        ("int_enum", _IdentifierIntEnum.ONE),
        ("float", 1.0),
        ("decimal", Decimal("1")),
        ("numeric_text", "1"),
        ("leading_zero_text", "01"),
        ("mapping", {"id": 1}),
        ("list", [1]),
        ("poison", _IdentifierPoison()),
        ("zero", 0),
        ("negative", -1),
        ("above_limit", limit + 1),
    )


def _invalid_present_values(limit: int) -> tuple[tuple[str, object], ...]:
    """``update_id`` may be absent, but every present value is exact-int only."""
    return tuple(item for item in _invalid_values(limit) if item[0] != "none")


def _normalized_row(*, telegram_user_id: object) -> SimpleNamespace:
    """A valid stored envelope except for the one DB identifier under test."""
    return SimpleNamespace(
        action="op",
        subject_short="0123abcd",
        membership_digest="abcdefghijkl",
        nonce="nonce123456",
        membership_revision=1,
        dispatch_attempt_id=uuid.UUID("11111111-2222-4333-8444-555555555555"),
        chat_id="42",
        callback_query_id="cbq-r37",
        message_id=555,
        telegram_user_id=telegram_user_id,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("user_id", "update_id"),
    (
        (1, 1),
        (TELEGRAM_USER_ID_MAX, TELEGRAM_UPDATE_ID_MAX),
        (777, 7),
    ),
    ids=("minimum", "maximum", "representative"),
)
def test_exact_bounded_wire_identifiers_normalize_without_changing_valid_values(
    user_id: int, update_id: int
) -> None:
    from app.services.order_proposals.telegram_callback import normalize_callback_update

    normalized = normalize_callback_update(
        make_update(data=_callback_data(), user_id=user_id, update_id=update_id)
    )

    assert type(normalized.telegram_user_id) is int
    assert normalized.telegram_user_id == user_id
    assert type(normalized.update_id) is int
    assert normalized.update_id == update_id


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "user_id"),
    _invalid_values(TELEGRAM_USER_ID_MAX),
    ids=tuple(case for case, _value in _invalid_values(TELEGRAM_USER_ID_MAX)),
)
def test_normalization_rejects_every_noncanonical_telegram_user_id(
    case: str, user_id: object
) -> None:
    __tracebackhide__ = True
    from app.services.order_proposals.telegram_callback import (
        CallbackNotNormalizable,
        normalize_callback_update,
    )

    with pytest.raises(CallbackNotNormalizable) as excinfo:
        normalize_callback_update(make_update(data=_callback_data(), user_id=user_id))

    assert excinfo.value.reason == INVALID_IDENTIFIER_REASON
    if isinstance(user_id, _IdentifierPoison):
        assert user_id.calls == [], case


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "update_id"),
    _invalid_present_values(TELEGRAM_UPDATE_ID_MAX),
    ids=tuple(case for case, _value in _invalid_present_values(TELEGRAM_UPDATE_ID_MAX)),
)
def test_normalization_rejects_every_noncanonical_update_id(
    case: str, update_id: object
) -> None:
    __tracebackhide__ = True
    from app.services.order_proposals.telegram_callback import (
        CallbackNotNormalizable,
        normalize_callback_update,
    )

    with pytest.raises(CallbackNotNormalizable) as excinfo:
        normalize_callback_update(
            make_update(data=_callback_data(), update_id=update_id)
        )

    assert excinfo.value.reason == INVALID_IDENTIFIER_REASON
    if isinstance(update_id, _IdentifierPoison):
        assert update_id.calls == [], case


@pytest.mark.unit
def test_normalization_requires_a_telegram_user_id_even_when_update_id_is_valid() -> (
    None
):
    from app.services.order_proposals.telegram_callback import (
        CallbackNotNormalizable,
        normalize_callback_update,
    )

    update = make_update(data=_callback_data(), update_id=7)
    update["callback_query"].pop("from")

    with pytest.raises(CallbackNotNormalizable) as excinfo:
        normalize_callback_update(update)
    assert excinfo.value.reason == INVALID_IDENTIFIER_REASON


@pytest.mark.unit
def test_normalization_allows_a_missing_update_id_when_callback_query_id_exists() -> (
    None
):
    from app.services.order_proposals.telegram_callback import normalize_callback_update

    normalized = normalize_callback_update(
        make_update(data=_callback_data(), user_id=777, update_id=None)
    )

    assert normalized.callback_query_id == "cbq-1"
    assert normalized.update_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inline_callback_rejects_invalid_identifier_before_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    __tracebackhide__ = True
    from app.services.order_proposals import telegram_callback as callback_module
    from app.services.order_proposals.telegram_callback import handle_callback_update

    poison = _IdentifierPoison()
    core_calls: list[object] = []

    async def _core(normalized, **kwargs):
        core_calls.append(normalized)
        return {"handled": True, "reason": "approved"}

    monkeypatch.setattr(callback_module, "handle_normalized_callback", _core)
    result = await handle_callback_update(
        make_update(data=_callback_data(), user_id=poison),
        now=now_kst(),
        notifier=FakeNotifier(),
    )

    assert result == {"handled": False, "reason": INVALID_IDENTIFIER_REASON}
    assert core_calls == []
    assert poison.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("user", "update"))
async def test_durable_ingress_rejects_invalid_identifier_before_persist_or_kick(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    __tracebackhide__ = True
    from app.services.order_proposals.callback_inbox import ingress as ingress_module
    from app.services.order_proposals.callback_inbox.ingress import (
        IngressResult,
        ingest_callback_update,
    )

    poison = _IdentifierPoison()
    calls: list[str] = []
    session_factory_calls: list[str] = []

    def _session_factory():
        session_factory_calls.append("session_factory")
        raise AssertionError("invalid identifier reached the session factory")

    async def _persist(*args, **kwargs):
        calls.append("persist")
        return uuid.uuid4(), False, False

    async def _enqueue(job_id: uuid.UUID) -> None:
        calls.append("enqueue")

    monkeypatch.setattr(ingress_module, "_persist", _persist)
    update = make_update(
        data=_callback_data(),
        user_id=poison if target == "user" else 777,
        update_id=poison if target == "update" else 7,
    )

    result = await ingest_callback_update(
        update,
        now=now_kst(),
        session_factory=_session_factory,
        enqueue_fn=_enqueue,
    )

    assert result == IngressResult(False, False, None, INVALID_IDENTIFIER_REASON, False)
    assert calls == []
    assert session_factory_calls == []
    assert poison.calls == []


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("target", ("user", "update"))
async def test_invalid_identifier_has_no_owned_row_or_queue_side_effect(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], target: str
) -> None:
    """The real durable ingress must reject before its DB and TaskIQ seams."""
    from sqlalchemy import func, select

    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
    from app.services.order_proposals.callback_inbox.ingress import (
        IngressResult,
        ingest_callback_update,
    )

    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(func.count()).select_from(TelegramCallbackInboxJob)
            )
        ).scalar_one()

    queued: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        queued.append(job_id)

    result = await ingest_callback_update(
        make_update(
            data=_callback_data(),
            user_id=0 if target == "user" else 777,
            update_id=0 if target == "update" else 7,
        ),
        now=now_kst(),
        enqueue_fn=_enqueue,
    )
    if result.job_id is not None:
        inbox_cleanup.append(result.job_id)

    async with AsyncSessionLocal() as session:
        after = (
            await session.execute(
                select(func.count()).select_from(TelegramCallbackInboxJob)
            )
        ).scalar_one()

    assert result == IngressResult(False, False, None, INVALID_IDENTIFIER_REASON, False)
    assert after == before
    assert queued == []


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("identity", ("callback_query", "update_fallback"))
async def test_valid_delivery_identity_variants_remain_accepted_and_deduped(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    identity: str,
) -> None:
    """R28 stays intact: either sanctioned identity can dedupe one delivery."""
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update = make_update(
        data=_callback_data(),
        update_id=None if identity == "callback_query" else 700_037,
        callback_id=f"cbq-r37-{identity}",
    )
    if identity == "update_fallback":
        update["callback_query"].pop("id")

    queued: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        queued.append(job_id)

    first = await ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)
    second = await ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)
    assert first.job_id is not None
    inbox_cleanup.append(first.job_id)

    assert first.accepted is True
    assert first.duplicate is False
    assert second.accepted is True
    assert second.duplicate is True
    assert second.job_id == first.job_id
    assert queued == [first.job_id]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "update_id"),
    _invalid_present_values(TELEGRAM_UPDATE_ID_MAX),
    ids=tuple(case for case, _value in _invalid_present_values(TELEGRAM_UPDATE_ID_MAX)),
)
def test_digest_helpers_reject_a_present_invalid_update_id_without_coercion(
    case: str, update_id: object
) -> None:
    __tracebackhide__ = True
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
        build_update_identity_digest,
    )

    with pytest.raises(ValueError):
        build_update_digest(update_id=update_id, callback_query_id="cbq-r37")
    with pytest.raises(ValueError):
        build_update_identity_digest(update_id=update_id)
    if isinstance(update_id, _IdentifierPoison):
        assert update_id.calls == [], case


@pytest.mark.unit
def test_valid_integer_digest_canonicalization_keeps_the_r28_hashes() -> None:
    """R37 narrows input types without changing the prior valid hash domain."""
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
        build_update_identity_digest,
    )

    assert (
        build_update_digest(update_id=7, callback_query_id=None)
        == "106910e1f044bd47e883f8aea7d53bf5b62a86e442f112f7e84800e0e4cef93e"
    )
    assert (
        build_update_digest(update_id=None, callback_query_id="7")
        == "dbe8e0eef45758417695febc05baf34014c033e82044adce6cdaa51590d9c51a"
    )
    assert (
        build_update_identity_digest(update_id=7)
        == "f9f2e095899c6b9cd5c538afb8c70d692b8a9054231268e52db25db16a508576"
    )
    assert build_update_identity_digest(update_id=None) is None


@pytest.mark.unit
def test_callback_query_identity_stays_primary_while_valid_update_digest_verifies_it() -> (
    None
):
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
        build_update_identity_digest,
    )

    assert build_update_digest(
        update_id=1, callback_query_id="cbq-r37"
    ) == build_update_digest(update_id=2, callback_query_id="cbq-r37")
    assert build_update_identity_digest(update_id=1) != build_update_identity_digest(
        update_id=2
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stored_user_id", "expected"),
    (("1", 1), (str(TELEGRAM_USER_ID_MAX), TELEGRAM_USER_ID_MAX)),
    ids=("minimum", "maximum"),
)
def test_worker_reconstruction_accepts_canonical_minimum_and_maximum_user_id_text(
    stored_user_id: str, expected: int
) -> None:
    from app.services.order_proposals.callback_inbox.worker import rebuild_normalized

    normalized = rebuild_normalized(_normalized_row(telegram_user_id=stored_user_id))

    assert type(normalized.telegram_user_id) is int
    assert normalized.telegram_user_id == expected
    assert normalized.telegram_user_id_str == stored_user_id


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "stored_user_id"),
    (
        ("whitespace", " 777"),
        ("leading_zero", "0777"),
        ("sign", "+777"),
        ("negative", "-777"),
        ("zero", "0"),
        ("above_limit", str(TELEGRAM_USER_ID_MAX + 1)),
        ("null", None),
        ("integer", 777),
        ("poison", _IdentifierPoison()),
    ),
    ids=(
        "whitespace",
        "leading_zero",
        "sign",
        "negative",
        "zero",
        "above_limit",
        "null",
        "integer",
        "poison",
    ),
)
def test_worker_reconstruction_rejects_noncanonical_stored_telegram_user_id(
    case: str, stored_user_id: object
) -> None:
    __tracebackhide__ = True
    from app.services.order_proposals.callback_inbox.worker import (
        EnvelopeInvalid,
        rebuild_normalized,
    )

    with pytest.raises(EnvelopeInvalid):
        rebuild_normalized(_normalized_row(telegram_user_id=stored_user_id))
    if isinstance(stored_user_id, _IdentifierPoison):
        assert stored_user_id.calls == [], case


@pytest.mark.asyncio
@pytest.mark.integration
async def test_valid_user_id_round_trips_as_db_decimal_then_exact_worker_integer(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker is handed an integer, while the retained DB value is text."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    user_id = 777
    queued: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        queued.append(job_id)

    result = await ingest_callback_update(
        make_update(data=_callback_data(), user_id=user_id, update_id=7),
        now=now_kst(),
        enqueue_fn=_enqueue,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    from .conftest import load_job

    stored = await load_job(result.job_id)
    assert stored is not None
    assert type(stored.telegram_user_id) is str
    assert stored.telegram_user_id == str(user_id)

    seen: list[object] = []

    async def _handler(normalized, **kwargs):
        seen.append(normalized)
        return {"handled": True, "reason": "approved"}

    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: FakeNotifier())
    processed = await process_callback_job(result.job_id, handler=_handler)

    assert processed["status"] == "succeeded"
    assert queued == [result.job_id]
    assert len(seen) == 1
    assert type(seen[0].telegram_user_id) is int
    assert seen[0].telegram_user_id == user_id
    assert seen[0].telegram_user_id_str == str(user_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_worker_discards_an_owned_noncanonical_db_user_id_before_core(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale/hand-edited active text value cannot enter the callback core."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(data=_callback_data(), user_id=777, update_id=8),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    async with AsyncSessionLocal() as session:
        await session.execute(
            sa.text(
                "UPDATE review.telegram_callback_inbox "
                "SET telegram_user_id = :user_id WHERE job_id = :job_id"
            ),
            {"user_id": " 777", "job_id": result.job_id},
        )
        await session.commit()

    core_calls: list[object] = []

    async def _handler(normalized, **kwargs):
        core_calls.append(normalized)
        return {"handled": True, "reason": "approved"}

    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: FakeNotifier())
    processed = await process_callback_job(result.job_id, handler=_handler)

    assert processed["status"] == "discarded"
    assert core_calls == []
