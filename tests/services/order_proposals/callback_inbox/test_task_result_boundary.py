"""R35 — TaskIQ's durable-inbox input and result boundary is closed.

This is deliberately a boundary test, not a worker-state-machine test.  The
Redis task argument and result backend are a separate trust boundary: callers
may supply arbitrary Python objects in an in-process test, and TaskIQ's
configured pickle result representation can otherwise retain an exception,
its arguments, and its cause chain even when the worker itself is careful.

The expected vocabularies below are intentionally independent of the runtime
constants.  Deriving them from production would let a widened production
vocabulary make this audit pass without a conscious contract change.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
import sentry_sdk
from sentry_sdk import logger as sentry_logger
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport
from taskiq import TaskiqMessage
from taskiq.compat import model_dump
from taskiq.receiver import Receiver
from taskiq.result import TaskiqResult
from taskiq.serializers import PickleSerializer
from taskiq_redis import RedisAsyncResultBackend

pytestmark = pytest.mark.unit


# Keep this inventory independent from ``contracts.WorkerStatus``.  ``disabled``
# belongs to the task gate only; it is never a worker or recovery-item result.
EXPECTED_PROCESS_STATUSES = frozenset(
    {
        "dead_letter",
        "discarded",
        "lock_contended",
        "not_claimable",
        "not_found",
        "retry_scheduled",
        "succeeded",
    }
)
EXPECTED_RECOVERY_ITEM_STATUSES = EXPECTED_PROCESS_STATUSES | {"error"}
EXPECTED_BACKLOG_KEYS = frozenset(
    {
        "pending",
        "processing",
        "retry_wait",
        "dead_letter",
        "oldest_pending_age_seconds",
    }
)
EXPECTED_RECOVERY_REPORT_KEYS = frozenset(
    {"status", "scanned", "claimed", "statuses", "backlog"}
)
NON_CLAIMED_ITEM_STATUSES = frozenset({"lock_contended", "not_claimable", "not_found"})

# The assertion helpers below must never echo these values.  Stable fragments
# catch a future truncation/redaction attempt just as reliably as the full
# marker, without allowing a failed RED test to become a disclosure surface.
_FAKE_SECRET = "r35-taskiq-secret-4e31d7c9a6b2"
_FAKE_FRAGMENTS = (
    _FAKE_SECRET,
    _FAKE_SECRET[:9],
    _FAKE_SECRET[9:20],
    _FAKE_SECRET[-10:],
)


class _BoundaryRuntimeError(RuntimeError):
    """Secret-bearing RuntimeError whose display text is intentionally safe."""

    def __init__(self, *, reject_render: bool = False) -> None:
        self.render_calls: list[str] = []
        self.reject_render = reject_render
        super().__init__(_FAKE_SECRET)

    def __str__(self) -> str:
        self.render_calls.append("str")
        if self.reject_render:
            raise AssertionError("boundary exception was rendered")
        # A baseline failure must be assertion-level and must not print the
        # fixture through pytest's normal unhandled-exception rendering.
        return "task boundary failure"

    def __repr__(self) -> str:
        self.render_calls.append("repr")
        if self.reject_render:
            raise AssertionError("boundary exception was rendered")
        return "<task boundary runtime error>"


class _CustomBaseException(BaseException):
    """An uncaught-by-``Exception`` control with safe display text."""

    def __init__(self, *, reject_render: bool = True) -> None:
        self.render_calls: list[str] = []
        self.reject_render = reject_render
        super().__init__(_FAKE_SECRET)

    def __str__(self) -> str:
        self.render_calls.append("str")
        if self.reject_render:
            raise AssertionError("boundary base exception was rendered")
        return "task boundary base failure"

    def __repr__(self) -> str:
        self.render_calls.append("repr")
        if self.reject_render:
            raise AssertionError("boundary base exception was rendered")
        return "<task boundary base exception>"


class _StringSubclass(str):
    """A string-looking value that the wire contract must reject."""


class _IntSubclass(int):
    """An int-looking value that the wire contract must reject."""


class _FloatSubclass(float):
    """A float-looking value that the wire contract must reject."""


class _DictSubclass(dict[str, Any]):
    """A dict-looking value that the wire contract must reject."""


class _Hostile:
    """Records every coercion attempt without placing it in assertion output."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __str__(self) -> str:
        self.calls.append("str")
        return _FAKE_SECRET

    def __repr__(self) -> str:
        self.calls.append("repr")
        return _FAKE_SECRET


class _HostileUUID(uuid.UUID):
    """A real UUID subclass that must not receive UUID authority."""

    __slots__ = ("calls",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "calls", [])
        super().__init__(value)

    def __str__(self) -> str:
        self.calls.append("str")
        return _FAKE_SECRET

    def __repr__(self) -> str:
        self.calls.append("repr")
        return _FAKE_SECRET


class _CanonicalUUIDImpersonator:
    """A non-UUID that would pass a string-round-trip implementation."""

    def __init__(self, canonical: str) -> None:
        self.canonical = canonical
        self.calls: list[str] = []

    def __str__(self) -> str:
        self.calls.append("str")
        return self.canonical

    def __repr__(self) -> str:
        self.calls.append("repr")
        return _FAKE_SECRET


class _HostileExtraKey:
    """A non-string dict key whose callbacks must stay outside the boundary."""

    def __init__(self, *, collision_key: str = "status") -> None:
        self.calls: list[str] = []
        self.collision_key = collision_key

    def __hash__(self) -> int:
        self.calls.append("hash")
        # Deliberately collide during test dict construction; callbacks are
        # reset before either production boundary sees the finished dict.
        return hash(self.collision_key)

    def __eq__(self, other: object) -> bool:
        self.calls.append("eq")
        return False

    def __str__(self) -> str:
        self.calls.append("str")
        return _FAKE_SECRET

    def __repr__(self) -> str:
        self.calls.append("repr")
        return _FAKE_SECRET


class _DynamicRecoveryFailure(Exception):
    """Its dynamic class name itself is protected recovery error material."""

    def __init__(self, *, reject_render: bool = True) -> None:
        self.render_calls: list[str] = []
        self.reject_render = reject_render
        super().__init__(_FAKE_SECRET)

    def __str__(self) -> str:
        self.render_calls.append("str")
        if self.reject_render:
            raise AssertionError("dynamic recovery exception was rendered")
        return "recovery item failure"

    def __repr__(self) -> str:
        self.render_calls.append("repr")
        if self.reject_render:
            raise AssertionError("dynamic recovery exception was rendered")
        return "<dynamic recovery failure>"


class _RenderTrackedExceptionGroup(ExceptionGroup):
    """An outer group that fails safely if a boundary renders the group itself."""

    def __new__(
        cls, message: str, exceptions: list[Exception]
    ) -> _RenderTrackedExceptionGroup:
        instance = super().__new__(cls, message, exceptions)
        instance.render_calls: list[str] = []
        return instance

    def __str__(self) -> str:
        self.render_calls.append("str")
        raise AssertionError("boundary exception group was rendered")

    def __repr__(self) -> str:
        self.render_calls.append("repr")
        raise AssertionError("boundary exception group was rendered")


# Make the baseline's ``type(exc).__name__`` log leak the exact marker too,
# while its string rendering remains safe if pytest ever has to show it.
_DynamicRecoveryFailure.__name__ = _FAKE_SECRET


def _raise_generic(message: str) -> None:
    """Fail without formatting a potentially untrusted actual value."""
    raise AssertionError(message) from None


def _assert_exception_unrendered(exception: object, *, where: str) -> None:
    calls = getattr(exception, "render_calls", None)
    if calls is None:
        return
    if type(calls) is not list or calls:
        _raise_generic(f"{where} rendered a protected exception")


def _assert_exact_string(value: object, expected: str, *, where: str) -> None:
    if type(value) is not str or value != expected:
        _raise_generic(f"{where} did not contain the fixed safe string")


def _assert_exact_keys(value: object, expected: frozenset[str], *, where: str) -> None:
    if type(value) is not dict:
        _raise_generic(f"{where} was not an exact built-in dict")
    if any(type(key) is not str for key in value):
        _raise_generic(f"{where} contained a non-built-in-string key")
    if set(value) != expected:
        _raise_generic(f"{where} did not contain the exact closed key set")


def _assert_job_result(value: object, *, status: str, job_id: str) -> None:
    _assert_exact_keys(value, frozenset({"status", "job_id"}), where="job result")
    assert type(value) is dict  # narrows for the type checker after the guard
    _assert_exact_string(value["status"], status, where="job result status")
    _assert_exact_string(value["job_id"], job_id, where="job result id")


def _assert_recovery_error(value: object) -> None:
    _assert_exact_keys(value, frozenset({"status"}), where="recovery error result")
    assert type(value) is dict
    _assert_exact_string(value["status"], "error", where="recovery error status")


def _assert_invalid_job_id_result(value: object) -> None:
    _assert_exact_keys(value, frozenset({"status"}), where="invalid-id result")
    assert type(value) is dict
    _assert_exact_string(value["status"], "invalid_job_id", where="invalid-id status")


def _walk_protected_text(value: object) -> list[str]:
    """Read built-in containers and exception chains without rendering objects."""
    found: list[str] = []
    if type(value) is str:
        found.append(value)
    elif type(value) is bytes:
        found.append(value.decode("utf-8", errors="ignore"))
    elif type(value) is dict:
        for key, item in value.items():
            found.extend(_walk_protected_text(key))
            found.extend(_walk_protected_text(item))
    elif type(value) in (list, tuple, set, frozenset):
        for item in value:
            found.extend(_walk_protected_text(item))
    elif isinstance(value, BaseException):
        found.extend(_walk_protected_text(value.args))
        if value.__cause__ is not None:
            found.extend(_walk_protected_text(value.__cause__))
        if value.__context__ is not None:
            found.extend(_walk_protected_text(value.__context__))
    return found


def _assert_no_protected_text(value: object, *, where: str) -> None:
    for rendered in _walk_protected_text(value):
        if any(fragment in rendered for fragment in _FAKE_FRAGMENTS):
            _raise_generic(f"protected task-boundary material reached {where}")


def _assert_all_clean(checks: tuple[Callable[[], None], ...], *, message: str) -> None:
    """Run every privacy scan before raising one safe, non-diagnostic RED."""
    failures = 0
    for check in checks:
        try:
            check()
        except AssertionError:
            failures += 1
    if failures:
        _raise_generic(message)


def _record_extras(record: logging.LogRecord) -> dict[str, object]:
    standard = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "taskName",
        "asctime",
        "message",
    }
    return {key: value for key, value in vars(record).items() if key not in standard}


def _assert_log_records_clean(records: list[logging.LogRecord]) -> None:
    if not records:
        _raise_generic("log scanner had no records to inspect")
    for record in records:
        _assert_no_protected_text(vars(record), where="full LogRecord")
        _assert_no_protected_text(record.msg, where="log message")
        _assert_no_protected_text(record.args, where="log args")
        _assert_no_protected_text(record.getMessage(), where="rendered log message")
        _assert_no_protected_text(record.exc_info, where="log exc_info")
        _assert_no_protected_text(record.exc_text, where="log exc_text")
        _assert_no_protected_text(record.stack_info, where="log stack")
        _assert_no_protected_text(_record_extras(record), where="log extras")


def _assert_events_clean(events: list[dict[str, Any]]) -> None:
    if not events:
        _raise_generic("Sentry scanner had no events to inspect")
    for event in events:
        _assert_no_protected_text(event, where="Sentry event")


def _assert_transaction_control(events: list[dict[str, Any]]) -> None:
    if not any(event.get("type") == "transaction" for event in events):
        _raise_generic("Sentry transaction positive control was not captured")


class _RecordSink(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Materialise exactly the string a production handler would emit.
        if record.exc_info and not record.exc_text:
            record.exc_text = self.format(record)
        self.records.append(record)


def _test_sentry_scopes() -> tuple[object, ...]:
    current = sentry_sdk.get_current_scope()
    isolation = sentry_sdk.get_isolation_scope()
    return (current,) if current is isolation else (current, isolation)


def _assert_test_sentry_breadcrumbs_clean() -> None:
    for scope in _test_sentry_scopes():
        breadcrumbs = getattr(scope, "_breadcrumbs", ())
        _assert_no_protected_text(
            list(breadcrumbs), where="Sentry isolation breadcrumbs"
        )


def _clear_test_sentry_scopes() -> None:
    for scope in _test_sentry_scopes():
        clear = getattr(scope, "clear_breadcrumbs", None)
        if not callable(clear):
            _raise_generic("Sentry test scope did not expose breadcrumb cleanup")
        clear()
    for scope in _test_sentry_scopes():
        if list(getattr(scope, "_breadcrumbs", ())):
            _raise_generic("Sentry test scope retained breadcrumbs after cleanup")
    _assert_test_sentry_breadcrumbs_clean()


def _clear_contaminated_sentry_controls(
    log_sink: _RecordSink,
    events: list[dict[str, Any]],
    envelope_item_types: list[str],
) -> None:
    log_sink.records.clear()
    events.clear()
    envelope_item_types.clear()
    _clear_test_sentry_scopes()
    if log_sink.records or events or envelope_item_types:
        _raise_generic("contaminated Sentry controls were not cleared")


@pytest.fixture
def task_boundary_sinks():
    """Unfiltered receiver leak proof: capture upstream log/event payloads."""
    events: list[dict[str, Any]] = []

    class _ListTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            for item in envelope.items:
                payload = item.payload.json
                if payload is not None:
                    events.append(payload)

        def capture_event(self, event) -> None:  # type: ignore[no-untyped-def]
            events.append(event)

    client = sentry_sdk.Client(
        dsn="https://r35public@r35.invalid/1",
        transport=_ListTransport(),
        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)
        ],
        traces_sample_rate=1.0,
        send_default_pii=False,
        auto_enabling_integrations=False,
        default_integrations=False,
    )
    with sentry_sdk.isolation_scope() as isolation_scope:
        current_scope = sentry_sdk.get_current_scope()
        previous_current_client = current_scope.client
        previous_isolation_client = isolation_scope.client
        current_scope.set_client(client)
        isolation_scope.set_client(client)

        sink = _RecordSink()
        sink.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(sink)
        root.setLevel(logging.DEBUG)
        try:
            # Positive controls keep the privacy scan from passing over empty
            # sinks or only unserialised Python values.
            logging.getLogger("r35.task_boundary.control").error(
                "safe TaskIQ boundary log control"
            )
            sentry_sdk.capture_message("safe TaskIQ boundary event control")
            with sentry_sdk.start_transaction(
                name="r35 task boundary transaction"
            ) as tx:
                tx.set_data("boundary", "safe")
            yield sink, events
        finally:
            root.removeHandler(sink)
            root.setLevel(previous_level)
            _clear_test_sentry_scopes()
            current_scope.set_client(previous_current_client)
            isolation_scope.set_client(previous_isolation_client)
            client.close(timeout=0)


_PRODUCTION_SAFE_LOGGER = "r35.task_boundary.production.safe_control"


@pytest.fixture
def production_sentry_sinks():
    """Use the shipped Sentry scrub hooks without opening a network connection."""
    from app.monitoring.sentry import (
        _before_breadcrumb,
        _before_send,
        _before_send_log,
        _before_send_transaction,
    )
    from app.services.order_proposals.callback_inbox.observability import (
        worker_transaction,
    )

    events: list[dict[str, Any]] = []
    envelope_item_types: list[str] = []

    class _ListTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            for item in envelope.items:
                item_type = item.headers.get("type")
                if type(item_type) is str:
                    envelope_item_types.append(item_type)
                payload = item.payload.json
                if payload is not None:
                    events.append(payload)

        def capture_event(self, event) -> None:  # type: ignore[no-untyped-def]
            events.append(event)

    client = sentry_sdk.Client(
        dsn="https://r35production@r35.invalid/1",
        transport=_ListTransport(),
        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)
        ],
        traces_sample_rate=1.0,
        send_default_pii=False,
        auto_enabling_integrations=False,
        default_integrations=False,
        before_send=_before_send,
        before_breadcrumb=_before_breadcrumb,
        before_send_log=_before_send_log,
        before_send_transaction=_before_send_transaction,
        enable_logs=True,
    )
    with sentry_sdk.isolation_scope() as isolation_scope:
        current_scope = sentry_sdk.get_current_scope()
        previous_current_client = current_scope.client
        previous_isolation_client = isolation_scope.client
        current_scope.set_client(client)
        isolation_scope.set_client(client)

        sink = _RecordSink()
        sink.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(sink)
        root.setLevel(logging.DEBUG)
        try:
            # The distinct logger must arrive as a LoggingIntegration event; a
            # standalone capture_message cannot satisfy this control.
            sentry_sdk.add_breadcrumb(
                category="r35.production.control",
                message="safe production-hook breadcrumb control",
            )
            logging.getLogger(_PRODUCTION_SAFE_LOGGER).error(
                "safe production-hook logger control"
            )
            sentry_logger.error("safe production-hook Sentry log control")
            job_id = uuid.uuid4()
            with worker_transaction(job_id) as transaction:
                if transaction is not None:
                    transaction.set_data("callback_job.state", "succeeded")
                    transaction.set_data("callback_job.attempt", 1)
                    transaction.set_data("r35.control", "safe")
            client.flush(timeout=1.0)
            yield sink, events, envelope_item_types
        finally:
            root.removeHandler(sink)
            root.setLevel(previous_level)
            _clear_test_sentry_scopes()
            current_scope.set_client(previous_current_client)
            isolation_scope.set_client(previous_isolation_client)
            client.close(timeout=0)


def _contains_mapping_pair(value: object, *, key: str, expected: object) -> bool:
    if type(value) is dict:
        if value.get(key) == expected:
            return True
        return any(
            _contains_mapping_pair(item, key=key, expected=expected)
            for item in value.values()
        )
    if type(value) in (list, tuple):
        return any(
            _contains_mapping_pair(item, key=key, expected=expected) for item in value
        )
    return False


_RECEIVER_POST_EVENT_MESSAGE = "safe TaskIQ Receiver post-event control"


class _HideTaskiqReceiverDiagnosticsFromPytest(logging.Filter):
    """Keep only pytest's renderer from exposing an upstream parser warning."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("taskiq.receiver")


def _hide_taskiq_receiver_diagnostics_from_pytest(
    request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> None:
    """Retain the test sink while preventing pytest from rendering raw input."""
    logging_plugin = request.config.pluginmanager.get_plugin("logging-plugin")
    pytest_handlers = [caplog.handler]
    for attribute in (
        "report_handler",
        "caplog_handler",
        "log_cli_handler",
        "log_file_handler",
    ):
        handler = getattr(logging_plugin, attribute, None)
        if isinstance(handler, logging.Handler) and handler not in pytest_handlers:
            pytest_handlers.append(handler)
    pytest_filter = _HideTaskiqReceiverDiagnosticsFromPytest()
    for handler in pytest_handlers:
        handler.addFilter(pytest_filter)

    def _remove_pytest_filter() -> None:
        for handler in pytest_handlers:
            handler.removeFilter(pytest_filter)

    request.addfinalizer(_remove_pytest_filter)


def _capture_safe_receiver_post_event(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Force any Receiver breadcrumb into a real event on the fixture client."""
    clients = tuple(getattr(scope, "client", None) for scope in _test_sentry_scopes())
    client = clients[0] if clients else None
    if client is None or any(candidate is not client for candidate in clients):
        _raise_generic("Receiver test did not retain one fixture-bound Sentry client")
    flush = getattr(client, "flush", None)
    if not callable(flush):
        _raise_generic("fixture-bound Sentry client did not expose flush")

    before_count = len(events)
    sentry_sdk.capture_message(_RECEIVER_POST_EVENT_MESSAGE)
    flush(timeout=1.0)
    new_events = events[before_count:]
    if len(new_events) != 1:
        _raise_generic(
            "safe post-Receiver Sentry event did not increase count exactly once"
        )
    if not any(
        _contains_mapping_pair(
            event, key="message", expected=_RECEIVER_POST_EVENT_MESSAGE
        )
        for event in new_events
    ):
        _raise_generic("safe post-Receiver Sentry event was not captured")
    return new_events


def _assert_production_sentry_controls(
    events: list[dict[str, Any]], envelope_item_types: list[str]
) -> None:
    from app.services.order_proposals.callback_inbox.observability import (
        WORKER_TRANSACTION_NAME,
        WORKER_TRANSACTION_OP,
    )

    if not any(event.get("logger") == _PRODUCTION_SAFE_LOGGER for event in events):
        _raise_generic("production LoggingIntegration logger control was not captured")
    if "log" not in envelope_item_types:
        _raise_generic("production before_send_log control was not captured")
    transactions = [event for event in events if event.get("type") == "transaction"]
    if not any(
        event.get("transaction") == WORKER_TRANSACTION_NAME for event in transactions
    ):
        _raise_generic("W5 worker transaction control was not captured")
    if not any(
        _contains_mapping_pair(event, key="op", expected=WORKER_TRANSACTION_OP)
        for event in transactions
    ):
        _raise_generic("W5 worker transaction operation was not captured")
    if not any(
        _contains_mapping_pair(event, key="callback_job.state", expected="succeeded")
        for event in transactions
    ):
        _raise_generic("W5 worker transaction data control was not captured")


def _snapshot_production_observability(
    log_sink: _RecordSink,
    events: list[dict[str, Any]],
    envelope_item_types: list[str],
) -> tuple[
    tuple[int, ...],
    tuple[dict[str, object], ...],
    list[dict[str, Any]],
    tuple[str, ...],
]:
    """Take an exact, safe snapshot before an input must be a no-op."""
    return (
        tuple(id(record) for record in log_sink.records),
        tuple(copy.deepcopy(dict(vars(record))) for record in log_sink.records),
        copy.deepcopy(events),
        tuple(envelope_item_types),
    )


def _assert_production_observability_unchanged(
    log_sink: _RecordSink,
    events: list[dict[str, Any]],
    envelope_item_types: list[str],
    snapshot: tuple[
        tuple[int, ...],
        tuple[dict[str, object], ...],
        list[dict[str, Any]],
        tuple[str, ...],
    ],
) -> None:
    """Reject even fixed-safe log/Sentry output for a rejected raw UUID."""
    record_ids, record_fields, expected_events, expected_item_types = snapshot
    if tuple(id(record) for record in log_sink.records) != record_ids:
        _raise_generic("invalid recovery id changed production log record count")
    if tuple(copy.deepcopy(dict(vars(record))) for record in log_sink.records) != (
        record_fields
    ):
        _raise_generic("invalid recovery id changed production log record contents")
    if events != expected_events:
        _raise_generic("invalid recovery id changed production Sentry event contents")
    if tuple(envelope_item_types) != expected_item_types:
        _raise_generic("invalid recovery id changed production Sentry envelope items")


def test_closed_worker_status_vocabularies_match_the_independent_contract() -> None:
    """A one-line runtime allowlist widening must fail this boundary audit."""
    from app.services.order_proposals.callback_inbox.contracts import (
        WORKER_STATUSES,
        WorkerStatus,
    )
    from app.services.order_proposals.callback_inbox.result_boundary import (
        PROCESS_STATUSES,
    )

    if PROCESS_STATUSES != EXPECTED_PROCESS_STATUSES:
        _raise_generic("result-boundary process statuses diverged from the contract")
    if WORKER_STATUSES != EXPECTED_PROCESS_STATUSES | {"disabled"}:
        _raise_generic(
            "worker statuses diverged from the contract plus its gate status"
        )
    if frozenset(item.value for item in WorkerStatus) != (
        EXPECTED_PROCESS_STATUSES | {"disabled"}
    ):
        _raise_generic("public WorkerStatus values diverged from the contract")


@pytest.mark.parametrize(
    "protected_text",
    _FAKE_FRAGMENTS,
    ids=("full", "prefix", "middle", "suffix"),
)
def test_task_boundary_scanner_detects_full_and_stable_secret_fragments(
    protected_text: str,
) -> None:
    """The recursive scanner itself cannot pass merely because it is inert."""
    with pytest.raises(AssertionError):
        _assert_no_protected_text(protected_text, where="scanner positive control")


def test_task_boundary_log_and_sentry_positive_controls_are_nonvacuous(
    task_boundary_sinks: tuple[_RecordSink, list[dict[str, Any]]],
) -> None:
    log_sink, events = task_boundary_sinks
    _assert_log_records_clean(log_sink.records)
    _assert_events_clean(events)
    _assert_transaction_control(events)


def test_production_sentry_hook_controls_are_nonvacuous(
    production_sentry_sinks: tuple[_RecordSink, list[dict[str, Any]], list[str]],
) -> None:
    log_sink, events, envelope_item_types = production_sentry_sinks
    _assert_log_records_clean(log_sink.records)
    _assert_events_clean(events)
    _assert_production_sentry_controls(events, envelope_item_types)


def test_task_boundary_raw_scanners_reject_contaminated_controls_and_clear_them(
    production_sentry_sinks: tuple[_RecordSink, list[dict[str, Any]], list[str]],
) -> None:
    """Prove LogRecord and actual TaskIQ/Pickle scanners reject contamination."""
    from app.core.taskiq_broker import broker, result_backend

    log_sink, events, envelope_item_types = production_sentry_sinks
    if broker.result_backend is not result_backend:
        _raise_generic(
            "configured broker result backend drifted during scanner control"
        )
    if type(broker.result_backend.serializer) is not PickleSerializer:
        _raise_generic(
            "scanner control no longer uses the production Pickle serializer"
        )

    contaminated_record = logging.LogRecord(
        "r35.task_boundary.contaminated_control",
        logging.ERROR,
        __file__,
        1,
        "contaminated control %s",
        (_FAKE_SECRET,),
        None,
    )
    contaminated_result: TaskiqResult[Any] = TaskiqResult(
        is_err=False,
        log=None,
        return_value={"boundary": _FAKE_SECRET},
        execution_time=0.0,
        labels={},
        error=None,
    )
    payload = model_dump(contaminated_result)
    raw = broker.result_backend.serializer.dumpb(payload)
    decoded = broker.result_backend.serializer.loadb(raw)

    controls: tuple[Callable[[], None], ...] = (
        lambda: _assert_log_records_clean([contaminated_record]),
        lambda: _assert_no_protected_text(
            payload, where="contaminated TaskiqResult model"
        ),
        lambda: _assert_no_protected_text(raw, where="contaminated Pickle bytes"),
        lambda: _assert_no_protected_text(
            decoded, where="contaminated Pickle decoded payload"
        ),
    )
    for control in controls:
        with pytest.raises(AssertionError):
            control()

    _clear_contaminated_sentry_controls(log_sink, events, envelope_item_types)


@pytest.mark.parametrize(
    "surface",
    ("event", "breadcrumb", "sentry_log", "transaction"),
    ids=("event", "breadcrumb", "sentry-log", "transaction"),
)
def test_production_sentry_scanners_reject_each_isolated_contaminated_surface(
    production_sentry_sinks: tuple[_RecordSink, list[dict[str, Any]], list[str]],
    surface: str,
) -> None:
    """Each in-memory Sentry surface must fail its scanner independently."""
    log_sink, events, envelope_item_types = production_sentry_sinks
    log_envelopes_before = envelope_item_types.count("log")

    if surface == "event":
        sentry_sdk.capture_event({"message": _FAKE_SECRET})
    elif surface == "breadcrumb":
        sentry_sdk.add_breadcrumb(
            category="r35.contaminated_control",
            message=_FAKE_SECRET,
        )
        sentry_sdk.capture_message("safe breadcrumb scanner trigger")
    elif surface == "sentry_log":
        sentry_logger.error(_FAKE_SECRET)
    elif surface == "transaction":
        with sentry_sdk.start_transaction(
            name="r35 contaminated scanner control"
        ) as tx:
            tx.set_data("r35.contaminated", _FAKE_SECRET)
    else:
        _raise_generic("unknown isolated Sentry contamination surface")

    sentry_sdk.flush(timeout=1.0)
    if (
        surface == "sentry_log"
        and envelope_item_types.count("log") <= log_envelopes_before
    ):
        _raise_generic("isolated Sentry log control did not create a log envelope")
    if surface == "breadcrumb":
        with pytest.raises(AssertionError):
            _assert_test_sentry_breadcrumbs_clean()
    with pytest.raises(AssertionError):
        _assert_events_clean(events)
    _clear_contaminated_sentry_controls(log_sink, events, envelope_item_types)


def _valid_recovery_report(*, populated: bool = True) -> dict[str, object]:
    statuses = dict.fromkeys(EXPECTED_RECOVERY_ITEM_STATUSES, 0)
    if populated:
        # Both claim-consuming and non-claiming outcomes must be representable
        # in a valid full report; one lone success cannot prove the arithmetic.
        statuses.update(
            {
                "dead_letter": 1,
                "not_found": 3,
                "retry_scheduled": 1,
                "succeeded": 2,
            }
        )
        scanned = 7
        claimed = 4
        backlog: dict[str, object] = {
            "pending": 2,
            "processing": 1,
            "retry_wait": 1,
            "dead_letter": 1,
            "oldest_pending_age_seconds": 0.0,
        }
    else:
        scanned = 0
        claimed = 0
        backlog = {
            "pending": 0,
            "processing": 0,
            "retry_wait": 0,
            "dead_letter": 0,
            "oldest_pending_age_seconds": None,
        }
    return {
        "status": "ok",
        "scanned": scanned,
        "claimed": claimed,
        "statuses": statuses,
        "backlog": backlog,
    }


def _assert_valid_recovery_report(value: object) -> None:
    _assert_exact_keys(value, EXPECTED_RECOVERY_REPORT_KEYS, where="recovery report")
    assert type(value) is dict
    _assert_exact_string(value["status"], "ok", where="recovery report status")
    for key in ("scanned", "claimed"):
        if type(value[key]) is not int or value[key] < 0:
            _raise_generic(f"recovery report {key} was not a nonnegative built-in int")
    statuses = value["statuses"]
    _assert_exact_keys(statuses, EXPECTED_RECOVERY_ITEM_STATUSES, where="statuses")
    assert type(statuses) is dict
    for count in statuses.values():
        if type(count) is not int or count < 0:
            _raise_generic("recovery status count was not a nonnegative built-in int")
    backlog = value["backlog"]
    _assert_exact_keys(backlog, EXPECTED_BACKLOG_KEYS, where="backlog")
    assert type(backlog) is dict
    for key in EXPECTED_BACKLOG_KEYS - {"oldest_pending_age_seconds"}:
        count = backlog[key]
        if type(count) is not int or count < 0:
            _raise_generic("recovery backlog count was not a nonnegative built-in int")
    age = backlog["oldest_pending_age_seconds"]
    if age is not None and (
        type(age) is not float or age < 0 or not math.isfinite(age)
    ):
        _raise_generic("recovery oldest age was not a finite nonnegative float or None")

    scanned = value["scanned"]
    claimed = value["claimed"]
    assert type(scanned) is int and type(claimed) is int
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_SCAN_LIMIT,
        recovery_scan_cap,
    )

    if claimed > scanned or scanned > recovery_scan_cap(RECOVERY_SCAN_LIMIT):
        _raise_generic("recovery report violated its default scan/claim bounds")
    if sum(statuses.values()) != scanned:
        _raise_generic("recovery statuses did not sum to scanned")
    expected_claimed = sum(
        count
        for status, count in statuses.items()
        if status not in NON_CLAIMED_ITEM_STATUSES
    )
    if claimed != expected_claimed:
        _raise_generic("recovery claimed did not match the closed item vocabulary")


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_enabled", (False, True), ids=("gate-off", "gate-on"))
@pytest.mark.parametrize(
    "case",
    (
        "invalid_text",
        "whitespace",
        "trailing_whitespace",
        "uppercase",
        "braces",
        "urn",
        "compact",
        "uuid_object",
        "string_subclass",
        "bytes",
        "number",
        "bool",
        "container",
        "hostile",
    ),
)
async def test_noncanonical_job_ids_are_rejected_before_gate_worker_or_database(
    monkeypatch: pytest.MonkeyPatch,
    worker_enabled: bool,
    case: str,
) -> None:
    """Both gate branches reject before worker, session, or engine authority."""
    from app.core import db
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = "01234567-89ab-4def-8abc-def012345678"
    hostile = _Hostile()
    values: dict[str, object] = {
        "invalid_text": "not-a-uuid",
        "whitespace": f" {canonical}",
        "trailing_whitespace": f"{canonical} ",
        "uppercase": canonical.upper(),
        "braces": f"{{{canonical}}}",
        "urn": f"urn:uuid:{canonical}",
        "compact": canonical.replace("-", ""),
        "uuid_object": uuid.UUID(canonical),
        "string_subclass": _StringSubclass(canonical),
        "bytes": canonical.encode(),
        "number": 1,
        "bool": True,
        "container": [canonical],
        "hostile": hostile,
    }
    touched: list[str] = []

    def _session_tripwire(*args: object, **kwargs: object) -> object:
        touched.append("session")
        return object()

    class _EngineTripwire:
        async def connect(self, *args: object, **kwargs: object) -> object:
            touched.append("engine")
            raise AssertionError("invalid TaskIQ input opened a database engine")

    async def _process_tripwire(*args: object, **kwargs: object) -> object:
        # Make every tripwire effective under a hypothetical accidental call:
        # no input reaches this seam, so none of these test-owned operations
        # may run under either gate state.
        touched.append("process")
        worker_module.AsyncSessionLocal()
        await db.engine.connect()
        return {"status": "succeeded", "job_id": canonical}

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        worker_enabled,
        raising=False,
    )
    monkeypatch.setattr(worker_module, "AsyncSessionLocal", _session_tripwire)
    monkeypatch.setattr(db, "engine", _EngineTripwire())
    monkeypatch.setattr(task_module, "process_callback_job", _process_tripwire)

    try:
        result = await task_module.run_telegram_callback_job(values[case])  # type: ignore[arg-type]
    except Exception:
        _raise_generic("noncanonical TaskIQ input crossed the task boundary")
    _assert_invalid_job_id_result(result)
    if touched:
        _raise_generic(
            "noncanonical TaskIQ input touched process or database authority"
        )
    if hostile.calls:
        _raise_generic("noncanonical TaskIQ input invoked hostile coercion")


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_enabled", (False, True), ids=("gate-off", "gate-on"))
@pytest.mark.parametrize("argument_style", ("positional", "keyword"))
@pytest.mark.parametrize("sentry_surface", ("unfiltered", "production"))
@pytest.mark.parametrize(
    "case",
    (
        "canonical_bytes",
        "canonical_bytearray",
        "canonical_string_subclass",
        "private_bytes",
        "private_list",
        "private_dict",
        "hostile_object",
    ),
)
async def test_receiver_rejects_raw_noncanonical_job_ids_before_task_gate_or_worker(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
    worker_enabled: bool,
    argument_style: str,
    sentry_surface: str,
    case: str,
) -> None:
    """The real TaskIQ Receiver must not coerce wire values before this task."""
    __tracebackhide__ = True
    from app.core import db
    from app.core.config import settings
    from app.core.taskiq_broker import broker, result_backend
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = "01234567-89ab-4def-8abc-def012345678"
    values: dict[str, object] = {
        "canonical_bytes": canonical.encode(),
        "canonical_bytearray": bytearray(canonical.encode()),
        "canonical_string_subclass": _StringSubclass(canonical),
        # This noncanonical private value proves the rejected input cannot be
        # reflected through the actual Receiver/result-serializer path.
        "private_bytes": _FAKE_SECRET.encode(),
        "private_list": [_FAKE_SECRET],
        "private_dict": {"private": _FAKE_SECRET},
        "hostile_object": _Hostile(),
    }
    touched: list[str] = []
    envelope_item_types: list[str] | None = None
    if sentry_surface == "unfiltered":
        log_sink, events = request.getfixturevalue("task_boundary_sinks")
    else:
        log_sink, events, envelope_item_types = request.getfixturevalue(
            "production_sentry_sinks"
        )

    # Keep a broken upstream Receiver/Pydantic log out of pytest's rendered
    # failure section.  The test-owned production-equivalent sink below still
    # receives and scans every record before the generic assertion is raised.
    _hide_taskiq_receiver_diagnostics_from_pytest(request, caplog)

    def _session_tripwire(*args: object, **kwargs: object) -> object:
        touched.append("session")
        return object()

    class _EngineTripwire:
        async def connect(self, *args: object, **kwargs: object) -> object:
            touched.append("engine")
            raise AssertionError("invalid Receiver input opened a database engine")

    async def _process_tripwire(*args: object, **kwargs: object) -> object:
        touched.append("process")
        worker_module.AsyncSessionLocal()
        await db.engine.connect()
        return {"status": "succeeded", "job_id": canonical}

    if broker.result_backend is not result_backend:
        _raise_generic("configured broker no longer owns the imported result backend")
    if type(broker.result_backend) is not RedisAsyncResultBackend:
        _raise_generic(
            "configured TaskIQ result backend is no longer RedisAsyncResultBackend"
        )
    if type(broker.result_backend.serializer) is not PickleSerializer:
        _raise_generic(
            "configured TaskIQ result serializer is no longer PickleSerializer"
        )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        worker_enabled,
        raising=False,
    )
    monkeypatch.setattr(worker_module, "AsyncSessionLocal", _session_tripwire)
    monkeypatch.setattr(db, "engine", _EngineTripwire())
    monkeypatch.setattr(task_module, "process_callback_job", _process_tripwire)

    raw_value = values[case]
    args: list[object] = [raw_value] if argument_style == "positional" else []
    kwargs: dict[str, object] = (
        {} if argument_style == "positional" else {"job_id": raw_value}
    )
    task = task_module.run_telegram_callback_job
    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name=task.task_name,
        labels=dict(task.labels),
        args=args,
        kwargs=kwargs,
    )
    receiver = Receiver(
        broker=broker,
        validate_params=True,
        max_async_tasks=1,
        run_startup=False,
    )
    result: TaskiqResult[Any] = await receiver.run_task(
        target=task.original_func,
        message=message,
    )
    payload = model_dump(result)
    raw = broker.result_backend.serializer.dumpb(payload)
    decoded = broker.result_backend.serializer.loadb(raw)
    post_receiver_events = _capture_safe_receiver_post_event(events)

    def _assert_receiver_result() -> None:
        if result.is_err is not False or result.error is not None:
            _raise_generic("Receiver did not return a safe invalid-id task result")
        _assert_invalid_job_id_result(result.return_value)

    def _assert_untouched() -> None:
        if touched:
            _raise_generic("raw Receiver input reached worker or database authority")

    def _assert_hostile_input_unrendered() -> None:
        if case == "hostile_object":
            hostile = values["hostile_object"]
            assert type(hostile) is _Hostile
            if hostile.calls:
                _raise_generic("Receiver rendered a hostile raw job-id object")

    def _assert_sentry_control() -> None:
        if sentry_surface == "unfiltered":
            _assert_transaction_control(events)
            return
        assert envelope_item_types is not None
        _assert_production_sentry_controls(events, envelope_item_types)

    _assert_all_clean(
        (
            _assert_receiver_result,
            _assert_untouched,
            _assert_hostile_input_unrendered,
            lambda: _assert_no_protected_text(raw, where="Receiver Pickle bytes"),
            lambda: _assert_no_protected_text(
                decoded, where="Receiver Pickle decoded result"
            ),
            lambda: _assert_no_protected_text(
                payload, where="Receiver TaskiqResult model"
            ),
            lambda: _assert_no_protected_text(
                result.error, where="Receiver TaskiqResult error object"
            ),
            lambda: _assert_log_records_clean(log_sink.records),
            lambda: _assert_events_clean(events),
            lambda: _assert_events_clean(post_receiver_events),
            _assert_test_sentry_breadcrumbs_clean,
            _assert_sentry_control,
        ),
        message="TaskIQ Receiver coerced or reflected a raw callback job id",
    )


@pytest.mark.asyncio
async def test_receiver_str_annotation_control_coerces_bytes_to_an_exact_string() -> (
    None
):
    """Positive control: the installed Receiver really applies annotations."""
    from app.core.taskiq_broker import broker

    canonical = "01234567-89ab-4def-8abc-def012345678"
    seen: list[object] = []

    async def _annotated_probe(job_id: str) -> dict[str, str]:
        seen.append(job_id)
        return {"status": "ok"}

    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name="r35.taskiq.annotation_control",
        labels={},
        args=[canonical.encode()],
        kwargs={},
    )
    receiver = Receiver(
        broker=broker,
        validate_params=True,
        max_async_tasks=1,
        run_startup=False,
    )
    result: TaskiqResult[Any] = await receiver.run_task(
        target=_annotated_probe,
        message=message,
    )
    if result.is_err is not False or result.error is not None:
        _raise_generic("TaskIQ annotation positive control did not complete safely")
    if len(seen) != 1 or type(seen[0]) is not str or seen[0] != canonical:
        _raise_generic("TaskIQ annotation positive control did not coerce bytes")


@pytest.mark.asyncio
@pytest.mark.parametrize("sentry_surface", ("unfiltered", "production"))
@pytest.mark.parametrize(
    "case",
    ("json_list", "json_dict", "hostile_object"),
    ids=("json-list", "json-dict", "hostile-object"),
)
async def test_receiver_validation_warnings_do_not_reach_sentry_post_event(
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
    sentry_surface: str,
    case: str,
) -> None:
    """A safe post-Receiver event must carry no parser-warning breadcrumb."""
    __tracebackhide__ = True
    from app.core.taskiq_broker import broker
    from app.tasks import telegram_callback_inbox_tasks as task_module

    values: dict[str, object] = {
        "json_list": [_FAKE_SECRET],
        "json_dict": {"private": _FAKE_SECRET},
        "hostile_object": _Hostile(),
    }
    envelope_item_types: list[str] | None = None
    if sentry_surface == "unfiltered":
        _log_sink, events = request.getfixturevalue("task_boundary_sinks")
    else:
        _log_sink, events, envelope_item_types = request.getfixturevalue(
            "production_sentry_sinks"
        )
    _hide_taskiq_receiver_diagnostics_from_pytest(request, caplog)

    task = task_module.run_telegram_callback_job
    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name=task.task_name,
        labels=dict(task.labels),
        args=[values[case]],
        kwargs={},
    )
    receiver = Receiver(
        broker=broker,
        validate_params=True,
        max_async_tasks=1,
        run_startup=False,
    )
    result: TaskiqResult[Any] = await receiver.run_task(
        target=task.original_func,
        message=message,
    )
    post_receiver_events = _capture_safe_receiver_post_event(events)

    def _assert_receiver_result() -> None:
        if result.is_err is not False or result.error is not None:
            _raise_generic("Receiver warning control did not return a safe result")
        _assert_invalid_job_id_result(result.return_value)

    def _assert_sentry_control() -> None:
        if sentry_surface == "unfiltered":
            _assert_transaction_control(events)
            return
        assert envelope_item_types is not None
        _assert_production_sentry_controls(events, envelope_item_types)

    _assert_all_clean(
        (
            _assert_receiver_result,
            lambda: _assert_events_clean(events),
            lambda: _assert_events_clean(post_receiver_events),
            _assert_test_sentry_breadcrumbs_clean,
            _assert_sentry_control,
        ),
        message="Receiver validation warning crossed the Sentry boundary",
    )


@pytest.mark.asyncio
async def test_all_uuid_version_and_variant_nibbles_use_the_canonical_wire_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No UUID version/variant restriction is smuggled into the input boundary."""
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    calls: list[object] = []

    async def _process(job_id: object) -> dict[str, str]:
        calls.append(job_id)
        assert type(job_id) is str
        return {"status": "succeeded", "job_id": job_id}

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "process_callback_job", _process)
    canonical_values = [
        "00000000-0000-0000-0000-000000000000",
        *(
            f"01234567-89ab-{version}def-{variant}234-56789abcdef0"
            for version in "0123456789abcdef"
            for variant in "0123456789abcdef"
        ),
    ]
    for canonical in canonical_values:
        if tuple(map(len, canonical.split("-"))) != (8, 4, 4, 4, 12):
            _raise_generic("test UUID inventory did not have canonical group lengths")
        try:
            parsed = uuid.UUID(canonical)
        except (TypeError, ValueError):
            _raise_generic("test UUID inventory did not parse")
        if str(parsed) != canonical:
            _raise_generic("test UUID inventory was not canonical")
        result = await task_module.run_telegram_callback_job(canonical)
        _assert_job_result(result, status="succeeded", job_id=canonical)
    if calls != canonical_values:
        _raise_generic("canonical UUID inventory did not reach the worker exactly once")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(EXPECTED_PROCESS_STATUSES))
async def test_worker_results_allow_only_the_independent_closed_vocabulary(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = str(uuid.uuid4())
    ignored_extra = _Hostile()

    async def _process(job_id: object) -> dict[str, object]:
        return {
            "status": status,
            "job_id": canonical,
            # The boundary must ignore extras without even rendering them.
            "private_extra": ignored_extra,
        }

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "process_callback_job", _process)
    result = await task_module.run_telegram_callback_job(canonical)
    _assert_job_result(result, status=status, job_id=canonical)
    _assert_no_protected_text(result, where="positive worker result")
    if ignored_extra.calls:
        _raise_generic("ignored worker extras invoked hostile coercion")


@pytest.mark.asyncio
async def test_non_string_worker_extra_keys_are_ignored_without_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required fields are read without touching arbitrary exact-dict extras."""
    from app.core.config import settings
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = str(uuid.uuid4())
    task_key = _HostileExtraKey()
    task_result: dict[object, object] = {
        "status": "succeeded",
        "job_id": canonical,
        task_key: _FAKE_SECRET,
    }
    task_key.calls.clear()

    async def _task_process(*args: object, **kwargs: object) -> object:
        return task_result

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "process_callback_job", _task_process)
    task_boundary_result = await task_module.run_telegram_callback_job(canonical)
    _assert_job_result(task_boundary_result, status="succeeded", job_id=canonical)
    _assert_no_protected_text(task_boundary_result, where="extra-key task result")

    recovery_job_id = uuid.uuid4()
    recovery_key = _HostileExtraKey()
    recovery_result: dict[object, object] = {
        "status": "succeeded",
        "job_id": str(recovery_job_id),
        recovery_key: _FAKE_SECRET,
    }
    recovery_key.calls.clear()

    async def _recovery_process(*args: object, **kwargs: object) -> object:
        return recovery_result

    recovery_status = await recovery_module._process_one(
        recovery_job_id,
        process_fn=_recovery_process,
        now_fn=now_kst,
        worker_kwargs={},
    )
    _assert_exact_string(
        recovery_status, "succeeded", where="recovery extra-key worker result"
    )
    if task_key.calls or recovery_key.calls:
        _raise_generic("worker extra key invoked a boundary callback")


def _invalid_worker_result(case: str, canonical: str, hostile: _Hostile) -> object:
    if case == "disabled_status":
        return {"status": "disabled", "job_id": canonical}
    if case == "unknown_status":
        return {"status": _FAKE_SECRET, "job_id": canonical}
    if case == "status_subclass":
        return {"status": _StringSubclass("succeeded"), "job_id": canonical}
    if case == "status_hostile":
        return {"status": hostile, "job_id": canonical}
    if case == "wrong_canonical_id":
        return {"status": "succeeded", "job_id": str(uuid.uuid4())}
    if case == "same_id_uppercase":
        return {"status": "succeeded", "job_id": canonical.upper()}
    if case == "same_id_compact":
        return {"status": "succeeded", "job_id": canonical.replace("-", "")}
    if case == "same_id_braces":
        return {"status": "succeeded", "job_id": f"{{{canonical}}}"}
    if case == "same_id_urn":
        return {"status": "succeeded", "job_id": f"urn:uuid:{canonical}"}
    if case == "secret_id":
        return {"status": "succeeded", "job_id": _FAKE_SECRET}
    if case == "uuid_id":
        return {"status": "succeeded", "job_id": uuid.UUID(canonical)}
    if case == "id_subclass":
        return {"status": "succeeded", "job_id": _StringSubclass(canonical)}
    if case == "id_hostile":
        return {"status": "succeeded", "job_id": hostile}
    if case == "missing_status":
        return {"job_id": canonical}
    if case == "missing_job_id":
        return {"status": "succeeded"}
    if case == "dict_subclass":
        return _DictSubclass(status="succeeded", job_id=canonical)
    if case == "top_key_subclass":
        return {
            _StringSubclass("status"): "succeeded",
            _StringSubclass("job_id"): canonical,
        }
    if case == "non_dict":
        return ["succeeded", canonical]
    raise AssertionError("unknown test-only worker-result case")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "disabled_status",
        "unknown_status",
        "status_subclass",
        "status_hostile",
        "wrong_canonical_id",
        "same_id_uppercase",
        "same_id_compact",
        "same_id_braces",
        "same_id_urn",
        "secret_id",
        "uuid_id",
        "id_subclass",
        "id_hostile",
        "missing_status",
        "missing_job_id",
        "dict_subclass",
        "top_key_subclass",
        "non_dict",
    ),
)
async def test_invalid_worker_result_shapes_collapse_to_the_fixed_error(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = "01234567-89ab-4def-8abc-def012345678"
    hostile = _Hostile()
    raw_result = _invalid_worker_result(case, canonical, hostile)

    async def _process(job_id: object) -> object:
        return raw_result

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "process_callback_job", _process)
    try:
        result = await task_module.run_telegram_callback_job(canonical)
    except Exception:
        _raise_generic("invalid worker result escaped the TaskIQ boundary")
    _assert_job_result(result, status="error", job_id=canonical)
    if hostile.calls:
        _raise_generic("invalid worker result invoked hostile coercion")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("ordinary", "exception_group"))
async def test_ordinary_worker_exception_and_exception_group_collapse_without_echoing(
    monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = str(uuid.uuid4())
    ordinary = _BoundaryRuntimeError(reject_render=True)
    cause = _BoundaryRuntimeError(reject_render=True)
    grouped = _BoundaryRuntimeError(reject_render=True)
    group = _RenderTrackedExceptionGroup("safe exception group", [grouped])

    async def _explode(job_id: object) -> object:
        raise ordinary from cause

    async def _group(job_id: object) -> object:
        raise group

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    process = _explode if failure_kind == "ordinary" else _group
    monkeypatch.setattr(task_module, "process_callback_job", process)
    try:
        result = await task_module.run_telegram_callback_job.original_func(canonical)
    except Exception:
        _raise_generic("ordinary worker failure escaped the TaskIQ boundary")
    for exception in (
        (ordinary, cause) if failure_kind == "ordinary" else (group, grouped)
    ):
        _assert_exception_unrendered(exception, where="worker exception boundary")
    _assert_job_result(result, status="error", job_id=canonical)
    _assert_no_protected_text(result, where="ordinary worker error result")


@pytest.mark.asyncio
async def test_configured_receiver_pickle_result_never_contains_an_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
    task_boundary_sinks: tuple[_RecordSink, list[dict[str, Any]]],
) -> None:
    """Exercise the actual Receiver result representation without Redis I/O."""
    from app.core.config import settings
    from app.core.taskiq_broker import broker, result_backend
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = str(uuid.uuid4())
    log_sink, events = task_boundary_sinks
    task = task_module.run_telegram_callback_job
    cause = _BoundaryRuntimeError()
    error = _BoundaryRuntimeError()

    async def _explode(job_id: object) -> object:
        raise error from cause

    if broker.result_backend is not result_backend:
        _raise_generic("configured broker no longer owns the imported result backend")
    if type(broker.result_backend) is not RedisAsyncResultBackend:
        _raise_generic(
            "configured TaskIQ result backend is no longer RedisAsyncResultBackend"
        )
    if type(broker.result_backend.serializer) is not PickleSerializer:
        _raise_generic(
            "configured TaskIQ result serializer is no longer PickleSerializer"
        )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "process_callback_job", _explode)

    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name=task.task_name,
        labels=dict(task.labels),
        args=[canonical],
        kwargs={},
    )
    receiver = Receiver(
        broker=broker,
        validate_params=True,
        max_async_tasks=1,
        run_startup=False,
    )
    result: TaskiqResult[Any] = await receiver.run_task(
        target=task.original_func,
        message=message,
    )
    payload = model_dump(result)
    raw = broker.result_backend.serializer.dumpb(payload)
    decoded = broker.result_backend.serializer.loadb(raw)

    # Run every actual boundary check before deciding why RED failed.  The
    # counter checks come first, before scanners inspect any error object.
    _assert_all_clean(
        (
            lambda: _assert_exception_unrendered(error, where="TaskIQ worker error"),
            lambda: _assert_exception_unrendered(cause, where="TaskIQ worker cause"),
            lambda: _assert_no_protected_text(raw, where="Pickle bytes"),
            lambda: _assert_no_protected_text(decoded, where="Pickle decoded result"),
            lambda: _assert_no_protected_text(payload, where="TaskiqResult model"),
            lambda: _assert_no_protected_text(
                result.error, where="TaskiqResult error object"
            ),
            lambda: _assert_log_records_clean(log_sink.records),
            lambda: _assert_events_clean(events),
            lambda: _assert_transaction_control(events),
        ),
        message="TaskIQ result boundary leaked protected material",
    )
    if result.is_err is not False or result.error is not None:
        _raise_generic("configured Receiver persisted a TaskIQ error object")
    _assert_job_result(result.return_value, status="error", job_id=canonical)


@pytest.mark.asyncio
async def test_configured_recovery_receiver_pickle_result_never_contains_an_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
    task_boundary_sinks: tuple[_RecordSink, list[dict[str, Any]]],
) -> None:
    """Exercise the recovery task through the configured Receiver and serializer."""
    from app.core.config import settings
    from app.core.taskiq_broker import broker, result_backend
    from app.tasks import telegram_callback_inbox_tasks as task_module

    log_sink, events = task_boundary_sinks
    task = task_module.recover_telegram_callback_jobs
    cause = _BoundaryRuntimeError()
    error = _BoundaryRuntimeError()

    async def _explode() -> object:
        raise error from cause

    if broker.result_backend is not result_backend:
        _raise_generic("configured broker no longer owns the imported result backend")
    if type(broker.result_backend) is not RedisAsyncResultBackend:
        _raise_generic(
            "configured TaskIQ result backend is no longer RedisAsyncResultBackend"
        )
    if type(broker.result_backend.serializer) is not PickleSerializer:
        _raise_generic(
            "configured TaskIQ result serializer is no longer PickleSerializer"
        )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "recover_callback_jobs", _explode)

    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name=task.task_name,
        labels=dict(task.labels),
        args=[],
        kwargs={},
    )
    receiver = Receiver(
        broker=broker,
        validate_params=True,
        max_async_tasks=1,
        run_startup=False,
    )
    result: TaskiqResult[Any] = await receiver.run_task(
        target=task.original_func,
        message=message,
    )
    payload = model_dump(result)
    raw = broker.result_backend.serializer.dumpb(payload)
    decoded = broker.result_backend.serializer.loadb(raw)

    _assert_all_clean(
        (
            lambda: _assert_exception_unrendered(error, where="TaskIQ recovery error"),
            lambda: _assert_exception_unrendered(cause, where="TaskIQ recovery cause"),
            lambda: _assert_no_protected_text(raw, where="Pickle bytes"),
            lambda: _assert_no_protected_text(decoded, where="Pickle decoded result"),
            lambda: _assert_no_protected_text(payload, where="TaskiqResult model"),
            lambda: _assert_no_protected_text(
                result.error, where="TaskiqResult error object"
            ),
            lambda: _assert_log_records_clean(log_sink.records),
            lambda: _assert_events_clean(events),
            lambda: _assert_transaction_control(events),
        ),
        message="TaskIQ recovery result boundary leaked protected material",
    )
    if result.is_err is not False or result.error is not None:
        _raise_generic("configured Receiver persisted a TaskIQ recovery error object")
    _assert_recovery_error(result.return_value)


def _mutated_recovery_report(case: str) -> object:
    report = _valid_recovery_report()
    statuses = report["statuses"]
    backlog = report["backlog"]
    assert type(statuses) is dict and type(backlog) is dict
    if case == "unknown_top_status":
        report["status"] = _FAKE_SECRET
    elif case == "top_status_subclass":
        report["status"] = _StringSubclass("ok")
    elif case == "top_key_subclass":
        value = report.pop("status")
        report[_StringSubclass("status")] = value
    elif case == "unknown_top_key":
        report["private"] = _FAKE_SECRET
    elif case == "missing_top_key":
        del report["backlog"]
    elif case == "top_dict_subclass":
        return _DictSubclass(report)
    elif case == "unknown_status_key":
        statuses[_FAKE_SECRET] = 0
    elif case == "status_key_subclass":
        value = statuses.pop("succeeded")
        statuses[_StringSubclass("succeeded")] = value
    elif case == "missing_status_key":
        del statuses["error"]
    elif case == "statuses_dict_subclass":
        report["statuses"] = _DictSubclass(statuses)
    elif case == "status_count_bool":
        statuses["succeeded"] = True
    elif case == "status_count_string":
        statuses["succeeded"] = "1"
    elif case == "status_count_subclass":
        statuses["succeeded"] = _IntSubclass(2)
    elif case == "status_count_float":
        statuses["succeeded"] = 2.0
    elif case == "status_count_negative":
        statuses["succeeded"] = -1
    elif case == "scanned_bool":
        report["scanned"] = True
    elif case == "scanned_string":
        report["scanned"] = "1"
    elif case == "scanned_subclass":
        report["scanned"] = _IntSubclass(7)
    elif case == "scanned_float":
        report["scanned"] = 7.0
    elif case == "scanned_negative":
        report["scanned"] = -1
    elif case == "claimed_bool":
        report["claimed"] = True
    elif case == "claimed_string":
        report["claimed"] = "1"
    elif case == "claimed_subclass":
        report["claimed"] = _IntSubclass(4)
    elif case == "claimed_float":
        report["claimed"] = 4.0
    elif case == "claimed_negative":
        report["claimed"] = -1
    elif case == "claimed_over_scanned":
        # This necessarily also disagrees with the closed status arithmetic:
        # keeping the status sum valid isolates the claim-vs-scan violation.
        scanned = report["scanned"]
        assert type(scanned) is int
        report["claimed"] = scanned + 1
    elif case == "cap_only":
        from app.services.order_proposals.callback_inbox.contracts import (
            RECOVERY_SCAN_LIMIT,
            recovery_scan_cap,
        )

        cap = recovery_scan_cap(RECOVERY_SCAN_LIMIT)
        statuses.update(dict.fromkeys(EXPECTED_RECOVERY_ITEM_STATUSES, 0))
        statuses["succeeded"] = cap + 1
        report["scanned"] = cap + 1
        report["claimed"] = cap + 1
    elif case == "claimed_over_execution_limit":
        from app.services.order_proposals.callback_inbox.contracts import (
            RECOVERY_SCAN_LIMIT,
            recovery_scan_cap,
        )

        # This remains below the candidate scan cap and satisfies the closed
        # status arithmetic.  Only the default execution budget is violated.
        claimed = RECOVERY_SCAN_LIMIT + 1
        if claimed > recovery_scan_cap(RECOVERY_SCAN_LIMIT):
            _raise_generic("test execution-limit case exceeded the scan cap")
        statuses.update(dict.fromkeys(EXPECTED_RECOVERY_ITEM_STATUSES, 0))
        statuses["succeeded"] = claimed
        report["scanned"] = claimed
        report["claimed"] = claimed
    elif case == "status_total_only":
        report["scanned"] = 8
    elif case == "claimed_sum_mismatch":
        report["claimed"] = 0
    elif case == "unknown_backlog_key":
        backlog[_FAKE_SECRET] = 0
    elif case == "backlog_key_subclass":
        value = backlog.pop("pending")
        backlog[_StringSubclass("pending")] = value
    elif case == "missing_backlog_key":
        del backlog["pending"]
    elif case == "backlog_dict_subclass":
        report["backlog"] = _DictSubclass(backlog)
    elif case == "backlog_count_bool":
        backlog["pending"] = True
    elif case == "backlog_count_string":
        backlog["pending"] = "1"
    elif case == "backlog_count_subclass":
        backlog["pending"] = _IntSubclass(2)
    elif case == "backlog_count_float":
        backlog["pending"] = 2.0
    elif case == "backlog_count_negative":
        backlog["pending"] = -1
    elif case == "age_integer":
        backlog["oldest_pending_age_seconds"] = 0
    elif case == "age_subclass":
        backlog["oldest_pending_age_seconds"] = _FloatSubclass(0.0)
    elif case == "age_bool":
        backlog["oldest_pending_age_seconds"] = True
    elif case == "age_string":
        backlog["oldest_pending_age_seconds"] = "0.0"
    elif case == "age_nan":
        backlog["oldest_pending_age_seconds"] = float("nan")
    elif case == "age_infinity":
        backlog["oldest_pending_age_seconds"] = float("inf")
    elif case == "age_negative_infinity":
        backlog["oldest_pending_age_seconds"] = float("-inf")
    elif case == "age_negative":
        backlog["oldest_pending_age_seconds"] = -1.0
    elif case == "backlog_hostile":
        backlog["pending"] = _Hostile()
    else:
        raise AssertionError("unknown test-only recovery mutation")
    return report


@pytest.mark.asyncio
@pytest.mark.parametrize("report_kind", ("populated", "all_zero"))
async def test_valid_full_recovery_reports_require_all_eight_statuses(
    monkeypatch: pytest.MonkeyPatch, report_kind: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    report = _valid_recovery_report(populated=report_kind == "populated")

    async def _recover() -> dict[str, object]:
        return report

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "recover_callback_jobs", _recover)
    result = await task_module.recover_telegram_callback_jobs()
    _assert_valid_recovery_report(result)
    assert type(result) is dict
    statuses = result["statuses"]
    assert type(statuses) is dict
    if set(statuses) != EXPECTED_RECOVERY_ITEM_STATUSES or len(statuses) != 8:
        _raise_generic(
            "successful recovery report did not retain all eight status keys"
        )
    if report_kind == "all_zero":
        if any(count != 0 for count in statuses.values()):
            _raise_generic(
                "all-zero recovery report did not retain zero-filled statuses"
            )
    else:
        if not any(
            count > 0
            for status, count in statuses.items()
            if status in NON_CLAIMED_ITEM_STATUSES
        ):
            _raise_generic("populated recovery report omitted non-claiming statuses")
        if not any(
            count > 0
            for status, count in statuses.items()
            if status not in NON_CLAIMED_ITEM_STATUSES
        ):
            _raise_generic("populated recovery report omitted claim-consuming statuses")


@pytest.mark.asyncio
async def test_recovery_report_at_default_execution_limit_remains_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict TaskIQ boundary accepts a complete report at its exact limit."""
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_SCAN_LIMIT,
    )
    from app.tasks import telegram_callback_inbox_tasks as task_module

    report = _valid_recovery_report(populated=False)
    statuses = report["statuses"]
    assert type(statuses) is dict
    statuses["succeeded"] = RECOVERY_SCAN_LIMIT
    report["scanned"] = RECOVERY_SCAN_LIMIT
    report["claimed"] = RECOVERY_SCAN_LIMIT

    async def _recover() -> dict[str, object]:
        return report

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "recover_callback_jobs", _recover)
    result = await task_module.recover_telegram_callback_jobs()
    _assert_valid_recovery_report(result)


@pytest.mark.parametrize(
    ("claimed", "expected_valid"),
    ((3, True), (4, False)),
    ids=("at-custom-limit", "over-custom-limit"),
)
def test_recovery_report_projector_obeys_the_explicit_execution_limit(
    claimed: int, expected_valid: bool
) -> None:
    """A caller-supplied execution cap cannot silently become the default 20."""
    from app.services.order_proposals.callback_inbox.result_boundary import (
        project_recovery_report,
    )

    report = _valid_recovery_report(populated=False)
    statuses = report["statuses"]
    assert type(statuses) is dict
    statuses["succeeded"] = claimed
    report["scanned"] = claimed
    report["claimed"] = claimed

    try:
        projected = project_recovery_report(
            report,
            execution_limit=3,
            scan_cap=8,
        )
    except TypeError:
        _raise_generic("recovery report projector omitted an execution-limit input")

    if expected_valid:
        _assert_valid_recovery_report(projected)
        assert type(projected) is dict
        if projected["claimed"] != claimed or projected["scanned"] != claimed:
            _raise_generic("projector did not retain the custom-limit report")
    elif projected is not None:
        _raise_generic("projector accepted a report over its explicit execution limit")


@pytest.mark.asyncio
async def test_recovery_task_passes_execution_and_scan_limits_by_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task owns both authoritative limits and passes neither positionally."""
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_SCAN_LIMIT,
        recovery_scan_cap,
    )
    from app.tasks import telegram_callback_inbox_tasks as task_module

    report = _valid_recovery_report(populated=False)
    calls: list[tuple[object, int, int]] = []
    expected_scan_cap = recovery_scan_cap(RECOVERY_SCAN_LIMIT)

    async def _recover() -> dict[str, object]:
        return report

    def _project(
        value: object, *, execution_limit: int, scan_cap: int
    ) -> dict[str, object]:
        calls.append((value, execution_limit, scan_cap))
        return report

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "recover_callback_jobs", _recover)
    monkeypatch.setattr(task_module, "project_recovery_report", _project)

    try:
        result = await task_module.recover_telegram_callback_jobs.original_func()
    except Exception:
        _raise_generic("recovery task did not call the fixed projector seam")
    _assert_valid_recovery_report(result)
    if calls != [(report, RECOVERY_SCAN_LIMIT, expected_scan_cap)]:
        _raise_generic(
            "recovery task did not pass both authoritative limits by keyword"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "unknown_top_status",
        "top_status_subclass",
        "top_key_subclass",
        "unknown_top_key",
        "missing_top_key",
        "top_dict_subclass",
        "unknown_status_key",
        "status_key_subclass",
        "missing_status_key",
        "statuses_dict_subclass",
        "status_count_bool",
        "status_count_string",
        "status_count_subclass",
        "status_count_float",
        "status_count_negative",
        "scanned_bool",
        "scanned_string",
        "scanned_subclass",
        "scanned_float",
        "scanned_negative",
        "claimed_bool",
        "claimed_string",
        "claimed_subclass",
        "claimed_float",
        "claimed_negative",
        "claimed_over_scanned",
        "cap_only",
        "claimed_over_execution_limit",
        "status_total_only",
        "claimed_sum_mismatch",
        "unknown_backlog_key",
        "backlog_key_subclass",
        "missing_backlog_key",
        "backlog_dict_subclass",
        "backlog_count_bool",
        "backlog_count_string",
        "backlog_count_subclass",
        "backlog_count_float",
        "backlog_count_negative",
        "age_integer",
        "age_subclass",
        "age_bool",
        "age_string",
        "age_nan",
        "age_infinity",
        "age_negative_infinity",
        "age_negative",
        "backlog_hostile",
    ),
)
async def test_invalid_recovery_reports_collapse_to_the_fixed_error(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    raw_report = _mutated_recovery_report(case)

    async def _recover() -> object:
        return raw_report

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "recover_callback_jobs", _recover)
    try:
        result = await task_module.recover_telegram_callback_jobs()
    except Exception:
        _raise_generic("invalid recovery report escaped the TaskIQ boundary")
    _assert_recovery_error(result)
    if case == "backlog_hostile":
        assert type(raw_report) is dict
        backlog = raw_report["backlog"]
        assert type(backlog) is dict
        hostile = backlog["pending"]
        assert isinstance(hostile, _Hostile)
        if hostile.calls:
            _raise_generic("invalid recovery report invoked hostile coercion")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("map_level", "collision_key"),
    (
        ("top", "status"),
        ("statuses", "succeeded"),
        ("backlog", "pending"),
    ),
    ids=("top-map", "statuses-map", "backlog-map"),
)
async def test_recovery_report_rejects_non_string_extra_keys_without_callbacks(
    monkeypatch: pytest.MonkeyPatch, map_level: str, collision_key: str
) -> None:
    """Each strict report map checks exact key type before membership lookup."""
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    report = _valid_recovery_report(populated=False)
    if map_level == "top":
        target = cast(dict[object, object], report)
    else:
        nested = report[map_level]
        if type(nested) is not dict:
            _raise_generic("test recovery report did not expose an exact nested map")
        target = cast(dict[object, object], nested)
    hostile_key = _HostileExtraKey(collision_key=collision_key)
    target[hostile_key] = _FAKE_SECRET
    # Construction may hash/compare a colliding key.  Production sees only
    # the finished exact dict, so every callback below must remain absent.
    hostile_key.calls.clear()

    async def _recover() -> dict[str, object]:
        return report

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "recover_callback_jobs", _recover)
    try:
        result = await task_module.recover_telegram_callback_jobs()
    except Exception:
        _raise_generic("non-string recovery report key escaped the TaskIQ boundary")
    _assert_recovery_error(result)
    if hostile_key.calls:
        _raise_generic("recovery report key invoked a boundary callback")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("ordinary", "exception_group"))
async def test_ordinary_recovery_exception_and_exception_group_collapse_to_error(
    monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    ordinary = _BoundaryRuntimeError(reject_render=True)
    grouped = _BoundaryRuntimeError(reject_render=True)
    group = _RenderTrackedExceptionGroup("safe exception group", [grouped])

    async def _ordinary() -> object:
        raise ordinary

    async def _group() -> object:
        raise group

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    recover = _ordinary if failure_kind == "ordinary" else _group
    monkeypatch.setattr(task_module, "recover_callback_jobs", recover)
    try:
        result = await task_module.recover_telegram_callback_jobs.original_func()
    except Exception:
        _raise_generic("ordinary recovery failure escaped the TaskIQ boundary")
    for exception in (ordinary,) if failure_kind == "ordinary" else (group, grouped):
        _assert_exception_unrendered(exception, where="recovery exception boundary")
    _assert_recovery_error(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "factory"),
    (
        (asyncio.CancelledError, asyncio.CancelledError),
        (KeyboardInterrupt, KeyboardInterrupt),
        (SystemExit, SystemExit),
        (_CustomBaseException, _CustomBaseException),
    ),
    ids=("cancelled", "keyboard_interrupt", "system_exit", "custom_base"),
)
@pytest.mark.parametrize("endpoint", ("job", "recovery"))
async def test_original_task_functions_preserve_every_non_exception_baseexception(
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
    factory: Callable[[], BaseException],
    endpoint: str,
) -> None:
    """The entry boundary catches ``Exception`` only, never cancellation/control flow."""
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    exception = factory()

    async def _explode(*args: object, **kwargs: object) -> object:
        raise exception

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    if endpoint == "job":
        canonical = str(uuid.uuid4())
        monkeypatch.setattr(task_module, "process_callback_job", _explode)
        with pytest.raises(exception_type) as caught:
            await task_module.run_telegram_callback_job.original_func(canonical)
    else:
        monkeypatch.setattr(
            settings,
            "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
            True,
            raising=False,
        )
        monkeypatch.setattr(task_module, "recover_callback_jobs", _explode)
        with pytest.raises(exception_type) as caught:
            await task_module.recover_telegram_callback_jobs.original_func()
    if caught.value is not exception:
        _raise_generic("entry boundary replaced a non-Exception control-flow object")
    _assert_exception_unrendered(exception, where="entry BaseException boundary")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(EXPECTED_PROCESS_STATUSES))
async def test_internal_recovery_item_accepts_only_valid_worker_statuses(
    status: str,
) -> None:
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    job_id = uuid.uuid4()
    ignored_extra = _Hostile()

    async def _process(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "status": status,
            "job_id": str(job_id),
            "ignored_extra": ignored_extra,
        }

    result = await recovery_module._process_one(
        job_id,
        process_fn=_process,
        now_fn=now_kst,
        worker_kwargs={},
    )
    _assert_exact_string(result, status, where="internal recovery item result")
    if ignored_extra.calls:
        _raise_generic("internal recovery rendered an ignored worker extra")


def _invalid_recovery_item(case: str, job_id: uuid.UUID, hostile: _Hostile) -> object:
    if case == "unknown_status":
        return {"status": _FAKE_SECRET, "job_id": str(job_id)}
    if case == "disabled_status":
        return {"status": "disabled", "job_id": str(job_id)}
    if case == "status_subclass":
        return {"status": _StringSubclass("succeeded"), "job_id": str(job_id)}
    if case == "status_hostile":
        return {"status": hostile, "job_id": str(job_id)}
    if case == "wrong_job_id":
        return {"status": "succeeded", "job_id": str(uuid.uuid4())}
    if case == "job_id_upper":
        return {"status": "succeeded", "job_id": str(job_id).upper()}
    if case == "job_id_compact":
        return {"status": "succeeded", "job_id": str(job_id).replace("-", "")}
    if case == "job_id_brace":
        return {"status": "succeeded", "job_id": f"{{{job_id}}}"}
    if case == "job_id_urn":
        return {"status": "succeeded", "job_id": f"urn:uuid:{job_id}"}
    if case == "uuid_job_id":
        return {"status": "succeeded", "job_id": job_id}
    if case == "job_id_subclass":
        return {"status": "succeeded", "job_id": _StringSubclass(str(job_id))}
    if case == "job_id_hostile":
        return {"status": "succeeded", "job_id": hostile}
    if case == "missing_status":
        return {"job_id": str(job_id)}
    if case == "missing_job_id":
        return {"status": "succeeded"}
    if case == "dict_subclass":
        return _DictSubclass(status="succeeded", job_id=str(job_id))
    if case == "top_key_subclass":
        return {
            _StringSubclass("status"): "succeeded",
            _StringSubclass("job_id"): str(job_id),
        }
    if case == "non_dict":
        return ("succeeded", str(job_id))
    raise AssertionError("unknown test-only recovery-item case")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "unknown_status",
        "disabled_status",
        "status_subclass",
        "status_hostile",
        "wrong_job_id",
        "job_id_upper",
        "job_id_compact",
        "job_id_brace",
        "job_id_urn",
        "uuid_job_id",
        "job_id_subclass",
        "job_id_hostile",
        "missing_status",
        "missing_job_id",
        "dict_subclass",
        "top_key_subclass",
        "non_dict",
    ),
)
async def test_internal_recovery_item_rejects_malformed_worker_results(
    case: str,
) -> None:
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    job_id = uuid.UUID("01234567-89ab-4def-8abc-def012345678")
    hostile = _Hostile()
    raw = _invalid_recovery_item(case, job_id, hostile)

    async def _process(*args: object, **kwargs: object) -> object:
        return raw

    try:
        result = await recovery_module._process_one(
            job_id,
            process_fn=_process,
            now_fn=now_kst,
            worker_kwargs={},
        )
    except Exception:
        _raise_generic("malformed recovery worker result escaped item boundary")
    _assert_exact_string(result, "error", where="malformed recovery item result")
    if hostile.calls:
        _raise_generic("malformed recovery item invoked hostile coercion")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ("plain_object", "uuid_subclass", "canonical_impersonator"),
    ids=("plain-object", "uuid-subclass", "canonical-impersonator"),
)
async def test_internal_recovery_item_rejects_a_non_uuid_job_id_before_coercion(
    production_sentry_sinks: tuple[_RecordSink, list[dict[str, Any]], list[str]],
    case: str,
) -> None:
    """Recovery accepts an exact UUID only and does nothing for every impostor."""
    __tracebackhide__ = True
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    log_sink, events, envelope_item_types = production_sentry_sinks
    canonical = "01234567-89ab-4def-8abc-def012345678"
    hostile: _Hostile | _HostileUUID | _CanonicalUUIDImpersonator
    if case == "plain_object":
        hostile = _Hostile()
    elif case == "uuid_subclass":
        hostile = _HostileUUID(canonical)
    elif case == "canonical_impersonator":
        hostile = _CanonicalUUIDImpersonator(canonical)
    else:
        _raise_generic("unknown test-only invalid recovery UUID case")
    # UUID construction and dict setup are outside the production boundary.
    hostile.calls.clear()
    process_calls: list[object] = []
    error = _BoundaryRuntimeError(reject_render=True)
    observability_before = _snapshot_production_observability(
        log_sink, events, envelope_item_types
    )

    async def _process(*args: object, **kwargs: object) -> object:
        process_calls.append(args)
        raise error

    try:
        result = await recovery_module._process_one(
            hostile,  # type: ignore[arg-type]
            process_fn=_process,
            now_fn=now_kst,
            worker_kwargs={},
        )
    except Exception:
        _raise_generic("non-UUID recovery item escaped its fixed error boundary")
    sentry_sdk.flush(timeout=1.0)

    def _assert_no_coercion_or_process() -> None:
        if hostile.calls:
            _raise_generic("non-UUID recovery item invoked hostile coercion")
        if process_calls:
            _raise_generic("non-UUID recovery item reached the worker")

    _assert_all_clean(
        (
            lambda: _assert_exact_string(
                result, "error", where="non-UUID recovery item result"
            ),
            _assert_no_coercion_or_process,
            lambda: _assert_exception_unrendered(
                error, where="non-UUID recovery item exception"
            ),
            lambda: _assert_production_sentry_controls(events, envelope_item_types),
            lambda: _assert_production_observability_unchanged(
                log_sink,
                events,
                envelope_item_types,
                observability_before,
            ),
            lambda: _assert_log_records_clean(log_sink.records),
            lambda: _assert_events_clean(events),
        ),
        message="non-UUID recovery job id crossed the recovery boundary",
    )


@pytest.mark.asyncio
async def test_internal_recovery_item_protected_returned_id_is_redacted_everywhere(
    production_sentry_sinks: tuple[_RecordSink, list[dict[str, Any]], list[str]],
) -> None:
    """A rejected returned identifier must never cross the log/Sentry boundary."""
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    log_sink, events, envelope_item_types = production_sentry_sinks
    job_id = uuid.uuid4()

    async def _process(*args: object, **kwargs: object) -> dict[str, str]:
        return {"status": "succeeded", "job_id": _FAKE_SECRET}

    try:
        result = await recovery_module._process_one(
            job_id,
            process_fn=_process,
            now_fn=now_kst,
            worker_kwargs={},
        )
    except Exception:
        _raise_generic("protected returned identifier escaped the item boundary")
    _assert_all_clean(
        (
            lambda: _assert_exact_string(
                result, "error", where="protected returned identifier result"
            ),
            lambda: _assert_production_sentry_controls(events, envelope_item_types),
            lambda: _assert_log_records_clean(log_sink.records),
            lambda: _assert_events_clean(events),
        ),
        message="protected returned identifier crossed the recovery boundary",
    )


@pytest.mark.asyncio
async def test_internal_recovery_item_hides_dynamic_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    caplog.set_level(logging.DEBUG, logger=recovery_module.__name__)
    job_id = uuid.uuid4()
    error = _DynamicRecoveryFailure(reject_render=True)

    async def _explode(*args: object, **kwargs: object) -> object:
        raise error

    try:
        result = await recovery_module._process_one(
            job_id,
            process_fn=_explode,
            now_fn=now_kst,
            worker_kwargs={},
        )
    except Exception:
        _raise_generic("dynamic recovery item escaped its boundary")
    _assert_exception_unrendered(error, where="dynamic recovery item exception")
    _assert_exact_string(result, "error", where="exception recovery item result")
    records = [
        record for record in caplog.records if record.name == recovery_module.__name__
    ]
    _assert_log_records_clean(records)
    if any("exception_type" in _record_extras(record) for record in records):
        _raise_generic("recovery item logged a dynamic exception class")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("ordinary", "exception_group"))
async def test_internal_recovery_item_collapses_exception_and_exception_group(
    failure_kind: str,
) -> None:
    """The item boundary catches ordinary failures, but not BaseException."""
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    job_id = uuid.uuid4()
    error = _BoundaryRuntimeError(reject_render=True)
    group = _RenderTrackedExceptionGroup("safe exception group", [error])

    async def _explode(*args: object, **kwargs: object) -> object:
        if failure_kind == "ordinary":
            raise error
        raise group

    try:
        result = await recovery_module._process_one(
            job_id,
            process_fn=_explode,
            now_fn=now_kst,
            worker_kwargs={},
        )
    except Exception:
        _raise_generic("internal recovery item leaked an ordinary exception")
    _assert_exception_unrendered(error, where="internal recovery ordinary exception")
    if failure_kind == "exception_group":
        _assert_exception_unrendered(group, where="internal recovery outer group")
    _assert_exact_string(result, "error", where="internal recovery ordinary result")


@pytest.mark.asyncio
async def test_internal_recovery_item_logging_failure_still_returns_fixed_error() -> (
    None
):
    """A broken log handler cannot end one recovery item or its sweep."""
    __tracebackhide__ = True
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    process_error = _BoundaryRuntimeError(reject_render=True)
    handler_error = _BoundaryRuntimeError(reject_render=True)

    async def _explode(*args: object, **kwargs: object) -> object:
        raise process_error

    class _RaisingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise handler_error

    recovery_logger = logging.getLogger(recovery_module.__name__)
    handler = _RaisingHandler(level=logging.ERROR)
    recovery_logger.addHandler(handler)
    try:
        try:
            result = await recovery_module._process_one(
                uuid.uuid4(),
                process_fn=_explode,
                now_fn=now_kst,
                worker_kwargs={},
            )
        except Exception:
            _raise_generic("recovery logging failure escaped its fixed error boundary")
    finally:
        recovery_logger.removeHandler(handler)

    _assert_exception_unrendered(process_error, where="recovery process exception")
    _assert_exception_unrendered(handler_error, where="recovery logging exception")
    _assert_exact_string(result, "error", where="recovery logging-failure result")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "factory"),
    (
        (asyncio.CancelledError, asyncio.CancelledError),
        (KeyboardInterrupt, KeyboardInterrupt),
        (SystemExit, SystemExit),
        (_CustomBaseException, _CustomBaseException),
    ),
    ids=("cancelled", "keyboard_interrupt", "system_exit", "custom_base"),
)
async def test_internal_recovery_logger_handler_preserves_non_exception_baseexception(
    exception_type: type[BaseException], factory: Callable[[], BaseException]
) -> None:
    """A logger repair may absorb ``Exception`` only, never control flow."""
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    process_error = _BoundaryRuntimeError(reject_render=True)
    handler_error = factory()

    async def _explode(*args: object, **kwargs: object) -> object:
        raise process_error

    class _RaisingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise handler_error

    recovery_logger = logging.getLogger(recovery_module.__name__)
    handler = _RaisingHandler(level=logging.ERROR)
    recovery_logger.addHandler(handler)
    try:
        with pytest.raises(exception_type) as caught:
            await recovery_module._process_one(
                uuid.uuid4(),
                process_fn=_explode,
                now_fn=now_kst,
                worker_kwargs={},
            )
    finally:
        recovery_logger.removeHandler(handler)

    if caught.value is not handler_error:
        _raise_generic("recovery logger replaced a non-Exception control-flow object")
    _assert_exception_unrendered(process_error, where="recovery logger process error")
    _assert_exception_unrendered(handler_error, where="recovery logger BaseException")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "factory"),
    (
        (asyncio.CancelledError, asyncio.CancelledError),
        (KeyboardInterrupt, KeyboardInterrupt),
        (SystemExit, SystemExit),
        (_CustomBaseException, _CustomBaseException),
    ),
    ids=("cancelled", "keyboard_interrupt", "system_exit", "custom_base"),
)
async def test_internal_recovery_item_preserves_every_non_exception_baseexception(
    exception_type: type[BaseException],
    factory: Callable[[], BaseException],
) -> None:
    """The item boundary must not turn control-flow into an error status."""
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    job_id = uuid.uuid4()
    exception = factory()

    async def _explode(*args: object, **kwargs: object) -> object:
        raise exception

    with pytest.raises(exception_type) as caught:
        await recovery_module._process_one(
            job_id,
            process_fn=_explode,
            now_fn=now_kst,
            worker_kwargs={},
        )
    if caught.value is not exception:
        _raise_generic("internal recovery item replaced a control-flow object")
    _assert_exception_unrendered(exception, where="internal recovery BaseException")


@pytest.mark.asyncio
async def test_internal_recovery_item_exception_surfaces_are_redacted(
    production_sentry_sinks: tuple[_RecordSink, list[dict[str, Any]], list[str]],
) -> None:
    """Ordinary item failures must be safe in the real W5 log and span hooks."""
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module
    from app.services.order_proposals.callback_inbox.observability import (
        worker_transaction,
    )

    log_sink, events, envelope_item_types = production_sentry_sinks
    job_id = uuid.uuid4()
    error = _DynamicRecoveryFailure(reject_render=True)

    async def _explode(*args: object, **kwargs: object) -> object:
        raise error

    try:
        with worker_transaction(job_id) as transaction:
            transaction.set_data("callback_job.state", "processing")
            transaction.set_data("callback_job.attempt", 1)
            result = await recovery_module._process_one(
                job_id,
                process_fn=_explode,
                now_fn=now_kst,
                worker_kwargs={},
            )
    except Exception:
        _raise_generic("dynamic recovery Sentry item escaped its boundary")
    _assert_exception_unrendered(error, where="internal recovery exception")
    _assert_exact_string(result, "error", where="internal recovery exception result")
    _assert_production_sentry_controls(events, envelope_item_types)
    _assert_all_clean(
        (
            lambda: _assert_log_records_clean(log_sink.records),
            lambda: _assert_events_clean(events),
        ),
        message="internal recovery item leaked protected exception material",
    )
