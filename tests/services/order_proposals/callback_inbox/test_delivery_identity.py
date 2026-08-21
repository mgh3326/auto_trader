"""W5 — one Telegram callback query is one delivery, whatever else changes.

Adversarial review R28. The delivery digest hashed the *pair*
``(update_id, callback_query_id)``, and the only uniqueness in the database
was on that digest. Telegram guarantees the callback query id is unique per
bot, but nothing stops a caller reaching the webhook with the same callback
query id under a *different* ``update_id`` -- at which point the pair differs,
the digest differs, the unique index sees two unrelated deliveries, and both
are accepted, persisted and queued.

Observed on the parent: two accepted results, two distinct job ids, two rows,
two active pending rows, two Redis kicks -- for one approval click whose
binding had been altered in between.

So the identity has to be the callback query id itself, domain-separated, and
``update_id`` has to move into the *verification* projection where a mismatch
fails closed rather than forking a second row.

Anti-vacuity note: the older race matrix perturbed ``subject_short``,
``membership_revision`` and ``nonce`` by rebuilding the membership digest from
the changed values, so those cases were two-field tampers being asserted as
one-field ones -- and ``callback_query_id`` was not in the matrix at all.
Every case here pins the baseline digest and asserts ``DIFF_COUNT == 1``
against the real normalised projection *before* exercising the conflict. Some
of the resulting envelopes are semantically invalid bindings; that is
deliberate. The ingress equality boundary is not the binding validator, and it
must reject every single-field mismatch on its own.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import CHAT_ID, make_update

pytestmark = pytest.mark.integration

#: Every field the stored row is compared on, including the update identity
#: that R28 moves out of the digest and into verification.
COMPARISON_FIELDS = (
    "callback_query_id",
    "update_identity_digest",
    "chat_id",
    "message_id",
    "telegram_user_id",
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
)

#: Fields that can differ while the delivery identity stays the same. The
#: callback query id cannot: changing it *is* a different delivery.
TAMPERABLE_FIELDS = tuple(
    field for field in COMPARISON_FIELDS if field != "callback_query_id"
)

_BASE_PROPOSAL = uuid.UUID("22222222-3333-4444-8555-666666666666")
_BASE_ATTEMPT = uuid.UUID("77777777-8888-4999-8aaa-bbbbbbbbbbbb")
_BASE_NONCE = "baselinenon"


def _variant(field: str | None) -> dict[str, Any]:
    """One delivery, differing from the baseline in exactly one field.

    The membership digest is always computed from the *baseline* members, so
    perturbing ``subject_short``/``membership_revision``/``nonce`` moves that
    field and nothing else. The binding is then internally inconsistent --
    which is the point: equality is checked before any of that is validated.
    """
    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )

    proposal_id = _BASE_PROPOSAL
    attempt_id = _BASE_ATTEMPT
    action = "op"
    nonce = _BASE_NONCE
    revision = 1
    chat_id = CHAT_ID
    user_id = 777
    message_id = 555

    frozen_digest = build_membership_digest(
        card_kind=ApprovalCardKind.MANUAL,
        membership_revision=1,
        members=[{"proposal_id": str(_BASE_PROPOSAL), "approval_nonce": _BASE_NONCE}],
    )
    digest = frozen_digest

    if field == "action":
        action = "dn"
    elif field == "subject_short":
        proposal_id = uuid.UUID("99999999-3333-4444-8555-666666666666")
    elif field == "dispatch_attempt_id":
        attempt_id = uuid.UUID("cccccccc-8888-4999-8aaa-bbbbbbbbbbbb")
    elif field == "membership_revision":
        revision = 2
    elif field == "membership_digest":
        digest = build_membership_digest(
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            members=[{"proposal_id": str(_BASE_PROPOSAL), "approval_nonce": "other"}],
        )
    elif field == "nonce":
        nonce = "variantnonc"
    elif field == "chat_id":
        chat_id = CHAT_ID + 1
    elif field == "telegram_user_id":
        user_id = 888
    elif field == "message_id":
        message_id = 556

    return {
        "data": build_callback_data(
            action=action,
            proposal_id=proposal_id,
            nonce=nonce,
            binding=DispatchBinding(
                attempt_id=attempt_id,
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=revision,
                membership_digest=digest,
            ),
        ),
        "chat_id": chat_id,
        "user_id": user_id,
        "message_id": message_id,
    }


def _projection(variant: dict[str, Any], *, update_id: int, callback_id: str):
    from app.services.order_proposals.callback_inbox.ingress import (
        normalized_envelope_projection,
    )
    from app.services.order_proposals.telegram_callback import (
        normalize_callback_update,
    )

    return normalized_envelope_projection(
        normalize_callback_update(
            make_update(
                data=variant["data"],
                update_id=update_id,
                callback_id=callback_id,
                chat_id=variant["chat_id"],
                user_id=variant["user_id"],
                message_id=variant["message_id"],
            )
        )
    )


def _assert_exactly_one_field_differs(
    field: str, *, callback_id: str, base_update_id: int, other_update_id: int
) -> None:
    """The precondition every conflict case below depends on."""
    base = _projection(
        _variant(None), update_id=base_update_id, callback_id=callback_id
    )
    if field == "update_identity_digest":
        other = _projection(
            _variant(None), update_id=other_update_id, callback_id=callback_id
        )
    else:
        other = _projection(
            _variant(field), update_id=base_update_id, callback_id=callback_id
        )

    assert set(base) == set(COMPARISON_FIELDS), sorted(base)
    differing = sorted(key for key in base if base[key] != other[key])
    assert differing == [field], (
        f"expected a one-field tamper on {field!r}, got {differing}"
    )


async def _rows_for(callback_id: str) -> list[dict[str, Any]]:
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )

    digest = build_update_digest(update_id=None, callback_query_id=callback_id)
    async with AsyncSessionLocal() as session:
        return [
            dict(row)
            for row in (
                await session.execute(
                    sa.text(
                        "SELECT job_id, state, update_digest "
                        "FROM review.telegram_callback_inbox "
                        "WHERE update_digest = :digest"
                    ),
                    {"digest": digest},
                )
            )
            .mappings()
            .all()
        ]


@pytest.fixture
def _wide_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"{CHAT_ID},{CHAT_ID + 1}",
        raising=False,
    )


# ---------------------------------------------------------------------------
# sequential: the reported counterexample
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reported_counterexample_directly(
    _bootstrap_test_schema, _wide_allowlist, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R28 — the raw evidence, counted off the stored callback query id.

    Deliberately independent of the projection and of the digest helper, so
    it shows the blocker itself rather than the shape of the fix: one
    callback query id, two update ids, an altered binding, and the parent
    persists and queues both.
    """
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 800_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-raw-{uuid.uuid4().hex[:12]}"
    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    async def _ingest(variant: dict[str, Any], sequence: int):
        return await ingest_callback_update(
            make_update(
                data=variant["data"],
                update_id=sequence,
                callback_id=callback_id,
                chat_id=variant["chat_id"],
                user_id=variant["user_id"],
                message_id=variant["message_id"],
            ),
            now=now_kst(),
            enqueue_fn=_enqueue,
        )

    first = await _ingest(_variant(None), update_id)
    second = await _ingest(_variant("nonce"), update_id + 1)
    for result in (first, second):
        if result.job_id is not None:
            inbox_cleanup.append(result.job_id)

    async with AsyncSessionLocal() as session:
        stored = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM review.telegram_callback_inbox "
                    "WHERE callback_query_id = :cid"
                ),
                {"cid": callback_id},
            )
        ).scalar_one()

    assert (first.accepted, second.accepted) == (True, False), (
        f"both deliveries were accepted: {first.reason} / {second.reason}"
    )
    assert second.reason == "delivery_conflict"
    assert first.job_id != second.job_id or second.job_id is None
    assert stored == 1, f"{stored} rows persisted for one callback query id"
    assert kicked == [first.job_id], kicked


@pytest.mark.asyncio
@pytest.mark.parametrize("field", TAMPERABLE_FIELDS)
async def test_one_callback_query_id_admits_exactly_one_call(
    _bootstrap_test_schema,
    _wide_allowlist,
    inbox_cleanup: list[uuid.UUID],
    field: str,
) -> None:
    """R28 — same callback query id, different update id, one field altered."""
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    base_update_id = 810_000 + uuid.uuid4().int % 10_000
    other_update_id = base_update_id + 1
    callback_id = f"cbq-seq-{uuid.uuid4().hex[:12]}"
    _assert_exactly_one_field_differs(
        field,
        callback_id=callback_id,
        base_update_id=base_update_id,
        other_update_id=other_update_id,
    )

    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    async def _ingest(variant: dict[str, Any], update_id: int):
        return await ingest_callback_update(
            make_update(
                data=variant["data"],
                update_id=update_id,
                callback_id=callback_id,
                chat_id=variant["chat_id"],
                user_id=variant["user_id"],
                message_id=variant["message_id"],
            ),
            now=now_kst(),
            enqueue_fn=_enqueue,
        )

    first = await _ingest(_variant(None), base_update_id)
    assert first.accepted is True
    assert first.job_id is not None
    inbox_cleanup.append(first.job_id)

    second = await _ingest(
        _variant(None) if field == "update_identity_digest" else _variant(field),
        other_update_id if field == "update_identity_digest" else base_update_id,
    )
    if second.job_id is not None:
        inbox_cleanup.append(second.job_id)

    assert second.accepted is False, f"{field}: a tampered redelivery was accepted"
    assert second.duplicate is False
    assert second.reason == "delivery_conflict"
    assert second.enqueued is False

    rows = await _rows_for(callback_id)
    assert len(rows) == 1, f"{field}: {len(rows)} rows for one callback query id"
    assert kicked == [first.job_id], kicked


@pytest.mark.asyncio
async def test_an_exact_redelivery_is_still_idempotent(
    _bootstrap_test_schema, _wide_allowlist, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Control: nothing above may turn a genuine Telegram retry into a refusal."""
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 820_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-dup-{uuid.uuid4().hex[:12]}"
    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    async def _ingest():
        return await ingest_callback_update(
            make_update(
                data=_variant(None)["data"],
                update_id=update_id,
                callback_id=callback_id,
                chat_id=CHAT_ID,
                user_id=777,
                message_id=555,
            ),
            now=now_kst(),
            enqueue_fn=_enqueue,
        )

    first = await _ingest()
    assert first.accepted is True
    inbox_cleanup.append(first.job_id)

    again = await _ingest()
    assert again.accepted is True
    assert again.duplicate is True
    assert again.reason == "duplicate"
    assert again.enqueued is False
    assert again.job_id == first.job_id

    assert len(await _rows_for(callback_id)) == 1
    assert kicked == [first.job_id]


# ---------------------------------------------------------------------------
# concurrent: the same counterexample at the PostgreSQL insert boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["nonce", "membership_revision", "chat_id"])
async def test_a_two_backend_race_on_one_callback_query_id(
    _bootstrap_test_schema,
    _wide_allowlist,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """R28 — and the database, not a process lock, is what decides it.

    Both sessions meet inside the repository immediately before the INSERT
    flushes, so they race at PostgreSQL. Different ``update_id`` values, so
    the parent's pair-digest gave them separate unique keys and both won.
    """
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    base_update_id = 830_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-race-{uuid.uuid4().hex[:12]}"
    _assert_exactly_one_field_differs(
        field,
        callback_id=callback_id,
        base_update_id=base_update_id,
        other_update_id=base_update_id + 1,
    )

    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    barrier = asyncio.Barrier(2)
    original_insert = repo_module.CallbackInboxRepository.insert

    async def _barriered_insert(self, **fields: Any):
        await barrier.wait()
        return await original_insert(self, **fields)

    monkeypatch.setattr(
        repo_module.CallbackInboxRepository, "insert", _barriered_insert, raising=True
    )

    async def _attempt(variant: dict[str, Any], update_id: int):
        return await ingest_callback_update(
            make_update(
                data=variant["data"],
                update_id=update_id,
                callback_id=callback_id,
                chat_id=variant["chat_id"],
                user_id=variant["user_id"],
                message_id=variant["message_id"],
            ),
            now=now_kst(),
            enqueue_fn=_enqueue,
        )

    first, second = await asyncio.gather(
        _attempt(_variant(None), base_update_id),
        _attempt(_variant(field), base_update_id + 1),
    )
    for result in (first, second):
        if result.job_id is not None:
            inbox_cleanup.append(result.job_id)

    accepted = [result for result in (first, second) if result.accepted]
    conflicted = [
        result for result in (first, second) if result.reason == "delivery_conflict"
    ]
    assert len(accepted) == 1, (field, first, second)
    assert len(conflicted) == 1, (field, first.reason, second.reason)

    rows = await _rows_for(callback_id)
    assert len(rows) == 1, f"{field}: {len(rows)} rows for one callback query id"
    assert kicked == [accepted[0].job_id], kicked


# ---------------------------------------------------------------------------
# the identity itself
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_digest_is_the_callback_query_id_and_is_domain_separated() -> None:
    """R28 — a stable primary identity, and no cross-kind collision."""
    from app.services.order_proposals.callback_inbox.contracts import (
        DeliveryIdentityMissing,
        build_update_digest,
    )

    # The update id must not move the identity.
    assert build_update_digest(
        update_id=1, callback_query_id="cbq-1"
    ) == build_update_digest(update_id=2, callback_query_id="cbq-1")

    # Different callback queries stay different.
    assert build_update_digest(
        update_id=1, callback_query_id="cbq-1"
    ) != build_update_digest(update_id=1, callback_query_id="cbq-2")

    # A supported fallback, for an update carrying no callback query id.
    assert build_update_digest(update_id=7, callback_query_id=None)

    # ... which must not collide with a callback query id of the same text.
    assert build_update_digest(
        update_id=7, callback_query_id=None
    ) != build_update_digest(update_id=None, callback_query_id="7")

    with pytest.raises(DeliveryIdentityMissing):
        build_update_digest(update_id=None, callback_query_id=None)


@pytest.mark.unit
def test_the_update_identity_is_stored_one_way_and_scrubbed() -> None:
    """No raw ``update_id`` column: a digest is enough to detect a mismatch."""
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
    from app.services.order_proposals.callback_inbox.contracts import (
        SCRUBBED_ON_TERMINAL,
    )

    columns = set(TelegramCallbackInboxJob.__table__.columns.keys())
    assert "update_identity_digest" in columns
    assert "update_id" not in columns, "a raw Telegram update id is being retained"
    assert "update_identity_digest" in SCRUBBED_ON_TERMINAL
