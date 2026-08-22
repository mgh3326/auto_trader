"""W5 — the normalize/core seam must not move any security boundary.

RED-before-fix item 16. ``handle_callback_update`` is split so the durable
ingress can validate an update without executing it, and the worker can
execute a *stored* envelope. Equivalence here covers accepted canonical inputs,
the post-normalization execution core, and the pre-existing downstream
authorization gates. R37 intentionally adds a numeric identifier trust
boundary, so not every input behaves as on the parent commit.

Mutants covered here: preflight bypass, nonce bypass, approval-hash bypass —
none of those gates moved into the seam.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.core.config import settings
from app.core.timezone import now_kst
from app.services.order_proposals import telegram_callback as callback_module
from app.services.order_proposals.approval_message import build_callback_data
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    CallbackEnvelope,
    DispatchBinding,
    build_membership_digest,
)

from .conftest import CHAT_ID, USER_ID, FakeNotifier, make_update

pytestmark = pytest.mark.unit


def _data(*, action: str = "op", nonce: str = "nonce123456") -> str:
    proposal_id = uuid.uuid4()
    return build_callback_data(
        action=action,
        proposal_id=proposal_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            membership_digest=build_membership_digest(
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=1,
                members=[{"proposal_id": str(proposal_id), "approval_nonce": nonce}],
            ),
        ),
    )


def test_normalisation_yields_exactly_the_existing_callback_envelope() -> None:
    from app.services.order_proposals.telegram_callback import (
        normalize_callback_update,
    )

    data = _data()
    normalized = normalize_callback_update(make_update(data=data))

    assert isinstance(normalized.callback, CallbackEnvelope)
    assert normalized.callback == callback_module.parse_callback_data(data)
    assert normalized.chat_id == CHAT_ID
    assert normalized.chat_id_key == str(CHAT_ID)
    assert normalized.message_id == 555
    assert normalized.telegram_user_id == USER_ID
    assert normalized.callback_query_id == "cbq-1"


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({}, "not_callback"),
        ({"callback_query": "nope"}, "not_callback"),
        (
            {"callback_query": {"id": "x", "message": {"chat": {"id": 42}}}},
            "malformed_callback_data",
        ),
    ],
)
def test_normalisation_reproduces_the_legacy_rejection_reasons(
    update: dict, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.order_proposals.telegram_callback import (
        CallbackNotNormalizable,
        normalize_callback_update,
    )

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "42", raising=False
    )
    with pytest.raises(CallbackNotNormalizable) as excinfo:
        normalize_callback_update(update)
    assert excinfo.value.reason == reason


def test_the_chat_allowlist_is_still_checked_before_the_callback_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy order: authz first, parsing second. A foreign chat never parses."""
    from app.services.order_proposals.telegram_callback import (
        CallbackNotNormalizable,
        normalize_callback_update,
    )

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "42", raising=False
    )
    parsed: list[str] = []

    def _spy(data):
        parsed.append(data)
        raise AssertionError("parsed callback data for a non-allowlisted chat")

    monkeypatch.setattr(callback_module, "parse_callback_data", _spy)
    with pytest.raises(CallbackNotNormalizable) as excinfo:
        normalize_callback_update(make_update(data=_data(), chat_id=999999))
    assert excinfo.value.reason == "chat_not_allowed"
    assert parsed == []


@pytest.mark.asyncio
async def test_the_inline_entrypoint_is_normalise_then_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``handle_callback_update`` must delegate, not keep a second copy."""
    from app.services.order_proposals.telegram_callback import handle_callback_update

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "42", raising=False
    )
    seen: list[object] = []

    async def _core(normalized, **kwargs):
        seen.append(normalized)
        return {"handled": True, "reason": "approved"}

    monkeypatch.setattr(callback_module, "handle_normalized_callback", _core)
    result = await handle_callback_update(
        make_update(data=_data()), now=now_kst(), notifier=FakeNotifier()
    )
    assert result == {"handled": True, "reason": "approved"}
    assert len(seen) == 1
    assert seen[0].chat_id_key == str(CHAT_ID)


@pytest.mark.asyncio
async def test_the_inline_entrypoint_still_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram's webhook contract is unchanged: a bounded dict, always."""
    from app.services.order_proposals.telegram_callback import handle_callback_update

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "42", raising=False
    )

    async def _boom(normalized, **kwargs):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(callback_module, "handle_normalized_callback", _boom)
    result = await handle_callback_update(
        make_update(data=_data()), now=now_kst(), notifier=FakeNotifier()
    )
    assert result == {"handled": False, "reason": "internal_error"}


@pytest.mark.asyncio
async def test_the_core_revalidates_the_chat_allowlist_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored envelope must be re-authorised, not trusted because it exists."""
    from app.services.order_proposals.telegram_callback import (
        handle_normalized_callback,
        normalize_callback_update,
    )

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "42", raising=False
    )
    normalized = normalize_callback_update(make_update(data=_data()))

    # The operator revokes the chat while the job is queued.
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "43", raising=False
    )

    def _no_service():  # pragma: no cover - must not be reached
        raise AssertionError("a revoked chat reached the service layer")

    result = await handle_normalized_callback(
        normalized,
        now=now_kst(),
        service_factory=_no_service,
        notifier=FakeNotifier(),
    )
    assert result == {"handled": False, "reason": "chat_not_allowed"}


def test_the_core_keeps_every_injection_point_the_inline_path_had() -> None:
    """Fakes must still be injectable, or the existing suites stop proving things."""
    from app.services.order_proposals.telegram_callback import (
        handle_normalized_callback,
    )

    parameters = inspect.signature(handle_normalized_callback).parameters
    for required in (
        "now",
        "service_factory",
        "notifier",
        "revalidate_fn",
        "loss_cut_preview_fn",
        "veto_cancel_fn",
        "veto_fetch_fn",
        "veto_toss_reconcile_fn",
        "window_evaluator",
        "now_fn",
    ):
        assert required in parameters, required


def test_the_seam_did_not_absorb_any_authorisation_gate() -> None:
    """Mutants: preflight / nonce / approval-hash bypass.

    The gates must still live where they lived — inside the per-action
    handlers and the service — never in the parsing seam that the durable
    ingress calls without executing anything.
    """
    import pathlib

    source = pathlib.Path(callback_module.__file__).read_text(encoding="utf-8")
    start = source.index("def normalize_callback_update(")
    end = source.index("async def handle_normalized_callback(")
    seam = source[start:end]
    for gate in (
        "preflight_published_proposal_callback",
        "consume_published_proposal_callback",
        "acquire_commit_lease",
        "acquire_target_mutation_lock",
        "revalidate_and_submit",
        "record_approval",
    ):
        assert gate not in seam, f"{gate} moved into the parsing seam"
