"""W5 — default-off gates, schedule labels, and closed-world task registration.

RED-before-fix items 6, 7 and 17.

Nothing here touches Redis, Telegram, a broker or the DB: these are the
declaration-level invariants that decide whether the durable path can run at
all, and they must hold on a bare checkout with no environment configured.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from app.core.config import Settings, settings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "field",
    [
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED",
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
    ],
)
def test_every_durable_gate_defaults_false(field: str) -> None:
    """RED item 6 — durable/worker/scheduler defaults are all false."""
    model_field = Settings.model_fields[field]
    assert model_field.default is False, f"{field} must ship default-off"
    assert model_field.annotation is bool


def test_recovery_cron_default_is_every_minute() -> None:
    assert (
        Settings.model_fields["ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_CRON"].default
        == "* * * * *"
    )


def _schedule_label_in_fresh_interpreter(*, enabled: bool, cron: str) -> object:
    """Import the task module in a *new* interpreter and report its real label.

    Adversarial review R2: ``schedule=`` is evaluated once, at import. A test
    that monkeypatches the helper after import proves nothing about the label
    the scheduler actually reads, so this re-imports from scratch under the
    environment the deployment would have.
    """
    env = dict(os.environ)
    env["ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED"] = (
        "true" if enabled else "false"
    )
    env["ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_CRON"] = cron
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "import json;"
            "from app.tasks import telegram_callback_inbox_tasks as m;"
            "print(json.dumps("
            "m.recover_telegram_callback_jobs.labels.get('schedule')))",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(pathlib.Path(__file__).resolve().parents[4]),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_schedule_is_empty_when_the_recovery_gate_is_off() -> None:
    """RED item 7 (off half) — a disabled scheduler registers no cron label."""
    assert _schedule_label_in_fresh_interpreter(enabled=False, cron="*/2 * * * *") == []


def test_schedule_is_exactly_one_label_when_the_recovery_gate_is_on() -> None:
    """RED item 7 (on half) — exactly one recovery schedule, never more."""
    labels = _schedule_label_in_fresh_interpreter(enabled=True, cron="*/2 * * * *")
    assert labels == [{"cron": "*/2 * * * *", "cron_offset": "Asia/Seoul"}]


def test_the_shipped_recovery_task_declaration_is_scheduleless() -> None:
    """The module ships with the gate off, so the registered label list is empty."""
    from app.tasks import telegram_callback_inbox_tasks as task_module

    schedule = task_module.recover_telegram_callback_jobs.labels.get("schedule")
    assert schedule == []


def test_worker_task_carries_no_schedule_label_at_all() -> None:
    """The per-job worker task is kicked, never cronned."""
    from app.tasks import telegram_callback_inbox_tasks as task_module

    assert task_module.run_telegram_callback_job.labels.get("schedule") in (None, [])


def test_neither_task_opts_into_taskiq_smart_retry() -> None:
    """R2 — the DB state machine is the only retry authority.

    ``SmartRetryMiddleware`` is installed broker-wide with
    ``default_retry_label=False``, so a task re-runs only if it opts in via
    ``retry_on_error``. A W5 task that opted in would replay an order-adjacent
    callback outside the inbox's attempt accounting.
    """
    from app.tasks import telegram_callback_inbox_tasks as task_module

    for task in (
        task_module.run_telegram_callback_job,
        task_module.recover_telegram_callback_jobs,
    ):
        assert "retry_on_error" not in task.labels, task.task_name
        assert "max_retries" not in task.labels, task.task_name


def test_task_module_is_registered_closed_world() -> None:
    """RED item 17 — ``taskiq worker app.tasks`` must discover the new tasks."""
    from app import tasks as task_package
    from app.tasks import telegram_callback_inbox_tasks as task_module

    assert task_module in task_package.TASKIQ_TASK_MODULES


def test_task_names_are_stable_and_namespaced() -> None:
    from app.tasks import telegram_callback_inbox_tasks as task_module

    assert (
        task_module.run_telegram_callback_job.task_name
        == "order_proposals.telegram_callback_job"
    )
    assert (
        task_module.recover_telegram_callback_jobs.task_name
        == "order_proposals.telegram_callback_recovery"
    )


@pytest.fixture
def _database_is_a_tripwire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make *any* DB touch an immediate, loud failure.

    R2 asks for "worker gate off => DB access 0" proved as an actual absence,
    not as a mocked-away call. Both entry points into PostgreSQL — the ORM
    session factory and the raw engine connection the advisory lock uses —
    are replaced by tripwires.
    """
    from app.core import db
    from app.services.order_proposals.callback_inbox import (
        ingress,
        recovery,
        worker,
    )

    touched: list[str] = []

    def _session_tripwire(*args, **kwargs):
        touched.append("AsyncSessionLocal")
        raise AssertionError("gate-off task opened a database session")

    async def _connect_tripwire(*args, **kwargs):
        touched.append("engine.connect")
        raise AssertionError("gate-off task opened a database connection")

    monkeypatch.setattr(db, "AsyncSessionLocal", _session_tripwire)
    monkeypatch.setattr(db.engine, "connect", _connect_tripwire)
    # The modules bind the factory by name at call time (never as a default
    # argument), so patching the module attribute really does disarm them.
    for module in (ingress, recovery, worker):
        monkeypatch.setattr(module, "AsyncSessionLocal", _session_tripwire)
    return touched


@pytest.mark.asyncio
async def test_worker_task_touches_no_database_while_its_gate_is_off(
    monkeypatch: pytest.MonkeyPatch, _database_is_a_tripwire: list[str]
) -> None:
    from app.tasks import telegram_callback_inbox_tasks as task_module

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        False,
        raising=False,
    )
    result = await task_module.run_telegram_callback_job(
        "1b3a3b3e-0000-4000-8000-000000000000"
    )
    assert result == {
        "status": "disabled",
        "job_id": "1b3a3b3e-0000-4000-8000-000000000000",
    }
    assert _database_is_a_tripwire == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovery_on", "worker_on", "expected"),
    [
        (False, False, "disabled"),
        (False, True, "disabled"),
        (True, False, "worker_disabled"),
    ],
)
async def test_recovery_task_touches_no_database_unless_both_gates_are_on(
    monkeypatch: pytest.MonkeyPatch,
    _database_is_a_tripwire: list[str],
    recovery_on: bool,
    worker_on: bool,
    expected: str,
) -> None:
    """Recovery *executes* handlers, so it inherits the worker gate too."""
    from app.tasks import telegram_callback_inbox_tasks as task_module

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        recovery_on,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        worker_on,
        raising=False,
    )
    assert await task_module.recover_telegram_callback_jobs() == {"status": expected}
    assert _database_is_a_tripwire == []


def test_worker_status_vocabulary_is_closed() -> None:
    """RED item 5 (result half) — only allowlisted statuses may leave the task."""
    from app.services.order_proposals.callback_inbox.contracts import WORKER_STATUSES

    assert WORKER_STATUSES == frozenset(
        {
            "dead_letter",
            "disabled",
            "discarded",
            "lock_contended",
            "not_claimable",
            "not_found",
            "retry_scheduled",
            "succeeded",
        }
    )


def test_inbox_state_vocabulary_matches_the_brief() -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        INBOX_STATES,
        TERMINAL_STATES,
    )

    assert set(INBOX_STATES) == {
        "pending",
        "processing",
        "succeeded",
        "discarded",
        "retry_wait",
        "dead_letter",
    }
    assert TERMINAL_STATES == frozenset({"succeeded", "discarded", "dead_letter"})


def test_no_handler_reason_string_is_ever_retry_evidence() -> None:
    """RED item 13, hardened by adversarial review R1 blocker 2.

    The brief's first cut retried a generic ``internal_error``. That string is
    *not* evidence that the broker leg never started: the callback core catches
    every exception, including one raised after ``revalidate_and_submit``
    submitted and before the transaction committed. Re-running that job would
    re-submit. So no reason string is retry evidence; only a *typed*
    ``mutation_not_started`` flag, which today's core never sets, is.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        MUTATION_NOT_STARTED_KEY,
        RETRYABLE_HANDLER_REASONS,
    )

    assert RETRYABLE_HANDLER_REASONS == frozenset()
    assert MUTATION_NOT_STARTED_KEY == "mutation_not_started"
    for never_retried in (
        "internal_error",
        "unverified",
        "submit_rejected",
        "nonce_replay",
        "guard_blocked",
        "expired",
        "chat_not_allowed",
        "approval_batch_nonce_replay",
    ):
        assert never_retried not in RETRYABLE_HANDLER_REASONS


def test_todays_callback_core_never_emits_mutation_not_started() -> None:
    """The typed escape hatch must be inert until a core change earns it."""
    import pathlib

    from app.services.order_proposals.callback_inbox.contracts import (
        MUTATION_NOT_STARTED_KEY,
    )

    core = (
        pathlib.Path(__file__).resolve().parents[4]
        / "app/services/order_proposals/telegram_callback.py"
    ).read_text(encoding="utf-8")
    assert MUTATION_NOT_STARTED_KEY not in core


def test_error_class_vocabulary_is_closed() -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        ERROR_CLASSES,
        RETRYABLE_ERROR_CLASSES,
    )

    assert ERROR_CLASSES == frozenset(
        {
            "attempts_exhausted",
            "chat_revoked",
            "envelope_invalid",
            "handler_ambiguous",
            "handler_exception",
            "pre_core_failure",
        }
    )
    # Only a failure that provably never entered the core may re-run.
    assert RETRYABLE_ERROR_CLASSES == frozenset({"pre_core_failure"})


def test_max_attempts_is_three() -> None:
    from app.services.order_proposals.callback_inbox.contracts import MAX_ATTEMPTS

    assert MAX_ATTEMPTS == 3
