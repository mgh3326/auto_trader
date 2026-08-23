"""W5 — the additive migration and the constraints it must produce.

RED-before-fix item 18, plus adversarial review R1 blocker 3 and R2's
migration checks.

The constraint tests write **raw rows through a fresh session**, not through
the ORM service, because the point is that the database refuses an unscrubbed
terminal row even when the code that should have scrubbed it is skipped.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from app.models.base import Base

_REPO = pathlib.Path(__file__).resolve().parents[4]
_MIGRATION = _REPO / "alembic/versions/20260821_w5_telegram_callback_inbox.py"
_REVISION = "20260821_w5_callback_inbox"
_PARENT = "20260820_rob1290_reconcile"
_MIGRATION_DIR = _REPO / "alembic/versions"


@pytest.mark.unit
def test_the_migration_declares_the_exact_c86_parent() -> None:
    tree = ast.parse(_MIGRATION.read_text(encoding="utf-8"))
    assignments: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign):
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
            assignments[target.id] = ast.literal_eval(value)
    assert assignments == {"revision": _REVISION, "down_revision": _PARENT}


@pytest.mark.unit
def test_the_migration_is_additive_and_touches_only_its_two_owned_tables() -> None:
    """Delegates to the scanner that has been shown hostile input.

    The version that lived here was closed-world about op *names* but not
    about op *receivers* or *phases*, so it accepted aliases, ``getattr``,
    helper indirection and a ``drop_table`` in ``upgrade``. The scanner it now
    calls is proved against a twelve-case corpus in
    ``test_migration_phase_guard.py``; duplicating a weaker copy here would
    just be a second thing to keep in step.
    """
    from ._migration_guard import scan

    result = scan(
        _MIGRATION.read_text(encoding="utf-8"),
        inbox_table="telegram_callback_inbox",
        cursor_table="telegram_callback_recovery_cursor",
        schema="review",
    )
    assert result.ok, result.offenders
    assert result.ops_by_phase["upgrade"] == [
        "create_table:telegram_callback_inbox",
        "create_index:telegram_callback_inbox",
        "create_table:telegram_callback_recovery_cursor",
    ]
    assert result.ops_by_phase["downgrade"] == [
        "drop_table:telegram_callback_recovery_cursor",
        "drop_index:telegram_callback_inbox",
        "drop_table:telegram_callback_inbox",
    ]


@pytest.mark.unit
def test_the_migration_chain_still_has_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(Config(str(_REPO / "alembic.ini")))
    heads = tuple(script.get_heads())
    assert len(heads) == 1, heads


@pytest.mark.unit
def test_r32_edits_only_the_original_w5_create_table_migration() -> None:
    """R32 DDL stays on the original W5 create-table migration."""
    script = ScriptDirectory.from_config(Config(str(_REPO / "alembic.ini")))
    assert len(tuple(script.get_heads())) == 1

    # A named R32/outcome-allowlist revision would be a forbidden follow-on.
    assert not tuple(
        path
        for path in _MIGRATION_DIR.glob("*.py")
        if "r32" in path.name.lower() or "outcome_allowlist" in path.name.lower()
    )

    # The original additive create-table revision is the only migration that
    # may mention either W5-owned table; a later constraint/backfill revision
    # fails and a bootstrap-only cursor table would leave production unready.
    w5_migrations = tuple(
        path
        for path in sorted(_MIGRATION_DIR.glob("*.py"))
        if (
            "telegram_callback_inbox" in path.read_text(encoding="utf-8")
            or "telegram_callback_recovery_cursor" in path.read_text(encoding="utf-8")
        )
    )
    assert w5_migrations == (_MIGRATION,)


@pytest.mark.unit
def test_the_orm_model_is_registered_and_exported() -> None:
    from app import models
    from app.models.telegram_callback_inbox import (
        TelegramCallbackInboxJob,
        TelegramCallbackRecoveryCursor,
    )

    assert models.TelegramCallbackInboxJob is TelegramCallbackInboxJob
    assert models.TelegramCallbackRecoveryCursor is TelegramCallbackRecoveryCursor
    assert TelegramCallbackInboxJob.__table__.schema == "review"
    assert TelegramCallbackRecoveryCursor.__table__.schema == "review"
    assert "review.telegram_callback_inbox" in Base.metadata.tables, sorted(
        Base.metadata.tables
    )
    assert "review.telegram_callback_recovery_cursor" in Base.metadata.tables, sorted(
        Base.metadata.tables
    )


@pytest.mark.unit
def test_the_cursor_orm_is_a_pii_free_singleton_with_no_index() -> None:
    from app.models.telegram_callback_inbox import TelegramCallbackRecoveryCursor

    table = TelegramCallbackRecoveryCursor.__table__
    assert tuple(table.columns.keys()) == ("id", "next_tier", "updated_at")
    assert table.c.id.primary_key is True
    assert isinstance(table.c.id.type, sa.SmallInteger)
    assert table.c.next_tier.nullable is False
    assert isinstance(table.c.next_tier.type, sa.SmallInteger)
    assert table.c.updated_at.nullable is False
    assert isinstance(table.c.updated_at.type, sa.TIMESTAMP)
    assert table.c.updated_at.type.timezone is True
    checks = {
        check.name: str(check.sqltext)
        for check in table.constraints
        if isinstance(check, sa.CheckConstraint)
    }
    assert checks == {
        "ck_telegram_callback_recovery_cursor_id": "id = 1",
        "ck_telegram_callback_recovery_cursor_next_tier": "next_tier >= 0 AND next_tier < 4",
    }
    assert table.indexes == set()


@pytest.mark.unit
def test_r36_records_the_v39_persistent_schema_bootstrap() -> None:
    """The completed v39 bootstrap covers both W5 ORM tables."""
    from tests._schema_bootstrap import SCHEMA_BOOTSTRAP_VERSION

    runbook = (_REPO / "docs/runbooks/telegram-callback-durable-inbox.md").read_text(
        encoding="utf-8"
    )
    assert SCHEMA_BOOTSTRAP_VERSION == 39
    assert (
        "The persistent pytest test-schema bootstrap is now v39 and covers both "
        "`review.telegram_callback_inbox` and `review.telegram_callback_recovery_cursor`."
    ) in runbook


@pytest.mark.unit
def test_the_orm_outcome_constraint_uses_the_closed_category_inventory() -> None:
    """ORM DDL must agree with the independent runtime projection vocabulary."""
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
    from app.services.order_proposals.callback_inbox.contracts import (
        OUTCOME_CATEGORIES,
        _sql_string_list,
    )

    constraint = next(
        item
        for item in TelegramCallbackInboxJob.__table__.constraints
        if item.name == "ck_telegram_callback_inbox_outcome"
    )
    expected = f"outcome IS NULL OR outcome IN ({_sql_string_list(OUTCOME_CATEGORIES)})"
    assert str(constraint.sqltext) == expected


@pytest.mark.unit
def test_the_orm_declares_the_fixed_cross_field_attempt_budget() -> None:
    """R34 — three is a fixed protocol constant, never a caller budget."""
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
    from app.services.order_proposals.callback_inbox.contracts import MAX_ATTEMPTS

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in TelegramCallbackInboxJob.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }

    assert MAX_ATTEMPTS == 3
    assert checks["ck_telegram_callback_inbox_attempt_count"] == (
        "attempt_count >= 0 AND attempt_count <= max_attempts"
    )
    assert checks["ck_telegram_callback_inbox_max_attempts"] == (
        f"max_attempts = {MAX_ATTEMPTS}"
    )


# --------------------------------------------------------------------------
# Everything below needs a real PostgreSQL.
# --------------------------------------------------------------------------

_INSERT = sa.text(
    """
    INSERT INTO review.telegram_callback_inbox
        (job_id, update_digest, state, attempt_count, max_attempts,
         received_at, available_at, started_at, handler_entered_at,
         handler_completed_at, terminal_state_pending, callback_query_id,
         chat_id, message_id, telegram_user_id, action, subject_short,
         dispatch_attempt_id, membership_revision, membership_digest, nonce,
         outcome, error_class, update_identity_digest)
    VALUES
        (:job_id, :update_digest, :state, :attempt_count, :max_attempts,
         :now, :now, :started_at, :handler_entered_at, :handler_completed_at,
         :terminal_state_pending, :callback_query_id,
         :chat_id, :message_id, :telegram_user_id, :action, :subject_short,
         :dispatch_attempt_id, :membership_revision, :membership_digest, :nonce,
         :outcome, :error_class, :update_identity_digest)
    """
)

_NOW = dt.datetime(2026, 8, 21, 6, 0, tzinfo=dt.UTC)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "job_id": uuid.uuid4(),
        "update_digest": uuid.uuid4().hex * 2,
        "state": "pending",
        "attempt_count": 0,
        "max_attempts": 3,
        "now": _NOW,
        "started_at": None,
        "handler_entered_at": None,
        "handler_completed_at": None,
        "terminal_state_pending": None,
        "callback_query_id": "cbq-1",
        "chat_id": "42",
        "message_id": 555,
        "telegram_user_id": "777",
        "action": "op",
        "subject_short": "0123abcd",
        "dispatch_attempt_id": uuid.uuid4(),
        "membership_revision": 1,
        "membership_digest": "abcdefghijkl",
        "nonce": "nonce123456",
        "outcome": None,
        "error_class": None,
        "update_identity_digest": "update-digest-1",
    }
    base.update(overrides)
    return base


def _accepts(connection: sa.Connection, **overrides: object) -> bool:
    savepoint = connection.begin_nested()
    try:
        connection.execute(_INSERT, _row(**overrides))
    except IntegrityError:
        savepoint.rollback()
        return False
    savepoint.rollback()
    return True


#: Every authority/PII column the threat brief requires a terminal row to
#: have dropped. Kept literal, not imported from ``contracts``, so a future
#: edit that *shrinks* the production tuple cannot silently shrink this test
#: with it.
AUTHORITY_FIELDS: tuple[str, ...] = (
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
    "update_identity_digest",
)

#: Every column a claimable row must carry, or the worker could not rebuild
#: and re-gate the envelope.
RECONSTRUCTION_FIELDS: tuple[str, ...] = (
    "chat_id",
    "telegram_user_id",
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
)


def test_active_reconstructability_has_exact_orm_and_original_migration_parity() -> (
    None
):
    """R37 — an active row cannot lack the user id needed by the callback core.

    This is deliberately independent of the production tuple.  The ORM
    bootstrap and the original still-unmerged Alembic create-table migration
    must express the same complete active reconstruction contract, while a
    terminal row remains nullable because terminal scrubbing removes authority.
    """
    from app.models.telegram_callback_inbox import _ACTIVE_RECONSTRUCTABLE_SQL
    from app.services.order_proposals.callback_inbox.contracts import (
        ACTIVE_REQUIRED_COLUMNS,
    )

    expected = (
        "chat_id",
        "telegram_user_id",
        "action",
        "subject_short",
        "dispatch_attempt_id",
        "membership_revision",
        "membership_digest",
        "nonce",
    )
    expected_sql = (
        "CASE WHEN state IN ('pending','processing','retry_wait') THEN "
        "chat_id IS NOT NULL AND telegram_user_id IS NOT NULL "
        "AND action IS NOT NULL AND subject_short IS NOT NULL "
        "AND dispatch_attempt_id IS NOT NULL AND membership_revision IS NOT NULL "
        "AND membership_digest IS NOT NULL AND nonce IS NOT NULL ELSE true END"
    )

    migration = _load_migration()
    assert RECONSTRUCTION_FIELDS == expected
    assert ACTIVE_REQUIRED_COLUMNS == expected
    assert _ACTIVE_RECONSTRUCTABLE_SQL == expected_sql
    assert migration._ACTIVE_RECONSTRUCTABLE == expected_sql  # noqa: SLF001


TERMINAL_STATES: tuple[str, ...] = ("succeeded", "discarded", "dead_letter")
ACTIVE_STATES: tuple[str, ...] = ("pending", "processing", "retry_wait")

_LIVE_VALUES: dict[str, object] = {
    "callback_query_id": "cbq-1",
    "chat_id": "42",
    "message_id": 555,
    "telegram_user_id": "777",
    "action": "op",
    "subject_short": "0123abcd",
    "dispatch_attempt_id": uuid.UUID("11111111-2222-4333-8444-555555555555"),
    "membership_revision": 1,
    "membership_digest": "abcdefghijkl",
    "nonce": "nonce123456",
    "update_identity_digest": "update-digest-1",
}


def _scrubbed() -> dict[str, object]:
    return dict.fromkeys(AUTHORITY_FIELDS)


def _sanity(connection: sa.Connection) -> bool:
    """A clean pending row must be accepted, or every assertion below is vacuous."""
    return _accepts(connection)


def _terminal_probe(connection: sa.Connection, state: str, retained: str | None):
    """Insert a terminal row, optionally retaining exactly one authority field."""
    overrides: dict[str, object] = {"state": state, **_scrubbed()}
    if retained is not None:
        overrides[retained] = _LIVE_VALUES[retained]
    return _accepts(connection, **overrides)


def _active_probe(connection: sa.Connection, state: str, missing: str | None):
    """Insert an active row, optionally dropping exactly one required field.

    ``processing`` additionally carries ``started_at``: a claimed row without
    one has no defined age, which is what the recovery scan filters on.
    """
    overrides: dict[str, object] = {"state": state}
    if state == "processing":
        overrides["started_at"] = _NOW
    if state == "retry_wait":
        # R21: `retry_wait` implies `pre_core_failure` and no outcome.
        overrides["error_class"] = "pre_core_failure"
    if missing is not None:
        overrides[missing] = None
    return _accepts(connection, **overrides)


def _attempt_budget_overrides(
    state: str, *, attempt_count: object, max_attempts: object
) -> dict[str, object]:
    """Make a shape otherwise valid for its state, then vary only its budget."""
    overrides: dict[str, object] = {
        "state": state,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
    }
    if state in TERMINAL_STATES:
        overrides.update(_scrubbed())
    elif state == "processing":
        overrides["started_at"] = _NOW
    elif state == "retry_wait":
        overrides["error_class"] = "pre_core_failure"
    return overrides


def _expected_attempt_budget_acceptance(
    state: str, case_name: str, accepted: bool
) -> bool:
    # ``attempt_count=3, max_attempts=3`` is the legal fixed upper boundary
    # everywhere except ``retry_wait``.  That exception is intentionally
    # separate from the R34 cross-field rule: R25's retry-budget CHECK says a
    # parked retry must still have another attempt left.
    if case_name == "fixed_upper_boundary":
        return state != "retry_wait"
    return accepted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_bootstrap_database_enforces_the_fixed_cross_field_budget_matrix(
    _bootstrap_test_schema,
) -> None:
    """R34 — raw PostgreSQL, every state, and every hostile cross-field shape.

    The test deliberately does not infer parity from constraint names.  It
    inserts values through raw SQL against the ORM-created schema, because a
    separate one-column range check would otherwise still accept a live
    ``attempt_count=3, max_attempts=4`` poison row.
    """
    from app.core.db import AsyncSessionLocal

    cases: tuple[tuple[str, object, object, bool], ...] = (
        ("fixed_lower_boundary", 0, 3, True),
        ("fixed_unspent_retry", 2, 3, True),
        ("fixed_upper_boundary", 3, 3, True),
        ("max_above_fixed", 3, 4, False),
        ("max_below_fixed", 0, 1, False),
        ("zero_max", 0, 0, False),
        ("negative_max", 0, -1, False),
        ("negative_attempt", -1, 3, False),
        ("attempt_above_fixed_budget", 4, 3, False),
        ("attempt_above_declared_budget", 2, 1, False),
        ("null_attempt", None, 3, False),
        ("null_max", 0, None, False),
    )

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        observed: dict[str, bool] = {}
        for state in (*ACTIVE_STATES, *TERMINAL_STATES):
            for name, candidate_count, candidate_max, _expected in cases:
                observed[f"{state}::{name}"] = _accepts(
                    connection,
                    **_attempt_budget_overrides(
                        state,
                        attempt_count=candidate_count,
                        max_attempts=candidate_max,
                    ),
                )
        return observed

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    expected: dict[str, bool] = {}
    for state in (*ACTIVE_STATES, *TERMINAL_STATES):
        for name, _candidate_count, _candidate_max, accepted in cases:
            expected[f"{state}::{name}"] = _expected_attempt_budget_acceptance(
                state, name, accepted
            )
    assert observed == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_wait_fixed_upper_boundary_is_rejected_by_the_retry_budget(
    _bootstrap_test_schema,
) -> None:
    """R25 remains distinct: fixed ``3`` is valid, but not while parked."""
    from app.core.db import AsyncSessionLocal

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        return {
            "pending_fixed_upper": _accepts(
                connection,
                **_attempt_budget_overrides("pending", attempt_count=3, max_attempts=3),
            ),
            "retry_wait_fixed_upper": _accepts(
                connection,
                **_attempt_budget_overrides(
                    "retry_wait", attempt_count=3, max_attempts=3
                ),
            ),
        }

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    assert observed == {
        "pending_fixed_upper": True,
        "retry_wait_fixed_upper": False,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_terminal_state_rejects_every_retained_authority_field(
    _bootstrap_test_schema,
) -> None:
    """R3 B1 — all 11 fields x all 3 terminal states, through raw SQL.

    The previous version of this test checked four fields on one state, which
    would have passed against a CHECK that only listed those four. Each probe
    is a raw INSERT on a fresh connection, so the ORM service that is supposed
    to scrub is not in the picture at all.
    """
    from app.core.db import AsyncSessionLocal

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        results: dict[str, bool] = {"__sanity__": _sanity(connection)}
        for state in TERMINAL_STATES:
            results[f"{state}::fully_scrubbed"] = _terminal_probe(
                connection, state, None
            )
            for field in AUTHORITY_FIELDS:
                results[f"{state}::{field}"] = _terminal_probe(connection, state, field)
        return results

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    expected: dict[str, bool] = {"__sanity__": True}
    for state in TERMINAL_STATES:
        expected[f"{state}::fully_scrubbed"] = True
        for field in AUTHORITY_FIELDS:
            expected[f"{state}::{field}"] = False
    assert observed == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_claimable_state_requires_every_reconstruction_field(
    _bootstrap_test_schema,
) -> None:
    """R3 B1 — a half-written active row must never become a runnable job."""
    from app.core.db import AsyncSessionLocal

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        results: dict[str, bool] = {"__sanity__": _sanity(connection)}
        for state in ACTIVE_STATES:
            results[f"{state}::complete"] = _active_probe(connection, state, None)
            for field in RECONSTRUCTION_FIELDS:
                results[f"{state}::{field}"] = _active_probe(connection, state, field)
        return results

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    expected: dict[str, bool] = {"__sanity__": True}
    for state in ACTIVE_STATES:
        expected[f"{state}::complete"] = True
        for field in RECONSTRUCTION_FIELDS:
            expected[f"{state}::{field}"] = False
    assert observed == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_processing_row_must_carry_the_moment_it_started(
    _bootstrap_test_schema,
) -> None:
    """R7 B11 — a claimed row with no ``started_at`` has no defined age.

    The recovery scan decides whether a ``processing`` row is stale enough to
    look at by comparing ``started_at`` against a window. A NULL there is not
    "very old" or "very new" -- the comparison is simply never true, so the
    row is invisible to the scan forever while occupying the state that says
    a worker owns it. The database must refuse to create that row at all.

    ``pending`` and ``retry_wait`` have not started, so they must still be
    accepted without one; this is a ``processing``-only invariant.
    """
    from app.core.db import AsyncSessionLocal

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        return {
            "__sanity__": _sanity(connection),
            "processing_without_started_at": _accepts(
                connection, state="processing", started_at=None
            ),
            "processing_with_started_at": _accepts(
                connection, state="processing", started_at=_NOW
            ),
            "pending_without_started_at": _accepts(
                connection, state="pending", started_at=None
            ),
            "retry_wait_without_started_at": _accepts(
                connection,
                state="retry_wait",
                started_at=None,
                error_class="pre_core_failure",
            ),
            # A terminal row keeps its timestamps; only authority is scrubbed.
            "terminal_with_started_at": _accepts(
                connection, state="succeeded", started_at=_NOW, **_scrubbed()
            ),
        }

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    assert observed == {
        "__sanity__": True,
        "processing_without_started_at": False,
        "processing_with_started_at": True,
        "pending_without_started_at": True,
        "retry_wait_without_started_at": True,
        "terminal_with_started_at": True,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_handler_markers_can_only_appear_in_causal_order(
    _bootstrap_test_schema,
) -> None:
    """R13 — a verdict without an entry is a shape that must not exist.

    Repair finalises a job *without re-running it* on the strength of
    ``handler_completed_at`` + ``terminal_state_pending``. If a row could
    carry those without ``handler_entered_at``, some other path could
    manufacture a "the handler already decided" claim for a job whose handler
    never ran. The three facts are causal, so the database enforces the order.
    """
    from app.core.db import AsyncSessionLocal

    entered = _NOW
    completed = _NOW

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        return {
            "__sanity__": _sanity(connection),
            # legal shapes, in order
            "processing_entered": _accepts(
                connection,
                state="processing",
                started_at=_NOW,
                handler_entered_at=entered,
            ),
            "processing_entered_completed": _accepts(
                connection,
                state="processing",
                started_at=_NOW,
                handler_entered_at=entered,
                handler_completed_at=completed,
            ),
            "processing_full_verdict": _accepts(
                connection,
                state="processing",
                started_at=_NOW,
                handler_entered_at=entered,
                handler_completed_at=completed,
                terminal_state_pending="succeeded",
            ),
            # illegal: completion without entry
            "completed_without_entry": _accepts(
                connection,
                state="processing",
                started_at=_NOW,
                handler_completed_at=completed,
            ),
            # illegal: a verdict without entry
            "verdict_without_entry": _accepts(
                connection,
                state="processing",
                started_at=_NOW,
                terminal_state_pending="succeeded",
            ),
            # illegal: a verdict without completion
            "verdict_without_completion": _accepts(
                connection,
                state="processing",
                started_at=_NOW,
                handler_entered_at=entered,
                terminal_state_pending="succeeded",
            ),
            # illegal: a queued row cannot remember a handler
            "pending_with_entry": _accepts(
                connection, state="pending", handler_entered_at=entered
            ),
            "retry_wait_with_entry": _accepts(
                connection,
                state="retry_wait",
                handler_entered_at=entered,
                error_class="pre_core_failure",
            ),
            "retry_wait_with_verdict": _accepts(
                connection,
                state="retry_wait",
                handler_entered_at=entered,
                handler_completed_at=completed,
                terminal_state_pending="succeeded",
                error_class="pre_core_failure",
            ),
            # A pre-core discard never entered the core; it stays legal.
            "terminal_without_any_marker": _accepts(
                connection, state="discarded", **_scrubbed()
            ),
            # A terminal row may keep the timestamps; only authority is scrubbed.
            "terminal_with_entry_and_completion": _accepts(
                connection,
                state="succeeded",
                handler_entered_at=entered,
                handler_completed_at=completed,
                **_scrubbed(),
            ),
        }

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    assert observed == {
        "__sanity__": True,
        "processing_entered": True,
        "processing_entered_completed": True,
        "processing_full_verdict": True,
        "completed_without_entry": False,
        "verdict_without_entry": False,
        "verdict_without_completion": False,
        "pending_with_entry": False,
        "retry_wait_with_entry": False,
        "retry_wait_with_verdict": False,
        "terminal_without_any_marker": True,
        "terminal_with_entry_and_completion": True,
    }


@pytest.mark.unit
def test_the_model_declares_the_handler_marker_order_checks() -> None:
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in TelegramCallbackInboxJob.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    name = "ck_telegram_callback_inbox_handler_marker_order"
    assert name in checks, sorted(checks)
    expression = checks[name]
    assert "handler_entered_at" in expression
    assert "handler_completed_at" in expression
    assert "terminal_state_pending" in expression


@pytest.mark.unit
def test_the_model_declares_the_processing_started_at_check() -> None:
    """Pinned on the ORM too, so ``create_all`` and Alembic cannot diverge."""
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in TelegramCallbackInboxJob.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    name = "ck_telegram_callback_inbox_processing_started_at"
    assert name in checks, sorted(checks)
    expression = checks[name]
    assert "started_at IS NOT NULL" in expression
    assert "processing" in expression


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_terminal_row_may_not_retain_a_pending_terminal_marker(
    _bootstrap_test_schema,
) -> None:
    """``terminal_state_pending`` is the repair marker; a finished row has none."""
    from app.core.db import AsyncSessionLocal

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        return {
            "retained": _accepts(
                connection,
                state="succeeded",
                terminal_state_pending="succeeded",
                **_scrubbed(),
            ),
            "cleared": _accepts(connection, state="succeeded", **_scrubbed()),
        }

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    assert observed == {"retained": False, "cleared": True}


def _load_migration():
    spec = importlib.util.spec_from_file_location("w5_inbox_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_exists(connection: sa.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass('review.telegram_callback_inbox') IS NOT NULL")
        ).scalar_one()
    )


def _cursor_table_exists(connection: sa.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text(
                "SELECT to_regclass('review.telegram_callback_recovery_cursor') IS NOT NULL"
            )
        ).scalar_one()
    )


def _cursor_row_count(connection: sa.Connection) -> int:
    return int(
        connection.execute(
            sa.text("SELECT count(*) FROM review.telegram_callback_recovery_cursor")
        ).scalar_one()
    )


def _index_names(connection: sa.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'review' "
                "AND tablename = 'telegram_callback_inbox'"
            )
        ).all()
    }


def _roundtrip(connection: sa.Connection) -> list[str]:
    migration = _load_migration()
    context = MigrationContext.configure(
        connection=connection, opts={"target_metadata": Base.metadata}
    )
    trace: list[str] = []
    with Operations.context(context):
        trace.append(f"initial_inbox={_table_exists(connection)}")
        trace.append(f"initial_cursor={_cursor_table_exists(connection)}")
        migration.downgrade()
        trace.append(f"after_downgrade_inbox={_table_exists(connection)}")
        trace.append(f"after_downgrade_cursor={_cursor_table_exists(connection)}")
        migration.upgrade()
        trace.append(f"after_upgrade_inbox={_table_exists(connection)}")
        trace.append(f"after_upgrade_cursor={_cursor_table_exists(connection)}")
        trace.append(f"cursor_empty={_cursor_row_count(connection) == 0}")
        trace.append(
            "recovery_index="
            f"{'ix_telegram_callback_inbox_state_available' in _index_names(connection)}"
        )
        # The constraints must come back with the table, not just the columns.
        trace.append(
            "terminal_check_alive="
            f"{not _accepts(connection, state='succeeded', **{**_scrubbed(), 'nonce': 'n'})}"
        )
    return trace


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upgrade_downgrade_upgrade_against_the_real_database(
    _bootstrap_test_schema,
) -> None:
    """RED item 18 — and it must leave the shared schema exactly as it found it."""
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        trace = await connection.run_sync(_roundtrip)
        await session.rollback()

    assert trace == [
        "initial_inbox=True",
        "initial_cursor=True",
        "after_downgrade_inbox=False",
        "after_downgrade_cursor=False",
        "after_upgrade_inbox=True",
        "after_upgrade_cursor=True",
        "cursor_empty=True",
        "recovery_index=True",
        "terminal_check_alive=True",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_shared_schema_survives_the_roundtrip(_bootstrap_test_schema) -> None:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        exists = await connection.run_sync(_table_exists)
        cursor_exists = await connection.run_sync(_cursor_table_exists)
        await session.rollback()
    assert exists is True
    assert cursor_exists is True
