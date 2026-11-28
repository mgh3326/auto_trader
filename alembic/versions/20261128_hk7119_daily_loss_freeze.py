"""hk 7119: per-account kill rows + DAILY_LOSS_FREEZE (6939 I3, I13).

Additive. ``review.execution_account_control`` (hk 5441) gets:

* four more accounts (``kis_live_kr``, ``kis_live_us``, ``toss_live_kr``,
  ``toss_live_us``), each seeded ``GLOBAL_FREEZE`` (I13: the default is frozen);
* a fourth stored mode ``DAILY_LOSS_FREEZE`` written only by the daily-loss
  monitor;
* an UPDATE guard: the account is immutable, and a ``system:`` writer may only
  move ``ACTIVE`` -> ``DAILY_LOSS_FREEZE`` as ``system:daily_loss_monitor``.
  A system writer can never unfreeze; unfreezing stays the operator command.

The existing DELETE/TRUNCATE triggers stay. ``at_app`` gets the same
privileges conditionally (hard rule 18). ``downgrade`` refuses while
operator-produced state exists — a new account row changed away from its
``GLOBAL_FREEZE`` seed, or any ``DAILY_LOSS_FREEZE`` row — and otherwise removes
exactly the seeded rows.
"""

# expand-contract: contract-step 7119

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261128_hk7119_loss_freeze"
down_revision: str | Sequence[str] | None = "20261128_hk6409_backfill_req"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "review.execution_account_control"
NEW_ACCOUNTS: tuple[str, ...] = (
    "kis_live_kr",
    "kis_live_us",
    "toss_live_kr",
    "toss_live_us",
)
SYSTEM_WRITER = "system:daily_loss_monitor"
#: Only the upgrade writes this exact row shape. Anything else on a new account
#: (an operator arm, a monitor or operator freeze) or a ``DAILY_LOSS_FREEZE`` on
#: any account is operator-produced state the downgrade must not forget.
SEED_REASON = "initial_control_row"
SEED_WRITER = "migration-hk7119"
_RESTRICT = "USING ERRCODE = 'restrict_violation'"

ACCOUNT_CHECK = (
    "account IN ('upbit_live', 'kis_live_kr', 'kis_live_us', "
    "'toss_live_kr', 'toss_live_us')"
)
MODE_CHECK = "mode IN ('ACTIVE', 'ENTRY_HALT', 'GLOBAL_FREEZE', 'DAILY_LOSS_FREEZE')"

#: Idempotent; the test schema bootstrap reads this tuple (one source).
UPGRADE_DDL: tuple[str, ...] = (
    f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS "
    "ck_execution_account_control_account",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_execution_account_control_account "
    f"CHECK ({ACCOUNT_CHECK})",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS ck_execution_account_control_mode",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_execution_account_control_mode "
    f"CHECK ({MODE_CHECK})",
    f"""
    CREATE OR REPLACE FUNCTION review.execution_account_control_update_guard()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.account IS DISTINCT FROM OLD.account THEN
            RAISE EXCEPTION 'review.execution_account_control account is immutable'
                {_RESTRICT};
        END IF;
        IF NEW.updated_by LIKE 'system:%' THEN
            IF NEW.updated_by <> '{SYSTEM_WRITER}'
               OR OLD.mode <> 'ACTIVE'
               OR NEW.mode <> 'DAILY_LOSS_FREEZE' THEN
                RAISE EXCEPTION
                    'review.execution_account_control a system writer may only freeze an ACTIVE account'
                    {_RESTRICT};
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    f"DROP TRIGGER IF EXISTS trg_execution_account_control_update ON {TABLE}",
    f"""
    CREATE TRIGGER trg_execution_account_control_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION review.execution_account_control_update_guard()
    """,
    *(
        f"INSERT INTO {TABLE} (account, mode, reason, updated_by) "
        f"VALUES ('{account}', 'GLOBAL_FREEZE', '{SEED_REASON}', "
        f"'{SEED_WRITER}') ON CONFLICT (account) DO NOTHING"
        for account in NEW_ACCOUNTS
    ),
)

#: Stage-4 role may not exist in development or CI databases (hard rule 18).
GRANT_DDL: tuple[str, ...] = (
    f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
    GRANT USAGE ON SCHEMA review TO at_app;
    GRANT SELECT, UPDATE ON {TABLE} TO at_app;
    END IF; END $$""",
)


def upgrade() -> None:
    for statement in (*UPGRADE_DDL, *GRANT_DDL):
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {TABLE} "
            "WHERE mode = 'DAILY_LOSS_FREEZE' "
            "OR (account = ANY(:accounts) AND ("
            f"mode IS DISTINCT FROM 'GLOBAL_FREEZE' "
            f"OR reason IS DISTINCT FROM '{SEED_REASON}' "
            f"OR updated_by IS DISTINCT FROM '{SEED_WRITER}'))"
        ),
        {"accounts": list(NEW_ACCOUNTS)},
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"refusing downgrade: {count} hk 7119 account control rows "
            "hold operator or monitor state"
        )
    # The hk 5441 DELETE trigger makes the table append-only. The migration
    # owns the table: lift it for this one statement and restore it in the same
    # transaction.
    op.execute(
        f"ALTER TABLE {TABLE} DISABLE TRIGGER trg_execution_account_control_delete"
    )
    bind.execute(
        sa.text(
            f"DELETE FROM {TABLE} WHERE account = ANY(:accounts) "
            f"AND mode = 'GLOBAL_FREEZE' AND reason = '{SEED_REASON}' "
            f"AND updated_by = '{SEED_WRITER}'"
        ),
        {"accounts": list(NEW_ACCOUNTS)},
    )
    op.execute(
        f"ALTER TABLE {TABLE} ENABLE TRIGGER trg_execution_account_control_delete"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_execution_account_control_update ON {TABLE}"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS review.execution_account_control_update_guard()"
    )
    op.execute(
        f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS "
        "ck_execution_account_control_mode"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_execution_account_control_mode "
        "CHECK (mode IN ('ACTIVE', 'ENTRY_HALT', 'GLOBAL_FREEZE'))"
    )
    op.execute(
        f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS "
        "ck_execution_account_control_account"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_execution_account_control_account "
        "CHECK (account IN ('upbit_live'))"
    )
