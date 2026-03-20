from collections.abc import Sequence

from alembic import op

revision: str = "d31f0a2b4c6d"
down_revision: str | Sequence[str] | None = "87541fdbc954"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.kr_candles_1m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_1m',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_1m',
                    INTERVAL '90 days'
                );
            END IF;

            IF to_regclass('public.kr_candles_1h') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_1h',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_1h',
                    INTERVAL '90 days'
                );
            END IF;

            IF to_regclass('public.kr_candles_5m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_5m',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_5m',
                    INTERVAL '90 days'
                );
            END IF;

            IF to_regclass('public.kr_candles_15m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_15m',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_15m',
                    INTERVAL '90 days'
                );
            END IF;

            IF to_regclass('public.kr_candles_30m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_30m',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_30m',
                    INTERVAL '90 days'
                );
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.kr_candles_1m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_1m',
                    if_exists => TRUE
                );
            END IF;

            IF to_regclass('public.kr_candles_1h') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_1h',
                    if_exists => TRUE
                );
            END IF;

            IF to_regclass('public.kr_candles_5m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_5m',
                    if_exists => TRUE
                );
            END IF;

            IF to_regclass('public.kr_candles_15m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_15m',
                    if_exists => TRUE
                );
            END IF;

            IF to_regclass('public.kr_candles_30m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_30m',
                    if_exists => TRUE
                );
            END IF;
        END
        $$
        """
    )
