"""W5 — nothing leaks out of the real ingress, worker or recovery.

Adversarial review R6, blocker 10. The sibling data-minimisation module checks
*helpers* with *safe inputs*, and skips ``exc_info``/``exc_text``/``stack_info``
entirely. That leaves the interesting question unasked: when a genuinely
secret-shaped value is sitting in the authority fields and an exception carrying
another one blows up mid-handler, does anything reach a log record or a Sentry
event?

So this module puts sentinels where production would actually find them --
chat id, telegram user id, callback-query id, the nonce, and the *message and
args of a raised exception* -- then drives the real ingress, the real worker
(default seam) and the real recovery sweep, capturing:

* every ``logging`` record on every logger, including ``msg``, ``args``, all
  ``extra`` attributes, ``exc_info``, ``exc_text`` and ``stack_info``;
* every Sentry event the SDK would have transmitted, with its contexts, tags,
  extras, breadcrumbs, spans, exception values and stack frames.

Both are walked recursively for *partial* matches, and the failure message
never prints the sentinel itself.

Deliberately, the ROB-1305 whole-event scrubber is **not** installed for these
tests. It is a real downstream defence, but proving the data never gets there
in the first place is the stronger statement, and the one that keeps holding
if the scrubber's key list ever drifts.
"""

from __future__ import annotations

import ast
import logging
import uuid
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport
from sqlalchemy import text

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import FakeNotifier, load_job, make_update

pytestmark = pytest.mark.integration

# Long, unmistakable, and shaped like the things that actually matter.
SENTINEL_NONCE = "W5nonceLEAK9xQ"
SENTINEL_CHAT = "-1009090909091"
SENTINEL_USER = "8811223344556"
SENTINEL_CBQ = "cbqLEAK-8f2a1c7d9e"
SENTINEL_EXC = "boom appkey=W5SECRETLEAKVALUE0123 bearer W5TOKENLEAK987"
SENTINEL_ARG = "W5ARGLEAK-556677"
# Retained authority fields the earlier sweep did not sentinelize. Each has to
# satisfy the real parser, so they are distinctive *within* their format.
SENTINEL_SUBJECT = "deadbeef"
SENTINEL_DIGEST = "ZzDigestSen1"
SENTINEL_ATTEMPT = "5eee5eee-1111-4222-8333-444444444444"
SENTINEL_MESSAGE_ID = 909090909091
SENTINEL_REVISION = 35
# R32: syntactically valid as the old slug regex but deliberately absent from
# the closed terminal category vocabulary. Full and partial scans use only
# generic failure messages so a failed privacy test does not print it.
R32_UNKNOWN_OUTCOME = "r32nonceopaquevalue"
R32_UNKNOWN_OUTCOME_FRAGMENTS = (
    R32_UNKNOWN_OUTCOME,
    R32_UNKNOWN_OUTCOME[:7],
    R32_UNKNOWN_OUTCOME[7:13],
    R32_UNKNOWN_OUTCOME[-7:],
)

SENTINELS = (
    SENTINEL_NONCE,
    SENTINEL_CHAT,
    SENTINEL_USER,
    SENTINEL_CBQ,
    "W5SECRETLEAKVALUE0123",
    "W5TOKENLEAK987",
    SENTINEL_ARG,
    "boom appkey",
    SENTINEL_SUBJECT,
    SENTINEL_DIGEST,
    SENTINEL_ATTEMPT,
    str(SENTINEL_MESSAGE_ID),
    *R32_UNKNOWN_OUTCOME_FRAGMENTS,
)

#: Only these may appear in a log record's own attributes or a span's data.
ALLOWED_EXTRA_KEYS = frozenset(
    {
        "callback_job.id",
        "callback_job.state",
        "callback_job.attempt",
        "callback_job.queue_delay_seconds",
        "callback_job.outcome",
        "callback_job.error_class",
        "callback_recovery.scanned",
        "callback_recovery.claimed",
        "callback_recovery.pending",
        "callback_recovery.processing",
        "callback_recovery.retry_wait",
        "callback_recovery.dead_letter",
        "exception_type",
        "lock_released",
    }
)

_LOGRECORD_STANDARD = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"taskName", "asctime", "message"}


class Leak(AssertionError):
    """Raised without ever printing the sentinel it found."""


def _walk(value: Any, path: str = "$") -> list[tuple[str, str]]:
    """Flatten anything into (path, rendered-string) pairs."""
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_walk(key, f"{path}.<key>"))
            found.extend(_walk(item, f"{path}.{key}"))
    elif isinstance(value, list | tuple | set | frozenset):
        for index, item in enumerate(value):
            found.extend(_walk(item, f"{path}[{index}]"))
    elif isinstance(value, BaseException):
        found.extend(_walk(str(value), f"{path}!str"))
        found.extend(_walk(getattr(value, "args", ()), f"{path}!args"))
    elif value is not None and not isinstance(value, int | float | bool):
        found.extend(_walk(str(value), f"{path}!repr"))
    elif value is not None:
        found.append((path, str(value)))
    return found


def assert_no_leak(value: Any, *, where: str) -> None:
    for path, rendered in _walk(value):
        for sentinel in SENTINELS:
            if sentinel in rendered:
                # The sentinel is deliberately not interpolated into the
                # message: a leak report must not become the leak.
                raise Leak(
                    f"{where}: a sentinel appeared at {path} "
                    f"(length {len(rendered)} string)"
                )


class _RecordSink(logging.Handler):
    """Captures records off the root logger, formatted and raw."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Force exc_text to be populated exactly as a real handler would.
        if record.exc_info and not record.exc_text:
            record.exc_text = self.format(record)
        self.records.append(record)


def assert_records_clean(sink: _RecordSink) -> None:
    assert sink.records, "nothing logged at all; the sweep would be vacuous"
    for record in sink.records:
        assert_no_leak(record.msg, where="log msg")
        assert_no_leak(record.args, where="log args")
        assert_no_leak(record.getMessage(), where="log rendered message")
        assert_no_leak(record.exc_info, where="log exc_info")
        assert_no_leak(record.exc_text, where="log exc_text")
        assert_no_leak(record.stack_info, where="log stack_info")
        extras = {
            key: value
            for key, value in vars(record).items()
            if key not in _LOGRECORD_STANDARD
        }
        assert_no_leak(extras, where="log extra")
        unknown = set(extras) - ALLOWED_EXTRA_KEYS
        assert not unknown, f"{record.name} logged unapproved fields: {sorted(unknown)}"


def assert_events_clean(
    events: list[dict[str, Any]], *, expect_any: bool = False
) -> None:
    if expect_any:
        assert events, "the Sentry sink captured nothing; this sweep would be vacuous"
    for event in events:
        for section in (
            "contexts",
            "tags",
            "extra",
            "breadcrumbs",
            "spans",
            "exception",
            "request",
            "user",
            "logentry",
            "message",
            "transaction",
        ):
            assert_no_leak(event.get(section), where=f"sentry {section}")
        assert_no_leak(event, where="sentry event (whole)")


@pytest.fixture
def sentry_sink(monkeypatch: pytest.MonkeyPatch):
    """A Sentry client whose transport is a list. No network, no DSN lookup."""
    events: list[dict[str, Any]] = []

    class _ListTransport(Transport):
        """Everything the SDK would have transmitted, kept in memory."""

        def capture_envelope(self, envelope) -> None:
            for item in envelope.items:
                payload = item.payload.json
                if payload is not None:
                    events.append(payload)

        def capture_event(self, event) -> None:  # pragma: no cover - 1.x path
            events.append(event)

    client = sentry_sdk.Client(
        dsn="https://w5publickey@w5.invalid/1",
        transport=_ListTransport(),
        traces_sample_rate=1.0,
        send_default_pii=False,
        auto_enabling_integrations=False,
        default_integrations=False,
    )
    scope = sentry_sdk.get_global_scope()
    previous = scope.client
    scope.set_client(client)
    try:
        yield events
    finally:
        client.close(timeout=0)
        scope.set_client(previous)


@pytest.fixture
def log_sink():
    sink = _RecordSink()
    sink.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(sink)
    root.setLevel(logging.DEBUG)
    try:
        yield sink
    finally:
        root.removeHandler(sink)
        root.setLevel(previous_level)


def _sentinel_data() -> str:
    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )

    # A proposal id whose first eight characters are the subject sentinel, so
    # the *retained* subject field carries one too.
    proposal_id = uuid.UUID(f"{SENTINEL_SUBJECT}-1111-4222-8333-444444444444")
    data = build_callback_data(
        action="op",
        proposal_id=proposal_id,
        nonce=SENTINEL_NONCE,
        binding=DispatchBinding(
            attempt_id=uuid.UUID(SENTINEL_ATTEMPT),
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=SENTINEL_REVISION,
            membership_digest=SENTINEL_DIGEST,
        ),
    )
    assert build_membership_digest is not None
    return data


async def _queue_sentinel_job(
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    *,
    callback_id: str = SENTINEL_CBQ,
) -> uuid.UUID:
    """One job whose every authority field is a sentinel."""
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        SENTINEL_CHAT,
        raising=False,
    )
    update_id = 630_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=_sentinel_data(),
            update_id=update_id,
            callback_id=callback_id,
            chat_id=int(SENTINEL_CHAT),
            user_id=int(SENTINEL_USER),
            message_id=SENTINEL_MESSAGE_ID,
        ),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.accepted is True, result
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


class _Exploding(Exception):
    """Carries a secret-shaped message and a secret-shaped arg."""

    def __init__(self) -> None:
        super().__init__(SENTINEL_EXC, SENTINEL_ARG)


def _assert_unclassified(value: object, *, where: str) -> None:
    """Keep a RED failure from echoing an unknown terminal value."""
    if value != "unclassified":
        raise AssertionError(f"{where}: expected the fixed fallback category")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    (
        R32_UNKNOWN_OUTCOME,
        f"unknown_outcome_family:{R32_UNKNOWN_OUTCOME}",
    ),
    ids=("bare", "unknown-prefix-payload"),
)
async def test_regex_valid_unknown_worker_reasons_are_discarded_as_unclassified(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    """R32 RED: category projection cannot change state/retry classification."""
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue_sentinel_job(
        inbox_cleanup,
        monkeypatch,
        callback_id=f"r32-callback-{uuid.uuid4().hex}",
    )

    async def _unknown_reason_handler(normalized, **kwargs):
        return {"handled": False, "reason": reason}

    result = await process_callback_job(job_id, handler=_unknown_reason_handler)

    # Unknown raw reasons remain an explicit terminal business discard. They
    # must never acquire retry authority while their display category is reduced.
    assert result["status"] == "discarded"
    terminal = await load_job(job_id)
    assert terminal is not None
    assert terminal.state == "discarded"
    assert terminal.error_class is None
    assert terminal.attempt_count == 1
    _assert_unclassified(terminal.outcome, where="terminal row")
    for field in (
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
    ):
        assert getattr(terminal, field) is None, field


@pytest.mark.asyncio
async def test_regex_valid_unknown_outcome_never_reaches_row_logs_or_sentry(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    log_sink: _RecordSink,
    sentry_sink: list[dict[str, Any]],
) -> None:
    """R32 RED: recursive full/prefix/middle/suffix scans stay private."""
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue_sentinel_job(
        inbox_cleanup,
        monkeypatch,
        callback_id=f"r32-callback-{uuid.uuid4().hex}",
    )

    async def _unknown_reason_handler(normalized, **kwargs):
        return {"handled": False, "reason": R32_UNKNOWN_OUTCOME}

    result = await process_callback_job(job_id, handler=_unknown_reason_handler)
    assert result["status"] == "discarded"
    terminal = await load_job(job_id)
    assert terminal is not None

    # Run every recursive scanner before reporting a generic failure. The
    # report intentionally identifies only the surface, never a fixture value.
    clean_surfaces: list[bool] = []
    for check in (
        lambda: assert_no_leak({"outcome": terminal.outcome}, where="terminal outcome"),
        lambda: assert_records_clean(log_sink),
        lambda: assert_events_clean(sentry_sink, expect_any=True),
    ):
        try:
            check()
        except AssertionError:
            clean_surfaces.append(False)
        else:
            clean_surfaces.append(True)
    if not all(clean_surfaces):
        raise AssertionError(
            "R32 protected outcome material escaped an observable surface"
        )

    # A missing outcome would hide the problem rather than solve it.
    _assert_unclassified(terminal.outcome, where="terminal row")


@pytest.mark.asyncio
async def test_a_raised_handler_never_leaks_its_message_or_the_envelope(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    log_sink: _RecordSink,
    sentry_sink: list[dict[str, Any]],
) -> None:
    """R6 B10 — the worst case: secrets in the row *and* in the exception."""
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue_sentinel_job(inbox_cleanup, monkeypatch)

    async def _explode(normalized, **kwargs):
        # The envelope really does carry the sentinels at this point.
        assert normalized.callback.nonce == SENTINEL_NONCE
        assert normalized.chat_id_key == SENTINEL_CHAT
        raise _Exploding

    result = await process_callback_job(job_id, handler=_explode)

    assert result["status"] == "dead_letter"
    row = await load_job(job_id)
    assert row is not None
    assert row.error_class == "handler_exception"
    assert row.nonce is None and row.chat_id is None

    assert_records_clean(log_sink)
    assert_events_clean(sentry_sink)


@pytest.mark.asyncio
async def test_the_default_worker_seam_logs_nothing_from_the_envelope(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    log_sink: _RecordSink,
    sentry_sink: list[dict[str, Any]],
) -> None:
    """The real task, the real core, the real Sentry transaction."""
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    job_id = await _queue_sentinel_job(inbox_cleanup, monkeypatch)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    notifier = FakeNotifier()
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)

    result = await task_module.run_telegram_callback_job(str(job_id))

    # The subject does not exist, so the real core fails closed -- which is
    # exactly the path that has a reason string to log.
    assert result["status"] == "discarded"
    assert_no_leak(result, where="task result")
    assert_records_clean(log_sink)
    # The worker opens a Sentry transaction on this path, so the sink must
    # have something in it -- otherwise the sweep below proves nothing.
    assert_events_clean(sentry_sink, expect_any=True)
    assert any(
        event.get("type") == "transaction"
        and event.get("transaction") == "order_proposals.telegram_callback_job"
        for event in sentry_sink
    ), [event.get("transaction") for event in sentry_sink]


@pytest.mark.asyncio
async def test_the_real_ingress_leaks_nothing_when_the_kick_fails(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    log_sink: _RecordSink,
    sentry_sink: list[dict[str, Any]],
) -> None:
    """The broker error path logs a class name, never the payload."""
    from app.core.config import settings
    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        SENTINEL_CHAT,
        raising=False,
    )

    async def _dead(message):
        raise _Exploding

    monkeypatch.setattr(broker, "kick", _dead)

    update_id = 640_000 + uuid.uuid4().int % 100_000
    result = await ingest_callback_update(
        make_update(
            data=_sentinel_data(),
            update_id=update_id,
            callback_id=SENTINEL_CBQ,
            chat_id=int(SENTINEL_CHAT),
            user_id=int(SENTINEL_USER),
        ),
        now=now_kst(),
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    assert result.enqueued is False

    assert_records_clean(log_sink)
    assert_events_clean(sentry_sink)


@pytest.mark.asyncio
async def test_a_failed_persist_leaks_nothing_and_says_nothing(
    _bootstrap_test_schema,
    monkeypatch: pytest.MonkeyPatch,
    log_sink: _RecordSink,
    sentry_sink: list[dict[str, Any]],
) -> None:
    """The 503 path logs an exception *class*, not the driver's message."""
    import contextlib

    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.ingress import (
        CallbackInboxUnavailable,
        ingest_callback_update,
    )

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        SENTINEL_CHAT,
        raising=False,
    )

    class _ExplodingSession:
        def add(self, *args, **kwargs) -> None:
            return None

        async def execute(self, *args, **kwargs):
            raise _Exploding

        async def flush(self, *args, **kwargs):
            raise _Exploding

        async def commit(self) -> None:
            raise _Exploding

        async def rollback(self) -> None:
            return None

    @contextlib.asynccontextmanager
    async def _factory():
        yield _ExplodingSession()

    with pytest.raises(CallbackInboxUnavailable) as excinfo:
        await ingest_callback_update(
            make_update(
                data=_sentinel_data(),
                update_id=641_001,
                callback_id=SENTINEL_CBQ,
                chat_id=int(SENTINEL_CHAT),
                user_id=int(SENTINEL_USER),
            ),
            now=now_kst(),
            session_factory=_factory,
        )
    # The exception the route turns into a 503 must itself be safe: the route
    # logs it, and a `raise ... from exc` chain would otherwise carry the
    # original message into any handler that formats it.
    assert_no_leak(str(excinfo.value), where="CallbackInboxUnavailable message")
    assert_records_clean(log_sink)
    assert_events_clean(sentry_sink)


@pytest.mark.asyncio
async def test_the_recovery_sweep_reports_only_aggregates(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    log_sink: _RecordSink,
    sentry_sink: list[dict[str, Any]],
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    await _queue_sentinel_job(inbox_cleanup, monkeypatch)

    async def _explode(normalized, **kwargs):
        raise _Exploding

    report = await recover_callback_jobs(handler=_explode)

    assert_no_leak(report, where="recovery report")
    assert_records_clean(log_sink)
    assert_events_clean(sentry_sink)


@pytest.mark.asyncio
async def test_the_committed_row_holds_no_secret_shaped_free_text(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal row's surviving columns must not carry an exception message."""
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue_sentinel_job(inbox_cleanup, monkeypatch)

    async def _explode(normalized, **kwargs):
        raise _Exploding

    await process_callback_job(job_id, handler=_explode)

    async with AsyncSessionLocal() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM review.telegram_callback_inbox "
                        "WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            )
            .mappings()
            .one()
        )
    assert_no_leak(dict(row), where="terminal inbox row")


def test_the_production_modules_never_format_an_exception_into_a_message() -> None:
    """``logger.error(f"...{exc}")`` is the classic way this invariant dies.

    Precise on purpose: ``str(job_id)`` is approved (an opaque UUID is the one
    identifier allowed through), so the guard tracks the *exception names*
    bound by ``except ... as NAME`` and forbids only those from reaching a log
    call -- except through ``type(NAME).__name__``, which is the sanctioned
    way to report what failed.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[4]
    targets = [
        *(root / "app/services/order_proposals/callback_inbox").rglob("*.py"),
        root / "app/routers/telegram_callback.py",
    ]
    log_methods = {
        "debug",
        "info",
        "warning",
        "error",
        "exception",
        "critical",
        "log",
    }
    offenders: list[str] = []
    inspected = 0
    log_calls = 0

    # Anti-vacuity: a negative guard over an empty file set passes for the
    # wrong reason, which is exactly what it looked like before the package
    # existed.
    assert targets, "no production modules to inspect"
    for path in targets:
        assert path.exists(), path

    for path in targets:
        inspected += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        caught = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and node.name
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in log_methods:
                continue
            # ... on a logger. ``asyncio.Task.exception()`` shares the name
            # and is not a logging call, and reporting it would push authors
            # towards rewriting correct code to appease the guard.
            if not _is_logger(func.value):
                continue
            log_calls += 1

            if func.attr == "exception":
                offenders.append(
                    f"{path.name}:{node.lineno} logger.exception captures exc_info"
                )
            if any(keyword.arg == "exc_info" for keyword in node.keywords):
                offenders.append(f"{path.name}:{node.lineno} passes exc_info")
            if any(isinstance(arg, ast.JoinedStr) for arg in node.args):
                offenders.append(
                    f"{path.name}:{node.lineno} interpolates into the log message"
                )

            # Any reference to a caught exception, other than through
            # type(exc).__name__, is a leak waiting to happen.
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Name) or inner.id not in caught:
                    continue
                if not _is_exception_class_name(node, inner):
                    offenders.append(
                        f"{path.name}:{node.lineno} logs the exception object "
                        f"({inner.id!r}) other than as its class name"
                    )

    assert not offenders, offenders
    assert inspected >= 7, f"only inspected {inspected} modules"
    assert log_calls >= 5, f"only found {log_calls} logging calls to check"


def _is_logger(node: ast.AST) -> bool:
    """Is this expression the module logger, or something logger-shaped?"""
    if isinstance(node, ast.Name):
        return "log" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "log" in node.attr.lower()
    return False


def _is_exception_class_name(call: ast.Call, name: ast.AST) -> bool:
    """Is this ``Name`` used only as ``type(name).__name__``?"""
    for node in ast.walk(call):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__name__"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "type"
            and node.value.args
            and node.value.args[0] is name
        ):
            return True
    return False


@pytest.mark.asyncio
async def test_a_deep_real_core_dependency_may_explode_without_leaking(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    log_sink: _RecordSink,
    sentry_sink: list[dict[str, Any]],
) -> None:
    """R8 SHOULD 2 — the secret comes from *inside* the real core.

    The earlier sweep raised from the handler seam. Here the exception is
    raised by a dependency the real ``handle_normalized_callback`` reaches on
    its own, with a secret-shaped message, while every retained authority
    field in the row is itself a sentinel: chat, user, callback-query id,
    message id, subject, attempt id, revision and digest.
    """
    from app.core.config import settings
    from app.services.order_proposals import telegram_callback as core_module
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    job_id = await _queue_sentinel_job(inbox_cleanup, monkeypatch)

    row = await load_job(job_id)
    assert row is not None
    # The row really does carry the sentinels the sweep will look for.
    assert row.nonce == SENTINEL_NONCE
    assert row.chat_id == SENTINEL_CHAT
    assert row.telegram_user_id == SENTINEL_USER
    assert row.callback_query_id == SENTINEL_CBQ
    assert row.message_id == SENTINEL_MESSAGE_ID
    assert row.subject_short == SENTINEL_SUBJECT
    assert str(row.dispatch_attempt_id) == SENTINEL_ATTEMPT
    assert row.membership_digest == SENTINEL_DIGEST
    assert row.membership_revision == SENTINEL_REVISION

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    notifier = FakeNotifier()
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)

    async def _explode_deep(service, proposal_short):
        raise _Exploding

    # A dependency the real core calls itself, well below the seam.
    monkeypatch.setattr(core_module, "_resolve_proposal_id", _explode_deep)

    result = await task_module.run_telegram_callback_job(str(job_id))

    # The core swallowed it into its own ambiguous report, as it must.
    assert result["status"] == "dead_letter"
    assert_no_leak(result, where="task result")

    terminal = await load_job(job_id)
    assert terminal is not None
    assert terminal.error_class == "handler_ambiguous"
    for field in (
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
    ):
        assert getattr(terminal, field) is None, field

    assert_records_clean(log_sink)
    assert_events_clean(sentry_sink, expect_any=True)
