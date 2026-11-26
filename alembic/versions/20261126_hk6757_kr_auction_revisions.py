"""Keep one final KIS auction minute beside its immutable first receipt."""

from alembic import op

revision = "20261126_hk6757_auction"
down_revision = "20261126_hk6674"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE research.kr_auction_minute_revisions (
            time_utc TIMESTAMPTZ NOT NULL,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL CHECK (venue = 'KRX'),
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            volume NUMERIC NOT NULL CHECK (volume > 0),
            value NUMERIC,
            revised_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (time_utc, symbol, venue)
        )
    """)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON research.kr_auction_minute_revisions TO at_app;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Block concurrent inserts between the preservation check and DROP TABLE.
    op.execute(
        "LOCK TABLE research.kr_auction_minute_revisions IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM research.kr_auction_minute_revisions) THEN
                RAISE EXCEPTION 'Cannot downgrade: KR auction minute revisions exist';
            END IF;
        END $$;
    """)
    op.drop_table("kr_auction_minute_revisions", schema="research")
