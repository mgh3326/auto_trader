"""W5 — what may be stored, queued, logged or sent to Sentry.

RED-before-fix items 5 and 15, plus adversarial review R2's evidence rules:
the search is recursive, covers ``LogRecord.args`` and every ``extra`` field
as well as the rendered message, and looks for **partial** matches of fake
sentinels rather than exact equality.

ROB-501 and ROB-1305 are the standing invariants this protects.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# Deliberately distinctive so a substring search cannot miss them.
FAKE_NONCE = "ZzNonceSentinel9"
FAKE_CHAT_ID = "-100999888777666"
FAKE_USER_ID = "424242424242"
FAKE_CALLBACK_QUERY_ID = "CbqSentinel1234567890"
FAKE_BOT_TOKEN = "1234567890:AAHfakeBotTokenSentinel"
FAKE_DIGEST = "DigestSentin"
ALL_SENTINELS = (
    FAKE_NONCE,
    FAKE_CHAT_ID,
    FAKE_USER_ID,
    FAKE_CALLBACK_QUERY_ID,
    FAKE_BOT_TOKEN,
    FAKE_DIGEST,
)


def _walk(value: Any) -> list[str]:
    """Flatten anything into the strings it could possibly render as."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_walk(key))
            found.extend(_walk(item))
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            found.extend(_walk(item))
    elif value is not None:
        found.append(str(value))
    return found


def assert_no_sentinels(value: Any, *, where: str) -> None:
    for rendered in _walk(value):
        for sentinel in ALL_SENTINELS:
            assert sentinel not in rendered, (
                f"{where} leaked {sentinel!r}: {rendered!r}"
            )


def test_the_inbox_table_stores_no_raw_update_and_no_free_text() -> None:
    """RED item 15 / mutant: raw payload persistence.

    A column set is a contract. Anything that could hold a whole ``Update``
    (JSON/JSONB/ARRAY) or a Telegram message body is forbidden outright, so
    "just stash the payload for debugging" cannot be added quietly.
    """
    from sqlalchemy import ARRAY, JSON
    from sqlalchemy.dialects.postgresql import JSONB

    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    table = TelegramCallbackInboxJob.__table__
    assert set(table.columns.keys()) == {
        "id",
        "job_id",
        "update_digest",
        "state",
        "attempt_count",
        "max_attempts",
        "received_at",
        "available_at",
        "started_at",
        "handler_entered_at",
        "handler_completed_at",
        "terminal_state_pending",
        "finished_at",
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
        "outcome",
        "error_class",
        "created_at",
        "updated_at",
    }
    for column in table.columns:
        assert not isinstance(column.type, JSONB | JSON | ARRAY), column.name
    # ``update_digest`` is the one-way dedupe tombstone, not the update; every
    # other shape that could hold a payload or a message body is banned.
    for forbidden in ("payload", "raw", "body", "token", "header", "secret"):
        assert not any(forbidden in name for name in table.columns.keys()), forbidden
    for exact in ("data", "text", "update", "message", "callback_data"):
        assert exact not in table.columns, exact


def test_the_delivery_digest_is_domain_separated_and_one_way() -> None:
    """R6 strengthening 4 — the separator is *in the hash input*.

    A constant that merely contains the word "telegram" proves nothing. This
    recomputes the digest by hand from the documented canonical form and
    requires an exact match, then shows that perturbing the domain, or either
    identity component, changes the output.
    """
    import hashlib
    import json

    from app.services.order_proposals.callback_inbox.contracts import (
        UPDATE_DIGEST_DOMAIN,
        build_update_digest,
    )

    digest = build_update_digest(
        update_id=123, callback_query_id=FAKE_CALLBACK_QUERY_ID
    )
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert_no_sentinels(digest, where="update_digest")

    def _expected(domain: str, kind: str, value: object) -> str:
        canonical = json.dumps(
            {"domain": domain, "identity_kind": kind, "value": str(value)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # The real separator is fed in: recomputing with it matches exactly ...
    assert digest == _expected(
        UPDATE_DIGEST_DOMAIN, "callback_query_id", FAKE_CALLBACK_QUERY_ID
    )
    # ... and recomputing with any other domain does not.
    for other in (
        "",
        "telegram",
        UPDATE_DIGEST_DOMAIN + "x",
        UPDATE_DIGEST_DOMAIN.replace("v1", "v2"),
        "order_proposals.telegram_callback_inbox.delivery",
    ):
        assert digest != _expected(
            other, "callback_query_id", FAKE_CALLBACK_QUERY_ID
        ), other

    # R28: the identity is the callback query id alone. The update id used to
    # participate, which is what let the same click arrive twice under two
    # update ids and be persisted twice.
    assert digest == build_update_digest(
        update_id=124, callback_query_id=FAKE_CALLBACK_QUERY_ID
    )
    assert digest != build_update_digest(update_id=123, callback_query_id=None)
    assert digest == build_update_digest(
        update_id=None, callback_query_id=FAKE_CALLBACK_QUERY_ID
    )
    # The kinds are separated, so an update id and a callback query id with
    # the same text cannot collide.
    assert build_update_digest(
        update_id="a", callback_query_id=None
    ) != build_update_digest(update_id=None, callback_query_id="a")
    # Deterministic.
    assert digest == build_update_digest(
        update_id=123, callback_query_id=FAKE_CALLBACK_QUERY_ID
    )


def test_the_digest_really_reads_the_module_constant() -> None:
    """R8 SHOULD 1 — the separator is *loaded*, not duplicated as a literal.

    A copy of the string inside the function would satisfy the recomputation
    test above while leaving ``UPDATE_DIGEST_DOMAIN`` decorative -- and a
    later edit to the constant would silently not change the digest. Rebinding
    the module attribute must change the output.
    """
    from app.services.order_proposals.callback_inbox import contracts

    before = contracts.build_update_digest(update_id=5, callback_query_id="cbq-5")
    original = contracts.UPDATE_DIGEST_DOMAIN
    try:
        contracts.UPDATE_DIGEST_DOMAIN = original + ".rebound"
        after = contracts.build_update_digest(update_id=5, callback_query_id="cbq-5")
    finally:
        contracts.UPDATE_DIGEST_DOMAIN = original
    assert after != before, (
        "the domain constant is not read at hash time; it is duplicated"
    )
    assert contracts.build_update_digest(update_id=5, callback_query_id="cbq-5") == (
        before
    )


def test_a_changed_binding_changes_the_stored_envelope_and_not_the_digest() -> None:
    """R8 SHOULD 5 — the replacement for a test that changed nothing.

    The delivery digest is an *identity* of the delivery, deliberately
    independent of the envelope it carries. That is the whole reason a
    tampered envelope reusing one delivery identity is a conflict rather than
    a duplicate: the digest cannot tell them apart, so the stored envelope has
    to. This pins both halves.
    """
    import uuid as _uuid

    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        normalized_envelope_projection,
    )
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )
    from app.services.order_proposals.telegram_callback import (
        normalize_callback_update,
    )

    def _build(nonce: str) -> str:
        proposal_id = _uuid.UUID("31111111-2222-4333-8444-555555555555")
        return build_callback_data(
            action="op",
            proposal_id=proposal_id,
            nonce=nonce,
            binding=DispatchBinding(
                attempt_id=_uuid.UUID("32222222-2222-4333-8444-555555555555"),
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=1,
                membership_digest=build_membership_digest(
                    card_kind=ApprovalCardKind.MANUAL,
                    membership_revision=1,
                    members=[
                        {"proposal_id": str(proposal_id), "approval_nonce": nonce}
                    ],
                ),
            ),
        )

    def _update(data: str) -> dict[str, Any]:
        return {
            "update_id": 9001,
            "callback_query": {
                "id": "cbq-9001",
                "from": {"id": 777},
                "message": {"chat": {"id": 42}, "message_id": 555},
                "data": data,
            },
        }

    import app.core.config as config_module

    original = config_module.settings.ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR
    config_module.settings.ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR = "42"
    try:
        first = normalize_callback_update(_update(_build("bindingaaaa")))
        second = normalize_callback_update(_update(_build("bindingbbbb")))
    finally:
        config_module.settings.ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR = original

    # The binding really did change ...
    projection_a = normalized_envelope_projection(first)
    projection_b = normalized_envelope_projection(second)
    assert projection_a != projection_b
    assert projection_a["nonce"] != projection_b["nonce"]
    assert projection_a["membership_digest"] != projection_b["membership_digest"]

    # ... and the delivery identity deliberately did not.
    digest_a = build_update_digest(update_id=9001, callback_query_id="cbq-9001")
    digest_b = build_update_digest(update_id=9001, callback_query_id="cbq-9001")
    assert digest_a == digest_b


def test_only_a_job_uuid_ever_reaches_the_queue() -> None:
    """RED item 5 — the Redis argument carries no authority and no PII."""
    import inspect

    from app.tasks import telegram_callback_inbox_tasks as task_module

    signature = inspect.signature(task_module.run_telegram_callback_job.original_func)
    assert list(signature.parameters) == ["job_id"]
    assert signature.parameters["job_id"].annotation in (str, "str")


@pytest.mark.asyncio
async def test_the_task_result_is_a_job_uuid_and_an_allowlisted_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.contracts import WORKER_STATUSES
    from app.tasks import telegram_callback_inbox_tasks as task_module

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    job_id = uuid.uuid4()

    async def _fake_process(job, **kwargs):
        return {
            "status": "discarded",
            "job_id": str(job),
            # A rogue extra key must not survive the task boundary.
            "reason": f"nonce={FAKE_NONCE} chat={FAKE_CHAT_ID}",
        }

    monkeypatch.setattr(task_module, "process_callback_job", _fake_process)
    result = await task_module.run_telegram_callback_job(str(job_id))

    assert set(result) == {"status", "job_id"}
    assert result["status"] in WORKER_STATUSES
    assert result["job_id"] == str(job_id)
    assert_no_sentinels(result, where="task result")
    # The whole result must be JSON-serialisable for the Redis backend.
    assert_no_sentinels(json.loads(json.dumps(result)), where="serialised result")


def test_the_sentry_span_payload_is_an_allowlist_of_safe_scalars() -> None:
    from app.services.order_proposals.callback_inbox.observability import (
        SAFE_SPAN_KEYS,
        build_worker_span_data,
    )

    data = build_worker_span_data(
        job_id=uuid.UUID("11111111-2222-4333-8444-555555555555"),
        state="succeeded",
        attempt_count=2,
        queue_delay_seconds=41.5,
        outcome="approved",
        error_class=None,
    )
    assert set(data) <= SAFE_SPAN_KEYS
    assert_no_sentinels(data, where="sentry span data")
    for value in data.values():
        assert isinstance(value, str | int | float | bool | type(None)), value


def test_the_span_builder_cannot_be_fed_authority_material() -> None:
    """Even a caller that tries must not get a sentinel into the span."""
    from app.services.order_proposals.callback_inbox.observability import (
        build_worker_span_data,
    )

    data = build_worker_span_data(
        job_id=uuid.UUID("11111111-2222-4333-8444-555555555555"),
        state="discarded",
        attempt_count=1,
        queue_delay_seconds=0.0,
        outcome=f"weird:{FAKE_NONCE}",
        error_class=None,
    )
    assert_no_sentinels(data, where="sentry span data (hostile outcome)")


def test_an_outcome_label_is_a_slug_never_a_payload() -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        OUTCOME_LABEL_PATTERN,
        normalize_outcome,
    )

    assert normalize_outcome("approved") == "approved"
    assert normalize_outcome("EXPIRED") == "expired"
    # The core's richer reasons keep only their stable prefix.
    assert normalize_outcome(f"proposal_superseded_by:{uuid.uuid4()}") == (
        "proposal_superseded_by"
    )
    assert normalize_outcome("approval_window:EXPIRED:now_at_or_after") == (
        "approval_window"
    )
    assert normalize_outcome(f"leak {FAKE_NONCE}") == "unclassified"
    assert normalize_outcome(None) is None
    for candidate in (
        "approved",
        "expired",
        "nonce_replay",
        "unclassified",
        "proposal_superseded_by",
    ):
        assert OUTCOME_LABEL_PATTERN.fullmatch(candidate), candidate


def test_worker_logging_carries_no_authority_in_message_args_or_extra(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R2 — search the rendered message *and* ``args`` *and* every ``extra``."""
    from app.services.order_proposals.callback_inbox import observability

    caplog.set_level(logging.DEBUG)
    observability.log_job_event(
        "order_proposals.telegram.callback_job_finished",
        job_id=uuid.UUID("11111111-2222-4333-8444-555555555555"),
        state="succeeded",
        attempt_count=1,
        queue_delay_seconds=3.25,
        outcome="approved",
        error_class=None,
    )
    assert caplog.records, "the event did not log at all"
    for record in caplog.records:
        assert_no_sentinels(record.getMessage(), where="log message")
        assert_no_sentinels(record.args, where="log args")
        assert_no_sentinels(
            {
                key: value
                for key, value in vars(record).items()
                if key
                not in {
                    "msg",
                    "args",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                    "created",
                    "msecs",
                    "relativeCreated",
                }
            },
            where="log record attributes",
        )


def test_the_inbox_never_imports_an_in_process_llm_provider() -> None:
    """ROB-501 — the runtime LLM boundary reaches new packages too."""
    import pathlib

    package = (
        pathlib.Path(__file__).resolve().parents[4]
        / "app/services/order_proposals/callback_inbox"
    )
    sources = list(package.rglob("*.py"))
    assert sources, "the package under test does not exist"
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "google.generativeai",
            "import openai",
            "from openai",
            "anthropic",
            "langchain",
        ):
            assert forbidden not in text, f"{path.name} imports {forbidden}"
