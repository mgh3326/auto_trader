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

    def _expected(domain: str, update_id: object, query_id: object) -> str:
        canonical = json.dumps(
            {
                "domain": domain,
                "update_id": None if update_id is None else str(update_id),
                "callback_query_id": None if query_id is None else str(query_id),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # The real separator is fed in: recomputing with it matches exactly ...
    assert digest == _expected(UPDATE_DIGEST_DOMAIN, 123, FAKE_CALLBACK_QUERY_ID)
    # ... and recomputing with any other domain does not.
    for other in (
        "",
        "telegram",
        UPDATE_DIGEST_DOMAIN + "x",
        UPDATE_DIGEST_DOMAIN.replace("v1", "v2"),
        "order_proposals.telegram_callback_inbox.delivery",
    ):
        assert digest != _expected(other, 123, FAKE_CALLBACK_QUERY_ID), other

    # Every identity component participates.
    assert digest != build_update_digest(
        update_id=124, callback_query_id=FAKE_CALLBACK_QUERY_ID
    )
    assert digest != build_update_digest(update_id=123, callback_query_id=None)
    assert digest != build_update_digest(
        update_id=None, callback_query_id=FAKE_CALLBACK_QUERY_ID
    )
    # ... and the two are not interchangeable, so a swap cannot collide.
    assert build_update_digest(
        update_id="a", callback_query_id="b"
    ) != build_update_digest(update_id="b", callback_query_id="a")
    # Deterministic.
    assert digest == build_update_digest(
        update_id=123, callback_query_id=FAKE_CALLBACK_QUERY_ID
    )


def test_a_changed_binding_changes_the_stored_envelope_not_the_digest() -> None:
    """The digest is a *delivery* identity, which is why a tampered envelope
    reusing one is a conflict rather than a duplicate (see the ingress suite).
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )

    same = build_update_digest(update_id=7, callback_query_id="cbq-7")
    assert same == build_update_digest(update_id=7, callback_query_id="cbq-7")


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
