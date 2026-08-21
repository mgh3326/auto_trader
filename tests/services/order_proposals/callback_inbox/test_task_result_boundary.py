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
import logging
import math
import traceback
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import sentry_sdk
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

    def __init__(self) -> None:
        super().__init__(_FAKE_SECRET)

    def __str__(self) -> str:
        # A baseline failure must be assertion-level and must not print the
        # fixture through pytest's normal unhandled-exception rendering.
        return "task boundary failure"


class _CustomBaseException(BaseException):
    """An uncaught-by-``Exception`` control with safe display text."""

    def __init__(self) -> None:
        super().__init__(_FAKE_SECRET)

    def __str__(self) -> str:
        return "task boundary base failure"


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


class _DynamicRecoveryFailure(Exception):
    """Its dynamic class name itself is protected recovery error material."""

    def __init__(self) -> None:
        super().__init__(_FAKE_SECRET)

    def __str__(self) -> str:
        return "recovery item failure"


# Make the baseline's ``type(exc).__name__`` log leak the exact marker too,
# while its string rendering remains safe if pytest ever has to show it.
_DynamicRecoveryFailure.__name__ = _FAKE_SECRET


def _raise_generic(message: str) -> None:
    """Fail without formatting a potentially untrusted actual value."""
    raise AssertionError(message) from None


def _assert_exact_string(value: object, expected: str, *, where: str) -> None:
    if type(value) is not str or value != expected:
        _raise_generic(f"{where} did not contain the fixed safe string")


def _assert_exact_keys(value: object, expected: frozenset[str], *, where: str) -> None:
    if type(value) is not dict:
        _raise_generic(f"{where} was not an exact built-in dict")
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
    """Read only built-in containers and exception args; never coerce objects."""
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
        if value.__traceback__ is not None:
            found.extend(
                traceback.format_exception(type(value), value, value.__traceback__)
            )
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


@pytest.fixture
def task_boundary_sinks():
    """Capture receiver logs plus Sentry event/transaction payloads in memory."""
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
    scope = sentry_sdk.get_global_scope()
    previous_client = scope.client
    scope.set_client(client)

    sink = _RecordSink()
    sink.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(sink)
    root.setLevel(logging.DEBUG)
    try:
        # Positive controls keep the privacy scan from passing over empty sinks
        # or only unserialised Python values.
        logging.getLogger("r35.task_boundary.control").error(
            "safe TaskIQ boundary log control"
        )
        sentry_sdk.capture_message("safe TaskIQ boundary event control")
        with sentry_sdk.start_transaction(name="r35 task boundary transaction") as tx:
            tx.set_data("boundary", "safe")
        yield sink, events
    finally:
        root.removeHandler(sink)
        root.setLevel(previous_level)
        client.close(timeout=0)
        scope.set_client(previous_client)


@pytest.mark.parametrize("protected_text", _FAKE_FRAGMENTS)
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


def _valid_recovery_report() -> dict[str, object]:
    statuses = dict.fromkeys(EXPECTED_RECOVERY_ITEM_STATUSES, 0)
    statuses["succeeded"] = 1
    return {
        "status": "ok",
        "scanned": 1,
        "claimed": 1,
        "statuses": statuses,
        "backlog": {
            "pending": 1,
            "processing": 0,
            "retry_wait": 0,
            "dead_letter": 0,
            "oldest_pending_age_seconds": 0.0,
        },
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
        "whitespace",
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

    canonical = str(uuid.uuid4())
    hostile = _Hostile()
    values: dict[str, object] = {
        "whitespace": f" {canonical}",
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
@pytest.mark.parametrize(
    "canonical",
    (
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-1111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ),
)
async def test_all_uuid_versions_and_the_nil_uuid_use_the_canonical_wire_form(
    monkeypatch: pytest.MonkeyPatch, canonical: str
) -> None:
    """No UUID version restriction is smuggled into the input boundary."""
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    calls: list[object] = []

    async def _process(job_id: object) -> dict[str, str]:
        calls.append(job_id)
        return {"status": "succeeded", "job_id": canonical}

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(task_module, "process_callback_job", _process)
    result = await task_module.run_telegram_callback_job(canonical)
    _assert_job_result(result, status="succeeded", job_id=canonical)
    if calls != [canonical]:
        _raise_generic("canonical UUID did not reach the worker exactly once")


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


def _invalid_worker_result(case: str, canonical: str, hostile: _Hostile) -> object:
    if case == "unknown_status":
        return {"status": _FAKE_SECRET, "job_id": canonical}
    if case == "status_subclass":
        return {"status": _StringSubclass("succeeded"), "job_id": canonical}
    if case == "status_hostile":
        return {"status": hostile, "job_id": canonical}
    if case == "wrong_canonical_id":
        return {"status": "succeeded", "job_id": str(uuid.uuid4())}
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
    if case == "non_dict":
        return ["succeeded", canonical]
    raise AssertionError("unknown test-only worker-result case")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "unknown_status",
        "status_subclass",
        "status_hostile",
        "wrong_canonical_id",
        "secret_id",
        "uuid_id",
        "id_subclass",
        "id_hostile",
        "missing_status",
        "missing_job_id",
        "dict_subclass",
        "non_dict",
    ),
)
async def test_invalid_worker_result_shapes_collapse_to_the_fixed_error(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    canonical = str(uuid.uuid4())
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

    async def _explode(job_id: object) -> object:
        try:
            raise _BoundaryRuntimeError()
        except _BoundaryRuntimeError as cause:
            raise _BoundaryRuntimeError() from cause

    async def _group(job_id: object) -> object:
        raise ExceptionGroup("safe exception group", [_BoundaryRuntimeError()])

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

    async def _explode(job_id: object) -> object:
        try:
            raise _BoundaryRuntimeError()
        except _BoundaryRuntimeError as cause:
            raise _BoundaryRuntimeError() from cause

    if type(result_backend) is not RedisAsyncResultBackend:
        _raise_generic(
            "configured TaskIQ result backend is no longer RedisAsyncResultBackend"
        )
    if type(result_backend.serializer) is not PickleSerializer:
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
        task_name=task_module.run_telegram_callback_job.task_name,
        labels={},
        args=[],
        kwargs={"job_id": canonical},
    )
    receiver = Receiver(
        broker=broker,
        validate_params=True,
        max_async_tasks=1,
        run_startup=False,
    )
    result: TaskiqResult[Any] = await receiver.run_task(
        target=task_module.run_telegram_callback_job.original_func,
        message=message,
    )
    payload = model_dump(result)
    raw = result_backend.serializer.dumpb(payload)
    decoded = result_backend.serializer.loadb(raw)

    # Scan every actual boundary representation before deciding why RED failed.
    _assert_all_clean(
        (
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


def _mutated_recovery_report(case: str) -> object:
    report = _valid_recovery_report()
    statuses = report["statuses"]
    backlog = report["backlog"]
    assert type(statuses) is dict and type(backlog) is dict
    if case == "unknown_top_status":
        report["status"] = _FAKE_SECRET
    elif case == "top_status_subclass":
        report["status"] = _StringSubclass("ok")
    elif case == "unknown_top_key":
        report["private"] = _FAKE_SECRET
    elif case == "missing_top_key":
        del report["backlog"]
    elif case == "top_dict_subclass":
        return _DictSubclass(report)
    elif case == "unknown_status_key":
        statuses[_FAKE_SECRET] = 0
    elif case == "missing_status_key":
        del statuses["error"]
    elif case == "statuses_dict_subclass":
        report["statuses"] = _DictSubclass(statuses)
    elif case == "status_count_bool":
        statuses["succeeded"] = True
    elif case == "status_count_string":
        statuses["succeeded"] = "1"
    elif case == "status_count_subclass":
        statuses["succeeded"] = _IntSubclass(1)
    elif case == "status_count_negative":
        statuses["succeeded"] = -1
    elif case == "scanned_bool":
        report["scanned"] = True
    elif case == "scanned_string":
        report["scanned"] = "1"
    elif case == "scanned_subclass":
        report["scanned"] = _IntSubclass(1)
    elif case == "scanned_negative":
        report["scanned"] = -1
    elif case == "claimed_bool":
        report["claimed"] = True
    elif case == "claimed_string":
        report["claimed"] = "1"
    elif case == "claimed_subclass":
        report["claimed"] = _IntSubclass(1)
    elif case == "claimed_negative":
        report["claimed"] = -1
    elif case == "claimed_over_scanned":
        report["claimed"] = 2
    elif case == "scan_over_default_cap":
        from app.services.order_proposals.callback_inbox.contracts import (
            RECOVERY_SCAN_LIMIT,
            recovery_scan_cap,
        )

        report["scanned"] = recovery_scan_cap(RECOVERY_SCAN_LIMIT) + 1
    elif case == "status_sum_mismatch":
        statuses["succeeded"] = 0
    elif case == "claimed_sum_mismatch":
        report["claimed"] = 0
    elif case == "unknown_backlog_key":
        backlog[_FAKE_SECRET] = 0
    elif case == "missing_backlog_key":
        del backlog["pending"]
    elif case == "backlog_dict_subclass":
        report["backlog"] = _DictSubclass(backlog)
    elif case == "backlog_count_bool":
        backlog["pending"] = True
    elif case == "backlog_count_string":
        backlog["pending"] = "1"
    elif case == "backlog_count_subclass":
        backlog["pending"] = _IntSubclass(1)
    elif case == "backlog_count_negative":
        backlog["pending"] = -1
    elif case == "age_integer":
        backlog["oldest_pending_age_seconds"] = 0
    elif case == "age_subclass":
        backlog["oldest_pending_age_seconds"] = _FloatSubclass(0.0)
    elif case == "age_nan":
        backlog["oldest_pending_age_seconds"] = float("nan")
    elif case == "age_infinity":
        backlog["oldest_pending_age_seconds"] = float("inf")
    elif case == "age_negative":
        backlog["oldest_pending_age_seconds"] = -1.0
    elif case == "backlog_hostile":
        backlog["pending"] = _Hostile()
    else:
        raise AssertionError("unknown test-only recovery mutation")
    return report


@pytest.mark.asyncio
async def test_valid_full_recovery_report_requires_all_eight_zero_filled_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    report = _valid_recovery_report()

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
    if any(count != 0 for status, count in statuses.items() if status != "succeeded"):
        _raise_generic("successful recovery report did not retain zero-filled statuses")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "unknown_top_status",
        "top_status_subclass",
        "unknown_top_key",
        "missing_top_key",
        "top_dict_subclass",
        "unknown_status_key",
        "missing_status_key",
        "statuses_dict_subclass",
        "status_count_bool",
        "status_count_string",
        "status_count_subclass",
        "status_count_negative",
        "scanned_bool",
        "scanned_string",
        "scanned_subclass",
        "scanned_negative",
        "claimed_bool",
        "claimed_string",
        "claimed_subclass",
        "claimed_negative",
        "claimed_over_scanned",
        "scan_over_default_cap",
        "status_sum_mismatch",
        "claimed_sum_mismatch",
        "unknown_backlog_key",
        "missing_backlog_key",
        "backlog_dict_subclass",
        "backlog_count_bool",
        "backlog_count_string",
        "backlog_count_subclass",
        "backlog_count_negative",
        "age_integer",
        "age_subclass",
        "age_nan",
        "age_infinity",
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
@pytest.mark.parametrize("failure_kind", ("ordinary", "exception_group"))
async def test_ordinary_recovery_exception_and_exception_group_collapse_to_error(
    monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    async def _ordinary() -> object:
        raise _BoundaryRuntimeError()

    async def _group() -> object:
        raise ExceptionGroup("safe exception group", [_BoundaryRuntimeError()])

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

    async def _explode(*args: object, **kwargs: object) -> object:
        raise factory()

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    if endpoint == "job":
        canonical = str(uuid.uuid4())
        monkeypatch.setattr(task_module, "process_callback_job", _explode)
        with pytest.raises(exception_type):
            await task_module.run_telegram_callback_job.original_func(canonical)
    else:
        monkeypatch.setattr(
            settings,
            "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
            True,
            raising=False,
        )
        monkeypatch.setattr(task_module, "recover_callback_jobs", _explode)
        with pytest.raises(exception_type):
            await task_module.recover_telegram_callback_jobs.original_func()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(EXPECTED_PROCESS_STATUSES))
async def test_internal_recovery_item_accepts_only_valid_worker_statuses(
    status: str,
) -> None:
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    job_id = uuid.uuid4()

    async def _process(*args: object, **kwargs: object) -> dict[str, str]:
        return {"status": status, "job_id": str(job_id)}

    result = await recovery_module._process_one(
        job_id,
        process_fn=_process,
        now_fn=now_kst,
        worker_kwargs={},
    )
    _assert_exact_string(result, status, where="internal recovery item result")


def _invalid_recovery_item(case: str, job_id: uuid.UUID, hostile: _Hostile) -> object:
    if case == "unknown_status":
        return {"status": _FAKE_SECRET, "job_id": str(job_id)}
    if case == "status_subclass":
        return {"status": _StringSubclass("succeeded"), "job_id": str(job_id)}
    if case == "status_hostile":
        return {"status": hostile, "job_id": str(job_id)}
    if case == "wrong_job_id":
        return {"status": "succeeded", "job_id": str(uuid.uuid4())}
    if case == "uuid_job_id":
        return {"status": "succeeded", "job_id": job_id}
    if case == "missing_status":
        return {"job_id": str(job_id)}
    if case == "missing_job_id":
        return {"status": "succeeded"}
    if case == "dict_subclass":
        return _DictSubclass(status="succeeded", job_id=str(job_id))
    if case == "non_dict":
        return ("succeeded", str(job_id))
    raise AssertionError("unknown test-only recovery-item case")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "unknown_status",
        "status_subclass",
        "status_hostile",
        "wrong_job_id",
        "uuid_job_id",
        "missing_status",
        "missing_job_id",
        "dict_subclass",
        "non_dict",
    ),
)
async def test_internal_recovery_item_rejects_malformed_worker_results(
    case: str,
) -> None:
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    job_id = uuid.uuid4()
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
async def test_internal_recovery_item_hides_dynamic_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.core.timezone import now_kst
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    caplog.set_level(logging.DEBUG, logger=recovery_module.__name__)
    job_id = uuid.uuid4()

    async def _explode(*args: object, **kwargs: object) -> object:
        raise _DynamicRecoveryFailure()

    result = await recovery_module._process_one(
        job_id,
        process_fn=_explode,
        now_fn=now_kst,
        worker_kwargs={},
    )
    _assert_exact_string(result, "error", where="exception recovery item result")
    records = [
        record for record in caplog.records if record.name == recovery_module.__name__
    ]
    _assert_log_records_clean(records)
    if any("exception_type" in _record_extras(record) for record in records):
        _raise_generic("recovery item logged a dynamic exception class")
