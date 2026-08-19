"""ROB-1290 r2 — the claim-state migration, rendered rather than assumed.

Alembic applies ``Base.metadata``'s naming convention
(``ck_%(table_name)s_%(constraint_name)s``) to ``drop_constraint`` as well
as to ``create_check_constraint``. Passing the *full* constraint name
therefore produces a double-prefixed, truncated identifier and the DROP
fails at runtime against a constraint that does not exist -- which a
source-level test reading the file would never notice, because the file
looks perfectly reasonable.

So this renders the real DDL with ``alembic upgrade --sql`` (offline mode:
no database is contacted) and asserts the statements that actually reach
Postgres. It deliberately does not shell out to a ``.venv/bin/alembic``
path the way the repo's older migration tests do -- that hard-coding is
why they cannot run outside one particular interpreter layout.
"""

from __future__ import annotations

import io
import logging
import pathlib

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _contain_alembic_logging_config():
    """Undo ``fileConfig``'s global logger sabotage.

    ``alembic/env.py`` calls ``logging.config.fileConfig(alembic.ini)``,
    which defaults to ``disable_existing_loggers=True`` and therefore
    silences every logger created before it -- for the rest of the pytest
    session. Without this, loading alembic here made an unrelated test
    elsewhere in the suite fail on an empty ``caplog``, and only when the
    two happened to run in the same process.
    """
    loggers = logging.Logger.manager.loggerDict
    before = {
        name: (obj.disabled, obj.level)
        for name, obj in loggers.items()
        if isinstance(obj, logging.Logger)
    }
    root = logging.getLogger()
    root_level, root_handlers = root.level, list(root.handlers)
    try:
        yield
    finally:
        for name, obj in loggers.items():
            if not isinstance(obj, logging.Logger):
                continue
            if name in before:
                obj.disabled, obj.level = before[name]
            else:
                # Created during the render; must not stay disabled either.
                obj.disabled = False
        root.setLevel(root_level)
        root.handlers[:] = root_handlers


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BEFORE = "20260819_rob1286_claims"
AFTER = "20260820_rob1290_reconcile"
CONSTRAINT = "ck_watch_event_repricing_claims_state"
TABLE = "review.watch_event_repricing_claims"


def _config(buffer: io.StringIO | None = None) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


def _render(*, upgrade: bool) -> str:
    buffer = io.StringIO()
    config = _config(buffer)
    if upgrade:
        command.upgrade(config, f"{BEFORE}:{AFTER}", sql=True)
    else:
        command.downgrade(config, f"{AFTER}:{BEFORE}", sql=True)
    return buffer.getvalue()


def test_the_chain_still_has_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(_config())
    assert list(script.get_heads()) == [AFTER]


def test_upgrade_drops_and_recreates_the_state_check_by_its_real_name() -> None:
    sql = _render(upgrade=True)

    assert f"ALTER TABLE {TABLE} DROP CONSTRAINT {CONSTRAINT};" in sql
    # The bug this test exists for: a double-prefixed identifier.
    assert "ck_watch_event_repricing_claims_ck_" not in sql

    add = next(line for line in sql.splitlines() if "ADD CONSTRAINT" in line)
    assert CONSTRAINT in add
    for state in (
        "started",
        "proposal_created",
        "rejected_with_reason",
        "expired_unprocessed",
        "awaiting_reconcile",
    ):
        assert f"'{state}'" in add


def test_upgrade_touches_nothing_but_that_constraint() -> None:
    sql = _render(upgrade=True)
    statements = [
        line.strip()
        for line in sql.splitlines()
        if line.strip().startswith(("ALTER", "CREATE", "DROP", "UPDATE", "INSERT"))
    ]
    # Two ALTERs on our table, plus alembic's own version bookkeeping.
    assert [s for s in statements if TABLE in s] == [
        f"ALTER TABLE {TABLE} DROP CONSTRAINT {CONSTRAINT};",
        next(s for s in statements if "ADD CONSTRAINT" in s),
    ]
    assert not [s for s in statements if "investment_watch_events" in s]
    assert not [s for s in statements if s.startswith("CREATE TABLE")]
    assert not [s for s in statements if s.startswith("DROP TABLE")]


def test_downgrade_rescues_quarantined_rows_before_narrowing() -> None:
    """The narrower CHECK would reject them, so they move first."""
    sql = _render(upgrade=False)
    lines = [line.strip() for line in sql.splitlines() if line.strip()]

    update_at = next(
        i for i, line in enumerate(lines) if line.startswith("UPDATE " + TABLE)
    )
    drop_at = next(i for i, line in enumerate(lines) if "DROP CONSTRAINT" in line)
    assert update_at < drop_at, "rows must be moved before the constraint narrows"
    assert "SET state = 'expired_unprocessed'" in lines[update_at]
    assert "WHERE state = 'awaiting_reconcile'" in lines[update_at]

    add = next(line for line in lines if "ADD CONSTRAINT" in line)
    assert "'awaiting_reconcile'" not in add
