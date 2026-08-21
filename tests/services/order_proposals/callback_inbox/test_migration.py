"""W5 — the additive migration and the constraints it must produce.

RED-before-fix item 18, plus adversarial review R1 blocker 3 and R2's
migration checks.

The constraint tests write **raw rows through a fresh session**, not through
the ORM service, because the point is that the database refuses an unscrubbed
terminal row even when the code that should have scrubbed it is skipped.
"""

from __future__ import annotations

import ast
import contextlib
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


#: Alembic operations that write or rewrite rows. None may appear.
_ROW_DML_OPS = frozenset(
    {
        "bulk_insert",
        "execute",
        "executemany",
    }
)
#: Alembic operations that touch an existing schema object.
_MUTATION_OPS = frozenset(
    {
        "alter_column",
        "drop_column",
        "add_column",
        "rename_table",
        "drop_constraint",
        "create_check_constraint",
        "create_foreign_key",
        "create_primary_key",
        "create_unique_constraint",
        "alter_table",
        "batch_alter_table",
    }
)
#: Every op the migration is allowed to use, and the object each may name.
_ALLOWED_OPS = frozenset({"create_table", "drop_table", "create_index", "drop_index"})


@pytest.mark.unit
def test_the_migration_is_additive_and_touches_only_its_own_table() -> None:
    """R6 strengthening 5 — cover the whole surface, not just literal SQL.

    A string-literal scan misses ``op.bulk_insert``, ``sa.text`` built from a
    variable, and every ALTER-shaped op. This walks the call graph instead:
    only ``create_table``/``drop_table``/``create_index``/``drop_index`` may
    appear, every one of them must name ``telegram_callback_inbox`` in the
    ``review`` schema, and no row-writing or schema-mutating op may be present
    at all.
    """
    source = _MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)

    ops: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "op":
            ops.append((node.func.attr, node.lineno))

    used = {name for name, _ in ops if name != "f"}
    assert not (used & _ROW_DML_OPS), f"row DML in an additive migration: {used}"
    assert not (used & _MUTATION_OPS), f"schema mutation: {used & _MUTATION_OPS}"
    assert used <= _ALLOWED_OPS, (
        f"unexpected alembic ops: {sorted(used - _ALLOWED_OPS)}"
    )
    assert "create_table" in used and "drop_table" in used

    # Every op names this table, in this schema, and nothing else.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not (isinstance(owner, ast.Name) and owner.id == "op"):
            continue
        if node.func.attr not in _ALLOWED_OPS:
            continue
        rendered = ast.dump(node)
        assert "_TABLE" in rendered or "telegram_callback_inbox" in rendered, (
            f"line {node.lineno}: an op that does not name the inbox table"
        )
        schemas = [
            kw for kw in node.keywords if kw.arg in {"schema", "table_name", "schema_"}
        ]
        if any(kw.arg == "schema" for kw in schemas):
            schema_kw = next(kw for kw in schemas if kw.arg == "schema")
            rendered_schema = ast.dump(schema_kw.value)
            assert "_SCHEMA" in rendered_schema or "review" in rendered_schema, (
                f"line {node.lineno}: an op outside the review schema"
            )

    # The module-level constants those ops resolve to.
    namespace: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            with contextlib.suppress(ValueError):
                namespace[node.targets[0].id] = ast.literal_eval(node.value)
    assert namespace.get("_TABLE") == "telegram_callback_inbox"
    assert namespace.get("_SCHEMA") == "review"

    # And no other table name appears anywhere in the file's SQL text.
    for other_table in (
        "order_proposals",
        "order_proposal_rungs",
        "order_proposal_approval_batches",
        "alembic_version",
        "watch_event_repricing_claims",
    ):
        assert other_table not in source.split('"""', 2)[-1], other_table


@pytest.mark.unit
def test_the_migration_chain_still_has_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(Config(str(_REPO / "alembic.ini")))
    heads = tuple(script.get_heads())
    assert heads == (_REVISION,), heads


@pytest.mark.unit
def test_the_orm_model_is_registered_and_exported() -> None:
    from app import models
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    assert models.TelegramCallbackInboxJob is TelegramCallbackInboxJob
    assert TelegramCallbackInboxJob.__table__.schema == "review"
    assert "review.telegram_callback_inbox" in Base.metadata.tables, sorted(
        Base.metadata.tables
    )


# --------------------------------------------------------------------------
# Everything below needs a real PostgreSQL.
# --------------------------------------------------------------------------

_INSERT = sa.text(
    """
    INSERT INTO review.telegram_callback_inbox
        (job_id, update_digest, state, attempt_count, max_attempts,
         received_at, available_at, terminal_state_pending, callback_query_id,
         chat_id, message_id, telegram_user_id, action, subject_short,
         dispatch_attempt_id, membership_revision, membership_digest, nonce,
         outcome, error_class)
    VALUES
        (:job_id, :update_digest, :state, :attempt_count, 3,
         :now, :now, :terminal_state_pending, :callback_query_id,
         :chat_id, :message_id, :telegram_user_id, :action, :subject_short,
         :dispatch_attempt_id, :membership_revision, :membership_digest, :nonce,
         :outcome, :error_class)
    """
)

_NOW = dt.datetime(2026, 8, 21, 6, 0, tzinfo=dt.UTC)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "job_id": uuid.uuid4(),
        "update_digest": uuid.uuid4().hex * 2,
        "state": "pending",
        "attempt_count": 0,
        "now": _NOW,
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


def _scrubbed() -> dict[str, object]:
    return {
        "callback_query_id": None,
        "chat_id": None,
        "message_id": None,
        "telegram_user_id": None,
        "action": None,
        "subject_short": None,
        "dispatch_attempt_id": None,
        "membership_revision": None,
        "membership_digest": None,
        "nonce": None,
    }


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
)

#: Every column a claimable row must carry, or the worker could not rebuild
#: and re-gate the envelope.
RECONSTRUCTION_FIELDS: tuple[str, ...] = (
    "chat_id",
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
)

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
    """Insert an active row, optionally dropping exactly one required field."""
    overrides: dict[str, object] = {"state": state}
    if missing is not None:
        overrides[missing] = None
    return _accepts(connection, **overrides)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_terminal_state_rejects_every_retained_authority_field(
    _bootstrap_test_schema,
) -> None:
    """R3 B1 — all 10 fields x all 3 terminal states, through raw SQL.

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


def _constraint_behaviour(connection: sa.Connection) -> dict[str, bool]:
    """The non-field constraints: dedupe, vocabularies, attempt bounds."""
    shared_digest = uuid.uuid4().hex * 2
    connection.execute(_INSERT, _row(update_digest=shared_digest))
    duplicate_digest_accepted = _accepts(connection, update_digest=shared_digest)

    return {
        "duplicate_digest": duplicate_digest_accepted,
        "unknown_state": _accepts(connection, state="not_a_state"),
        "negative_attempts": _accepts(connection, attempt_count=-1),
        "attempts_over_max": _accepts(connection, attempt_count=4),
        "attempts_at_max": _accepts(connection, attempt_count=3),
        "unknown_action": _accepts(connection, action="zz"),
        "unknown_terminal_pending": _accepts(
            connection, terminal_state_pending="pending"
        ),
        "bad_outcome_label": _accepts(connection, outcome="leak: nonce123456"),
        "bad_error_class": _accepts(connection, error_class="not_a_class"),
    }


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
        trace.append(f"initial={_table_exists(connection)}")
        migration.downgrade()
        trace.append(f"after_downgrade={_table_exists(connection)}")
        migration.upgrade()
        trace.append(f"after_upgrade={_table_exists(connection)}")
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
        "initial=True",
        "after_downgrade=False",
        "after_upgrade=True",
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
        await session.rollback()
    assert exists is True
