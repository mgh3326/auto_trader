"""hk 7092: widen the mover run error vocabulary with auth / rate_limited.

``classify_error`` now reports Naver 401/403 as ``auth`` and 429 as
``rate_limited`` instead of the blanket ``http_4xx``. Those are closed reason
strings the ``invest_momentum_event_runs`` CHECK must accept, so the CHECK is
replaced by definition text (same ``pg_get_constraintdef`` marker pattern as
hk 6510 — the naming convention double-prefixes/truncates the model name).

Additive only: every previously valid reason stays valid. Downgrade refuses
while rows carry the new reasons.
"""

# expand-contract: contract-step 7092

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261127_hk7092_run_reasons"
down_revision: str | Sequence[str] | None = "20261127_hk7037_us_kis_day_val"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "invest_momentum_event_runs"
CHECK = "ck_invest_momentum_event_runs_status_reason"
MARKER = "no_rows"

_VOCABULARY = (
    "'http_4xx', 'http_5xx', 'auth', 'rate_limited', 'timeout', "
    "'transport', 'bad_response', 'unparseable', 'unexpected', "
    "'source_unavailable'"
)
_VOCABULARY_OLD = (
    "'http_4xx', 'http_5xx', 'timeout', 'transport', "
    "'bad_response', 'unparseable', 'unexpected', 'source_unavailable'"
)


def _drop_check() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = '{TABLE}'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%{MARKER}%'
            LOOP
                EXECUTE format('ALTER TABLE {TABLE} DROP CONSTRAINT %I', c.conname);
            END LOOP;
        END
        $$
        """
    )


def _check_sql(reasons: str) -> str:
    return (
        "CHECK ("
        "(status = 'ok' AND reason IS NULL AND row_count > 0)"
        " OR (status = 'empty' AND reason = 'no_rows' AND row_count = 0)"
        " OR (status = 'error' AND row_count = 0 AND reason IN ("
        f"{reasons})))"
    )


def upgrade() -> None:
    _drop_check()
    # expand-contract: contract-step 7092
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} " + _check_sql(_VOCABULARY))


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.{TABLE}
                WHERE reason IN ('auth', 'rate_limited')
            ) THEN
                RAISE EXCEPTION
                    '{TABLE} has auth/rate_limited rows; downgrade would discard them';
            END IF;
        END
        $$
        """
    )
    _drop_check()
    # expand-contract: contract-step 7092
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} " + _check_sql(_VOCABULARY_OLD)
    )
