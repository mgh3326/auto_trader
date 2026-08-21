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


@pytest.mark.unit
def test_the_migration_declares_the_exact_c86_parent_and_no_row_dml() -> None:
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

    executed = [
        ast.literal_eval(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    for statement in executed:
        upper = str(statement).upper()
        for dml in ("INSERT", "UPDATE ", "DELETE"):
            assert dml not in upper, statement


@pytest.mark.unit
def test_the_migration_chain_still_has_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(Config(str(_REPO / "alembic.ini")))
    heads = script.get_heads()
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
         received_at, available_at, callback_query_id, chat_id, message_id,
         telegram_user_id, action, subject_short, dispatch_attempt_id,
         membership_revision, membership_digest, nonce, outcome, error_class)
    VALUES
        (:job_id, :update_digest, :state, :attempt_count, 3,
         :now, :now, :callback_query_id, :chat_id, :message_id,
         :telegram_user_id, :action, :subject_short, :dispatch_attempt_id,
         :membership_revision, :membership_digest, :nonce, :outcome,
         :error_class)
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


def _constraint_behaviour(connection: sa.Connection) -> dict[str, bool]:
    """One pass over every constraint this migration is responsible for."""
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
        "active_row_missing_nonce": _accepts(connection, nonce=None),
        "active_row_missing_chat": _accepts(connection, chat_id=None),
        # A terminal row that kept its authority material must be refused.
        "terminal_with_nonce": _accepts(
            connection, state="succeeded", **{**_scrubbed(), "nonce": "nonce123456"}
        ),
        "terminal_with_chat": _accepts(
            connection, state="discarded", **{**_scrubbed(), "chat_id": "42"}
        ),
        "terminal_with_binding": _accepts(
            connection,
            state="dead_letter",
            **{**_scrubbed(), "membership_digest": "abcdefghijkl"},
        ),
        "terminal_with_subject": _accepts(
            connection,
            state="succeeded",
            **{**_scrubbed(), "subject_short": "0123abcd"},
        ),
        "terminal_fully_scrubbed": _accepts(
            connection, state="succeeded", **_scrubbed()
        ),
        "bad_outcome_label": _accepts(connection, outcome="leak: nonce123456"),
        "bad_error_class": _accepts(connection, error_class="not_a_class"),
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_constraint_behaves_as_specified(_bootstrap_test_schema) -> None:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        behaviour = await connection.run_sync(_constraint_behaviour)
        await session.rollback()

    assert behaviour == {
        "duplicate_digest": False,
        "unknown_state": False,
        "negative_attempts": False,
        "attempts_over_max": False,
        "attempts_at_max": True,
        "unknown_action": False,
        "active_row_missing_nonce": False,
        "active_row_missing_chat": False,
        "terminal_with_nonce": False,
        "terminal_with_chat": False,
        "terminal_with_binding": False,
        "terminal_with_subject": False,
        "terminal_fully_scrubbed": True,
        "bad_outcome_label": False,
        "bad_error_class": False,
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
